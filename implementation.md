# Implementation notes

Both features call Google Gemini through one small client,
[`zerver/lib/llm.py`](zerver/lib/llm.py) (`generate_text`). It uses Zulip's
`OutgoingSession` (enforced timeouts, proxy support), reads the key from
`GEMINI_API_KEY` or `zproject/dev-secrets.conf`, and raises typed errors
(`LLMNotConfiguredError`, `LLMError`) so callers can degrade gracefully. No new
dependencies were added; Zulip already ships `requests`.

**Demo video (both features):** <https://www.youtube.com/watch?v=zC6H85PWoLI>

---

## Feature 1 — Message Recap

**Backend** — [`zerver/lib/recap.py`](zerver/lib/recap.py), endpoint
`GET /api/v1/messages/recap` in
[`zerver/views/recap.py`](zerver/views/recap.py), registered in
[`zproject/urls.py`](zproject/urls.py).

`generate_recap(user)` runs on each click of the recap button:

1. **Collect unreads** — `get_unread_message_ids()` calls Zulip's existing
   `get_raw_unread_data()` (the same query the web app runs at page load) and
   unions the channel, 1:1 DM and group-DM message IDs.
2. **Cap** — the newest `MAX_RECAP_MESSAGES = 200` are kept; the response
   reports `truncated` and `included_count` so the UI can say so. This bounds
   both latency and token cost.
3. **Fetch content** — `fetch_recap_messages()` uses Zulip's `messages_for_ids`
   with `apply_markdown=False` to get plain text plus sender, channel and topic.
4. **Prompt** — `build_recap_prompt()` groups messages by conversation
   (`#channel > topic` or "Direct messages with …") and prefixes every message
   with its database ID as `[#123]`. Gemini is asked (JSON mode,
   `RECAP_SYSTEM_INSTRUCTION`) for
   `{sections: [{heading, points: [{text, message_ids}]}]}` and told to cite
   only IDs from the input.

**How links are created** — `parse_recap_response()`. For each `message_id`
the model cites, the backend looks it up in the dict of messages it actually
sent. IDs that don't match (hallucinations) are dropped, so a bad citation can
never become a broken link. For matches, `message_narrow_url()` builds the
fragment with Zulip's own encoders:

- channel messages: `stream_message_url(realm=None, include_base_url=False)` →
  `#narrow/channel/3-Verona/topic/release.204.2E2/near/102`
- DMs: `#narrow/dm/{encode_user_ids(ids)}/near/{id}`

The model never produces a URL; it only echoes IDs. Each point is returned as
`{text, references: [{message_id, url, sender, conversation}]}`. Using
Zulip's encoders means Unicode channel names and group DMs are handled
identically to links Zulip generates itself.

Errors: `LLMNotConfiguredError`/`LLMError` become `JsonableError` (HTTP 400
with a readable message); a malformed model response yields an empty recap
rather than a 500. Zero unreads short-circuits before any LLM call.

**Frontend** — [`web/src/message_recap.ts`](web/src/message_recap.ts) and
[`web/templates/message_recap.hbs`](web/templates/message_recap.hbs). A button
added to [`web/templates/navbar.hbs`](web/templates/navbar.hbs)
(`#recap-button`, hidden for logged-out spectators) opens Zulip's
`dialog_widget` modal, mirroring the existing topic-summary modal. The modal
shows a spinner, calls the endpoint via `channel.get`, validates the response
with a zod schema, and renders sections → bullets → sender chips. Each chip is
an ordinary `<a href="#narrow/…">`, so Zulip's hash router performs the
navigation and highlights the message; the click handler only closes the
modal. Styles are in [`web/styles/modal.css`](web/styles/modal.css); the
module is initialised from `web/src/ui_init.js`.

**Tests** — [`zerver/tests/test_recap.py`](zerver/tests/test_recap.py)
(LLM mocked): correct channel and DM link formats, hallucinated ID dropped,
no-unread path skips the LLM, error paths, prompt grouping.

---

## Feature 2 — Topic Title Improver

**Backend** — [`zerver/lib/topic_drift.py`](zerver/lib/topic_drift.py), hook in
[`zerver/actions/message_send.py`](zerver/actions/message_send.py)
(`maybe_enqueue_topic_drift_check`), worker branch in
[`zerver/worker/deferred_work.py`](zerver/worker/deferred_work.py)
(`topic_drift_check`), endpoints `GET`/`POST /api/v1/topics/title_suggestion`
in [`zerver/views/topic_title.py`](zerver/views/topic_title.py).

Because this runs on every channel message, the design is three stages,
cheapest first.

**Stage 1 — synchronous gate** (`should_check_topic_drift`). One indexed `COUNT`
plus one memcached lookup, no network I/O. It returns early unless the sender
is human, a key is configured, the topic has ≥ `DRIFT_MIN_MESSAGES` (5)
messages, the count is a multiple of `DRIFT_CHECK_EVERY_N_MESSAGES` (3), and
no cooldown key exists. If it passes, a 10-minute cooldown key is written and
a job is enqueued with `queue_event_on_commit`.

**Stage 2 — asynchronous check** (`check_topic_drift`, in the existing
`deferred_work` RabbitMQ worker). Fetches the last `DRIFT_CONTEXT_MESSAGES`
(15) messages, builds a prompt with the current title, and asks Gemini (JSON
mode) for `{drifted, suggested_title, reason}` in one call.
`parse_drift_response` accepts a suggestion only if `drifted` is true and the
title is non-empty, different from the current one, and ≤ 60 characters.

**Stage 3 — delivery.** The suggestion is cached in memcached for 24 h and a
`topic_title_suggestion` event is pushed to the **sender only** — they just
wrote the message and have the context. Anyone who later posts in the topic
gets the cached suggestion re-sent (`notify_sender_of_existing_suggestion`)
with no LLM call.

**Latency.** The send path adds ~1 ms (one COUNT, one cache read); the 1–2 s
Gemini round-trip happens in the worker. Measured send time stayed ≈ 0.5 s.

**Cost.** Calls per topic are bounded by *min(messages / 3, one per 10
minutes)*; a burst of 30 messages costs one call. Each call sends ≤ 15
messages truncated to 500 chars (≈ 1–2 k tokens) to the cheapest Flash-Lite
tier. Short topics, bot messages, and servers with no key never trigger one.

**Scalability.** All state lives in memcached, shared by every web process and
worker, with TTL expiry instead of cleanup jobs. Work goes through Zulip's
existing queue, so LLM throughput scales by adding workers, and a slow or
failing LLM (`LLMError` is caught and logged) never blocks message delivery.
Known limits: memcached is not durable, the 15-message window cannot see
drift that began earlier, and the thresholds are untuned constants.

**Frontend** —
[`web/src/topic_title_suggestion.ts`](web/src/topic_title_suggestion.ts).
`server_events_dispatch.js` routes the event to `handle_event`, which renders
an `AlertBanner` in Zulip's top alert area with **Rename topic** / **Dismiss**.
Accept issues `PATCH /json/messages/<id>` with `propagate_mode: "change_all"`
— Zulip's standard topic move — so permissions, "MOVED" markers and live
sidebar updates come for free. Dismiss is remembered locally. A **Suggest a
better title** item in the topic ⋮ menu (`topic_popover.ts`) calls the `POST`
endpoint for an on-demand check.

**Tests** — [`zerver/tests/test_topic_drift.py`](zerver/tests/test_topic_drift.py):
gate fires once at the right count and respects the cooldown, no key → no
enqueue, event goes to the sender only, cached suggestion re-sent without a
second LLM call, LLM failure swallowed, force endpoint, response parsing.

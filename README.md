# Zulip with LLM features — Message Recap & Topic Title Improver

Individual Assignment 1, 17-645 Machine Learning in Production (Fall 2026).

This repository is a fork of [Zulip](https://github.com/zulip/zulip) with two
LLM-powered features added:

1. **Message Recap** — a navbar button that summarizes all of a user's unread
   messages on one page, with clickable links back to each original message.
2. **Topic Title Improver** — detects when a topic's conversation has drifted
   away from its title and offers a better one, which the user can accept with
   one click.

Both features use Google Gemini through its REST API. No new Python or
JavaScript dependencies were added. See [implementation.md](implementation.md)
for how they work and for the demo video.

---

## 1. Prerequisites

- **Docker Desktop** (macOS/Windows) or Docker Engine (Linux)
- **Vagrant** 2.4+ (`brew install --cask vagrant` on macOS)
- **Git**
- A **Gemini API key** (free): go to <https://aistudio.google.com/apikey>,
  sign in with a Google account, click **Create API key**, and copy it.

> Recommended: give Docker Desktop at least **6 GB of memory**
> (Docker Desktop → Settings → Resources → Memory). Zulip's webpack watcher is
> memory-hungry and gets OOM-killed on the default 2–4 GB, which makes the web
> app hang on the loading spinner.

## 2. Clone and start the development environment

This follows the standard Zulip Vagrant workflow
(<https://zulip.readthedocs.io/en/latest/development/setup-recommended.html>).

```bash
git clone https://github.com/Ishai21/zulip.git
cd zulip
vagrant up --provider=docker     # first run takes 10–20 minutes
```

## 3. Provide the Gemini API key

The key is read at runtime from **either** of these (checked in this order).
Neither is committed to git.

**Option A — environment variable** (simplest):

```bash
vagrant ssh
export GEMINI_API_KEY="your-key-here"
```

(The variable must be set in the same shell that runs `./tools/run-dev` below.)

**Option B — Zulip's dev secrets file** (persists across shells; the file is
already in `.gitignore`):

```bash
vagrant ssh
echo "gemini_api_key = your-key-here" >> ~/zulip/zproject/dev-secrets.conf
```

Optional: choose a different Gemini model with `GEMINI_MODEL=...` (env var) or
`gemini_model = ...` in the secrets file. The default is `gemini-3.5-flash-lite`,
which is fast and within the free tier.

## 4. Run the server

Inside the VM (`vagrant ssh`), from `~/zulip`:

```bash
./tools/run-dev
```

Wait for `frontend (webpack ...) compiled successfully`, then open
<http://localhost:9991> and log in as any development user (e.g.
**hamlet@zulip.com** or **iago@zulip.com**; no password needed).

`./tools/run-dev` must be started from an interactive `vagrant ssh` shell —
running it via `vagrant ssh -c "..."` fails with `os.setpgrp: Operation not
permitted`.

## 5. Using the features

### Message Recap

1. Make sure the logged-in user has some unread messages (send a few messages
   as another user, or use the API snippet below).
2. Click the **inbox-style icon** in the top-right navbar (immediately left of
   the `?` help icon; tooltip "Recap unread messages").
3. A modal shows a recap grouped by conversation. Each bullet has one or more
   **sender chips**; clicking a chip closes the modal and jumps to that exact
   message.

To create unread messages for `hamlet` quickly from your host machine:

```bash
KEY=$(curl -s -X POST 'http://localhost:9991/api/v1/dev_fetch_api_key' --data-urlencode 'username=iago@zulip.com' | sed -E 's/.*"api_key":"([^"]+)".*/\1/')
curl -s -X POST http://localhost:9991/api/v1/messages -u iago@zulip.com:$KEY \
  -d type=stream -d to=Verona --data-urlencode 'topic=release 4.2' \
  --data-urlencode 'content=We are cutting the 4.2 branch Friday at noon.'
```

Or call the endpoint directly:

```bash
curl -s "http://localhost:9991/api/v1/messages/recap" -u hamlet@zulip.com:$HAMLET_KEY
```

### Topic Title Improver

**Automatic:** post messages in a channel topic. Once the topic has at least 5
messages, every 3rd message (the 6th, 9th, 12th, …) triggers a background
check, at most once per topic every 10 minutes. If the LLM judges that the
conversation has drifted from the title, the sender of that message sees a
banner at the top of the page: *"The topic … seems to have drifted. Suggested
title: …"* with **Rename topic** and **Dismiss** buttons. Anyone who posts in
the topic afterwards sees the same banner (no additional LLM call).

**Manual (for demos):** in the left sidebar, hover a topic → click **⋮** →
**Suggest a better title**. This runs the check immediately, bypassing the
thresholds. If the title still fits, a short green notice says so.

> The adjacent menu item **"Summarize recent messages"** is Zulip's own
> OpenAI-based feature and is unrelated to this assignment; it is not
> configured and will show an empty dialog.

Direct API access:

```bash
# Read the pending suggestion for a topic (null if none)
curl -s "http://localhost:9991/api/v1/topics/title_suggestion?stream_id=11&topic=sprint%20planning" -u iago@zulip.com:$KEY

# Force a check now
curl -s -X POST "http://localhost:9991/api/v1/topics/title_suggestion" -u iago@zulip.com:$KEY \
  -d stream_id=11 --data-urlencode "topic=sprint planning"
```

## 6. Running the tests

All LLM calls are mocked; the tests run offline. Inside the VM:

```bash
./tools/test-backend zerver.tests.test_recap zerver.tests.test_topic_drift
./tools/lint zerver/lib/llm.py zerver/lib/recap.py zerver/lib/topic_drift.py \
    zerver/views/recap.py zerver/views/topic_title.py \
    web/src/message_recap.ts web/src/topic_title_suggestion.ts
```

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| Web app stuck on the Zulip loading spinner; `502` for `/webpack/app.js` | webpack was OOM-killed. Raise Docker memory (see §1), `vagrant reload`, restart `./tools/run-dev`. |
| New UI elements (recap icon, menu item) don't appear | The browser cached an old bundle. Hard-refresh (Cmd/Ctrl+Shift+R) or open a private window. Brave users: disable Shields for `localhost`. |
| Recap modal says "Message recap is not configured on this server." | No Gemini key found. See §3, then restart `./tools/run-dev`. |
| `git` commands inside the VM fail with "dubious ownership" | `git config --global --add safe.directory '*'` inside the VM. |
| `./tools/run-dev` errors with `os.setpgrp` | Run it from an interactive `vagrant ssh` shell, not `vagrant ssh -c`. |

## 8. Files added or changed

| Area | Files |
|---|---|
| LLM client | `zerver/lib/llm.py` |
| Recap backend | `zerver/lib/recap.py`, `zerver/views/recap.py`, `zerver/tests/test_recap.py` |
| Recap frontend | `web/src/message_recap.ts`, `web/templates/message_recap.hbs`, `web/templates/navbar.hbs`, `web/templates/tooltip_templates.hbs`, `web/styles/modal.css` |
| Title Improver backend | `zerver/lib/topic_drift.py`, `zerver/views/topic_title.py`, `zerver/actions/message_send.py` (hook), `zerver/worker/deferred_work.py` (job), `zerver/tests/test_topic_drift.py` |
| Title Improver frontend | `web/src/topic_title_suggestion.ts`, `web/src/server_events_dispatch.js`, `web/src/topic_popover.ts`, `web/templates/popovers/left_sidebar/left_sidebar_topic_actions_popover.hbs` |
| Routing / init | `zproject/urls.py`, `web/src/ui_init.js` |

---

Original Zulip documentation: <https://zulip.readthedocs.io/>

import hashlib
import json
import logging
from typing import TypedDict

from zerver.lib.cache import cache_delete, cache_get, cache_set
from zerver.lib.llm import LLMError, LLMNotConfiguredError, generate_text, get_gemini_api_key
from zerver.lib.message import SendMessageRequest
from zerver.lib.queue import queue_event_on_commit
from zerver.lib.topic import messages_for_topic
from zerver.models import Message, Realm, Stream
from zerver.models.constants import MAX_TOPIC_NAME_LENGTH
from zerver.tornado.django_api import send_event_on_commit

logger = logging.getLogger(__name__)

# Drift only makes sense once a topic has some history; very short
# topics are skipped without any LLM call.
DRIFT_MIN_MESSAGES = 5
# Once a topic is long enough, only every Nth message triggers a check,
# which bounds the LLM cost per topic to O(messages / N).
DRIFT_CHECK_EVERY_N_MESSAGES = 3
# How many trailing messages are sent to the model as context.
DRIFT_CONTEXT_MESSAGES = 15
MAX_MESSAGE_CHARS = 500
# After a check runs for a topic, no further checks are enqueued for
# this long, so bursts of messages cannot fan out into many LLM calls.
DRIFT_COOLDOWN_SECONDS = 10 * 60
SUGGESTION_TTL_SECONDS = 24 * 60 * 60

DRIFT_SYSTEM_INSTRUCTION = """\
You review a team chat topic to decide whether its title still describes
the conversation. Topics drift when the discussion moves to a different
subject for several messages, or a distinct sub-thread takes over.

You will receive the current title and the most recent messages, oldest
first. Respond with JSON only, matching this schema exactly:
{"drifted": <true|false>, "suggested_title": "<string>", "reason": "<string>"}

Rules:
- Set drifted to true only when the recent messages are clearly and
  consistently about something the current title does not describe.
  A single off-topic message or a brief aside is not drift.
- When drifted is true, suggested_title must be a short, specific
  title (at most 8 words) describing what the recent messages are
  actually about. Do not include quotes or trailing punctuation.
- When drifted is false, suggested_title must be an empty string.
- reason is one short sentence for the user.
"""


class TopicTitleSuggestion(TypedDict):
    stream_id: int
    topic_name: str
    suggested_title: str
    reason: str
    message_id: int


def topic_cache_key(prefix: str, stream_id: int, topic_name: str) -> str:
    # Topic names are arbitrary Unicode, so hash them into a key that
    # satisfies validate_cache_key.
    digest = hashlib.sha1(topic_name.lower().encode()).hexdigest()
    return f"{prefix}:{stream_id}:{digest}"


def suggestion_cache_key(stream_id: int, topic_name: str) -> str:
    return topic_cache_key("topic_title_suggestion", stream_id, topic_name)


def cooldown_cache_key(stream_id: int, topic_name: str) -> str:
    return topic_cache_key("topic_drift_cooldown", stream_id, topic_name)


def get_topic_title_suggestion(stream_id: int, topic_name: str) -> TopicTitleSuggestion | None:
    return cache_get(suggestion_cache_key(stream_id, topic_name))


def clear_topic_title_suggestion(stream_id: int, topic_name: str) -> None:
    cache_delete(suggestion_cache_key(stream_id, topic_name))


def should_check_topic_drift(send_request: SendMessageRequest) -> bool:
    """Cheap, synchronous gate that runs on every channel message send.

    It must stay inexpensive: a single indexed COUNT plus one cache
    lookup, and no external requests.
    """
    message = send_request.message
    stream = send_request.stream
    if stream is None or message.sender.is_bot or get_gemini_api_key() is None:
        return False
    topic_name = message.topic_name()
    if topic_name == "":
        return False

    assert stream.recipient_id is not None
    message_count = messages_for_topic(
        send_request.realm.id, stream.recipient_id, topic_name
    ).count()
    if message_count < DRIFT_MIN_MESSAGES or message_count % DRIFT_CHECK_EVERY_N_MESSAGES != 0:
        return False

    return cache_get(cooldown_cache_key(stream.id, topic_name)) is None


def notify_sender_of_existing_suggestion(send_request: SendMessageRequest) -> bool:
    """Re-surface a pending suggestion to whoever posts in the topic next.

    Costs one cache lookup and no LLM call, so a suggestion made while
    one participant was active is still seen by the others.
    """
    stream = send_request.stream
    if stream is None or send_request.message.sender.is_bot:
        return False
    suggestion = get_topic_title_suggestion(stream.id, send_request.message.topic_name())
    if suggestion is None:
        return False
    send_event_on_commit(
        send_request.realm,
        {"type": "topic_title_suggestion", **suggestion},
        [send_request.message.sender_id],
    )
    return True


def maybe_enqueue_topic_drift_check(send_request: SendMessageRequest) -> None:
    if notify_sender_of_existing_suggestion(send_request):
        return
    if not should_check_topic_drift(send_request):
        return
    stream = send_request.stream
    assert stream is not None
    topic_name = send_request.message.topic_name()
    cache_set(
        cooldown_cache_key(stream.id, topic_name),
        True,
        timeout=DRIFT_COOLDOWN_SECONDS,
        pickled_tupled=False,
    )
    queue_event_on_commit(
        "deferred_work",
        {
            "type": "topic_drift_check",
            "realm_id": send_request.realm.id,
            "stream_id": stream.id,
            "topic_name": topic_name,
            "sender_id": send_request.message.sender_id,
            "message_id": send_request.message.id,
        },
    )


def build_drift_prompt(topic_name: str, messages: list[Message]) -> str:
    lines = [f"Current title: {topic_name}", "", "Recent messages:"]
    for message in messages:
        content = message.content.strip()
        if len(content) > MAX_MESSAGE_CHARS:
            content = content[:MAX_MESSAGE_CHARS] + "…"
        lines.append(f"{message.sender.full_name}: {content}")
    return "\n".join(lines)


def parse_drift_response(raw_response: str, topic_name: str) -> tuple[str, str] | None:
    """Return (suggested_title, reason) if the model reported drift."""
    try:
        data = json.loads(raw_response)
        drifted = bool(data["drifted"])
        suggested_title = str(data.get("suggested_title", "")).strip().strip("\"'")
        reason = str(data.get("reason", "")).strip()
    except (ValueError, KeyError, TypeError):
        return None

    if not drifted or suggested_title == "":
        return None
    if suggested_title.lower() == topic_name.lower():
        return None
    if len(suggested_title) > MAX_TOPIC_NAME_LENGTH:
        suggested_title = suggested_title[:MAX_TOPIC_NAME_LENGTH].rstrip()
    return suggested_title, reason


def check_topic_drift(
    realm: Realm, stream: Stream, topic_name: str, sender_id: int, message_id: int
) -> TopicTitleSuggestion | None:
    """Run the LLM drift check for a topic and publish any suggestion.

    Runs in a queue worker, so it may take seconds without affecting
    message send latency.
    """
    assert stream.recipient_id is not None
    messages = list(
        messages_for_topic(realm.id, stream.recipient_id, topic_name)
        .select_related("sender")
        .order_by("-id")[:DRIFT_CONTEXT_MESSAGES]
    )
    messages.reverse()
    if not messages:
        return None

    try:
        raw_response = generate_text(
            build_drift_prompt(topic_name, messages),
            system_instruction=DRIFT_SYSTEM_INSTRUCTION,
            json_output=True,
            max_output_tokens=256,
        )
    except LLMNotConfiguredError:
        raise
    except LLMError:
        logger.warning("Topic drift check skipped for stream %s: LLM unavailable", stream.id)
        return None

    parsed = parse_drift_response(raw_response, topic_name)
    if parsed is None:
        clear_topic_title_suggestion(stream.id, topic_name)
        return None
    suggested_title, reason = parsed

    suggestion = TopicTitleSuggestion(
        stream_id=stream.id,
        topic_name=topic_name,
        suggested_title=suggested_title,
        reason=reason,
        message_id=message_id,
    )
    cache_set(
        suggestion_cache_key(stream.id, topic_name),
        suggestion,
        timeout=SUGGESTION_TTL_SECONDS,
        pickled_tupled=False,
    )
    # Only the sender is notified: they just wrote the message that
    # triggered the check and still have the context to judge it.
    send_event_on_commit(realm, {"type": "topic_title_suggestion", **suggestion}, [sender_id])
    return suggestion

import json
from collections import defaultdict
from typing import Any, TypedDict

from zerver.lib.llm import generate_text
from zerver.lib.message import get_raw_unread_data, messages_for_ids
from zerver.lib.topic import get_topic_from_message_info
from zerver.lib.url_encoding import encode_user_ids, stream_message_url
from zerver.models import UserProfile

# Caps how many unread messages are sent to the model in one request,
# bounding both latency and per-request token cost.
MAX_RECAP_MESSAGES = 200
# Long messages are truncated in the prompt; the recap links back to
# the original, so the model only needs enough to understand the gist.
MAX_MESSAGE_CHARS = 600

RECAP_SYSTEM_INSTRUCTION = """\
You write concise recaps of unread chat messages for a busy team member.

You will receive messages grouped by conversation. Every message starts
with a tag like [#123] where 123 is its message id.

Respond with JSON only, matching this schema exactly:
{
  "sections": [
    {
      "heading": "<short name of the conversation or theme>",
      "points": [
        {"text": "<one or two sentence summary>", "message_ids": [<int>, ...]}
      ]
    }
  ]
}

Rules:
- Every point MUST cite the ids of the messages it summarizes in
  message_ids. Only use ids that appear in the input.
- Prioritize decisions, questions directed at the reader, action items,
  and deadlines. Skip pleasantries.
- Keep the whole recap short: at most 3 points per section.
- Do not put message ids or tags inside "text".
"""


class RecapReference(TypedDict):
    message_id: int
    url: str
    sender: str
    conversation: str


class RecapPoint(TypedDict):
    text: str
    references: list[RecapReference]


class RecapSection(TypedDict):
    heading: str
    points: list[RecapPoint]


class RecapResult(TypedDict):
    sections: list[RecapSection]
    unread_count: int
    included_count: int
    truncated: bool


def get_unread_message_ids(user_profile: UserProfile) -> list[int]:
    raw = get_raw_unread_data(user_profile)
    message_ids = set(raw["stream_dict"]) | set(raw["pm_dict"]) | set(raw["huddle_dict"])
    return sorted(message_ids)


def fetch_recap_messages(user_profile: UserProfile, message_ids: list[int]) -> list[dict[str, Any]]:
    # Plain (unrendered) content keeps the prompt compact and free of HTML.
    return messages_for_ids(
        message_ids=message_ids,
        user_message_flags={message_id: [] for message_id in message_ids},
        search_fields={},
        apply_markdown=False,
        client_gravatar=True,
        allow_empty_topic_name=True,
        message_edit_history_visibility_policy=user_profile.realm.message_edit_history_visibility_policy,
        user_profile=user_profile,
        realm=user_profile.realm,
    )


def conversation_label(message: dict[str, Any]) -> str:
    if message["type"] == "stream":
        return f"#{message['display_recipient']} > {get_topic_from_message_info(message)}"
    names = [recipient["full_name"] for recipient in message["display_recipient"]]
    return "Direct messages with " + ", ".join(names)


def message_narrow_url(message: dict[str, Any]) -> str:
    """Return a URL fragment that narrows the web app to this message.

    Fragment-only URLs (no realm host) let the frontend navigate in
    place via hashchange rather than reloading the page.
    """
    if message["type"] == "stream":
        return stream_message_url(realm=None, message=message, include_base_url=False)
    user_ids = [recipient["id"] for recipient in message["display_recipient"]]
    return f"#narrow/dm/{encode_user_ids(user_ids)}/near/{message['id']}"


def build_recap_prompt(messages: list[dict[str, Any]]) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for message in messages:
        grouped[conversation_label(message)].append(message)

    chunks = []
    for label, conversation_messages in grouped.items():
        lines = [f"## {label}"]
        for message in conversation_messages:
            content = message["content"].strip()
            if len(content) > MAX_MESSAGE_CHARS:
                content = content[:MAX_MESSAGE_CHARS] + "…"
            lines.append(f"[#{message['id']}] {message['sender_full_name']}: {content}")
        chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


def parse_recap_response(
    raw_response: str, messages_by_id: dict[int, dict[str, Any]]
) -> list[RecapSection]:
    """Validate the model's JSON and attach links for every cited message.

    Any cited id that is not one of the user's unread messages is
    dropped, so a hallucinated id can never produce a broken link.
    """
    try:
        data = json.loads(raw_response)
        raw_sections = data["sections"]
    except (ValueError, KeyError, TypeError):
        return []

    sections: list[RecapSection] = []
    for raw_section in raw_sections:
        points: list[RecapPoint] = []
        for raw_point in raw_section.get("points", []):
            references: list[RecapReference] = []
            seen: set[int] = set()
            for raw_id in raw_point.get("message_ids", []):
                if not isinstance(raw_id, int) or raw_id in seen:
                    continue
                message = messages_by_id.get(raw_id)
                if message is None:
                    continue
                seen.add(raw_id)
                references.append(
                    RecapReference(
                        message_id=raw_id,
                        url=message_narrow_url(message),
                        sender=message["sender_full_name"],
                        conversation=conversation_label(message),
                    )
                )
            text = str(raw_point.get("text", "")).strip()
            if text:
                points.append(RecapPoint(text=text, references=references))
        if points:
            sections.append(RecapSection(heading=str(raw_section.get("heading", "")), points=points))
    return sections


def generate_recap(user_profile: UserProfile) -> RecapResult:
    unread_ids = get_unread_message_ids(user_profile)
    unread_count = len(unread_ids)
    # Keep the newest unreads when over the cap.
    included_ids = unread_ids[-MAX_RECAP_MESSAGES:]
    truncated = len(included_ids) < unread_count

    if not included_ids:
        return RecapResult(sections=[], unread_count=0, included_count=0, truncated=False)

    messages = fetch_recap_messages(user_profile, included_ids)
    messages_by_id = {message["id"]: message for message in messages}

    raw_response = generate_text(
        build_recap_prompt(messages),
        system_instruction=RECAP_SYSTEM_INSTRUCTION,
        json_output=True,
        max_output_tokens=4096,
    )
    sections = parse_recap_response(raw_response, messages_by_id)

    return RecapResult(
        sections=sections,
        unread_count=unread_count,
        included_count=len(messages),
        truncated=truncated,
    )

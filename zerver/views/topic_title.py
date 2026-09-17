from typing import Annotated

from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _
from pydantic import Json, StringConstraints

from zerver.lib.exceptions import JsonableError
from zerver.lib.llm import LLMNotConfiguredError
from zerver.lib.response import json_success
from zerver.lib.streams import access_stream_by_id
from zerver.lib.topic import messages_for_topic
from zerver.lib.topic_drift import check_topic_drift, get_topic_title_suggestion
from zerver.lib.typed_endpoint import typed_endpoint
from zerver.models import UserProfile
from zerver.models.constants import MAX_TOPIC_NAME_LENGTH


@typed_endpoint
def get_topic_title_suggestion_backend(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    stream_id: Json[int],
    topic: Annotated[str, StringConstraints(max_length=MAX_TOPIC_NAME_LENGTH)],
) -> HttpResponse:
    stream, _sub = access_stream_by_id(user_profile, stream_id)
    suggestion = get_topic_title_suggestion(stream.id, topic)
    return json_success(request, data={"suggestion": suggestion})


@typed_endpoint
def check_topic_title_backend(
    request: HttpRequest,
    user_profile: UserProfile,
    *,
    stream_id: Json[int],
    topic: Annotated[str, StringConstraints(max_length=MAX_TOPIC_NAME_LENGTH)],
) -> HttpResponse:
    """Run the drift check synchronously, bypassing the send-time heuristics.

    Automatic checks happen in the background as messages are sent; this
    lets a user ask for an assessment of a topic on demand.
    """
    stream, _sub = access_stream_by_id(user_profile, stream_id)
    assert stream.recipient_id is not None
    latest = (
        messages_for_topic(user_profile.realm_id, stream.recipient_id, topic)
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
    )
    if latest is None:
        raise JsonableError(_("No messages in this topic."))
    try:
        suggestion = check_topic_drift(user_profile.realm, stream, topic, user_profile.id, latest)
    except LLMNotConfiguredError:
        raise JsonableError(_("Topic title suggestions are not configured on this server."))
    return json_success(request, data={"suggestion": suggestion})

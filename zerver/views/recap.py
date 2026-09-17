from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext as _

from zerver.lib.exceptions import JsonableError
from zerver.lib.llm import LLMError, LLMNotConfiguredError
from zerver.lib.recap import generate_recap
from zerver.lib.response import json_success
from zerver.lib.typed_endpoint import typed_endpoint_without_parameters
from zerver.models import UserProfile


@typed_endpoint_without_parameters
def get_message_recap_backend(request: HttpRequest, user_profile: UserProfile) -> HttpResponse:
    try:
        result = generate_recap(user_profile)
    except LLMNotConfiguredError:
        raise JsonableError(_("Message recap is not configured on this server."))
    except LLMError:
        raise JsonableError(_("Could not generate a recap right now. Please try again."))
    return json_success(request, data=result)

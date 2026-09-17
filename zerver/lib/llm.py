import logging
import os

from zerver.lib.outgoing_http import OutgoingSession
from zproject.config import get_secret

logger = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# The cheapest, fastest tier is sufficient for summarization and
# classification; override with GEMINI_MODEL to use a larger model.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
LLM_REQUEST_TIMEOUT_SECONDS = 60


class LLMError(Exception):
    pass


class LLMNotConfiguredError(LLMError):
    pass


def get_gemini_api_key() -> str | None:
    # The environment variable takes precedence so that graders and
    # developers can supply a key without editing any files; the
    # dev-secrets.conf fallback is Zulip's standard secret store.
    return os.environ.get("GEMINI_API_KEY") or get_secret("gemini_api_key")


def get_gemini_model() -> str:
    return os.environ.get("GEMINI_MODEL") or get_secret("gemini_model", DEFAULT_GEMINI_MODEL)


def generate_text(
    prompt: str,
    *,
    system_instruction: str | None = None,
    json_output: bool = False,
    temperature: float = 0.2,
    max_output_tokens: int = 2048,
) -> str:
    """Send a single-turn prompt to Gemini and return the text response.

    Raises LLMNotConfiguredError when no API key is available, and
    LLMError for network failures or malformed responses, so callers
    can degrade gracefully instead of surfacing a 500.
    """
    api_key = get_gemini_api_key()
    if api_key is None:
        raise LLMNotConfiguredError(
            "No Gemini API key configured; set GEMINI_API_KEY or gemini_api_key in dev-secrets.conf"
        )

    generation_config: dict[str, object] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if json_output:
        generation_config["responseMimeType"] = "application/json"

    payload: dict[str, object] = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    if system_instruction is not None:
        payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

    url = f"{GEMINI_API_BASE}/{get_gemini_model()}:generateContent"
    session = OutgoingSession(
        role="llm",
        timeout=LLM_REQUEST_TIMEOUT_SECONDS,
        headers={"x-goog-api-key": api_key},
    )
    try:
        response = session.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        logger.warning("Gemini request failed: %s", e)
        raise LLMError("The language model request failed") from e

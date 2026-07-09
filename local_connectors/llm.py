import logging
import os
import time
from datetime import datetime
import instructor
from google import genai
from google.genai import types
from pydantic import BaseModel

from core.models import MatchResult

logging.getLogger("instructor").setLevel(logging.CRITICAL)

_gemini = None


def _get_gemini() -> instructor.Instructor:
    global _gemini

    if _gemini is None:
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set")

        _gemini = instructor.from_genai(
            genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=30_000),
            ),
        )

    return _gemini

EXTRACT_MODEL = "gemini-3.1-flash-lite"
JUDGE_MODEL = "gemini-2.5-flash" #gemini-3.5-flash gemini-3.1-flash-lite | gemini-3-flash gemini-2.5-flash gemini-2.5-flash-lite

_FALLBACKS: dict[str, list[str]] = {
    EXTRACT_MODEL: [
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
    ],
    JUDGE_MODEL: [
        "gemini-2.5-flash-lite",
        "gemini-3.5-flash"
    ],
}

_RETRYABLE = ("503", "429", "500", "502", "504", "UNAVAILABLE", "capacity", "rate limit")


def _is_quota_error(error: Exception) -> bool:
    error_str = str(error)
    return "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota exceeded" in error_str or "rate limit" in error_str


def _call_with_fallback[T: BaseModel](
    schema: type[T],
    messages: list[dict],
    model: str,
    max_attempts: int = 4,
    backoff: float = 2.0,
) -> T:
    
    gemini = _get_gemini()
    chain: list[tuple[instructor.Instructor, str]] = [(gemini, model)] + [(gemini, fallback_model) for fallback_model in _FALLBACKS.get(model, [])]
    last_error: Exception | None = None

    for client, current_model in chain:
        for attempt in range(1, max_attempts + 1):
            try:
                result = client.chat.completions.create(
                    model=current_model,
                    response_model=schema,
                    messages=messages,
                    strict=False,
                )

                if current_model != model:
                    print(f"Fallback model used: {current_model}")
                return result

            except Exception as e:
                last_error = e
                error_str = str(e)
                # server error or rate limit
                if any(marker in error_str for marker in _RETRYABLE):
                    wait = backoff * attempt
                    print(f"  ⏳ API {current_model}: {error_str[:80]}... "
                          f"(attempt {attempt}/{max_attempts}, waiting {wait:.0f}с)")
                    time.sleep(wait)
                else:
                    # 400, 404
                    raise

        if len(chain) > 1:
            print(f"  🔄 Model {current_model} unavailable, trying next...")

    raise last_error


def extract[T: BaseModel](
    text: str,
    schema: type[T],
    instruction: str,
    model: str = EXTRACT_MODEL,
) -> T:
    return _call_with_fallback(
        schema=schema,
        messages=[
            {"role": "user", "content": f"{instruction}\n\n---\n{text}"},
        ],
        model=model,
    )



def score_match(vacancy_raw: str, profile_summary: str, prefs_summary: str) -> MatchResult:
    instruction = (
        "Rate the suitability of the vacancy for the candidate on a scale of 0..100 and explain the reasons.\n"
        f"Candidate: {profile_summary}\n"
        f"Priorities: {prefs_summary}"
    )
    try:
        return _call_with_fallback(
            schema=MatchResult,
            messages=[
                {"role": "user", "content": f"{instruction}\n\n---\n{vacancy_raw}"},
            ],
            model=JUDGE_MODEL,
        )
    except Exception as error:
        if _is_quota_error(error):
            print("  ⚠️ Gemini quota exhausted; returning neutral score for this vacancy.")
            return MatchResult(score=0, reasons=["Gemini quota exhausted"])
        raise


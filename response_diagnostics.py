"""Privacy-safe evidence and semantic classification for CLI responses.

The helpers in this module intentionally retain only structural observations.
They never retain event bodies, model text, prompts, tool arguments, or raw
stdout/stderr.  Callers may use :func:`safe_review_suffix` only with an already
identified final assistant response, never with an arbitrary event envelope.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal, TypeAlias, cast


ContentErrorCode: TypeAlias = Literal[
    "empty_model_response",
    "stream_closed_before_final",
    "content_parse_failed",
    "content_filtered",
    "final_block_missing",
]
OutputFormat: TypeAlias = Literal["json", "stream-json"]
SafeEventType: TypeAlias = Literal["init", "step_update", "result", "unknown"]

CONTENT_ERROR_CODES: tuple[ContentErrorCode, ...] = (
    "empty_model_response",
    "stream_closed_before_final",
    "content_parse_failed",
    "content_filtered",
    "final_block_missing",
)
MAX_RESPONSE_ID_LENGTH = 128
MAX_DIAGNOSTIC_COUNT = 1_000_000
MAX_REVIEW_SUFFIX_CHARS = 512
MAX_REVIEW_SCAN_CHARS = 65_536

_SAFE_RESPONSE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_EVENT_TYPES = frozenset(("init", "step_update", "result"))
_FILTER_REASONS = frozenset((
    "blocked",
    "content_filter",
    "content_filtered",
    "prohibited_content",
    "safety",
))


def safe_response_id(value: object) -> str | None:
    """Return an allowlisted opaque response identifier, never arbitrary text."""

    if not isinstance(value, str) or len(value) > MAX_RESPONSE_ID_LENGTH:
        return None
    return value if _SAFE_RESPONSE_ID.fullmatch(value) else None


def _safe_optional_count(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return min(value, MAX_DIAGNOSTIC_COUNT)


def _safe_required_count(value: object) -> int:
    count = _safe_optional_count(value)
    return count if count is not None else 0


@dataclass(frozen=True, slots=True)
class ResponseDiagnostics:
    """Immutable, bounded observations safe enough for durable snapshots."""

    output_format: OutputFormat | None = None
    final_event_seen: bool | None = None
    last_safe_event_type: SafeEventType | None = None
    response_id: str | None = None
    content_block_count: int | None = None
    malformed_event_count: int = 0

    def __post_init__(self) -> None:
        output_format = (
            self.output_format
            if self.output_format in ("json", "stream-json")
            else None
        )
        final_event_seen = (
            self.final_event_seen if isinstance(self.final_event_seen, bool) else None
        )
        if self.last_safe_event_type in _SAFE_EVENT_TYPES:
            event_type: SafeEventType | None = cast(
                SafeEventType, self.last_safe_event_type
            )
        elif isinstance(self.last_safe_event_type, str):
            event_type = "unknown"
        else:
            event_type = None

        object.__setattr__(self, "output_format", output_format)
        object.__setattr__(self, "final_event_seen", final_event_seen)
        object.__setattr__(self, "last_safe_event_type", event_type)
        object.__setattr__(self, "response_id", safe_response_id(self.response_id))
        object.__setattr__(
            self, "content_block_count", _safe_optional_count(self.content_block_count)
        )
        object.__setattr__(
            self, "malformed_event_count", _safe_required_count(self.malformed_event_count)
        )

    def as_dict(self) -> dict[str, object]:
        """Return a fresh JSON-compatible mapping with no raw response material."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContentClassification:
    """One mutually exclusive semantic failure and its safe retry hint."""

    error_type: ContentErrorCode
    retryable: bool


def _normalized_reason(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in _FILTER_REASONS else None


def _payload_reports_filter(payload: Mapping[str, object] | None) -> bool:
    if payload is None:
        return False
    if payload.get("content_filtered") is True:
        return True
    if _normalized_reason(payload.get("error_type")) == "content_filtered":
        return True
    for key in ("finish_reason", "finishReason", "block_reason", "blockReason"):
        if _normalized_reason(payload.get(key)) is not None:
            return True
    return False


def _payload_output_tokens(payload: Mapping[str, object] | None) -> int | float | None:
    if payload is None:
        return None
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None
    value = usage.get("output_tokens")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def is_retryable_content_error(error_type: ContentErrorCode, mode: str) -> bool:
    """Permit a retry only for read-only plan runs with transient missing output."""

    return mode == "plan" and error_type in (
        "stream_closed_before_final",
        "final_block_missing",
    )


def classify_content_result(
    payload: Mapping[str, object] | None,
    diagnostics: ResponseDiagnostics,
    *,
    parse_failed: bool = False,
    content_filtered: bool = False,
    output_tokens: int | float | None = None,
    mode: str = "plan",
) -> ContentClassification | None:
    """Classify final content observations using deterministic precedence.

    A non-blank string in ``payload["response"]`` is deliverable content and
    succeeds unless the caller separately rejects it during verification.
    Malformed intermediate NDJSON does not poison a later valid final result.
    ``output_tokens`` may carry allowlisted usage observed outside the final
    payload; invalid or non-finite values are treated as unavailable.
    """

    response_present = payload is not None and "response" in payload
    response = payload.get("response") if response_present else None
    deliverable = isinstance(response, str) and bool(response.strip())

    # A filter is a semantic failure only when no usable final content exists.
    if not deliverable and (
        content_filtered is True or _payload_reports_filter(payload)
    ):
        code: ContentErrorCode = "content_filtered"
    elif diagnostics.output_format == "stream-json" and not diagnostics.final_event_seen:
        code = (
            "content_parse_failed"
            if parse_failed is True or diagnostics.malformed_event_count > 0
            else "stream_closed_before_final"
        )
    elif parse_failed is True:
        code = "content_parse_failed"
    elif payload is None or not response_present:
        code = "final_block_missing"
    elif not isinstance(response, str):
        code = "content_parse_failed"
    elif deliverable:
        return None
    else:
        tokens = output_tokens
        if not isinstance(tokens, (int, float)) or isinstance(tokens, bool):
            tokens = _payload_output_tokens(payload)
        else:
            try:
                if tokens < 0 or not math.isfinite(tokens):
                    tokens = _payload_output_tokens(payload)
            except OverflowError:
                tokens = _payload_output_tokens(payload)
        code = "final_block_missing" if tokens is not None and tokens > 0 else (
            "empty_model_response"
        )

    return ContentClassification(
        error_type=code,
        retryable=is_retryable_content_error(code, mode),
    )


_ANSI_ESCAPE = re.compile(
    r"(?:\x1B\[[0-?]*[ -/]*[@-~]|\x1B\][^\x07\x1B]*(?:\x07|\x1B\\))"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]{0,48}PRIVATE KEY)-----.*?"
    r"-----END (?P=label)-----",
    re.IGNORECASE | re.DOTALL,
)
_PRIVATE_KEY_MARKER = re.compile(
    r"-----\s*(?:BEGIN|END)\s+[A-Z0-9 ]{0,64}PRIVATE KEY\s*-----",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_KNOWN_SECRET_TOKEN = re.compile(
    r"(?ix)(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|"
    r"AIza[A-Za-z0-9_-]{20,}|"
    r"AKIA[A-Z0-9]{16}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}"
    r")(?![A-Za-z0-9_-])"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?ix)(?<![A-Za-z0-9])(?P<name>"
    r"(?:[A-Za-z][A-Za-z0-9_-]{0,32}[_-])?"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"auth(?:orization)?|bearer|client[_-]?secret|credential|cookie|password|"
    r"private[_-]?key|secret|session(?:[_-]?(?:id|token))?|token))"
    r"(?![A-Za-z0-9])\s*[:=]\s*"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)"
)
_TOKEN_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9+/=_.-])[A-Za-z0-9+/=_.-]{24,2048}"
    r"(?![A-Za-z0-9+/=_.-])"
)
_OVERSIZED_TOKEN = re.compile(r"[A-Za-z0-9+/=_.-]{2049,}")
_RAW_PAYLOAD_MARKER = re.compile(
    r"(?is)(?:<\s*/?\s*(?:assistant|function_call|function_result|scratchpad|"
    r"system|tool|tool_call|tool_input|tool_result|tool_response|thinking|"
    r"user|user_input)\b|"
    r"[\"'](?:arguments|messages|prompt|system|system_prompt|thinking|thought|"
    r"tool|tool_args|tool_input|tool_result|user_prompt|user_input)[\"']\s*:|"
    r"\brole\s*[:=]\s*[\"']?(?:tool|user)\b)"
)
_URI_USERINFO = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@")


def _entropy(value: str) -> float:
    frequencies: dict[str, int] = {}
    for character in value:
        frequencies[character] = frequencies.get(character, 0) + 1
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length)
        for count in frequencies.values()
    )


def _looks_high_entropy(value: str) -> bool:
    classes = sum((
        any(character.islower() for character in value),
        any(character.isupper() for character in value),
        any(character.isdigit() for character in value),
        any(character in "+/=_-" for character in value),
    ))
    return classes >= 2 and _entropy(value) >= 3.5


def _strip_controls(value: str) -> str:
    return "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in value
    )


def safe_review_suffix(
    value: object, *, max_chars: int = MAX_REVIEW_SUFFIX_CHARS
) -> str | None:
    """Return a bounded, fail-closed suffix of a final assistant response.

    The input must already be known to be final assistant text.  Structured
    user/tool/thinking payload markers and ambiguous credential containers are
    rejected instead of heuristically persisted.
    """

    if (
        not isinstance(value, str)
        or not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
        or max_chars < 1
        or max_chars > MAX_REVIEW_SUFFIX_CHARS
        or not value
        or len(value) > MAX_REVIEW_SCAN_CHARS
    ):
        return None
    if _RAW_PAYLOAD_MARKER.search(value) or _URI_USERINFO.search(value):
        return None
    if _OVERSIZED_TOKEN.search(value):
        return None

    cleaned = _PRIVATE_KEY_BLOCK.sub(" [REDACTED_PRIVATE_KEY] ", value)
    # An incomplete or non-standard private-key block is unsafe to retain.
    if _PRIVATE_KEY_MARKER.search(cleaned):
        return None
    cleaned = _ANSI_ESCAPE.sub("", cleaned)
    cleaned = _strip_controls(cleaned)
    cleaned = _BEARER.sub("Bearer [REDACTED]", cleaned)
    cleaned = _JWT.sub("[REDACTED]", cleaned)
    cleaned = _KNOWN_SECRET_TOKEN.sub("[REDACTED]", cleaned)
    cleaned = _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: f"{match.group('name')}=[REDACTED]", cleaned
    )
    cleaned = _TOKEN_CANDIDATE.sub(
        lambda match: "[REDACTED]"
        if _looks_high_entropy(match.group(0))
        else match.group(0),
        cleaned,
    )
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return None
    return cleaned[-max_chars:]

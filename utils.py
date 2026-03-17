"""
utils.py
--------
Shared utilities for the healthcare AI documentation pipeline.

Provides:
- GCP Secret Manager access
- Structured JSON logging with PHI-safe sanitization
- Common exception types
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from functools import lru_cache
from typing import Any

from google.cloud import secretmanager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GCP_PROJECT: str = os.environ["GCP_PROJECT"]

# PHI field names that must never appear in log output.
_PHI_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "patient_name",
        "first_name",
        "last_name",
        "full_name",
        "date_of_birth",
        "dob",
        "phone",
        "phone_number",
        "email",
        "email_address",
        "ssn",
        "social_security",
        "address",
        "transcript_text",
        "note_body",
        "soap_note",
        "raw_text",
    }
)

# Regex patterns for common PHI in free-text strings.
_PHI_PATTERNS: list[tuple[str, str]] = [
    # SSN: 123-45-6789
    (r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED-SSN]"),
    # US phone numbers
    (r"\b(\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b", "[REDACTED-PHONE]"),
    # Email addresses
    (r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", "[REDACTED-EMAIL]"),
    # ISO date of birth pattern (YYYY-MM-DD)
    (r"\b(19|20)\d{2}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b", "[REDACTED-DATE]"),
    # US date format MM/DD/YYYY
    (r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])/(19|20)\d{2}\b", "[REDACTED-DATE]"),
]

_COMPILED_PHI_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(pattern, re.IGNORECASE), replacement)
    for pattern, replacement in _PHI_PATTERNS
]


# ---------------------------------------------------------------------------
# PHI Sanitization
# ---------------------------------------------------------------------------


def sanitize_for_logging(value: Any) -> Any:
    """
    Recursively sanitize a value before it enters a log record.

    - Dicts: redact any key listed in _PHI_FIELD_NAMES; recurse into values.
    - Strings: apply regex PHI pattern replacement.
    - Lists: recurse into each element.
    - All other types: returned unchanged.

    Args:
        value: The value to sanitize. May be a dict, list, str, or primitive.

    Returns:
        A sanitized copy of the input. Never mutates the original.
    """
    if isinstance(value, dict):
        return {
            k: "[REDACTED-PHI]" if k.lower() in _PHI_FIELD_NAMES else sanitize_for_logging(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize_for_logging(item) for item in value]
    if isinstance(value, str):
        return _redact_phi_patterns(value)
    return value


def _redact_phi_patterns(text: str) -> str:
    for pattern, replacement in _COMPILED_PHI_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Structured Logging
# ---------------------------------------------------------------------------


class _StructuredJsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects for Cloud Logging.

    Cloud Logging ingests structured JSON logs and maps the `severity` field
    to the native log severity level. The `message` field is used as the
    log entry's text payload.
    """

    _SEVERITY_MAP: dict[int, str] = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": self._SEVERITY_MAP.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"),
        }

        # Attach any extra structured fields passed via record.__dict__.
        for key, val in record.__dict__.items():
            if key not in {
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "levelname", "levelno", "lineno",
                "message", "module", "msecs", "msg", "name", "pathname",
                "process", "processName", "relativeCreated", "stack_info",
                "thread", "threadName",
            }:
                payload[key] = val

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger that writes PHI-safe structured JSON to stdout.

    Cloud Functions captures stdout and ingests it into Cloud Logging.
    Using the structured formatter ensures log entries are queryable by
    field (e.g., outcome, file_id, duration_ms).

    Args:
        name: Logger name, typically __name__ of the calling module.

    Returns:
        Configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Already configured (idempotent in warm instances).

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_StructuredJsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


# ---------------------------------------------------------------------------
# Secret Manager
# ---------------------------------------------------------------------------


@lru_cache(maxsize=32)
def get_secret(secret_id: str, version: str = "latest") -> str:
    """
    Fetch a secret value from GCP Secret Manager.

    Results are cached per secret_id+version for the lifetime of the
    function instance (warm start), reducing Secret Manager API calls
    and associated latency on subsequent invocations.

    Args:
        secret_id: The Secret Manager secret name (not the full resource path).
        version: Secret version to fetch. Defaults to "latest".

    Returns:
        The secret value as a decoded UTF-8 string.

    Raises:
        google.api_core.exceptions.NotFound: If the secret does not exist.
        google.api_core.exceptions.PermissionDenied: If the caller lacks access.
    """
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{GCP_PROJECT}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


# ---------------------------------------------------------------------------
# Retry Utility
# ---------------------------------------------------------------------------


def retry_with_backoff(
    fn,
    *args,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    logger: logging.Logger | None = None,
    **kwargs,
) -> Any:
    """
    Call `fn(*args, **kwargs)` with exponential backoff on failure.

    Doubles the delay after each failure, capped at `max_delay` seconds.
    Jitter is not applied here; callers that need it should wrap this function.

    Args:
        fn: Callable to invoke.
        *args: Positional arguments for fn.
        max_attempts: Maximum number of total attempts (including the first).
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Maximum delay in seconds between attempts.
        retryable_exceptions: Tuple of exception types that trigger a retry.
            All other exceptions propagate immediately.
        logger: Optional logger for retry warning messages.
        **kwargs: Keyword arguments for fn.

    Returns:
        The return value of fn on success.

    Raises:
        The last exception raised by fn after all attempts are exhausted.
    """
    delay = base_delay
    last_exc: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_attempts:
                break
            if logger:
                logger.warning(
                    "Retryable error on attempt %d/%d — retrying in %.1fs",
                    attempt,
                    max_attempts,
                    delay,
                    extra={
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "retry_delay_seconds": delay,
                        "error_type": type(exc).__name__,
                    },
                )
            time.sleep(delay)
            delay = min(delay * 2, max_delay)

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------


class PipelineError(Exception):
    """Base class for all pipeline-specific errors."""


class TranscriptParseError(PipelineError):
    """Raised when a .vtt transcript cannot be parsed into usable text."""


class GeminiError(PipelineError):
    """Raised when the Gemini API returns an error or invalid response."""


class SalesforceMatchError(PipelineError):
    """Raised when no Salesforce contact can be matched to the transcript."""


class SalesforceAPIError(PipelineError):
    """Raised when the Salesforce REST API returns an unexpected error."""


class DriveAPIError(PipelineError):
    """Raised when a Google Drive API call fails after retries."""

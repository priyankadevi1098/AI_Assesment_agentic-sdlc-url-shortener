"""OpenAI-compatible HTTPS API backend: environment configuration, bounded
retries, timeouts, output-size limits and sanitized errors.

No API key, raw request body or raw response body is ever included in a raised
error message — only the operation and a bounded, generic reason.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_OUTPUT_BYTES = 1_000_000
RETRYABLE_HTTP_STATUSES = (408, 429, 500, 502, 503, 504)


class LlmConfigError(RuntimeError):
    """Raised when API backend configuration is missing or invalid."""


class LlmRequestError(RuntimeError):
    """Raised when the API request exhausts retries or returns an unrecoverable error."""


class SchemaValidationError(RuntimeError):
    """Raised when a task payload or model response does not match its declared schema."""


@dataclass(frozen=True)
class LlmConfig:
    """API backend configuration, loaded once from environment variables."""

    model: str
    api_key: Optional[str]
    base_url: str
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    @classmethod
    def from_env(cls, env: Dict[str, str]) -> "LlmConfig":
        """Build configuration from an environment mapping.

        Raises:
            LlmConfigError: ``LLM_MODEL`` is missing, the base URL is not HTTPS
                (or loopback HTTP), or no API key is configured for a
                non-loopback endpoint.
        """
        model = env.get("LLM_MODEL")
        if not model:
            raise LlmConfigError("LLM_MODEL environment variable is required for the API backend")

        base_url = env.get("LLM_BASE_URL", DEFAULT_BASE_URL)
        parsed = urlparse(base_url)
        is_loopback_http = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme != "https" and not is_loopback_http:
            raise LlmConfigError(
                "LLM_BASE_URL must use HTTPS, or plain HTTP only against localhost "
                f"(got scheme={parsed.scheme!r} host={parsed.hostname!r})"
            )

        api_key = env.get("LLM_API_KEY")
        if not api_key and not is_loopback_http:
            raise LlmConfigError("LLM_API_KEY environment variable is required for non-loopback endpoints")

        return cls(model=model, api_key=api_key, base_url=base_url)


def validate_schema(payload: dict, schema: dict) -> None:
    """Validate required keys and declared top-level types.

    ``schema`` is ``{"required": [...], "properties": {"key": type_or_tuple}}``.

    Raises:
        SchemaValidationError: the payload is not a JSON object, a required key
            is missing, or a declared property has the wrong type.
    """
    if not isinstance(payload, dict):
        raise SchemaValidationError("payload must be a JSON object")

    missing = [key for key in schema.get("required", []) if key not in payload]
    if missing:
        raise SchemaValidationError(f"missing required fields: {missing}")

    for key, expected_type in schema.get("properties", {}).items():
        if key in payload and not isinstance(payload[key], expected_type):
            raise SchemaValidationError(f"field {key!r} has the wrong type")


class ApiBackend:
    """OpenAI-compatible HTTPS backend with bounded, explicit retries.

    The HTTP transport and the sleep function are both injected so tests never
    need a real network call or a real delay.
    """

    def __init__(self, config: LlmConfig,
                 transport: Optional[Callable[[urllib.request.Request], Any]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._config = config
        self._transport = transport or self._default_transport
        self._sleep = sleep

    def _default_transport(self, request: urllib.request.Request) -> bytes:
        with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
            return response.read(self._config.max_output_bytes + 1)

    def generate(self, task_payload: dict, schema: dict) -> dict:
        """Send a structured task to the API and return its validated JSON response.

        Raises:
            SchemaValidationError: the task payload or the response does not
                match its declared schema.
            LlmRequestError: the request exhausted its bounded retries, or the
                response exceeded the output-size limit or was not valid JSON.
        """
        validate_schema(task_payload, schema)

        body = json.dumps({"model": self._config.model, "input": task_payload}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        last_error: Optional[str] = None
        for attempt in range(1, self._config.max_retries + 1):
            request = urllib.request.Request(
                f"{self._config.base_url}/responses", data=body, headers=headers, method="POST"
            )
            try:
                raw = self._transport(request)
            except urllib.error.HTTPError as exc:
                if exc.code in RETRYABLE_HTTP_STATUSES and attempt < self._config.max_retries:
                    last_error = f"HTTP {exc.code}"
                    self._sleep(2 ** (attempt - 1))
                    continue
                raise LlmRequestError(f"API request failed with HTTP {exc.code}") from None
            except urllib.error.URLError:
                if attempt < self._config.max_retries:
                    last_error = "network error"
                    self._sleep(2 ** (attempt - 1))
                    continue
                raise LlmRequestError("API request failed: network error after retries") from None
            else:
                return self._parse_response(raw, schema)

        raise LlmRequestError(f"API request exhausted {self._config.max_retries} attempts ({last_error})")

    def _parse_response(self, raw: bytes, schema: dict) -> dict:
        if len(raw) > self._config.max_output_bytes:
            raise LlmRequestError("API response exceeded the configured output size limit")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            raise LlmRequestError("API response was not valid JSON") from None
        validate_schema(result, schema.get("response", schema))
        return result

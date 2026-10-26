"""Pluggable LLM providers, all speaking one tiny interface: JSON in, dict out.

Everything the parser asks a model for is a *structured* answer (line indices,
an option letter, a confidence), never prose. So the whole provider layer is one
method -- `complete_json` -- and every provider is responsible for coaxing its
own API into returning a single JSON object that matches the caller's schema.

Design notes worth keeping in mind before editing:

* Gemini is the default because the free tier is what the user actually has.
  It is the only provider that uses a vendor SDK (`google-genai`); the other
  three go over plain HTTP with `requests` so that choosing OpenAI or Anthropic
  costs the app no extra dependency.
* `temperature=0` everywhere. This is extraction, not writing.
* Backoff is deterministic (no `random`): a workflow that reruns must behave the
  same way twice, and jitter derived from the attempt number is enough to stop
  four worker threads from retrying in perfect lockstep.
* The API key is never logged, never put in an exception message and never in a
  dataclass `repr`.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, TypeVar

from app.models import ImageAsset

log = logging.getLogger(__name__)

T = TypeVar("T")

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ProviderError(Exception):
    """Any provider-side failure the caller is expected to survive.

    `app.parse.ai` catches this per chunk and falls back to the rule-based
    parser, so a raised ProviderError must always carry enough detail to end up
    in a user-visible warning -- and never carry the API key.
    """


class RateLimited(ProviderError):
    """Retries were exhausted on 429/5xx. The free tier hits this in practice."""


class NoApiKey(ProviderError):
    """No usable credential; the app should fall back to offline parsing."""


# --------------------------------------------------------------------------- #
# Retry / backoff policy
# --------------------------------------------------------------------------- #

#: Total attempts per logical call (1 initial + 2 retries).
MAX_ATTEMPTS = 3
#: 2s, 4s, 8s before attempts 2, 3, 4.
BACKOFF_BASE_S = 2.0
#: Statuses worth retrying: rate limits, gateway wobble, request timeouts.
RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

_MIME_BY_FMT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "emf": "image/png",   # Word sometimes reports EMF/WMF for a raster blob
    "wmf": "image/png",
}

#: Models choke on huge inline images and the free tier counts every byte, so
#: anything past this is dropped with a warning rather than sent.
MAX_INLINE_IMAGE_BYTES = 6 * 1024 * 1024


class _Transient(Exception):
    """Internal marker: this attempt failed but retrying may work."""

    def __init__(self, detail: str, status: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status = status


def backoff_delay(attempt: int) -> float:
    """Seconds to wait *after* a failed `attempt` (1-based).

    Deterministic on purpose -- see the module docstring. The `0.25 * attempt`
    term is the jitter: enough to desynchronise concurrent workers, small enough
    that the schedule stays 2s/4s/8s-ish and predictable in tests.
    """
    return BACKOFF_BASE_S * (2 ** (attempt - 1)) + 0.25 * attempt


def image_mime(asset: ImageAsset) -> str:
    """Best-effort MIME type for an extracted image."""
    return _MIME_BY_FMT.get((asset.fmt or "png").lower().lstrip("."), "image/png")


def usable_images(images: Iterable[ImageAsset] | None) -> list[ImageAsset]:
    """Drop empty/oversized blobs so one bad picture cannot fail a whole call."""
    out: list[ImageAsset] = []
    for img in images or ():
        if not img.data:
            continue
        if len(img.data) > MAX_INLINE_IMAGE_BYTES:
            log.warning(
                "skipping %d-byte image (anchor L%s): over inline limit",
                len(img.data), img.anchor_line,
            )
            continue
        out.append(img)
    return out


# --------------------------------------------------------------------------- #
# JSON salvage
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n(.*?)```", re.DOTALL)


def _snippet(raw: str, limit: int = 240) -> str:
    """A short, single-line excerpt for an error message."""
    flat = " ".join((raw or "").split())
    return flat[:limit] + ("..." if len(flat) > limit else "")


def _outermost_object(raw: str) -> str | None:
    """Return the first balanced `{...}` span, ignoring braces inside strings.

    Models like to prepend "Here is the JSON:" or append a closing remark, and
    occasionally emit two objects. Taking the first balanced object is both
    cheap and correct for our single-object schemas.
    """
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return None


def extract_json_object(raw: str | None, *, provider: str = "provider") -> dict:
    """Parse a model reply into a dict, surviving fences and stray prose.

    A bare JSON array is wrapped as `{"items": [...]}` -- some models answer the
    top-level array instead of the object, and losing a whole chunk of work over
    a missing wrapper would be a poor trade.
    """
    if raw is None or not raw.strip():
        raise ProviderError(f"{provider}: empty response")

    text = raw.strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    elif text.startswith("```"):
        # Unterminated fence: drop the opening line and any trailing backticks.
        text = text.split("\n", 1)[-1].rstrip("`").strip()

    for candidate in (text, _outermost_object(text)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {"items": value}

    raise ProviderError(
        f"{provider}: response was not JSON ({_snippet(raw)})"
    )


# --------------------------------------------------------------------------- #
# Schema adaptation
# --------------------------------------------------------------------------- #


def gemini_schema(schema: dict) -> dict:
    """A deep copy of `schema` trimmed to what the Gemini API accepts.

    Two reasons this exists: the SDK *mutates* the dict it is handed while
    inlining `$defs`, so sharing a module-level schema object across calls would
    slowly corrupt it; and `additionalProperties` is rejected outright by the
    Gemini Developer API.
    """
    drop = {"additionalProperties", "additional_properties", "$schema", "$id"}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items() if k not in drop}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(copy.deepcopy(schema))


def strict_json_schema(schema: dict) -> dict:
    """A deep copy rewritten for OpenAI-style `strict` structured output.

    Strict mode demands every object list all of its properties in `required`
    and set `additionalProperties: false`; optional-ness is expressed by a
    `"null"` member in the type union instead. Our schema is written in the
    Gemini dialect (`nullable: true`), so translate it here.
    """

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node

        out: dict[str, Any] = {}
        nullable = bool(node.pop("nullable", False))
        for key, value in node.items():
            if key in ("propertyOrdering", "property_ordering", "nullable"):
                continue
            out[key] = walk(value)

        if nullable and isinstance(out.get("type"), str):
            out["type"] = [out["type"], "null"]

        if out.get("type") == "object" or "properties" in out:
            props = out.get("properties") or {}
            out["properties"] = props
            out["required"] = list(props)
            out["additionalProperties"] = False
        return out

    return walk(copy.deepcopy(schema))


# --------------------------------------------------------------------------- #
# Base provider
# --------------------------------------------------------------------------- #


@dataclass
class Provider(ABC):
    """One model endpoint, reduced to "give me JSON matching this schema"."""

    api_key: str = field(repr=False)
    model: str = ""

    # -- interface ---------------------------------------------------------- #

    @abstractmethod
    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict,
        *,
        images: list[ImageAsset] | None = None,
        timeout: float = 120.0,
    ) -> dict:
        """Send one request and return the parsed JSON object.

        Raises `NoApiKey`, `RateLimited` or `ProviderError`; never returns None
        and never raises anything a caller cannot recognise.
        """

    @property
    def name(self) -> str:
        return type(self).__name__.removesuffix("Provider").lower()

    # -- shared plumbing ---------------------------------------------------- #

    def _require_key(self) -> None:
        if not (self.api_key or "").strip():
            raise NoApiKey(f"{self.name}: no API key configured")

    def _with_retries(self, attempt_fn: Callable[[int], T]) -> T:
        """Run `attempt_fn(attempt)`, retrying `_Transient` failures."""
        last = "unknown error"
        last_status: int | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return attempt_fn(attempt)
            except _Transient as exc:
                last, last_status = exc.detail, exc.status
                if attempt >= MAX_ATTEMPTS:
                    break
                delay = backoff_delay(attempt)
                log.warning(
                    "%s attempt %d/%d failed (%s); retrying in %.2fs",
                    self.name, attempt, MAX_ATTEMPTS, last, delay,
                )
                time.sleep(delay)
        raise RateLimited(
            f"{self.name}: gave up after {MAX_ATTEMPTS} attempts "
            f"(last status {last_status}: {last})"
        )

    def _classify_status(self, status: int, body: str) -> None:
        """Turn a non-2xx HTTP status into the right exception."""
        detail = f"HTTP {status}: {_snippet(body)}"
        if status in RETRY_STATUSES:
            raise _Transient(detail, status)
        if status in (401, 403):
            raise NoApiKey(f"{self.name}: credential rejected ({status})")
        raise ProviderError(f"{self.name}: {detail}")


# --------------------------------------------------------------------------- #
# Gemini (default)
# --------------------------------------------------------------------------- #

#: See app.config.DEFAULT_MODEL for the measurements behind this choice. Do not
#: put a `gemini-2.5-*` name here: those 404 for keys issued after mid-2026.
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"


@dataclass
class GeminiProvider(Provider):
    """Google AI Studio / Gemini Developer API via the `google-genai` SDK.

    This is the default and the path that has to be right: the free tier is the
    only key most users will ever have. Structured output is requested with
    `response_mime_type="application/json"` plus `response_schema`, which makes
    the reply a JSON object by construction rather than by persuasion.
    """

    model: str = DEFAULT_GEMINI_MODEL
    _client: Any = field(default=None, init=False, repr=False, compare=False)

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict,
        *,
        images: list[ImageAsset] | None = None,
        timeout: float = 120.0,
    ) -> dict:
        self._require_key()
        genai, gtypes, gerrors, transient = _load_genai()

        parts = [gtypes.Part.from_text(text=user)]
        for img in usable_images(images):
            parts.append(
                gtypes.Part.from_bytes(data=img.data, mime_type=image_mime(img))
            )
        contents = [gtypes.Content(role="user", parts=parts)]

        config = gtypes.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=gemini_schema(schema),
            temperature=0.0,
            candidate_count=1,
            # HttpOptions.timeout is in MILLISECONDS (verified against the SDK).
            http_options=gtypes.HttpOptions(timeout=int(max(1.0, timeout) * 1000)),
        )

        def attempt(_n: int) -> dict:
            client = self._ensure_client(genai)
            try:
                resp = client.models.generate_content(
                    model=self.model, contents=contents, config=config
                )
            except gerrors.ServerError as exc:
                raise _Transient(_api_error_detail(exc), _api_error_code(exc)) from exc
            except gerrors.ClientError as exc:
                code = _api_error_code(exc)
                detail = _api_error_detail(exc)
                if code in RETRY_STATUSES:
                    raise _Transient(detail, code) from exc
                if code in (401, 403):
                    raise NoApiKey(
                        f"{self.name}: credential rejected ({code})"
                    ) from exc
                raise ProviderError(f"{self.name}: {detail}") from exc
            except gerrors.APIError as exc:      # anything else the SDK maps
                raise ProviderError(f"{self.name}: {_api_error_detail(exc)}") from exc
            except transient as exc:             # socket/timeout level failures
                self._client = None              # rebuild the client next time
                raise _Transient(f"{type(exc).__name__}: {exc}") from exc
            return self._payload_of(resp, gtypes)

        return self._with_retries(attempt)

    # -- helpers ------------------------------------------------------------ #

    def _ensure_client(self, genai: Any) -> Any:
        if self._client is None:
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _payload_of(self, resp: Any, gtypes: Any) -> dict:
        """Pull the JSON object out of a GenerateContentResponse."""
        self._check_finish_reason(resp, gtypes)

        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, dict) and parsed:
            return parsed
        if isinstance(parsed, list) and parsed:
            return {"items": parsed}

        return extract_json_object(getattr(resp, "text", None), provider=self.name)

    def _check_finish_reason(self, resp: Any, gtypes: Any) -> None:
        """Convert a non-STOP finish into a clear, actionable error.

        MAX_TOKENS is the realistic failure here: the flash models spend output
        tokens on thinking, so a very dense chunk can be cut off mid-JSON. The
        caller turns this into a rules-based fallback for that chunk.
        """
        feedback = getattr(resp, "prompt_feedback", None)
        blocked = getattr(feedback, "block_reason", None) if feedback else None
        if blocked:
            raise ProviderError(f"{self.name}: prompt blocked ({blocked})")

        candidates = getattr(resp, "candidates", None) or []
        if not candidates:
            raise ProviderError(f"{self.name}: response contained no candidates")

        reason = getattr(candidates[0], "finish_reason", None)
        if reason is None:
            return
        ok = {gtypes.FinishReason.STOP, gtypes.FinishReason.FINISH_REASON_UNSPECIFIED}
        if reason in ok:
            return
        if reason is gtypes.FinishReason.MAX_TOKENS:
            raise ProviderError(
                f"{self.name}: output truncated (MAX_TOKENS) -- chunk too dense"
            )
        raise ProviderError(f"{self.name}: generation stopped ({reason})")


_GENAI_CACHE: tuple[Any, Any, Any, tuple[type[BaseException], ...]] | None = None


def _load_genai() -> tuple[Any, Any, Any, tuple[type[BaseException], ...]]:
    """Import `google.genai` lazily and cache the handles.

    Lazy because this is a desktop GUI: importing the SDK (and httpx, and
    pydantic) at app start would show up as launch latency for a user who never
    turns AI on.
    """
    global _GENAI_CACHE
    if _GENAI_CACHE is not None:
        return _GENAI_CACHE
    try:
        from google import genai
        from google.genai import errors as gerrors
        from google.genai import types as gtypes
    except ImportError as exc:      # pragma: no cover - dependency is declared
        raise ProviderError(
            "google-genai is not installed; run: pip install google-genai"
        ) from exc

    transient: list[type[BaseException]] = [TimeoutError, ConnectionError, OSError]
    try:
        import httpx      # a google-genai dependency, so effectively always here
    except ImportError:   # pragma: no cover
        pass
    else:
        transient.extend((httpx.TimeoutException, httpx.TransportError))

    _GENAI_CACHE = (genai, gtypes, gerrors, tuple(transient))
    return _GENAI_CACHE


def _api_error_code(exc: Any) -> int | None:
    code = getattr(exc, "code", None)
    return code if isinstance(code, int) else None


def _api_error_detail(exc: Any) -> str:
    """A log-safe one-liner for a genai APIError (never echoes the key)."""
    code = _api_error_code(exc)
    message = getattr(exc, "message", None) or str(exc)
    status = getattr(exc, "status", None)
    bits = [b for b in (f"HTTP {code}" if code else "", str(status or "")) if b]
    return f"{' '.join(bits)}: {_snippet(message)}".lstrip(": ")


# --------------------------------------------------------------------------- #
# HTTP providers (no extra dependency: plain `requests`)
# --------------------------------------------------------------------------- #


@dataclass
class _HttpProvider(Provider):
    """Shared POST-JSON-and-retry machinery for the REST-based providers."""

    def _post(
        self, url: str, headers: dict[str, str], body: dict, timeout: float
    ) -> dict:
        import requests   # lazy for the same launch-latency reason as genai

        def attempt(_n: int) -> dict:
            try:
                resp = requests.post(
                    url, headers=headers, json=body, timeout=timeout
                )
            except requests.Timeout as exc:
                raise _Transient(f"timeout after {timeout:.0f}s") from exc
            except requests.ConnectionError as exc:
                raise _Transient(f"connection error: {_snippet(str(exc))}") from exc
            except requests.RequestException as exc:
                raise ProviderError(
                    f"{self.name}: request failed ({_snippet(str(exc))})"
                ) from exc

            if resp.status_code >= 400:
                self._classify_status(resp.status_code, resp.text)
            try:
                data = resp.json()
            except ValueError as exc:
                raise ProviderError(
                    f"{self.name}: non-JSON envelope ({_snippet(resp.text)})"
                ) from exc
            if not isinstance(data, dict):
                raise ProviderError(f"{self.name}: unexpected envelope type")
            return data

        return self._with_retries(attempt)

    def _data_url_parts(self, images: list[ImageAsset] | None) -> list[dict[str, Any]]:
        """OpenAI-style `image_url` content parts."""
        parts: list[dict[str, Any]] = []
        for img in usable_images(images):
            b64 = base64.b64encode(img.data).decode("ascii")
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image_mime(img)};base64,{b64}"},
                }
            )
        return parts


@dataclass
class OpenAIProvider(_HttpProvider):
    """OpenAI chat completions with a strict `json_schema` response format."""

    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"
    schema_name: str = "extraction"

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict,
        *,
        images: list[ImageAsset] | None = None,
        timeout: float = 120.0,
    ) -> dict:
        self._require_key()
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        content.extend(self._data_url_parts(images))
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": self.schema_name,
                    "strict": True,
                    "schema": strict_json_schema(schema),
                },
            },
        }
        data = self._post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            body,
            timeout,
        )
        return self._read_choice(data)

    def _read_choice(self, data: dict) -> dict:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name}: no choices in response")
        first = choices[0] or {}
        if first.get("finish_reason") == "length":
            raise ProviderError(f"{self.name}: output truncated (length)")
        message = first.get("message") or {}
        if message.get("refusal"):
            raise ProviderError(
                f"{self.name}: model refused ({_snippet(str(message['refusal']))})"
            )
        text = message.get("content")
        if isinstance(text, list):      # some gateways return content parts
            text = "".join(
                p.get("text", "") for p in text if isinstance(p, dict)
            )
        return extract_json_object(text, provider=self.name)


@dataclass
class GroqProvider(OpenAIProvider):
    """Groq is OpenAI-compatible; only the host and default model differ."""

    model: str = "llama-3.3-70b-versatile"
    base_url: str = "https://api.groq.com/openai/v1"


@dataclass
class AnthropicProvider(_HttpProvider):
    """Claude Messages API, using a single tool as the structured-output channel.

    Anthropic has no `response_format`, so the sanctioned trick is to declare one
    tool whose `input_schema` is our schema and force it with `tool_choice`. The
    tool is never executed -- we just read `tool_use.input`.
    """

    model: str = "claude-sonnet-4-5"
    base_url: str = "https://api.anthropic.com/v1"
    api_version: str = "2023-06-01"
    max_tokens: int = 8192
    tool_name: str = "emit_result"

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict,
        *,
        images: list[ImageAsset] | None = None,
        timeout: float = 120.0,
    ) -> dict:
        self._require_key()
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for img in usable_images(images):
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_mime(img),
                        "data": base64.b64encode(img.data).decode("ascii"),
                    },
                }
            )
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "tools": [
                {
                    "name": self.tool_name,
                    "description": "Return the structured result. Call exactly once.",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": self.tool_name},
        }
        data = self._post(
            f"{self.base_url.rstrip('/')}/messages",
            {
                "x-api-key": self.api_key,
                "anthropic-version": self.api_version,
                "Content-Type": "application/json",
            },
            body,
            timeout,
        )
        return self._read_tool_use(data)

    def _read_tool_use(self, data: dict) -> dict:
        blocks = data.get("content") or []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                payload = block.get("input")
                if isinstance(payload, dict):
                    return payload
                if isinstance(payload, str):
                    return extract_json_object(payload, provider=self.name)
        if data.get("stop_reason") == "max_tokens":
            raise ProviderError(f"{self.name}: output truncated (max_tokens)")
        # No tool call: fall back to any text the model produced instead.
        texts = [
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        return extract_json_object("".join(texts), provider=self.name)


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

PROVIDERS: dict[str, type[Provider]] = {
    "gemini": GeminiProvider,
    "google": GeminiProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "claude": AnthropicProvider,
    "groq": GroqProvider,
}

#: Substrings that make a configured model name plausible for a provider. Guards
#: the very common mishap of switching provider in Settings and leaving a
#: "gemini-*" name in the model box, which would 404 on every call.
_MODEL_HINTS: dict[str, tuple[str, ...]] = {
    "gemini": ("gemini", "gemma", "learnlm"),
    "openai": ("gpt", "o1", "o3", "o4", "chatgpt"),
    "anthropic": ("claude",),
    "groq": (
        "llama", "mixtral", "gemma", "qwen", "kimi", "deepseek",
        "gpt-oss", "allam", "compound",
    ),
}


def _canonical(provider: str) -> str:
    key = (provider or "").strip().lower()
    return {"google": "gemini", "claude": "anthropic"}.get(key, key)


def _resolve_key(settings: Any, provider: str) -> str:
    """Ask config for the key, tolerating a missing/partial settings object."""
    try:
        from app.config import resolve_api_key
    except ImportError:      # standalone use / early bootstrap
        pass
    else:
        try:
            key = resolve_api_key(settings)
        except (AttributeError, TypeError, OSError) as exc:
            log.warning("resolve_api_key failed (%s); using settings.api_key", exc)
        else:
            if key:
                return str(key)
    return str(getattr(settings, "api_key", "") or "")


def make_provider(settings: Any) -> Provider | None:
    """Build the configured provider, or None when AI cannot be used.

    None -- rather than an exception -- because "no key" is a supported mode:
    the app runs the rule-based parser and shows an amber banner.
    """
    name = _canonical(getattr(settings, "provider", "gemini"))
    if not name or name == "none":
        log.info("AI disabled (provider=%r)", name or "none")
        return None

    cls = PROVIDERS.get(name)
    if cls is None:
        log.warning("unknown provider %r; AI disabled", name)
        return None

    key = _resolve_key(settings, name).strip()
    if not key:
        log.info("no API key for provider %r; AI disabled", name)
        return None

    model = str(getattr(settings, "model", "") or "").strip()
    hints = _MODEL_HINTS.get(name, ())
    if model and hints and not any(h in model.lower() for h in hints):
        log.warning(
            "model %r does not look like a %s model; using the provider default",
            model, name,
        )
        model = ""

    provider = cls(api_key=key, model=model) if model else cls(api_key=key)
    log.info("using %s provider with model %s", provider.name, provider.model)
    return provider

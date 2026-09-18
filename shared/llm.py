"""Provider-agnostic LLM abstraction shared by every agent that needs a
model call (Researcher's semantic summary, Reviewer's narrative).

No caller outside this module knows which provider is in use — provider
selection happens entirely through environment variables
(``LLM_PROVIDER``, ``*_API_KEY``, ``OLLAMA_BASE_URL``, ``MODEL_SELECTIONS``)
and every provider implementation speaks the same :class:`LLMClient`
protocol and raises the same :class:`LLMError` on failure. Callers never
branch on provider identity.

The LLM only ever produces text (summaries, narratives). It never decides
risk scores — see ``agents/reviewer/scoring.py`` for the deterministic,
model-free scoring logic this is deliberately kept separate from.

Usage::

    from shared.llm import get_llm_client, LLMError

    client = get_llm_client(purpose="summary")
    try:
        response = await client.complete(
            system="You summarize commits in 2-3 sentences.",
            prompt="...",
            max_tokens=300,
        )
    except LLMError:
        response_text = ""  # degrade gracefully, never block the pipeline
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx

from shared.logging import configure_logging

__all__ = [
    "LLMClient",
    "LLMResponse",
    "LLMError",
    "AnthropicClient",
    "OpenAIClient",
    "OllamaClient",
    "NullLLMClient",
    "get_llm_client",
    "get_model_for",
    "DEFAULT_TIMEOUT_SECONDS",
]

log = configure_logging(service_name="llm")

DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))

_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.1",
}


@dataclass(frozen=True)
class LLMResponse:
    """Standardized response shape, identical across every provider."""

    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    finish_reason: str | None = None


class LLMError(RuntimeError):
    """Raised on any provider failure (timeout, HTTP error, malformed
    response, missing configuration). Callers catch this one type
    regardless of which provider is configured."""


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic completion interface. Every implementation in
    this module satisfies this protocol; nothing outside this module
    should implement it directly."""

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> LLMResponse: ...


def get_model_for(purpose: str, provider: str) -> str:
    """Resolve the model to use for ``purpose`` (e.g. ``"summary"``,
    ``"narrative"``) on ``provider``.

    Reads ``MODEL_SELECTIONS``, a comma-separated ``purpose=model`` list
    (e.g. ``MODEL_SELECTIONS=summary=claude-haiku-4-5-20251001,narrative=claude-sonnet-5``),
    falling back to that provider's built-in default when ``purpose``
    isn't listed.
    """
    raw = os.environ.get("MODEL_SELECTIONS", "")
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        if key.strip() == purpose and value.strip():
            return value.strip()
    return _DEFAULT_MODELS.get(provider, _DEFAULT_MODELS["anthropic"])


class _BaseLLMClient:
    """Shared timeout/retry-hook/logging plumbing for every real provider.

    Retry hooks are deliberately a no-op stub in Phase 3 — actual
    backoff/retry looping is deferred to Phase 4 — but the constructor
    surface exists now so Phase 4 doesn't need to touch call sites.
    """

    provider: str = "base"

    def __init__(
        self,
        *,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 0,
        retry_backoff_seconds: float = 0.0,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds

    async def _retrying(self, fn):
        """Phase 4 hook point: currently calls ``fn`` once. A future phase
        can loop up to ``self.max_retries`` times with
        ``self.retry_backoff_seconds`` between attempts here, without
        changing any caller."""
        return await fn()

    def _log_usage(self, response: LLMResponse) -> None:
        log.info(
            "llm completion",
            extra={
                "provider": response.provider,
                "model": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "latency_ms": response.latency_ms,
            },
        )


class AnthropicClient(_BaseLLMClient):
    """Calls the Anthropic Messages API directly over HTTPS (no SDK
    dependency, to keep provider wiring contained to this module)."""

    provider = "anthropic"

    def __init__(self, *, api_key: str, model: str, **kwargs) -> None:
        super().__init__(model=model, **kwargs)
        self.api_key = api_key

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, temperature: float = 0.2
    ) -> LLMResponse:
        async def _call() -> LLMResponse:
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": self.api_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                            "system": system,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPError as exc:
                raise LLMError(f"anthropic request failed: {exc}") from exc

            latency_ms = (time.monotonic() - started) * 1000
            try:
                text = "".join(
                    block.get("text", "") for block in data["content"] if block.get("type") == "text"
                )
                usage = data.get("usage", {})
                response = LLMResponse(
                    text=text,
                    model=data.get("model", self.model),
                    provider=self.provider,
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    latency_ms=latency_ms,
                    finish_reason=data.get("stop_reason"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise LLMError(f"anthropic response malformed: {exc}") from exc

            self._log_usage(response)
            return response

        return await self._retrying(_call)


class OpenAIClient(_BaseLLMClient):
    """Calls the OpenAI Chat Completions API directly over HTTPS."""

    provider = "openai"

    def __init__(self, *, api_key: str, model: str, **kwargs) -> None:
        super().__init__(model=model, **kwargs)
        self.api_key = api_key

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, temperature: float = 0.2
    ) -> LLMResponse:
        async def _call() -> LLMResponse:
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "content-type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": prompt},
                            ],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPError as exc:
                raise LLMError(f"openai request failed: {exc}") from exc

            latency_ms = (time.monotonic() - started) * 1000
            try:
                choice = data["choices"][0]
                usage = data.get("usage", {})
                response = LLMResponse(
                    text=choice["message"]["content"] or "",
                    model=data.get("model", self.model),
                    provider=self.provider,
                    input_tokens=int(usage.get("prompt_tokens", 0)),
                    output_tokens=int(usage.get("completion_tokens", 0)),
                    latency_ms=latency_ms,
                    finish_reason=choice.get("finish_reason"),
                )
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                raise LLMError(f"openai response malformed: {exc}") from exc

            self._log_usage(response)
            return response

        return await self._retrying(_call)


class OllamaClient(_BaseLLMClient):
    """Calls a local/self-hosted Ollama server's chat API. No API key."""

    provider = "ollama"

    def __init__(self, *, base_url: str, model: str, **kwargs) -> None:
        super().__init__(model=model, **kwargs)
        self.base_url = base_url.rstrip("/")

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, temperature: float = 0.2
    ) -> LLMResponse:
        async def _call() -> LLMResponse:
            started = time.monotonic()
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(
                        f"{self.base_url}/api/chat",
                        json={
                            "model": self.model,
                            "stream": False,
                            "options": {"temperature": temperature, "num_predict": max_tokens},
                            "messages": [
                                {"role": "system", "content": system},
                                {"role": "user", "content": prompt},
                            ],
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
            except httpx.HTTPError as exc:
                raise LLMError(f"ollama request failed: {exc}") from exc

            latency_ms = (time.monotonic() - started) * 1000
            try:
                response = LLMResponse(
                    text=data["message"]["content"] or "",
                    model=data.get("model", self.model),
                    provider=self.provider,
                    input_tokens=int(data.get("prompt_eval_count", 0)),
                    output_tokens=int(data.get("eval_count", 0)),
                    latency_ms=latency_ms,
                    finish_reason="stop" if data.get("done") else None,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise LLMError(f"ollama response malformed: {exc}") from exc

            self._log_usage(response)
            return response

        return await self._retrying(_call)


class NullLLMClient:
    """Used when no provider is configured (``LLM_PROVIDER`` unset/``none``,
    or a required API key is missing). Always raises :class:`LLMError` so
    callers exercise the same degrade-gracefully path they'd need for a
    real provider outage, rather than silently no-op'ing."""

    provider = "none"

    def __init__(self, *, reason: str) -> None:
        self.reason = reason

    async def complete(
        self, *, system: str, prompt: str, max_tokens: int, temperature: float = 0.2
    ) -> LLMResponse:
        raise LLMError(f"no LLM provider configured: {self.reason}")


def get_llm_client(purpose: str = "default") -> LLMClient:
    """Build the configured provider's client for a given ``purpose``
    (e.g. ``"summary"`` for Researcher's cheap/small model, ``"narrative"``
    for Reviewer's explanation model).

    Reads ``LLM_PROVIDER`` (``anthropic`` | ``openai`` | ``ollama`` |
    ``none``); returns a :class:`NullLLMClient` for ``none``, an unset
    value, or a configured provider missing its required credential —
    every case behaves identically to callers (an :class:`LLMError` on
    ``complete()``).
    """
    provider = os.environ.get("LLM_PROVIDER", "none").strip().lower()

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return NullLLMClient(reason="ANTHROPIC_API_KEY not set")
        return AnthropicClient(api_key=api_key, model=get_model_for(purpose, provider))

    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return NullLLMClient(reason="OPENAI_API_KEY not set")
        return OpenAIClient(api_key=api_key, model=get_model_for(purpose, provider))

    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL", "")
        if not base_url:
            return NullLLMClient(reason="OLLAMA_BASE_URL not set")
        return OllamaClient(base_url=base_url, model=get_model_for(purpose, provider))

    return NullLLMClient(reason=f"LLM_PROVIDER={provider!r}")

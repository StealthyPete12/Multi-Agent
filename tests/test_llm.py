import pytest

from shared.llm import (
    AnthropicClient,
    LLMError,
    NullLLMClient,
    OllamaClient,
    OpenAIClient,
    get_llm_client,
    get_model_for,
)


def test_get_llm_client_defaults_to_null_when_unset(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    client = get_llm_client()
    assert isinstance(client, NullLLMClient)


def test_get_llm_client_none_provider_returns_null(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "none")
    client = get_llm_client()
    assert isinstance(client, NullLLMClient)


def test_get_llm_client_anthropic_without_key_returns_null(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = get_llm_client()
    assert isinstance(client, NullLLMClient)


def test_get_llm_client_anthropic_with_key_selects_anthropic(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    client = get_llm_client(purpose="summary")
    assert isinstance(client, AnthropicClient)
    assert client.api_key == "sk-test"


def test_get_llm_client_openai_without_key_returns_null(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = get_llm_client()
    assert isinstance(client, NullLLMClient)


def test_get_llm_client_openai_with_key_selects_openai(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = get_llm_client()
    assert isinstance(client, OpenAIClient)


def test_get_llm_client_ollama_without_base_url_returns_null(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    client = get_llm_client()
    assert isinstance(client, NullLLMClient)


def test_get_llm_client_ollama_with_base_url_selects_ollama(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    client = get_llm_client()
    assert isinstance(client, OllamaClient)


def test_get_model_for_uses_model_selections_env(monkeypatch):
    monkeypatch.setenv("MODEL_SELECTIONS", "summary=tiny-model,narrative=big-model")
    assert get_model_for("summary", "anthropic") == "tiny-model"
    assert get_model_for("narrative", "anthropic") == "big-model"


def test_get_model_for_falls_back_to_provider_default(monkeypatch):
    monkeypatch.delenv("MODEL_SELECTIONS", raising=False)
    assert get_model_for("summary", "anthropic") != ""
    assert get_model_for("unknown-purpose", "openai") != ""


async def test_null_llm_client_raises_llm_error():
    client = NullLLMClient(reason="test")
    with pytest.raises(LLMError):
        await client.complete(system="s", prompt="p", max_tokens=10)


async def test_anthropic_client_success(httpx_mock):
    httpx_mock.add_response(
        url="https://api.anthropic.com/v1/messages",
        json={
            "model": "claude-haiku-4-5-20251001",
            "content": [{"type": "text", "text": "a short summary."}],
            "usage": {"input_tokens": 12, "output_tokens": 8},
            "stop_reason": "end_turn",
        },
    )
    client = AnthropicClient(api_key="sk-test", model="claude-haiku-4-5-20251001", rate_limit_enabled=False)
    response = await client.complete(system="sys", prompt="hi", max_tokens=50)
    assert response.text == "a short summary."
    assert response.provider == "anthropic"
    assert response.input_tokens == 12
    assert response.output_tokens == 8


async def test_anthropic_client_http_error_raises_llm_error(httpx_mock):
    # 500 is retryable (see tests/test_llm_retry.py for dedicated retry
    # coverage); pin max_retries=0 here so this test asserts only the
    # single-attempt-exhausted -> LLMError behavior, matching one mocked
    # response.
    httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", status_code=500)
    client = AnthropicClient(api_key="sk-test", model="m", max_retries=0, rate_limit_enabled=False)
    with pytest.raises(LLMError):
        await client.complete(system="sys", prompt="hi", max_tokens=50)


async def test_anthropic_client_malformed_response_raises_llm_error(httpx_mock):
    httpx_mock.add_response(url="https://api.anthropic.com/v1/messages", json={"nope": True})
    client = AnthropicClient(api_key="sk-test", model="m", rate_limit_enabled=False)
    with pytest.raises(LLMError):
        await client.complete(system="sys", prompt="hi", max_tokens=50)


async def test_openai_client_success(httpx_mock):
    httpx_mock.add_response(
        url="https://api.openai.com/v1/chat/completions",
        json={
            "model": "gpt-4o-mini",
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
    )
    client = OpenAIClient(api_key="sk-test", model="gpt-4o-mini", rate_limit_enabled=False)
    response = await client.complete(system="sys", prompt="hi", max_tokens=50)
    assert response.text == "hello"
    assert response.provider == "openai"
    assert response.input_tokens == 5
    assert response.output_tokens == 3


async def test_ollama_client_success(httpx_mock):
    httpx_mock.add_response(
        url="http://localhost:11434/api/chat",
        json={
            "model": "llama3.1",
            "message": {"content": "a summary"},
            "prompt_eval_count": 20,
            "eval_count": 10,
            "done": True,
        },
    )
    client = OllamaClient(base_url="http://localhost:11434", model="llama3.1", rate_limit_enabled=False)
    response = await client.complete(system="sys", prompt="hi", max_tokens=50)
    assert response.text == "a summary"
    assert response.provider == "ollama"
    assert response.finish_reason == "stop"

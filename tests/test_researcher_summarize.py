from agents.researcher.summarize import generate_semantic_summary
from shared.llm import LLMError, LLMResponse


class FakeLLMClient:
    def __init__(self, *, text: str | None = None, raises: bool = False) -> None:
        self.text = text
        self.raises = raises
        self.calls: list[dict] = []

    async def complete(self, *, system, prompt, max_tokens, temperature=0.2):
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.raises:
            raise LLMError("boom")
        return LLMResponse(
            text=self.text or "",
            model="fake-model",
            provider="fake",
            input_tokens=10,
            output_tokens=5,
            latency_ms=1.0,
        )


async def test_generate_semantic_summary_returns_llm_text():
    client = FakeLLMClient(text="  Refactors the auth module.  ")

    summary = await generate_semantic_summary(
        client,
        commit_message="refactor: auth",
        changed_files=["auth/login.py"],
        diff_excerpt="-old\n+new",
    )

    assert summary == "Refactors the auth module."
    assert len(client.calls) == 1
    assert client.calls[0]["max_tokens"] == 300
    assert "refactor: auth" in client.calls[0]["prompt"]
    assert "auth/login.py" in client.calls[0]["prompt"]
    assert "-old" in client.calls[0]["prompt"]


async def test_generate_semantic_summary_returns_empty_on_llm_error():
    client = FakeLLMClient(raises=True)

    summary = await generate_semantic_summary(
        client,
        commit_message="fix: bug",
        changed_files=["a.py"],
        diff_excerpt="",
    )

    assert summary == ""


async def test_generate_semantic_summary_handles_empty_changed_files_and_diff():
    client = FakeLLMClient(text="No files listed.")

    summary = await generate_semantic_summary(
        client, commit_message="chore: noop", changed_files=[], diff_excerpt=""
    )

    assert summary == "No files listed."
    prompt = client.calls[0]["prompt"]
    assert "(none listed)" in prompt
    assert "(no diff available)" in prompt

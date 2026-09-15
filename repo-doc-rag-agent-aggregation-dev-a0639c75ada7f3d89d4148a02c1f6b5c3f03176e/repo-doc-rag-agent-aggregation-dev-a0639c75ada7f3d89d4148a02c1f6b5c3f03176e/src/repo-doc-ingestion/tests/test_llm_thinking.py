from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


class _FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def test_doubao_summarizer_disables_thinking(monkeypatch) -> None:
    from app.parser.conversion_core import summarizer as module

    completions = _FakeCompletions('{"summary":"简要摘要"}')
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setattr(module, "get_async_ark_client", lambda: client)
    summarizer = module.DoubaoSummarizer(
        summary_input_chars=1000,
        summary_max_chars=100,
        model="test-model",
    )

    result = asyncio.run(summarizer.summarize("待总结正文"))

    assert result == "简要摘要"
    assert completions.calls[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in completions.calls[0]


def test_ark_vision_ocr_disables_thinking(tmp_path, monkeypatch) -> None:
    import fitz
    import volcenginesdkarkruntime

    from app.parser.conversion_core.parser import DocumentParser

    pdf_path = tmp_path / "one-page.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "hello")
    document.save(pdf_path)
    document.close()

    calls: list[dict] = []

    class FakeArk:
        def __init__(self, **_kwargs) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )

        def _create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content='{"text":"hello"}'))
                ]
            )

    monkeypatch.setattr(volcenginesdkarkruntime, "Ark", FakeArk)
    monkeypatch.setenv("ARK_API_KEY", "test-key")
    parser = DocumentParser.__new__(DocumentParser)
    parser.pdf_vision_model = "test-vision-model"
    parser.pdf_vision_concurrency = 1
    parser._pdf_vision_rate_limiter = None

    pages = parser._extract_pdf_pages_with_ark_vision(str(pdf_path))

    assert pages == [(1, "hello")]
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in calls[0]

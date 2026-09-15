from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).parents[2] / "scripts" / "run_compound_retrieval_acceptance.py"
    spec = importlib.util.spec_from_file_location("compound_acceptance", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_compound_suite_covers_all_categories_without_history_only_coreference() -> None:
    module = _load_script()
    assert 10 <= len(module.CASES) <= 20
    categories = {item.category for case in module.CASES for item in case.expected}
    assert categories == {"scope_direct", "routed_direct", "routed_focused", "routed_broad"}
    assert all(
        "这篇文档" not in case.query and "这个文件" not in case.query for case in module.CASES
    )


def test_response_evaluation_binds_expected_question_to_group_and_document() -> None:
    module = _load_script()
    case = module.CompoundCase(
        name="sample",
        query="问题",
        expected=(
            module.ExpectedQuestion(("营业额",), "routed_focused", module.HUANGSHAN, ("123",)),
        ),
    )
    body = {
        "debug": {
            "groups": [
                {
                    "group_ref": "g001",
                    "category": "routed_focused",
                    "queries": ["A 公司营业额是多少"],
                }
            ],
            "classification_trace": [{"phase": "normalized_queries"}],
        },
        "chunks": [
            {
                "document_id": module.HUANGSHAN,
                "document_ids": [module.HUANGSHAN],
                "content": "营业额是 123 元",
            }
        ],
        "coverage": {"covered_group_refs": ["g001"]},
        "usage": {"returned_count": 1, "llm_request_count": 4},
        "warnings": [],
    }

    result = module.evaluate_response("robust", case, body, 10)

    assert (result.retained, result.category_correct, result.group_covered) == (1, 1, 1)
    assert result.routed_document_hit == 1
    assert result.focused_content_hit == 1
    assert result.robust_phase_count == 1


def test_missing_local_service_is_started_with_requested_strategy_and_stopped() -> None:
    module = _load_script()
    probes = iter([False, True])
    process_calls = []

    class Process:
        def __init__(self) -> None:
            self.terminated = False

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None) -> None:
            del timeout

    process = Process()

    def process_factory(command, **kwargs):
        process_calls.append((command, kwargs))
        return process

    managed = module.ensure_retrieval_service(
        strategy="robust",
        base_url="http://127.0.0.1:8420",
        startup_timeout=1,
        ready_probe=lambda *_: next(probes),
        process_factory=process_factory,
        sleeper=lambda _: None,
    )
    try:
        assert managed.owned is True
        assert process_calls[0][1]["env"]["APP_PORT"] == "8420"
        assert process_calls[0][1]["env"]["RAG_QUERY_CLASSIFICATION_STRATEGY"] == "robust"
        assert process_calls[0][1]["env"]["RAG_DEBUG_ENABLED"] == "true"
    finally:
        managed.close()

    assert process.terminated is True


def test_unreachable_remote_service_fails_without_trying_to_start_process() -> None:
    module = _load_script()

    with pytest.raises(RuntimeError, match="only auto-starts loopback"):
        module.ensure_retrieval_service(
            strategy="fast",
            base_url="http://example.com:8320",
            startup_timeout=1,
            ready_probe=lambda *_: False,
            process_factory=lambda *_args, **_kwargs: pytest.fail("must not start"),
            sleeper=lambda _: None,
        )

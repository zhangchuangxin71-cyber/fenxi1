from app.adapter.history import compact_history


def test_history_discards_artifact_bodies_and_tool_results() -> None:
    huge_html = "<html>" + ("x" * 30_000) + "</html>"
    result = compact_history(
        [
            {"role": "assistant", "content": huge_html},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "大纲增加风险小节"},
                    {"type": "function_call_output", "output": "large"},
                ],
            },
        ]
    )
    assert result == [
        {"role": "assistant", "content": "[历史产物正文已省略]"},
        {"role": "user", "content": "大纲增加风险小节"},
    ]

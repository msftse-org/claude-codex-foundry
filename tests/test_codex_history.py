import pytest

from claude_foundry.codex_history import export, visible_transcript


def test_visible_handoff_omits_hidden_and_tool_state():
    turns = [
        {
            "items": [
                {
                    "type": "userMessage",
                    "content": [
                        {"type": "text", "text": "Original question"},
                        {"type": "image", "url": "private-image"},
                    ],
                },
                {"type": "agentMessage", "text": "Visible answer"},
                {"type": "reasoning", "encrypted_content": "hidden-secret"},
                {"type": "commandExecution", "aggregatedOutput": "tool-secret"},
            ]
        }
    ]
    text, count, omitted = visible_transcript(turns)
    assert count == 2 and omitted == 3
    assert "Original question" in text and "Visible answer" in text
    assert all(secret not in text for secret in ("hidden-secret", "tool-secret", "private-image"))


def test_empty_history_and_overwrite_are_refused(tmp_path):
    with pytest.raises(ValueError, match="no exportable"):
        visible_transcript([])
    p = tmp_path / "reviewed.md"
    p.write_text("keep me")
    with pytest.raises(ValueError, match="already exists"):
        export("00000000-0000-0000-0000-000000000001", p)
    assert p.read_text() == "keep me"

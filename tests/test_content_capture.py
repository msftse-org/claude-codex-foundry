import json
from datetime import UTC, datetime
from uuid import uuid4

from claude_foundry.portal.content_capture import ContentCapture


class Blobs:
    def __init__(self):
        self.values = {}

    def upload_blob(self, name, value, **kwargs):
        self.values[name] = value


def test_capture_preserves_prompt_and_streamed_response_with_bounds():
    capture = object.__new__(ContentCapture)
    capture.container = Blobs()
    capture.max_bytes = 32
    request_id, principal, group = str(uuid4()), str(uuid4()), str(uuid4())
    name, record = capture.begin(
        request_id,
        "openai",
        principal,
        group,
        "gpt-test",
        {"input": "hello"},
    )
    capture.finish(name, record, b"response " * 10, 200, True)
    saved = json.loads(capture.container.values[name])
    assert saved["api_id"] == "codex-governed"
    assert saved["request"]["content"] == '{"input":"hello"}'
    assert saved["response"]["truncated"] is True
    assert saved["response"]["captured_bytes"] == 32
    assert saved["complete"] is True
    assert name.startswith(f"{datetime.now(UTC):%Y/%m/%d}/z-")
    assert name.endswith(f"-{request_id}.json")

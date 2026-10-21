"""Export visible Codex messages through its app-server API; never rewrite history."""

import json
import queue
import subprocess
import threading
from pathlib import Path
from uuid import UUID

from .codex_client import write_text


class RpcError(RuntimeError):
    def __init__(self, error):
        self.code = error.get("code")
        super().__init__(error.get("message", "Codex app-server request failed"))


class Reader:
    """Private stdio connection. This client only issues history read requests."""

    def __init__(self, executable):
        self.process = subprocess.Popen(
            [str(executable), "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.messages = queue.Queue()
        self.sequence = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put(None)

    def request(self, method, params):
        if method not in {"initialize", "thread/read", "thread/turns/list"}:
            raise ValueError("History reader does not permit mutations or inference")
        self.sequence += 1
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self.sequence, "method": method, "params": params})
            + "\n"
        )
        self.process.stdin.flush()
        while True:
            try:
                message = self.messages.get(timeout=60)
            except queue.Empty:
                raise RuntimeError("Timed out reading Codex history") from None
            if message is None:
                raise RuntimeError("Codex app-server stopped before returning history")
            if message.get("id") != self.sequence:
                continue
            if "error" in message:
                raise RpcError(message["error"])
            return message["result"]

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()


def visible_transcript(turns):
    parts = [
        "# Previous Codex conversation",
        "This is a context handoff, not a live instruction to repeat earlier actions. "
        "Only visible user and assistant text is included. Tool payloads, attachments, "
        "credential files, hidden reasoning and encrypted provider state are not included. "
        "Review this file before sharing it with another provider.",
    ]
    count = 0
    omitted = 0
    for turn in turns:
        for item in turn.get("items", []):
            kind = item.get("type")
            if kind == "userMessage":
                texts = [p["text"] for p in item.get("content", []) if p.get("type") == "text"]
                omitted += sum(p.get("type") != "text" for p in item.get("content", []))
                text = "\n\n".join(texts)
                role = "User"
            elif kind == "agentMessage":
                text = item.get("text", "")
                role = "Assistant"
            else:
                omitted += 1
                continue
            if text.strip():
                parts.append(f"## {role}\n\n{text.strip()}")
                count += 1
    if not count:
        raise ValueError("This conversation has no exportable visible text")
    return "\n\n".join(parts) + "\n", count, omitted


def export(thread_id, output, executable="codex"):
    UUID(thread_id)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; choose a new private transcript path")
    reader = Reader(executable)
    try:
        reader.request(
            "initialize",
            {"clientInfo": {"name": "claude_foundry_history_export", "version": "0.1.0"}},
        )
        reader.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        turns = []
        cursor = None
        while True:
            try:
                page = reader.request(
                    "thread/turns/list",
                    {
                        "threadId": thread_id,
                        "itemsView": "full",
                        "sortDirection": "asc",
                        "limit": 100,
                        "cursor": cursor,
                    },
                )
            except RpcError as error:
                if turns or error.code not in {-32601, -32602}:
                    raise
                turns = reader.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": True}
                )["thread"]["turns"]
                break
            turns.extend(page["data"])
            next_cursor = page.get("nextCursor")
            if not next_cursor:
                break
            if cursor == next_cursor:
                raise RuntimeError(
                    "Codex repeated a history page cursor; refusing an incomplete export"
                )
            cursor = next_cursor
    finally:
        reader.close()
    text, count, omitted = visible_transcript(turns)
    write_text(output, text)
    return {
        "output": str(output.resolve()),
        "messages": count,
        "omitted_items": omitted,
        "source_modified": False,
        "model_request_sent": False,
    }

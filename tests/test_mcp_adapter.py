"""Gate for the MCP stdio adapter: spawns it as a subprocess, speaks MCP over stdio.
Needs a live Isaac (like test_server.py) and the mcp/.venv install."""

import json
import os
import subprocess
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER = os.path.join(REPO, "mcp", ".venv", "bin", "isaac-agent-mcp")


class McpClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            [ADAPTER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO,
        )
        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self.stdin, self.stdout, self.stderr = self.proc.stdin, self.proc.stdout, self.proc.stderr
        self.next_id = 0
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"},
            },
        )
        self.notify("notifications/initialized", {})

    def request(self, method, params, timeout=120):
        self.next_id += 1
        self.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}) + "\n"
        )
        self.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.stdout.readline()
            if not line:
                raise RuntimeError(f"adapter died: {self.stderr.read()[:2000]}")
            msg = json.loads(line)
            if msg.get("id") == self.next_id:
                assert "error" not in msg, msg
                return msg["result"]
        raise TimeoutError(method)

    def notify(self, method, params):
        self.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
        self.stdin.flush()

    def call_tool(self, name, arguments, timeout=120):
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout)

    def close(self):
        self.proc.terminate()
        self.proc.wait(timeout=5)


@pytest.fixture(scope="module")
def client():
    c = McpClient()
    yield c
    c.close()


def test_tools_list_has_docs(client):
    tools = client.request("tools/list", {})["tools"]
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == {"isaac_exec", "isaac_events"}
    assert "agent.viewport" in by_name["isaac_exec"]["description"]  # live helperDocs embedded


def test_exec_roundtrip(client):
    r = client.call_tool("isaac_exec", {"code": "print('mcp'); 21*2"})
    text = r["content"][0]["text"]
    assert "mcp" in text and "result: 42" in text
    assert not r.get("isError")


def test_exec_error(client):
    r = client.call_tool("isaac_exec", {"code": "agent.attach(b'partial', 'text/plain')\n1/0"})
    assert "ZeroDivisionError" in r["content"][0]["text"]
    assert "saved to" in r["content"][0]["text"]
    assert r["isError"]


def test_exec_path(client, tmp_path):
    script = tmp_path / "experiment.py"
    script.write_text("pt_mcp_file = 21\nawait asyncio.sleep(0)\npt_mcp_file * 2", encoding="utf-8")
    r = client.call_tool("isaac_exec", {"path": os.path.relpath(script, REPO)})
    assert "result: 42" in r["content"][0]["text"]
    assert not r.get("isError")
    r = client.call_tool("isaac_exec", {"code": "pt_mcp_file"})
    assert "result: 21" in r["content"][0]["text"]
    for arguments in ({}, {"code": "1", "path": str(script)}, {"path": str(tmp_path / "missing.py")}):
        assert client.call_tool("isaac_exec", arguments)["isError"]


def test_large_output_saved(client):
    r = client.call_tool("isaac_exec", {"code": "print('中文🚀' * 20000)"})
    text = r["content"][0]["text"]
    assert len(text.encode("utf-8")) < 52_000
    path = text.split("Full output saved to ", 1)[1].split("]", 1)[0]
    try:
        with open(path, encoding="utf-8") as f:
            assert f.read().startswith("中文🚀" * 20000)
    finally:
        os.unlink(path)


def test_exec_image(client):
    r = client.call_tool("isaac_exec", {"code": "agent.image(await agent.viewport(width=128))"})
    images = [c for c in r["content"] if c["type"] == "image"]
    assert images and images[0]["mimeType"] == "image/png"
    import base64

    assert base64.b64decode(images[0]["data"])[:8] == b"\x89PNG\r\n\x1a\n"


def test_exec_timeout_cancels(client):
    t0 = time.monotonic()
    r = client.call_tool("isaac_exec", {"code": "await asyncio.sleep(60)", "timeout_s": 2})
    assert time.monotonic() - t0 < 15
    assert "CancelledError" in r["content"][0]["text"]


def test_events_buffered_and_drained(client):
    client.call_tool("isaac_events", {"flush": True})
    client.call_tool("isaac_exec", {"code": "for i in range(5): agent.emit(f'mcp.page.{i}', i)"})
    assert client.call_tool("isaac_events", {"max": 0})["isError"]  # Must not consume anything.
    first = client.call_tool("isaac_events", {"max": 2})["content"][0]["text"]
    assert "mcp.page.0" in first and "mcp.page.1" in first and "mcp.page.2" not in first
    assert "3 notifications remain buffered" in first
    next_page = client.call_tool("isaac_events", {"max": 1, "flush": False})["content"][0]["text"]
    assert "mcp.page.2" in next_page and "2 notifications remain buffered" in next_page
    flushed = client.call_tool("isaac_events", {"max": 1, "flush": True})["content"][0]["text"]
    assert "mcp.page.3" in flushed and "mcp.page.4" not in flushed
    assert "1 remaining notifications flushed" in flushed
    assert "no buffered notifications" in client.call_tool("isaac_events", {})["content"][0]["text"]


def test_nonimage_media_to_file(client):
    r = client.call_tool("isaac_exec", {"code": "agent.attach(b'hello-mcp', 'text/plain', name='note')"})
    text = r["content"][0]["text"]
    assert "saved to" in text
    path = text.split("saved to ", 1)[1].strip().split()[0]
    with open(path) as f:
        assert f.read() == "hello-mcp"

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import socket
import sys

import pytest

from backend.mcp_service import MCPManager, validate_server


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [True, False])
async def test_mcp_business_result_reaches_worker_runtime(tmp_path, monkeypatch, is_error):
    from unittest.mock import AsyncMock
    from knowe_core.tool_registry import ToolRegistry
    from backend import mcp_tools
    from backend.runtime import RuntimeConfig, ToolCall, WorkerRuntime

    response = {"isError": is_error, "content": [{"type": "text", "text": "remote result"}]}
    manager = type("Manager", (), {"request": AsyncMock(return_value=response)})()
    monkeypatch.setattr(mcp_tools, "get_manager", lambda: manager)
    registry = ToolRegistry()
    mcp_tools.register_mcp_tools(registry, lambda reg, **kw: reg.register(**kw))
    runtime = WorkerRuntime(model=None, registry=registry, workspace_root=tmp_path,
                            config=RuntimeConfig(enforce_fixed_tools=False))
    result = await runtime._execute_tool(ToolCall("call", "call_mcp_tool", {
        "server_id": "server", "tool_name": "remote", "arguments": {},
    }), None, None)
    assert result.ok is not is_error
    assert result.facts["result"] == response
    assert result.error_code == ("mcp_tool_error" if is_error else "")
    if is_error:
        assert result.error and "failed" in result.summary


@pytest.mark.asyncio
async def test_stdio_discovery_calls_reconnect_and_disable(tmp_path):
    script = tmp_path / "server.py"
    script.write_text('''from mcp.server.fastmcp import FastMCP
mcp = FastMCP("test")
counter = 0
@mcp.tool()
def increment(value: int) -> dict:
    global counter
    counter += value
    return {"count": counter}
mcp.run(transport="stdio")
''')
    manager = MCPManager(tmp_path / "config")
    saved = await manager.save({"name": "Local test", "transport": "stdio", "command": sys.executable,
                                "args": [str(script)], "env": {"TEST_SECRET": "do-not-echo"}})
    server_id = saved["server"]["id"]
    try:
        assert "do-not-echo" not in json.dumps(manager.listing())
        assert (tmp_path / "config/mcp.json").stat().st_mode & 0o777 == 0o600
        tools = await manager.request(server_id, "list")
        assert tools[0]["name"] == "increment"
        result = await manager.request(server_id, "call", ("increment", {"value": 2}))
        assert json.loads(result["content"][0]["text"])["count"] == 2
        result = await manager.request(server_id, "call", ("increment", {"value": 3}))
        assert json.loads(result["content"][0]["text"])["count"] == 5  # Same live session/process.
        with pytest.raises(ValueError, match="schema"):
            await manager.request(server_id, "call", ("increment", {"value": "not an integer"}))
        with pytest.raises(ValueError, match="未提供"):
            await manager.request(server_id, "call", ("invented", {}))
        await manager.disconnect(server_id)
        result = await manager.request(server_id, "call", ("increment", {"value": 1}))
        assert json.loads(result["content"][0]["text"])["count"] == 1
        await manager.save({"id": server_id, "allowed_tools": []})
        assert await manager.request(server_id, "list") == []
        assert len(await manager.request(server_id, "list", testing=True)) == 1
        with pytest.raises(ValueError, match="未获准"):
            await manager.request(server_id, "call", ("increment", {"value": 1}))
        await manager.save({"id": server_id, "name": "Local test", "enabled": False})
        assert manager.listing()["servers"][0]["status"] == "disabled"
        assert manager.read()[0]["env"]["TEST_SECRET"] == "do-not-echo"
        with pytest.raises(ValueError, match="停用"):
            await manager.request(server_id, "list")
        await manager.delete(server_id)
        assert manager.listing()["servers"] == []
    finally:
        await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "sse"])
async def test_remote_protocols_discovery_and_call(tmp_path, transport):
    import uvicorn
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("remote-test", stateless_http=True)
    @mcp.tool()
    def echo(value: str) -> str:
        return "reply: " + value
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = mcp.streamable_http_app() if transport == "http" else mcp.sse_app()
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    manager = MCPManager(tmp_path)
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        assert server.started
        saved = await manager.save({"name": "remote", "transport": transport,
                                    "url": f"http://127.0.0.1:{port}/" + ("mcp" if transport == "http" else "sse")})
        sid = saved["server"]["id"]
        tools = await manager.request(sid, "list")
        assert tools[0]["name"] == "echo"
        result = await manager.request(sid, "call", ("echo", {"value": "你好"}))
        assert result["content"][0]["text"] == "reply: 你好"
    finally:
        await manager.close()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@pytest.mark.asyncio
async def test_import_is_atomic_and_secrets_stay_private(tmp_path):
    manager = MCPManager(tmp_path)
    with pytest.raises(ValueError):
        await manager.import_config({"mcpServers": {"good": {"url": "https://example.com/mcp"}, "bad": {"url": "file:///secret"}}})
    assert manager.read() == []
    await manager.import_config({"mcpServers": {"remote": {
        "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer secret"},
    }, "local": {"command": "npx", "args": ["-y", "example"], "env": {"API_KEY": "secret"}}}})
    assert len(manager.listing()["servers"]) == 2
    assert "secret" not in json.dumps(manager.listing())
    assert manager.listing()["servers"][0]["header_names"] == ["Authorization"]
    with pytest.raises(ValueError, match="已有"):
        await manager.import_config({"mcpServers": {"remote": {"url": "https://example.com/mcp"}}})
    assert len(manager.read()) == 2


@pytest.mark.parametrize("changes", [{"url": "file:///a"}, {"headers": {"Authorization": "a\nb"}}, {"transport": "invalid"}])
def test_invalid_remote_settings(changes):
    with pytest.raises(ValueError):
        validate_server({"name": "remote", "url": "https://example.com/mcp", **changes})

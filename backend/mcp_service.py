"""MCP client connections owned by one task each, using the official Python SDK."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from urllib.parse import urlsplit
import uuid
import weakref

from .config import CONFIG


def _text_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 100 or any(
        not isinstance(k, str) or not isinstance(v, str) or not k or "\n" in k or "\r" in k
        for k, v in value.items()
    ):
        raise ValueError(f"{label} 必须是字符串键值对象")
    return value


def validate_server(raw: dict, previous: dict | None = None) -> dict:
    previous = previous or {}
    name = str(raw.get("name", previous.get("name", ""))).strip()
    if not name or len(name) > 80:
        raise ValueError("请填写不超过 80 字符的连接名称")
    transport = raw.get("transport", previous.get("transport", "http"))
    if transport not in {"http", "sse", "stdio"}:
        raise ValueError("传输方式必须为 http、sse 或 stdio")
    result = {"id": previous.get("id") or uuid.uuid4().hex, "name": name,
              "transport": transport, "enabled": raw.get("enabled", previous.get("enabled", True)) is True,
              "kind": previous.get("kind", "third_party")}
    try:
        result["timeout"] = min(300, max(5, int(raw.get("timeout", previous.get("timeout", 60)))))
    except (TypeError, ValueError) as exc:
        raise ValueError("超时时间必须是秒数") from exc
    if transport == "stdio":
        command = str(raw.get("command", previous.get("command", ""))).strip()
        args = raw.get("args", previous.get("args", []))
        if not command or "\x00" in command or not isinstance(args, list) or len(args) > 100 or any(not isinstance(a, str) or "\x00" in a for a in args):
            raise ValueError("请输入启动命令和字符串数组形式的参数")
        env = _text_map(raw.get("env", previous.get("env", {})), "环境变量")
        cwd = str(raw.get("cwd", previous.get("cwd", ""))).strip()
        if cwd and not Path(cwd).expanduser().is_dir():
            raise ValueError("启动目录不存在于运行 Knowe 的机器上")
        result.update(command=command, args=args, env=env, cwd=cwd)
    else:
        url = str(raw.get("url", previous.get("url", ""))).strip()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("请输入有效的 HTTP(S) MCP 服务地址；凭据请填入请求头")
        headers = _text_map(raw.get("headers", previous.get("headers", {})), "请求头")
        if any("\r" in value or "\n" in value for value in headers.values()):
            raise ValueError("请求头不能包含换行")
        result.update(url=url, headers=headers)
    allowed = raw.get("allowed_tools", previous.get("allowed_tools"))
    if allowed is not None and (not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed)):
        raise ValueError("allowed_tools 必须是工具名数组")
    result["allowed_tools"] = allowed
    return result


def _public(row: dict) -> dict:
    return {**{key: value for key, value in row.items() if key not in {"headers", "env"}},
            "header_names": list(row.get("headers", {})), "env_names": list(row.get("env", {}))}


class Connection:
    def __init__(self, config: dict, log_root: Path):
        self.config = config
        self.log_root = log_root
        self.queue: asyncio.Queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.current: asyncio.Future | None = None
        self.tools: list[dict] = []
        self.task = asyncio.create_task(self.run(), name=f"mcp-{config['id']}")

    def error(self, error: BaseException) -> str:
        # SDK exception groups often wrap the actionable transport exception.
        while isinstance(error, BaseExceptionGroup) and error.exceptions:
            error = error.exceptions[0]
        text = str(error) or type(error).__name__
        for value in [*self.config.get("headers", {}).values(), *self.config.get("env", {}).values()]:
            if value:
                text = text.replace(value, "[redacted]")
        return text[:600]

    async def refresh_tools(self, session) -> list[dict]:
        cursor = None
        tools = []
        seen = set()
        for _ in range(30):
            result = await session.list_tools(cursor=cursor)
            tools.extend(tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in result.tools)
            cursor = result.nextCursor
            if not cursor:
                self.tools = tools
                return tools
            if cursor in seen:
                raise ValueError("MCP 返回了重复的分页游标")
            seen.add(cursor)
        raise ValueError("MCP 工具列表超过分页限制")

    async def run(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.client.sse import sse_client
        cfg = self.config
        try:
            async with AsyncExitStack() as stack:
                if cfg["transport"] == "stdio":
                    self.log_root.mkdir(parents=True, exist_ok=True)
                    log_path = self.log_root / (cfg["id"] + ".log")
                    if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
                        log_path.unlink()
                    errlog = stack.enter_context(log_path.open("a", encoding="utf-8"))
                    log_path.chmod(0o600)
                    streams = await stack.enter_async_context(stdio_client(StdioServerParameters(
                        command=cfg["command"], args=cfg["args"], env=cfg["env"], cwd=cfg["cwd"] or None,
                    ), errlog=errlog))
                elif cfg["transport"] == "sse":
                    streams = await stack.enter_async_context(sse_client(
                        cfg["url"], headers=cfg["headers"], timeout=cfg["timeout"], sse_read_timeout=cfg["timeout"],
                    ))
                else:
                    import httpx
                    http = await stack.enter_async_context(httpx.AsyncClient(
                        headers=cfg["headers"], timeout=cfg["timeout"], follow_redirects=False,
                    ))
                    streams = await stack.enter_async_context(streamable_http_client(cfg["url"], http_client=http))
                session = await stack.enter_async_context(ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=cfg["timeout"]),
                ))
                await session.initialize()
                await self.refresh_tools(session)
                self.ready.set_result(True)
                while True:
                    try:
                        operation, payload, future = await asyncio.wait_for(self.queue.get(), 300)
                    except asyncio.TimeoutError:
                        return
                    if future.cancelled():
                        continue
                    self.current = future
                    try:
                        if operation == "list":
                            result = await self.refresh_tools(session)
                        else:
                            name, arguments = payload
                            tool = next((t for t in self.tools if t["name"] == name), None)
                            if tool is None:
                                raise ValueError("服务未提供这个工具，请刷新工具列表")
                            from jsonschema import validate, ValidationError, SchemaError
                            try:
                                validate(arguments, tool["inputSchema"])
                            except (ValidationError, SchemaError) as exc:
                                raise ValueError("MCP 工具参数不符合声明的 schema：" + exc.message[:250]) from exc
                            answer = await session.call_tool(name, arguments)
                            result = answer.model_dump(mode="json", by_alias=True, exclude_none=True)
                        if not future.done():
                            future.set_result(result)
                    except asyncio.CancelledError:
                        if not future.done():
                            future.set_exception(ValueError("MCP 连接已关闭，调用结果尚未确认"))
                        raise
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(ValueError(self.error(exc)))
                    finally:
                        self.current = None
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(ValueError(self.error(exc)))
                # The owner may have timed out before awaiting readiness.
                self.ready.exception()
        finally:
            pending = [self.current] if self.current else []
            while not self.queue.empty():
                pending.append(self.queue.get_nowait()[2])
            for future in pending:
                if not future.done():
                    future.set_exception(ValueError("MCP 连接已关闭，请检查配置后重试"))

    async def request(self, operation: str, payload=None):
        try:
            async with asyncio.timeout(self.config["timeout"] + 5):
                await asyncio.shield(self.ready)
                if self.task.done():
                    raise ValueError("MCP 连接已关闭，请重新连接")
                future = asyncio.get_running_loop().create_future()
                await self.queue.put((operation, payload, future))
                return await future
        except asyncio.CancelledError:
            await self.close()
            raise
        except TimeoutError as exc:
            await self.close()
            raise ValueError("MCP 操作超时；未自动重试工具调用，请先核对外部执行结果") from exc

    async def close(self) -> None:
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task


class MCPManager:
    def __init__(self, root: Path):
        self.root = root
        self.connections: dict[str, Connection] = {}
        self.last_errors: dict[str, str] = {}
        self.mutation_lock = asyncio.Lock()

    def read(self) -> list[dict]:
        path = self.root / "mcp.json"
        if not path.exists():
            return []
        value = json.loads(path.read_text("utf-8"))
        if not isinstance(value, list):
            raise ValueError("MCP 配置损坏，请恢复 mcp.json 备份")
        return value

    def write(self, rows: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temp = tempfile.mkstemp(prefix=".mcp-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(rows, output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, self.root / "mcp.json")
        finally:
            Path(temp).unlink(missing_ok=True)

    def listing(self) -> dict:
        rows = []
        for row in self.read():
            connection = self.connections.get(row["id"])
            status = "disabled" if not row["enabled"] else "connected" if (
                connection and connection.ready.done() and not connection.task.done() and not connection.ready.exception()
            ) else "error" if row["id"] in self.last_errors else "disconnected"
            rows.append({**_public(row), "status": status, "error": self.last_errors.get(row["id"], ""),
                         "tool_count": len(connection.tools) if connection and status == "connected" else 0})
        return {"ok": True, "servers": rows}

    async def save(self, raw: dict) -> dict:
        async with self.mutation_lock:
            rows = self.read()
            previous = next((row for row in rows if row["id"] == raw.get("id")), None)
            if raw.get("id") and previous is None:
                raise ValueError("MCP 连接不存在")
            if previous and previous.get("kind") == "system":
                raise ValueError("系统 MCP 配置不可修改")
            row = validate_server(raw, previous)
            if any(item["name"] == row["name"] and item["id"] != row["id"] for item in rows):
                raise ValueError("连接名称已存在")
            await self.disconnect(row["id"])
            self.write([item for item in rows if item["id"] != row["id"]] + [row])
            self.last_errors.pop(row["id"], None)
            return {"ok": True, "server": _public(row)}

    async def import_config(self, payload: dict) -> dict:
        servers = payload.get("mcpServers")
        if not isinstance(servers, dict) or not servers or len(servers) > 50:
            raise ValueError("请提供包含 mcpServers 对象的配置（最多 50 个连接）")
        prepared = []
        for name, raw in servers.items():
            if not isinstance(raw, dict):
                raise ValueError("每个 MCP 连接配置必须是对象")
            transport = raw.get("transport") or raw.get("type") or ("stdio" if raw.get("command") else "http")
            if transport in {"streamable-http", "streamable_http"}:
                transport = "http"
            prepared.append(validate_server({**raw, "name": name, "transport": transport}))
        async with self.mutation_lock:
            rows = self.read()
            if set(row["name"] for row in rows) & set(row["name"] for row in prepared):
                raise ValueError("导入包含已有的连接名称，请修改名称或编辑原连接")
            self.write(rows + prepared)
        return {"ok": True, "imported": len(prepared)}

    async def disconnect(self, server_id: str) -> None:
        connection = self.connections.pop(server_id, None)
        if connection:
            await connection.close()

    async def delete(self, server_id: str) -> dict:
        async with self.mutation_lock:
            rows = self.read()
            row = next((item for item in rows if item["id"] == server_id), None)
            if not row:
                raise ValueError("MCP 连接不存在")
            if row.get("kind") == "system":
                raise ValueError("系统 MCP 连接不可删除")
            await self.disconnect(server_id)
            self.write([item for item in rows if item["id"] != server_id])
            self.last_errors.pop(server_id, None)
        return {"ok": True}

    async def request(self, server_id: str, operation: str, payload=None, *, testing=False):
        cfg = next((row for row in self.read() if row["id"] == server_id), None)
        if not cfg or (not testing and not cfg["enabled"]):
            raise ValueError("MCP 连接不存在或已停用")
        if operation == "call" and cfg.get("allowed_tools") is not None and payload[0] not in cfg["allowed_tools"]:
            raise ValueError("此工具未获准使用")
        connection = self.connections.get(server_id)
        if connection is None or connection.task.done():
            connection = Connection(cfg, self.root / "mcp-logs")
            self.connections[server_id] = connection
        try:
            result = await connection.request(operation, payload)
            self.last_errors.pop(server_id, None)
            if operation == "list" and not testing and cfg.get("allowed_tools") is not None:
                result = [tool for tool in result if tool["name"] in cfg["allowed_tools"]]
            return result
        except ValueError as exc:
            self.last_errors[server_id] = str(exc)
            raise
        finally:
            if testing and not cfg["enabled"]:
                await self.disconnect(server_id)

    async def close(self) -> None:
        await asyncio.gather(*(connection.close() for connection in self.connections.values()))
        self.connections.clear()


_MANAGERS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def get_manager() -> MCPManager:
    loop = asyncio.get_running_loop()
    root = Path(CONFIG.data_dir).expanduser().resolve() / "extensions"
    managers = _MANAGERS.setdefault(loop, {})
    if root not in managers:
        managers[root] = MCPManager(root)
    return managers[root]


async def close_managers() -> None:
    managers = _MANAGERS.pop(asyncio.get_running_loop(), {})
    await asyncio.gather(*(manager.close() for manager in managers.values()))

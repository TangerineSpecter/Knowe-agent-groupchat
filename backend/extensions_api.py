"""Extension management over the existing authenticated runtime HTTP connection."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import unquote

import httpx

from .knowledge_api import KnowledgeHttpResponse
from . import skill_installer


async def dispatch_extensions_http(method: str, path: str, body: bytes) -> KnowledgeHttpResponse:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    try:
        payload = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            raise ValueError("请求必须是 JSON 对象")
        route = parts[2:]
        if method == "GET" and route == ["skills", "settings"]:
            result = {"ok": True, "manual_directory": str(skill_installer.skill_root()),
                      "max_archive_bytes": skill_installer.MAX_ARCHIVE}
        elif method == "POST" and route == ["skills", "prepare"]:
            result = await skill_installer.prepare(payload)
        elif method == "POST" and route == ["skills", "install"]:
            result = await asyncio.to_thread(
                skill_installer.install, str(payload.get("token") or ""), payload.get("selected"),
                replace=payload.get("replace") is True,
            )
        elif route and route[0] == "mcp":
            from .mcp_service import get_manager
            manager = get_manager()
            if method == "GET" and route == ["mcp"]:
                result = manager.listing()
            elif method == "POST" and route == ["mcp", "save"]:
                result = await manager.save(payload)
            elif method == "POST" and route == ["mcp", "import"]:
                result = await manager.import_config(payload)
            elif method == "POST" and route == ["mcp", "delete"]:
                result = await manager.delete(str(payload.get("id") or ""))
            elif method == "POST" and route == ["mcp", "test"]:
                result = {"ok": True, "tools": await manager.request(str(payload.get("id") or ""), "list", testing=True)}
            else:
                return KnowledgeHttpResponse(404, {"ok": False, "error": "not_found"})
        else:
            return KnowledgeHttpResponse(404, {"ok": False, "error": "not_found"})
        return KnowledgeHttpResponse(200, result)
    except (ValueError, OSError, httpx.HTTPError) as exc:
        return KnowledgeHttpResponse(400, {"ok": False, "error": str(exc)[:500]})

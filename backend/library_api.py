"""File library projection and authenticated runtime routes."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import parse_qs

from .attachments import verify_path
from .config import CONFIG
from .file_library import FileLibrary
from .knowledge_api import KnowledgeHttpResponse


def library_for(server) -> FileLibrary:
    if getattr(server, "_file_library", None) is None:
        server._file_library = FileLibrary(server.data_root)
    return server._file_library


def upload_root() -> Path | None:
    root = os.environ.get("KNOWE_WEB_WORKSPACE_ROOT", "")
    return Path(root).resolve() / "uploads" if root else None


def record_event(server, event: dict) -> None:
    if not event.get("files") and not event.get("attachments"):
        return
    pid = str(event.get("project_id") or "")
    group = pid.split(":")[1] if pid.startswith("dm:") else pid
    project = server.hub.projects.get(group)
    directory = server.project_dirs.get(group)
    library_for(server).record_event(
        event, project_root=Path(directory).resolve() if directory else None,
        project_name=project.name if project else group,
        upload_root=upload_root(),
        verify_attachment=lambda raw: verify_path(str(raw.get("path") or ""), str(raw.get("sig") or ""),
                                                   CONFIG.attachment_key or CONFIG.runtime_token),
    )


def sync(server) -> dict:
    library = library_for(server)
    root = upload_root()
    if root:
        library.sync_uploads(root)
    # Import existing message file cards once per process. New events are observed live.
    imported = getattr(server, "_library_imported_projects", set())
    for pid, project in list(server.hub.projects.items()):
        if pid in imported:
            continue
        if server.store:
            for event in server.store.load_all_events(pid):
                record_event(server, {**event, "project_id": pid})
        for event in project.ring.events():
            record_event(server, {**event, "project_id": pid})
        imported.add(pid)
    server._library_imported_projects = imported
    library.refresh()
    return {"ok": True}


def busy_projects(server) -> set[str]:
    busy = set()
    for pid, engine in server.engines.items():
        project = server.hub.projects.get(pid)
        for member in project.members if project else []:
            agent_id = member.get("id") or member.get("agent_id")
            if agent_id and engine.worker_is_busy(agent_id):
                busy.add(pid)
                busy.update(key for key in server.hub.projects if key.startswith(f"dm:{pid}:"))
    return busy


async def dispatch_library_http(server, method: str, path: str, query: str, body: bytes) -> KnowledgeHttpResponse:
    try:
        library = library_for(server)
        if method == "GET" and path == "/api/library":
            await asyncio.to_thread(sync, server)
            params = parse_qs(query)
            pick = lambda key, default="": (params.get(key) or [default])[0]
            result = await asyncio.to_thread(library.listing, query=pick("query"), project=pick("project"),
                origin=pick("origin"), status=pick("status", "active"), favorite=pick("favorite") == "1",
                sort=pick("sort", "newest"), offset=int(pick("offset", "0")))
        elif method == "POST" and path == "/api/library/action":
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("请求必须是对象")
            selected = payload.get("files")
            if not isinstance(selected, list) or not selected or len(selected) > 100:
                raise ValueError("每次请选择 1–100 个文件")
            if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in selected):
                raise ValueError("文件选择无效")
            blocked = busy_projects(server)
            successes, failures = [], []
            for item in selected:
                try:
                    changed = await asyncio.to_thread(library.action, item["id"], str(payload.get("action") or ""),
                        version=str(item.get("version") or ""), confirm_references=payload.get("confirm_references") is True,
                        busy_projects=blocked)
                    successes.append(changed)
                except (ValueError, OSError) as exc:
                    failures.append({"id": item["id"], "error": str(exc)})
            result = {"ok": True, "succeeded": successes, "failed": failures}
        else:
            return KnowledgeHttpResponse(404, {"ok": False, "error": "not_found"})
        return KnowledgeHttpResponse(200, result)
    except (ValueError, OSError) as exc:
        return KnowledgeHttpResponse(400, {"ok": False, "error": str(exc)[:500]})

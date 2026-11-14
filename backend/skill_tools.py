"""Agent-facing skill discovery, loading and explicit installation."""
from __future__ import annotations

import asyncio
import json
from typing import Any
import httpx

from . import skill_installer

SKILL_START = "<knowe-selected-skills>"
SKILL_END = "</knowe-selected-skills>"


def selected_skill_context(engine: Any, skill_ids: Any) -> str:
    if skill_ids is None or skill_ids == []:
        return ""
    if engine.project_id == "__platform__":
        raise ValueError("知知会话不支持执行技能，请在项目群聊或成员私聊中选择技能。")
    if not isinstance(skill_ids, list) or len(skill_ids) > 8 or not all(isinstance(value, str) for value in skill_ids):
        raise ValueError("每条消息最多选择 8 个技能")
    blocks = []
    for pack_id in dict.fromkeys(skill_ids):
        result = engine.read_skillpack(pack_id)
        if not result.get("found"):
            raise ValueError(f"所选技能已停用或不存在：{pack_id}。请重新选择后发送。")
        if result["pack"].get("user_invocable") is False:
            raise ValueError("该技能仅供 Agent 自动使用")
        blocks.append({"pack_id": pack_id, "name": result["pack"]["name"],
                       "resource_directory": result.get("resource_directory"),
                       "instructions": result.get("instructions"), "body_md": result["body_md"]})
    text = json.dumps(blocks, ensure_ascii=False)
    if len(text) > 128_000:
        raise ValueError("所选技能正文过大，请减少本次选择的技能")
    return f"{SKILL_START}\nThese skills were explicitly selected by the user. Follow them for this task.\n{text}\n{SKILL_END}"


def extract_selected_skill_context(content: str) -> str:
    start, end = content.find(SKILL_START), content.find(SKILL_END)
    if start >= 0 and end > start:
        return content[start:end + len(SKILL_END)]
    return ""


def register_skill_tools(registry: Any, engine: Any, register: Any) -> None:
    def add(name: str, description: str, properties: dict, required: list, operation: Any) -> None:
        async def handle(args: dict, **_: Any) -> str:
            try:
                result = await operation(args)
                return json.dumps(result, ensure_ascii=False)
            except (ValueError, OSError, httpx.HTTPError) as exc:
                return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        register(registry, name=name, description=description,
                 parameters={"type": "object", "properties": properties, "required": required,
                             "additionalProperties": False}, handler=handle)

    async def listing(args: dict) -> dict:
        assets = getattr(engine, "_assets", None)
        if assets is None:
            from .knowledge_api import _merged_skillpacks
            packs = await asyncio.to_thread(_merged_skillpacks, None)
        else:
            packs = await asyncio.to_thread(assets.list_skillpacks, engine.project_id, engine.internal_workspace)
        query = str(args.get("query") or "").casefold()
        rows = [row for kind in ("system_builtin", "project_experience", "third_party")
                for row in packs.get(kind, []) if row.get("status") == "active"
                and row.get("automatic", True)
                and query in (row["name"] + " " + row.get("description", "")).casefold()]
        return {"ok": True, "skills": rows}

    async def reading(args: dict) -> dict:
        return await asyncio.to_thread(engine.read_skillpack, str(args.get("pack_id") or ""))

    async def preparing(args: dict) -> dict:
        return await skill_installer.prepare({"url": args.get("url")})

    async def installing(args: dict) -> dict:
        return await asyncio.to_thread(skill_installer.install, str(args.get("token") or ""),
                                       args.get("selected"), replace=args.get("replace") is True)

    add("list_skillpacks", "Find enabled skills for the current task. Read the selected skill before following it.",
        {"query": {"type": "string"}}, [], listing)
    if not registry.has("read_skillpack"):
        add("read_skillpack", "Read an enabled skill and expose its scripts/templates in the project. Resolve relative paths using resource_directory.",
            {"pack_id": {"type": "string"}}, ["pack_id"], reading)
    add("prepare_skill_install", "Preview a GitHub skill repository. Only use when the user asks to install skills. Returns candidate paths and an installation token; does not install or execute code.",
        {"url": {"type": "string"}}, ["url"], preparing)
    add("install_skillpack", "Install skills from a prepared preview ONLY when requested by the user. Pass exact selected paths from the preview. Replace existing skills only if the user requested replacement. Does not run scripts or install dependencies.",
        {"token": {"type": "string"}, "selected": {"type": "array", "items": {"type": "string"}},
         "replace": {"type": "boolean"}}, ["token", "selected"], installing)


def platform_skill_registry():
    from knowe_core.tool_registry import ToolRegistry
    registry = ToolRegistry()
    register_skill_tools(registry, None, lambda reg, **kw: reg.register(**kw))
    return registry

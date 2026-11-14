"""Validated, staged Agent Skills installation. Installing never executes package code."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
import time
from urllib.parse import quote, unquote, urlsplit
import uuid
import zipfile

import httpx
import yaml

from .config import CONFIG

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 96 * 1024 * 1024
MAX_FILES = 4000
STAGE_TTL = 3600
_LOCK = threading.RLock()


def skill_root() -> Path:
    from .knowledge_assets import KnowledgeAssetManager
    return KnowledgeAssetManager.third_party_skill_dir().resolve()


def staging_root() -> Path:
    return Path(CONFIG.data_dir).expanduser().resolve() / "skill-install-staging"


def metadata(text: str) -> dict:
    """Parse standard YAML, including multiline descriptions, with a bounded header."""
    match = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
    if not match or len(match[1]) > 16_384:
        raise ValueError("SKILL.md 必须包含 YAML 头部（name、description）")
    try:
        value = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise ValueError("SKILL.md 的 YAML 格式无效") from exc
    if not isinstance(value, dict):
        raise ValueError("SKILL.md 头部必须是对象")
    name, description = value.get("name"), value.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
        raise ValueError("技能 name 需为不超过 64 字符的小写英文、数字和连字符")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("技能 description 不能为空且不能超过 1024 字符")
    return {"name": name, "description": description.strip(),
            "user_invocable": value.get("user-invocable") is not False,
            "automatic": value.get("disable-model-invocation") is not True}


def _extract(raw: bytes, destination: Path) -> None:
    if len(raw) > MAX_ARCHIVE:
        raise ValueError("技能压缩包不能超过 32 MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("请选择有效的 ZIP 技能包") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES or sum(item.file_size for item in infos) > MAX_EXPANDED:
            raise ValueError("技能包解压后过大或文件过多")
        total = 0
        for item in infos:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename
                    or ":" in item.filename or "\x00" in item.filename
                    or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise ValueError("技能包包含不安全的路径或链接")
            if any(part in {".git", "node_modules", "__MACOSX"} for part in path.parts):
                continue
            target = destination.joinpath(*path.parts)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, target.open("xb") as output:
                while chunk := source.read(256 * 1024):
                    total += len(chunk)
                    if total > MAX_EXPANDED:
                        raise ValueError("技能包解压后超过 96 MB")
                    output.write(chunk)
            target.chmod(0o755 if mode & 0o111 else 0o644)


async def _github_archive(source: str) -> tuple[bytes, str, str]:
    source = source.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", source):
        source = "https://github.com/" + source
    parsed = urlsplit(source)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username is not None or parsed.port:
        raise ValueError("请输入 GitHub 仓库地址、技能目录地址或 owner/repo；其他来源请导入 ZIP")
    parts = [unquote(p) for p in parsed.path.strip("/").split("/")]
    if len(parts) < 2 or any(not re.fullmatch(r"[\w.-]+", part) for part in parts[:2]):
        raise ValueError("GitHub 仓库地址无效")
    owner, repo = parts[:2]
    repo = repo.removesuffix(".git")
    subdir = ""
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
        if len(parts) == 2:
            response = await client.get(f"https://api.github.com/repos/{owner}/{repo}")
            response.raise_for_status()
            ref = response.json()["default_branch"]
        elif len(parts) >= 4 and parts[2] in {"tree", "blob"}:
            ref = parts[3]
            subdir = "/".join(parts[4:])
            if parts[2] == "blob" and subdir.endswith("SKILL.md"):
                subdir = subdir.removesuffix("SKILL.md").rstrip("/")
        else:
            raise ValueError("请使用仓库首页或 tree/分支/技能目录地址")
        if ".." in PurePosixPath(subdir).parts:
            raise ValueError("技能目录无效")
        # Fixed origin: user-supplied URLs can never request local or arbitrary services.
        url = f"https://codeload.github.com/{owner}/{repo}/zip/{quote(ref, safe='')}"
        data = bytearray()
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_ARCHIVE:
                    raise ValueError("仓库压缩包超过 32 MB，请单独打包需要的技能后导入")
    return bytes(data), source, subdir


def _clean_stages(root: Path) -> None:
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink() and time.time() - child.stat().st_mtime > STAGE_TTL:
            shutil.rmtree(child)


def prepare_archive(raw: bytes, source: str = "ZIP", subdir: str = "") -> dict:
    with _LOCK:
        root = staging_root()
        root.mkdir(parents=True, exist_ok=True)
        _clean_stages(root)
        token = uuid.uuid4().hex
        stage = root / token
        content = stage / "content"
        content.mkdir(parents=True)
        try:
            _extract(raw, content)
            search = content
            if subdir:
                children = list(content.iterdir())
                if len(children) != 1 or not children[0].is_dir():
                    raise ValueError("仓库压缩包结构无效")
                search = children[0] / subdir
                if not search.is_relative_to(content) or not search.is_dir():
                    raise ValueError("仓库中找不到指定的技能目录")
            candidates, invalid = [], []
            for md in sorted(search.rglob("SKILL.md")):
                if md.stat().st_size > 512 * 1024:
                    invalid.append({"path": str(md.relative_to(content)), "error": "SKILL.md 超过 512 KB"})
                    continue
                try:
                    info = metadata(md.read_text("utf-8"))
                    relative = md.parent.relative_to(content).as_posix()
                    candidates.append({**info, "path": relative, "exists": (skill_root() / info["name"]).exists()})
                except (ValueError, UnicodeError) as exc:
                    invalid.append({"path": str(md.relative_to(content)), "error": str(exc)})
            if not candidates:
                raise ValueError("未发现有效的 SKILL.md 技能目录" + (f"：{invalid[0]['error']}" if invalid else ""))
            manifest = {"token": token, "source": source, "sha256": hashlib.sha256(raw).hexdigest(),
                        "created_at": time.time(), "skills": candidates, "invalid": invalid}
            (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), "utf-8")
            return {"ok": True, **manifest}
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise


async def prepare(payload: dict) -> dict:
    if payload.get("archive_base64"):
        try:
            raw = base64.b64decode(payload["archive_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("ZIP 数据无效") from exc
        return await asyncio.to_thread(prepare_archive, raw, "ZIP")
    raw, source, subdir = await _github_archive(str(payload.get("url") or ""))
    return await asyncio.to_thread(prepare_archive, raw, source, subdir)


def install(token: str, selected: list[str], *, replace: bool = False) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("安装预览已失效，请重新导入")
    with _LOCK:
        stage = staging_root() / token
        try:
            manifest = json.loads((stage / "manifest.json").read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("安装预览已失效，请重新导入") from exc
        if time.time() - manifest["created_at"] > STAGE_TTL:
            raise ValueError("安装预览已过期，请重新导入")
        if not isinstance(selected, list) or not selected or not all(isinstance(s, str) for s in selected):
            raise ValueError("请选择要安装的技能")
        known = {row["path"]: row for row in manifest["skills"]}
        if len(selected) != len(set(selected)) or any(path not in known for path in selected):
            raise ValueError("选择的技能无效")
        rows = [known[path] for path in selected]
        if len({row["name"] for row in rows}) != len(rows):
            raise ValueError("选中的技能名称重复，请每个名称只选一个")
        root = skill_root()
        root.mkdir(parents=True, exist_ok=True)
        for row in rows:
            target = root / row["name"]
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                raise ValueError("安装位置不是普通目录")
            if target.exists() and not replace:
                raise ValueError(f"技能 {row['name']} 已安装，请明确选择替换")
        # Stage every copy first; roll back the whole batch if any rename fails.
        transaction = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
        changed: list[tuple[Path, Path]] = []
        try:
            for row in rows:
                incoming = transaction / row["name"]
                shutil.copytree(stage / "content" / row["path"], incoming)
                (incoming / ".knowe-source.json").write_text(json.dumps({
                    "source": manifest["source"], "sha256": manifest["sha256"], "installed_at": time.time(),
                }, ensure_ascii=False), "utf-8")
            for row in rows:
                target, backup = root / row["name"], transaction / (row["name"] + ".previous")
                if target.exists():
                    target.rename(backup)
                changed.append((target, backup))
                (transaction / row["name"]).rename(target)
        except BaseException:
            for target, backup in reversed(changed):
                if target.exists():
                    shutil.rmtree(target)
                if backup.exists():
                    backup.rename(target)
            raise
        finally:
            shutil.rmtree(transaction, ignore_errors=True)
        shutil.rmtree(stage, ignore_errors=True)
        return {"ok": True, "installed": [row["name"] for row in rows]}


def materialize(source: Path, workspace: Path, pack_id: str) -> str:
    """Expose a validated resource snapshot in the project, never a link to managed storage."""
    workspace = workspace.resolve(strict=True)
    parent = workspace / ".knowe-skills"
    if parent.is_symlink():
        raise ValueError("项目技能目录不能是符号链接")
    parent.mkdir(exist_ok=True)
    digest = hashlib.sha256()
    files: list[Path] = []
    total = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("技能目录包含符号链接，请重新安装")
        if path.is_file():
            total += path.stat().st_size
            files.append(path)
            if len(files) > MAX_FILES or total > MAX_EXPANDED:
                raise ValueError("技能资源超过大小限制")
            digest.update(str(path.relative_to(source)).encode())
            digest.update(path.read_bytes())
    name = hashlib.sha256(pack_id.encode()).hexdigest()[:12] + "-" + digest.hexdigest()[:12]
    target = parent / name
    if target.is_symlink():
        raise ValueError("项目技能目录不能是符号链接")
    if target.exists():
        # A Worker may edit its copy. Never mistake that edited copy for the installed skill.
        expected = {path.relative_to(source).as_posix(): path for path in files}
        actual = list(target.rglob("*"))
        intact = not any(path.is_symlink() for path in actual)
        if intact:
            actual_files = {path.relative_to(target).as_posix(): path for path in actual if path.is_file()}
            intact = actual_files.keys() == expected.keys() and all(
                actual_files[key].read_bytes() == path.read_bytes() for key, path in expected.items())
        if not intact:
            target = parent / (name + "-" + uuid.uuid4().hex[:8])
    if not target.exists():
        staging = Path(tempfile.mkdtemp(prefix=".copy-", dir=parent))
        try:
            for path in files:
                dest = staging / path.relative_to(source)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            try:
                staging.rename(target)
            except FileExistsError:
                pass
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return target.relative_to(workspace).as_posix()

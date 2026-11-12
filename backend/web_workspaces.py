"""Directory policy shared by web APIs and all engine project-creation paths."""
from __future__ import annotations

import os
import re
from pathlib import Path


def web_workspace_root() -> Path | None:
    value = os.environ.get('KNOWE_WEB_WORKSPACE_ROOT', '').strip()
    return Path(value).expanduser().resolve() if value else None


def contained_directory(root: Path, raw: str = '') -> Path:
    path = (root / raw).resolve()
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError('请选择工作根目录内存在的文件夹')
    return path


def project_directory(project_id: str, raw: str | None) -> str | None:
    root = web_workspace_root()
    if root is None:
        return raw  # Desktop policy remains unchanged.
    if raw and raw.strip():
        return str(contained_directory(root, raw.strip()))
    if not re.fullmatch(r'project_\d{14}', project_id):
        raise ValueError('无效的项目 ID，无法创建工作目录')
    root.mkdir(parents=True, exist_ok=True)
    projects = root / 'projects'
    projects.mkdir(exist_ok=True)
    if not projects.resolve().is_relative_to(root):
        raise ValueError('项目目录超出工作根目录')
    target = projects / project_id
    target.mkdir(exist_ok=True)
    return str(contained_directory(root, str(target)))

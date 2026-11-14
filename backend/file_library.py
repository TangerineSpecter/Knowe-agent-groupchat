"""Persistent file catalog and recoverable trash for uploads and project deliverables.

The catalog references existing files instead of copying every attachment or output.
Desktop attachments outside managed roots are references: removing them never deletes
the user's original. Filesystem transitions are journalled before moving bytes.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
from typing import Any
import uuid

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _regular(path: Path, root: Path | None = None) -> Path:
    path = path.absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("资料文件路径不能经过符号链接")
    resolved = path.resolve()
    if root is not None and not resolved.is_relative_to(root.resolve()):
        raise ValueError("文件已超出原来的受管目录")
    if not resolved.is_file():
        raise FileNotFoundError("文件已不在原位置")
    return resolved


def _version(path: Path) -> str:
    st = path.stat()
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


class FileLibrary:
    def __init__(self, data_root: Path):
        self.root = data_root.resolve() / "library"
        self.root.mkdir(parents=True, exist_ok=True)
        self.trash = self.root / "trash"
        self.trash.mkdir(exist_ok=True)
        with _LOCKS_GUARD:
            self.lock = _LOCKS.setdefault(str(self.root), threading.RLock())
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, root TEXT NOT NULL,
                    name TEXT NOT NULL, ext TEXT NOT NULL, origin TEXT NOT NULL,
                    managed INTEGER NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL,
                    version TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    deleted REAL, favorite INTEGER NOT NULL DEFAULT 0, trash_path TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS refs (
                    file_id TEXT NOT NULL, project_id TEXT NOT NULL, project_name TEXT NOT NULL,
                    seq INTEGER NOT NULL, agent_id TEXT NOT NULL, origin TEXT NOT NULL,
                    PRIMARY KEY(file_id, project_id, seq, origin));
                CREATE INDEX IF NOT EXISTS refs_project ON refs(project_id, file_id);
                CREATE INDEX IF NOT EXISTS files_status ON files(status, mtime);
                CREATE TABLE IF NOT EXISTS checkpoints (project_id TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            ''')
        self.recover()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "catalog.sqlite3", timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self) -> None:
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT * FROM files WHERE status IN ('trashing','restoring','purging')").fetchall()
            for row in rows:
                original = Path(row["path"])
                trashed = Path(row["trash_path"]) if row["trash_path"] else None
                if not row["managed"]:
                    state = {"trashing": "trash", "restoring": "active" if original.is_file() else "missing", "purging": "purged"}[row["status"]]
                elif row["status"] == "purging":
                    state = "trash" if trashed and trashed.exists() else "purged"
                elif trashed and trashed.is_file():
                    state = "trash"
                else:
                    state = "active" if original.is_file() else "missing"
                db.execute("UPDATE files SET status=? WHERE id=?", (state, row["id"]))

    def register(self, path: Path, *, origin: str, managed_root: Path | None = None,
                 project_id: str = "", project_name: str = "", seq: int = 0, agent_id: str = "",
                 metadata: dict | None = None) -> str:
        if managed_root is None:
            # Canonicalize OS aliases such as macOS /var and /tmp for signed local
            # references. Keep the final component intact so file symlinks stay rejected.
            path = path.parent.resolve() / path.name
        path = _regular(path, managed_root)
        info = path.stat()
        with self.lock, self.connect() as db:
            old = db.execute("SELECT id,status,version FROM files WHERE path=?", (str(path),)).fetchone()
            if old and old["status"] in {"trash", "trashing", "restoring", "purging"}:
                return old["id"]
            file_id = old["id"] if old else uuid.uuid4().hex
            if old and old["status"] == "purged" and managed_root is None and old["version"] == _version(path):
                return file_id
            if old and old["status"] == "purged":
                # Preserve the old reference tombstone if another file appears at this path.
                db.execute("UPDATE files SET path=? WHERE id=?", (str(path) + ".purged-" + file_id, file_id))
                file_id = uuid.uuid4().hex
            db.execute('''INSERT INTO files(id,path,root,name,ext,origin,managed,size,mtime,version,created,metadata)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                size=excluded.size,mtime=excluded.mtime,version=excluded.version,status='active',
                metadata=CASE WHEN excluded.metadata='{}' THEN files.metadata ELSE excluded.metadata END''',
                (file_id, str(path), str(managed_root.resolve()) if managed_root else "", path.name,
                 path.suffix.lower().lstrip("."), origin, int(managed_root is not None), info.st_size,
                 info.st_mtime, _version(path), time.time(), json.dumps(metadata or {}, ensure_ascii=False)))
            if project_id:
                db.execute("INSERT OR REPLACE INTO refs VALUES(?,?,?,?,?,?)",
                           (file_id, project_id, project_name, seq, agent_id, origin))
            return file_id

    def record_event(self, event: dict, *, project_root: Path | None, project_name: str,
                     upload_root: Path | None, verify_attachment=None) -> None:
        project_id = str(event.get("project_id") or "")
        seq = int(event.get("seq") or 0)
        for raw in event.get("files") or []:
            if not isinstance(raw, dict) or not project_root:
                continue
            relative = raw.get("path")
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                continue
            try:
                self.register(project_root / relative, origin="generated", managed_root=project_root,
                              project_id=project_id, project_name=project_name, seq=seq,
                              agent_id=str(event.get("agent_id") or ""), metadata=raw)
            except (ValueError, OSError):
                continue
        for raw in event.get("attachments") or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("path"), str):
                continue
            path = Path(raw["path"])
            if not path.is_absolute() or (verify_attachment and not verify_attachment(raw)):
                continue
            managed_root = upload_root if upload_root and path.resolve().is_relative_to(upload_root.resolve()) else None
            try:
                self.register(path, origin="upload" if managed_root else "external", managed_root=managed_root,
                              project_id=project_id, project_name=project_name, seq=seq, metadata=raw)
            except (ValueError, OSError):
                continue

    def sync_uploads(self, upload_root: Path) -> int:
        if not upload_root.is_dir():
            return 0
        count = 0
        for directory, names, files in os.walk(upload_root, followlinks=False):
            names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
            for name in files:
                try:
                    self.register(Path(directory) / name, origin="upload", managed_root=upload_root)
                    count += 1
                except (ValueError, OSError):
                    continue
        return count

    def refresh(self) -> None:
        """Refresh sizes and missing state without reviving deliberately trashed files."""
        with self.lock, self.connect() as db:
            for row in db.execute("SELECT * FROM files WHERE status IN ('active','missing')").fetchall():
                try:
                    path = _regular(Path(row["path"]), Path(row["root"]) if row["managed"] else None)
                    st = path.stat()
                    db.execute("UPDATE files SET status='active',size=?,mtime=?,version=? WHERE id=?",
                               (st.st_size, st.st_mtime, _version(path), row["id"]))
                except (ValueError, OSError):
                    db.execute("UPDATE files SET status='missing' WHERE id=?", (row["id"],))

    def listing(self, *, query="", project="", origin="", status="active", favorite=False,
                sort="newest", offset=0, limit=60) -> dict:
        clauses, values = [], []
        if status == "trash":
            clauses.append("f.status='trash'")
        else:
            clauses.append("f.status IN ('active','missing')")
        if query:
            clauses.append("f.name LIKE ? ESCAPE '\\'")
            values.append("%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if project:
            clauses.append("EXISTS(SELECT 1 FROM refs r WHERE r.file_id=f.id AND r.project_id=?)")
            values.append(project)
        if origin == "unreferenced":
            clauses.append("f.origin='upload' AND NOT EXISTS(SELECT 1 FROM refs r WHERE r.file_id=f.id)")
        elif origin:
            clauses.append("f.origin=?")
            values.append(origin)
        if favorite:
            clauses.append("f.favorite=1")
        order = {"newest": "f.mtime DESC", "largest": "f.size DESC", "name": "f.name COLLATE NOCASE"}.get(sort, "f.mtime DESC")
        where = " AND ".join(clauses)
        limit, offset = min(100, max(1, int(limit))), max(0, int(offset))
        with self.connect() as db:
            total = db.execute(f"SELECT count(*) FROM files f WHERE {where}", values).fetchone()[0]
            rows = db.execute(f"SELECT f.* FROM files f WHERE {where} ORDER BY {order},f.id LIMIT ? OFFSET ?",
                              [*values, limit, offset]).fetchall()
            items = []
            for row in rows:
                item = {key: row[key] for key in ("id", "name", "ext", "origin", "managed", "size", "mtime", "version", "created", "status", "favorite", "deleted")}
                item["references"] = [dict(ref) for ref in db.execute("SELECT project_id,project_name,seq,agent_id,origin FROM refs WHERE file_id=? ORDER BY seq DESC", (row["id"],))]
                items.append(item)
            totals = db.execute('''SELECT
                coalesce(sum(CASE WHEN status='active' AND managed=1 THEN size ELSE 0 END),0) AS active_bytes,
                coalesce(sum(CASE WHEN status='trash' AND managed=1 THEN size ELSE 0 END),0) AS trash_bytes,
                count(CASE WHEN status='active' THEN 1 END) AS active_count
                FROM files''').fetchone()
            projects = [dict(row) for row in db.execute("SELECT project_id,max(project_name) AS name FROM refs GROUP BY project_id ORDER BY name")]
        return {"ok": True, "items": items, "total": total, "offset": offset,
                "next_offset": offset + limit if offset + limit < total else None,
                "storage": dict(totals), "projects": projects}

    def resolve(self, file_id: str) -> tuple[Path, dict]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM files WHERE id=? AND status='active'", (file_id,)).fetchone()
        if not row:
            raise FileNotFoundError("文件已移入回收站、删除或不在原位置")
        path = _regular(Path(row["path"]), Path(row["root"]) if row["managed"] else None)
        return path, dict(row)

    @staticmethod
    def _move(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise ValueError("目标位置已有文件，不能覆盖；请先移走同名文件")
        try:
            os.rename(source, target)
        except OSError as exc:
            import errno
            if exc.errno != errno.EXDEV:
                raise
            staging = target.with_name(target.name + ".copying")
            try:
                shutil.copy2(source, staging)
                def digest(path):
                    with path.open("rb") as handle:
                        return hashlib.file_digest(handle, "sha256").digest()
                if digest(source) != digest(staging):
                    raise OSError("跨磁盘移动校验失败，原文件已保留")
                os.rename(staging, target)
                source.unlink()
            finally:
                staging.unlink(missing_ok=True)

    def action(self, file_id: str, action: str, *, version: str = "", confirm_references=False,
               busy_projects: set[str] | None = None) -> dict:
        with self.lock:
            with self.connect() as db:
                row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
                if not row:
                    raise ValueError("资料不存在")
                row = dict(row)
                refs = db.execute("SELECT DISTINCT project_id FROM refs WHERE file_id=?", (file_id,)).fetchall()
                if action == "favorite":
                    db.execute("UPDATE files SET favorite=1-favorite WHERE id=?", (file_id,))
                    return {"ok": True}
                if action not in {"trash", "restore", "purge"}:
                    raise ValueError("不支持的文件操作")
                if any(ref[0] in (busy_projects or set()) for ref in refs):
                    raise ValueError("关联项目仍有任务执行，请结束任务后再操作文件")
                if action == "trash" and refs and not confirm_references:
                    raise ValueError("文件仍被聊天引用，请确认影响后再移入回收站")
                source = Path(row["path"])
                trash = self.trash / file_id
                if action == "trash":
                    if row["status"] not in {"active", "missing"}:
                        raise ValueError("文件不在资料空间中")
                    if row["managed"] and (row["status"] == "active" or source.exists()):
                        _regular(source, Path(row["root"]))
                        if not version or version != _version(source):
                            raise ValueError("文件已发生变化，请刷新后重试")
                    db.execute("UPDATE files SET status='trashing',trash_path=?,deleted=? WHERE id=?", (str(trash), time.time(), file_id))
                elif action == "restore":
                    if row["status"] != "trash":
                        raise ValueError("文件不在回收站中")
                    if row["managed"] and trash.exists():
                        _regular(trash, self.trash)
                        if source.exists():
                            raise ValueError("原位置已有同名文件，不能覆盖；请先移走同名文件")
                        for component in (source.parent, *source.parents):
                            if component.is_symlink():
                                raise ValueError("恢复路径包含符号链接")
                        if not source.resolve().is_relative_to(Path(row["root"]).resolve()):
                            raise ValueError("恢复位置超出受管目录")
                    db.execute("UPDATE files SET status='restoring' WHERE id=?", (file_id,))
                else:
                    if row["status"] != "trash":
                        raise ValueError("请先移入回收站，再彻底删除")
                    db.execute("UPDATE files SET status='purging' WHERE id=?", (file_id,))
            try:
                if action == "trash":
                    if row["managed"] and source.is_file():
                        self._move(source, trash)
                    state = "trash"
                elif action == "restore":
                    if row["managed"] and trash.is_file():
                        self._move(trash, source)
                    state = "active" if source.is_file() else "missing"
                else:
                    if trash.exists():
                        _regular(trash, self.trash).unlink()
                    state = "purged"
                with self.connect() as db:
                    db.execute("UPDATE files SET status=? WHERE id=?", (state, file_id))
                return {"ok": True, "id": file_id, "status": state,
                        "released_bytes": row["size"] if action == "purge" and row["managed"] else 0}
            except BaseException:
                self.recover()
                raise

    def discard_upload(self, path: Path, upload_root: Path) -> bool:
        """Remove only an unreferenced managed upload, never a sent attachment."""
        path = _regular(path, upload_root)
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM files WHERE path=? AND origin='upload' AND managed=1 AND status='active'", (str(path),)).fetchone()
            if not row or db.execute("SELECT 1 FROM refs WHERE file_id=? LIMIT 1", (row["id"],)).fetchone():
                return False
            path.unlink()
            db.execute("UPDATE files SET status='purged' WHERE id=?", (row["id"],))
            return True

"""Durable single-user web sessions' notification projection (not a second task engine)."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class WebState:
    def __init__(self, data_root: Path):
        self.path = data_root / 'web' / 'notifications.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY, event_key TEXT NOT NULL UNIQUE,
                project_id TEXT NOT NULL, project_name TEXT NOT NULL, kind TEXT NOT NULL,
                body TEXT NOT NULL, created_at TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
                card_id TEXT, resolved INTEGER NOT NULL DEFAULT 0)''')
            db.execute('CREATE INDEX IF NOT EXISTS notifications_read ON notifications(read, id)')
            db.execute('CREATE TABLE IF NOT EXISTS revoked_sessions (digest TEXT PRIMARY KEY, expires REAL NOT NULL)')
            db.execute('''CREATE TABLE IF NOT EXISTS resolved_approvals (
                project_id TEXT NOT NULL, card_id TEXT NOT NULL,
                PRIMARY KEY(project_id, card_id))''')
            # Older projections keyed recovered cards by their new event sequence. Merge
            # those duplicates before adding the stable card-identity constraint.
            duplicates = db.execute('''SELECT project_id, card_id, min(id) AS keep_id,
                max(read) AS was_read, max(resolved) AS was_resolved FROM notifications
                WHERE kind='approval_card' AND card_id IS NOT NULL
                GROUP BY project_id, card_id HAVING count(*)>1''').fetchall()
            for row in duplicates:
                db.execute('UPDATE notifications SET read=?, resolved=? WHERE id=?',
                           (row['was_read'], row['was_resolved'], row['keep_id']))
                db.execute("DELETE FROM notifications WHERE kind='approval_card' AND project_id=? AND card_id=? AND id<>?",
                           (row['project_id'], row['card_id'], row['keep_id']))
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS notification_approval_identity
                ON notifications(project_id, card_id)
                WHERE kind='approval_card' AND card_id IS NOT NULL''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, event: dict[str, Any]) -> None:
        kind = event.get('type')
        pid = str(event.get('project_id') or '')
        if kind == 'approval_resolved':
            card_id = event.get('card_id')
            if not pid or not isinstance(card_id, str) or not card_id:
                return
            with self.connect() as db:
                # Resolution may arrive before startup history has projected the card.
                db.execute('INSERT OR IGNORE INTO resolved_approvals VALUES(?,?)', (pid, card_id))
                db.execute('UPDATE notifications SET resolved=1, read=1 WHERE project_id=? AND card_id=?',
                           (pid, card_id))
            return
        if kind not in {'approval_card', 'error', 'completion_status'}:
            return
        if kind == 'completion_status' and not event.get('terminal'):
            return
        # Stable across replay/restart; ordinary agent messages remain chat unreads.
        key = f"{pid}:{event.get('event_id') or event.get('seq')}:{kind}"
        if not pid or event.get('seq') is None:
            return
        body = {key: event[key] for key in ('status', 'reason', 'message', 'content', 'tool', 'agent_id') if key in event}
        if kind == 'approval_card':
            body['tool'] = event.get('tool', '')
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO notifications
                (event_key,project_id,project_name,kind,body,created_at,card_id) VALUES(?,?,?,?,?,?,?)''',
                       (key, pid, str(event.get('project_name') or pid), kind,
                        json.dumps(body, ensure_ascii=False), str(event.get('ts') or ''), event.get('card_id')))
            if kind == 'approval_card':
                # The insert holds the write transaction: a concurrent resolution is
                # either visible here or will update the card after this commit.
                db.execute('''UPDATE notifications SET resolved=1, read=1
                    WHERE project_id=? AND card_id=? AND EXISTS (
                        SELECT 1 FROM resolved_approvals WHERE project_id=? AND card_id=?)''',
                           (pid, event.get('card_id'), pid, event.get('card_id')))

    def list(self, before: int = 0, limit: int = 50) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute('SELECT * FROM notifications WHERE (?=0 OR id<?) ORDER BY id DESC LIMIT ?',
                              (before, before, min(100, max(1, limit)))).fetchall()
            unread = db.execute('SELECT count(*) FROM notifications WHERE read=0').fetchone()[0]
            pending = db.execute("SELECT count(*) FROM notifications WHERE read=0 AND kind='approval_card' AND resolved=0").fetchone()[0]
            latest = db.execute('SELECT coalesce(max(id),0) FROM notifications').fetchone()[0]
        items = [dict(row) for row in rows]
        for row in items:
            row['body'] = json.loads(row['body'])
        return {'items': items, 'unread': unread, 'pending': pending, 'latest_id': latest,
                'next_before': items[-1]['id'] if len(items) == limit else None}

    def mark_read(self, *, item_id: int = 0, through: int = 0) -> None:
        with self.connect() as db:
            if item_id:
                db.execute('UPDATE notifications SET read=1 WHERE id=?', (item_id,))
            elif through:
                # Do not swallow events that arrived after the page was fetched.
                db.execute('UPDATE notifications SET read=1 WHERE id<=?', (through,))

    def revoked(self, digest: str) -> bool:
        with self.connect() as db:
            return db.execute('SELECT 1 FROM revoked_sessions WHERE digest=? AND expires>?', (digest, time.time())).fetchone() is not None

    def revoke(self, digest: str, expires: float) -> None:
        with self.connect() as db:
            db.execute('DELETE FROM revoked_sessions WHERE expires<=?', (time.time(),))
            db.execute('INSERT OR REPLACE INTO revoked_sessions VALUES(?,?)', (digest, expires))

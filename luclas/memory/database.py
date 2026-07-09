import sqlite3
import os
from contextlib import contextmanager
from config import DB_PATH


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id          TEXT PRIMARY KEY,
                content     TEXT NOT NULL,
                type        TEXT DEFAULT '',
                tags        TEXT DEFAULT '[]',
                importance  INTEGER DEFAULT 5,
                source      TEXT DEFAULT '',
                credibility TEXT DEFAULT '',
                access_count INTEGER DEFAULT 0,
                embedding   BLOB,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS task_records (
                id           TEXT PRIMARY KEY,
                session_id   TEXT DEFAULT '',
                goal         TEXT NOT NULL,
                summary      TEXT DEFAULT '',
                artifacts    TEXT DEFAULT '[]',
                tree         TEXT DEFAULT '{}',
                importance   INTEGER DEFAULT 7,
                tier         TEXT DEFAULT 'active',
                status       TEXT DEFAULT 'running',
                created_at   TEXT DEFAULT (datetime('now','localtime')),
                completed_at TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS task_summaries (
                id           TEXT PRIMARY KEY,
                content      TEXT NOT NULL,
                period_start TEXT DEFAULT '',
                period_end   TEXT DEFAULT '',
                record_ids   TEXT DEFAULT '[]',
                created_at   TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id            TEXT PRIMARY KEY,
                name          TEXT NOT NULL,
                goal          TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_time TEXT NOT NULL,
                schedule_day  TEXT DEFAULT '',
                enabled       INTEGER DEFAULT 1,
                last_run      TEXT DEFAULT '',
                created_at    TEXT DEFAULT (datetime('now','localtime'))
            );
        """)
    _migrate()


def _migrate():
    """幂等迁移：为旧数据库补列/补表。"""
    with get_conn() as conn:
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN source TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN credibility TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id            TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    goal          TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    schedule_time TEXT NOT NULL,
                    schedule_day  TEXT DEFAULT '',
                    enabled       INTEGER DEFAULT 1,
                    last_run      TEXT DEFAULT '',
                    notify_channel TEXT DEFAULT 'terminal',
                    created_at    TEXT DEFAULT (datetime('now','localtime'))
                );
            """)
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN notify_channel TEXT DEFAULT 'terminal'")
        except Exception:
            pass

        # task_records.status: added when the redundant `tasks` table was folded
        # into task_records. Backfill from each record's tree (root node status)
        # before dropping `tasks`, so nothing is lost.
        try:
            # No DEFAULT here on purpose: existing rows must land as NULL so the
            # backfill below (not SQLite's own default-fill) decides their status.
            conn.execute("ALTER TABLE task_records ADD COLUMN status TEXT")
        except Exception:
            pass
        try:
            import json as _json
            rows = conn.execute(
                "SELECT id, tree FROM task_records WHERE status IS NULL OR status=''"
            ).fetchall()
            for r in rows:
                try:
                    tree = _json.loads(r["tree"] or "{}")
                except Exception:
                    tree = {}
                status = tree.get("status") or "done"
                if status not in ("done", "failed"):
                    status = "done"
                conn.execute("UPDATE task_records SET status=? WHERE id=?", (status, r["id"]))
        except Exception:
            pass
        try:
            conn.execute("DROP TABLE IF EXISTS tasks")
        except Exception:
            pass


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

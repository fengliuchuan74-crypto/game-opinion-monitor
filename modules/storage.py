"""Persistent operational state. Transactions always close their connections."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database(path: Path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def backup_database(path: Path, directory: Path | None = None) -> Path:
    path = Path(path)
    directory = directory or path.parent / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"reviews_{datetime.now():%Y%m%d_%H%M%S_%f}.sqlite3"
    with database(path) as source:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("备份完整性检查未通过")
        finally:
            destination.close()
    return target


def init_operations(connection):
    connection.executescript("""
        CREATE INDEX IF NOT EXISTS idx_reviews_game_date ON review_records(app_id,country,date);
        CREATE INDEX IF NOT EXISTS idx_reviews_game_time ON review_records(app_id,country,julianday(date));
        CREATE TABLE IF NOT EXISTS review_revisions (
          id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL,
          changed_at TEXT NOT NULL, previous_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS review_overrides (
          review_id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL,
          sentiment TEXT NOT NULL, category TEXT NOT NULL, note TEXT NOT NULL,
          reviewer TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, object_id TEXT NOT NULL,
          changed_at TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS monitor_targets (
          app_id TEXT NOT NULL, country TEXT NOT NULL, enabled INTEGER DEFAULT 0,
          interval_minutes INTEGER DEFAULT 30, pages INTEGER DEFAULT 3,
          next_due TEXT, requested INTEGER DEFAULT 0, lease_until TEXT,
          last_success TEXT, last_attempt TEXT, last_status TEXT DEFAULT '未采集',
          failures INTEGER DEFAULT 0, PRIMARY KEY(app_id,country));
        CREATE TABLE IF NOT EXISTS collection_runs (
          id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, country TEXT NOT NULL,
          started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
          fetched INTEGER DEFAULT 0, inserted INTEGER DEFAULT 0,
          updated INTEGER DEFAULT 0, duplicates INTEGER DEFAULT 0,
          pages INTEGER DEFAULT 0, detail TEXT DEFAULT '', oldest_date TEXT,
          stop_reason TEXT DEFAULT '');
        CREATE INDEX IF NOT EXISTS idx_runs_game ON collection_runs(app_id,country,id);
        CREATE TABLE IF NOT EXISTS action_items (
          id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, country TEXT NOT NULL,
          category TEXT NOT NULL, title TEXT NOT NULL, priority TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT '待核实', owner TEXT NOT NULL DEFAULT '',
          due_date TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
          plan_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_actions_game ON action_items(app_id,country,status);
        CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS alerts (
          id INTEGER PRIMARY KEY, app_id TEXT NOT NULL, country TEXT NOT NULL,
          created_at TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL,
          fingerprint TEXT NOT NULL UNIQUE, acknowledged_at TEXT);
    """)
    for table,definitions in {
        'monitor_targets':{'request_json':"TEXT NOT NULL DEFAULT '{}'",'last_review_at':'TEXT',
            'active_run_id':'INTEGER','lease_token':'TEXT'},
        'collection_runs':{'request_json':"TEXT NOT NULL DEFAULT '{}'",'result_json':"TEXT NOT NULL DEFAULT '{}'",
            'checkpoint_json':"TEXT NOT NULL DEFAULT '{}'",'control':"TEXT NOT NULL DEFAULT ''",
            'retry_count':'INTEGER NOT NULL DEFAULT 0','available_at':'TEXT'},
    }.items():
        columns={row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        for name,definition in definitions.items():
            if name not in columns: connection.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    from .agent_analysis import init_agent_tables
    init_agent_tables(connection)


def audit(connection, kind, object_id, actor, payload):
    connection.execute("INSERT INTO audit_log(kind,object_id,changed_at,actor,payload) VALUES(?,?,?,?,?)",
                       (kind, str(object_id), utc_now(), actor, json.dumps(payload, ensure_ascii=False)))

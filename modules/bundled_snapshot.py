"""Load the published review snapshot once into a pristine local database."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import uuid
from pathlib import Path

from .review_store import init_db
from .storage import database

REVIEW_COLUMNS = (
    'id', 'dedupe_key', 'content_hash', 'platform', 'external_id', 'data_source',
    'collected_at', 'app_id', 'country', 'date', 'author', 'title', 'content',
    'rating', 'likes', 'comments', 'shares', 'url', 'note_type', 'topic',
)
PROFILE_COLUMNS = (
    'app_id', 'country', 'platform', 'app_name', 'seller', 'bundle_id', 'track_url',
    'created_at', 'updated_at', 'last_collected_at',
)
RESULT_COLUMNS = (
    'review_id', 'content_hash', 'model', 'reasoning_effort', 'prompt_version',
    'result_json', 'actual_model', 'run_id', 'analyzed_at',
)
SETTING_COLUMNS = ('app_id', 'country', 'model', 'reasoning_effort', 'updated_at')
TABLES = {
    'game_profiles': PROFILE_COLUMNS,
    'review_records': REVIEW_COLUMNS,
    'agent_review_results': RESULT_COLUMNS,
    'agent_settings': SETTING_COLUMNS,
}
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_FOLDER = Path(__file__).resolve().parents[1] / 'bundled_data'


def snapshot_info(db_path: Path) -> dict:
    """Read provenance without creating or modifying a database."""
    path = Path(db_path)
    if not path.exists():
        return {}
    with database(path) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='store_meta'").fetchone():
            return {}
        row = connection.execute("SELECT value FROM store_meta WHERE key='bundled_snapshot'").fetchone()
    if not row:
        return {}
    try:
        value = json.loads(row[0])
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _has_local_data(connection):
    # An existing database with local settings, jobs or data is never reseeded.
    return any(connection.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in (
        'review_records', 'game_profiles', 'monitor_targets', 'agent_settings',
        'agent_runs', 'agent_review_results', 'collection_runs', 'review_overrides',
        'action_items', 'review_revisions', 'audit_log', 'alerts', 'runtime_state',
    ))


def _read_snapshot(folder: Path):
    metadata = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    packed = (folder / 'snapshot.json.gz').read_bytes()
    if metadata.get('format_version') != 1 or hashlib.sha256(packed).hexdigest() != metadata.get('sha256'):
        raise ValueError('随附评论快照校验失败，请重新下载完整项目。')
    with gzip.GzipFile(fileobj=io.BytesIO(packed)) as handle:
        unpacked = handle.read(MAX_PAYLOAD_BYTES + 1)
    if len(unpacked) > MAX_PAYLOAD_BYTES:
        raise ValueError('随附评论快照超过允许大小。')
    payload = json.loads(unpacked)
    if payload.get('format_version') != 1 or set(payload.get('tables', {})) != set(TABLES):
        raise ValueError('随附评论快照结构不受支持。')
    tables = payload['tables']
    for table, columns in TABLES.items():
        if not isinstance(tables[table], list) or any(set(row) != set(columns) for row in tables[table]):
            raise ValueError(f'随附评论快照字段错误：{table}')
    reviews = {row['id']: row for row in tables['review_records']}
    profiles = {(row['app_id'], row['country']) for row in tables['game_profiles']}
    if (len(reviews) != len(tables['review_records']) or len(reviews) != metadata.get('review_count')
            or len(profiles) != metadata.get('game_count')
            or len(tables['agent_review_results']) != metadata.get('agent_result_count')):
        raise ValueError('随附评论快照条数不一致。')
    for row in reviews.values():
        if (row['platform'] != 'App Store' or (row['app_id'], row['country']) not in profiles
                or not isinstance(row['content'], str)):
            raise ValueError('随附评论快照包含无效评论。')
    for row in tables['agent_review_results']:
        review = reviews.get(row['review_id'])
        result = json.loads(row['result_json'])
        if (not review or review['content_hash'] != row['content_hash']
                or result.get('review_id') != row['review_id'] or result.get('content_hash') != row['content_hash']):
            raise ValueError('随附 Agent 解读与评论版本不一致。')
    for row in tables['agent_settings']:
        if (row['app_id'], row['country']) not in profiles:
            raise ValueError('随附 Agent 展示设置与游戏不一致。')
    caches = payload.get('caches', {})
    if set(caches) - {'app_versions', 'app_icons'}:
        raise ValueError('随附快照缓存类型不受支持。')
    for kind, entries in caches.items():
        for key, value in entries.items():
            if (not re.fullmatch(r'[0-9]+_[a-z]{2}\.json', key)
                    or key != f"{value.get('app_id')}_{value.get('country')}.json"
                    or (value.get('app_id'), value.get('country')) not in profiles):
                raise ValueError('随附快照缓存归属无效。')
            if kind == 'app_icons' and not str(value.get('data_uri', '')).startswith('data:image/png;base64,'):
                raise ValueError('随附图标格式无效。')
    return metadata, payload


def _write_cache_if_missing(target: Path, value: dict):
    if target.exists():
        return
    pending = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with pending.open('x', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # Importers hold the same SQLite write lock. Publish only complete JSON;
        # leave a previously present local cache untouched on a retry.
        if not target.exists():
            os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


def ensure_bundled_snapshot(db_path: Path, folder: Path | None = None) -> dict:
    """Seed only a pristine DB; never copy queues, schedules, credentials or logs.

    Calls are idempotent across launcher/UI processes. SQLite serializes the final
    emptiness check and all inserts in one transaction. Existing user data wins.
    """
    folder = Path(folder) if folder is not None else DEFAULT_FOLDER
    if os.environ.get('APPSTORE_SKIP_BUNDLED_SNAPSHOT') == '1' or not (folder / 'manifest.json').exists():
        return {}
    path = Path(db_path)
    init_db(path)
    with database(path) as connection:
        marker = connection.execute("SELECT value FROM store_meta WHERE key='bundled_snapshot'").fetchone()
        if marker:
            return json.loads(marker[0])
        if _has_local_data(connection):
            return {}
    metadata, payload = _read_snapshot(folder)
    with database(path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        marker = connection.execute("SELECT value FROM store_meta WHERE key='bundled_snapshot'").fetchone()
        if marker:
            return json.loads(marker[0])
        if _has_local_data(connection):
            return {}
        for table, columns in TABLES.items():
            query = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
            connection.executemany(query, [[row[key] for key in columns] for row in payload['tables'][table]])
        # Only display configuration is imported. Schema defaults keep automatic
        # analysis disabled, and neither collection nor Agent task queues are copied.
        connection.execute("UPDATE agent_meta SET value=1 WHERE key='revision'")
        for kind, entries in payload.get('caches', {}).items():
            destination = path.parent / kind
            destination.mkdir(parents=True, exist_ok=True)
            for name, value in entries.items():
                _write_cache_if_missing(destination / name, value)
        connection.execute("INSERT INTO store_meta(key,value) VALUES('bundled_snapshot',?)",
                           (json.dumps(metadata, ensure_ascii=False),))
    return metadata

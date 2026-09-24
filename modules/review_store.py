from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from collectors.base import RawReview
from .storage import database, backup_database, init_operations


SCHEMA_VERSION = 8


@dataclass(slots=True)
class SaveResult:
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    failed: int = 0
    errors: list[str] | None = None


_connect = database


def init_db(db_path: Path) -> None:
    db_path = Path(db_path)
    if db_path.exists():
        with _connect(db_path) as previous:
            table = previous.execute("SELECT 1 FROM sqlite_master WHERE name='store_meta'").fetchone()
            version = previous.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone() if table else None
        if version and int(version[0]) > SCHEMA_VERSION:
            raise RuntimeError('数据库版本高于当前程序，请使用较新版本，避免降级写入')
        if version and int(version[0]) == SCHEMA_VERSION:
            return
        backup_database(db_path)
    with _connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                platform TEXT NOT NULL,
                external_id TEXT,
                data_source TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                app_id TEXT,
                country TEXT,
                date TEXT,
                author TEXT,
                title TEXT,
                content TEXT NOT NULL,
                rating REAL,
                likes INTEGER DEFAULT 0,
                comments INTEGER DEFAULT 0,
                shares INTEGER DEFAULT 0,
                url TEXT,
                note_type TEXT,
                topic TEXT,
                raw_json TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS store_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS game_profiles (
                app_id TEXT NOT NULL,
                country TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'App Store',
                app_name TEXT NOT NULL,
                seller TEXT,
                bundle_id TEXT,
                track_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_collected_at TEXT,
                PRIMARY KEY (app_id, country)
            )
            """
        )
        init_operations(connection)
        _migrate_dedupe_keys(connection)
        _backfill_game_profiles(connection)
        connection.execute(
            "INSERT OR REPLACE INTO store_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def _migrate_dedupe_keys(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE review_records
        SET dedupe_key = 'external:' || platform || ':' ||
            COALESCE(app_id, '') || ':' || COALESCE(country, '') || ':' || external_id
        WHERE external_id IS NOT NULL
          AND external_id != ''
          AND dedupe_key NOT LIKE 'external:%:%:%:%'
        """
    )


def _backfill_game_profiles(connection: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = connection.execute(
        """
        SELECT
            app_id,
            country,
            COALESCE(MAX(platform), 'App Store') AS platform,
            MAX(collected_at) AS last_collected_at
        FROM review_records
        WHERE app_id IS NOT NULL AND app_id != ''
        GROUP BY app_id, country
        """
    ).fetchall()
    for row in rows:
        app_id = str(row["app_id"])
        country = str(row["country"] or "")
        connection.execute(
            """
            INSERT OR IGNORE INTO game_profiles (
                app_id, country, platform, app_name, seller, bundle_id,
                track_url, created_at, updated_at, last_collected_at
            ) VALUES (?, ?, ?, ?, '', '', '', ?, ?, ?)
            """,
            (
                app_id,
                country,
                row["platform"] or "App Store",
                f"App {app_id}",
                now,
                now,
                row["last_collected_at"],
            ),
        )


def _content_hash(review: RawReview) -> str:
    base = f"{review.platform}|{review.author}|{review.date}|{review.title}|{review.content}|{review.rating}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _without_nonprinting_controls(content: str) -> str:
    # Public-list text can contain transport controls (for example U+0014)
    # absent from RSS. Keep whitespace and Unicode format characters, which
    # can change line breaks, layout or the meaning of visible text.
    return ''.join(char for char in content
                   if unicodedata.category(char) != 'Cc' or char.isspace())


def _dedupe_key(review: RawReview, content_hash: str) -> str:
    app_id = review.app_id or ""
    country = review.country or ""
    if review.external_id:
        return f"external:{review.platform}:{app_id}:{country}:{review.external_id}"
    return f"hash:{review.platform}:{app_id}:{country}:{review.author}:{review.date}:{content_hash}"


def _upsert_game_profile_connection(
    connection: sqlite3.Connection,
    *,
    app_id: str,
    country: str,
    app_name: str | None = None,
    seller: str | None = None,
    bundle_id: str | None = None,
    track_url: str | None = None,
    platform: str = "App Store",
    last_collected_at: str | None = None,
) -> None:
    app_id = str(app_id).strip()
    country = str(country or "").strip().lower()
    if not app_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    fallback_name = app_name or f"App {app_id}"
    connection.execute(
        """
        INSERT INTO game_profiles (
            app_id, country, platform, app_name, seller, bundle_id, track_url,
            created_at, updated_at, last_collected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(app_id, country) DO UPDATE SET
            platform = excluded.platform,
            app_name = CASE
                WHEN excluded.app_name != '' AND excluded.app_name NOT LIKE 'App %'
                THEN excluded.app_name ELSE game_profiles.app_name END,
            seller = CASE WHEN excluded.seller != '' THEN excluded.seller ELSE game_profiles.seller END,
            bundle_id = CASE WHEN excluded.bundle_id != '' THEN excluded.bundle_id ELSE game_profiles.bundle_id END,
            track_url = CASE WHEN excluded.track_url != '' THEN excluded.track_url ELSE game_profiles.track_url END,
            updated_at = excluded.updated_at,
            last_collected_at = COALESCE(excluded.last_collected_at, game_profiles.last_collected_at)
        """,
        (
            app_id,
            country,
            platform or "App Store",
            fallback_name,
            seller or "",
            bundle_id or "",
            track_url or "",
            now,
            now,
            last_collected_at,
        ),
    )


def upsert_game_profile(
    db_path: Path,
    *,
    app_id: str,
    country: str,
    app_name: str | None = None,
    seller: str | None = None,
    bundle_id: str | None = None,
    track_url: str | None = None,
    platform: str = "App Store",
    last_collected_at: str | None = None,
) -> None:
    init_db(db_path)
    with _connect(db_path) as connection:
        _upsert_game_profile_connection(
            connection,
            app_id=app_id,
            country=country,
            app_name=app_name,
            seller=seller,
            bundle_id=bundle_id,
            track_url=track_url,
            platform=platform,
            last_collected_at=last_collected_at,
        )


def save_reviews(db_path: Path, reviews: list[RawReview], *, connection=None) -> SaveResult:
    # A collector page and its cursor commit together in the worker transaction.
    if connection is None:
        init_db(db_path)
    result = SaveResult(errors=[])
    columns = ['dedupe_key', 'content_hash', 'platform', 'external_id', 'data_source',
               'collected_at', 'app_id', 'country', 'date', 'author', 'title', 'content',
               'rating', 'likes', 'comments', 'shares', 'url', 'note_type', 'topic', 'raw_json']
    with (_connect(db_path) if connection is None else nullcontext(connection)) as connection:
        profiles = {}
        for review in reviews:
            if not review.content.strip():
                result.failed += 1
                result.errors.append('空正文未写入')
                continue
            content_hash = _content_hash(review)
            values = {name: getattr(review, name, None) for name in columns}
            values.update(dedupe_key=_dedupe_key(review, content_hash), content_hash=content_hash,
                          raw_json=json.dumps(review.raw_json or {}, ensure_ascii=False))
            old = connection.execute('SELECT * FROM review_records WHERE dedupe_key=?',
                                     (values['dedupe_key'],)).fetchone()
            if old:
                # Older historical imports must not overwrite a newer review revision.
                old_time = pd.to_datetime(old['date'], errors='coerce', utc=True)
                new_time = pd.to_datetime(review.date, errors='coerce', utc=True)
                if pd.notna(old_time) and (pd.isna(new_time) or new_time < old_time):
                    result.duplicates += 1
                    continue
                if pd.notna(old_time) and old_time==new_time:
                    # RSS offsets and public-list UTC timestamps can denote the same revision.
                    review=replace(review,date=old['date'])
                    if (review.data_source=='app_store_public_review_list' and not review.topic
                            and all(old[k]==getattr(review,k) for k in ('title','rating'))
                            and _without_nonprinting_controls(old['content']) ==
                                _without_nonprinting_controls(review.content)):
                        # Only inherit metadata. Preserve the received body and
                        # its revision history verbatim, including source noise.
                        review=replace(review,topic=old['topic'] or '',likes=old['likes'],
                                       comments=old['comments'],shares=old['shares'])
                    values.update({name:getattr(review,name,None) for name in columns if name not in ('dedupe_key','raw_json','content_hash')})
                    values['content_hash']=_content_hash(review)
                if all(old[k]==values[k] for k in ('platform','author','date','title','content','rating')):
                    # Numeric 1 and 1.0 must not invalidate a review that has not changed.
                    values['content_hash']=old['content_hash']
                changed = any(old[k] != values[k] for k in ('title','content','rating','date','topic'))
                if changed:
                    connection.execute('INSERT INTO review_revisions(review_id,changed_at,previous_json) VALUES(?,?,?)',
                        (old['id'], datetime.now(timezone.utc).isoformat(), json.dumps(dict(old), ensure_ascii=False)))
                    result.updated += 1
                else:
                    result.duplicates += 1
                assignments = ','.join(f'{k}=?' for k in columns if k != 'dedupe_key')
                connection.execute(f'UPDATE review_records SET {assignments} WHERE id=?',
                                   [values[k] for k in columns if k != 'dedupe_key'] + [old['id']])
            else:
                marks = ','.join('?' for _ in columns)
                connection.execute(f"INSERT INTO review_records ({','.join(columns)}) VALUES ({marks})",
                                   [values[k] for k in columns])
                result.inserted += 1
            if review.app_id:
                profiles[(review.app_id,review.country or '')]=(review.platform,review.collected_at)
        for (app_id,country),(platform,collected_at) in profiles.items():
            _upsert_game_profile_connection(connection, app_id=app_id, country=country,
                                            platform=platform, last_collected_at=collected_at)
    return result


def _where_for_game(
    app_id: str | None = None, country: str | None = None
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    if app_id:
        clauses.append("app_id = ?")
        params.append(str(app_id))
    if country:
        clauses.append("country = ?")
        params.append(str(country).lower())
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)


def read_reviews(
    db_path: Path,
    limit: int | None = None,
    app_id: str | None = None,
    country: str | None = None,
    start_at: str | None = None,
    end_at: str | None = None,
) -> pd.DataFrame:
    init_db(db_path)
    where_sql, params = _where_for_game(app_id, country)
    for comparator,value in [('>=',start_at),('<',end_at)]:
        if value is not None:
            where_sql += (' AND ' if where_sql else ' WHERE ') + f'julianday(date) {comparator} julianday(?)'
            params.append(str(value))
    query = """
        SELECT
            id AS review_id, content_hash, platform, date, author, title, content, rating, likes, comments,
            shares, url, note_type, topic, data_source, collected_at, app_id,
            country, external_id
        FROM review_records
    """ + where_sql + """
        ORDER BY COALESCE(julianday(date), julianday(collected_at)) DESC, id DESC
    """
    if limit is not None:
        query += " LIMIT ?"
        params.append(int(limit))
    with _connect(db_path) as connection:
        return pd.read_sql_query(query, connection, params=params)


def review_stats(
    db_path: Path, app_id: str | None = None, country: str | None = None
) -> dict[str, object]:
    init_db(db_path)
    where_sql, params = _where_for_game(app_id, country)
    with _connect(db_path) as connection:
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                MAX(collected_at) AS last_collected_at,
                COUNT(DISTINCT platform) AS platform_count
            FROM review_records
            {where_sql}
            """,
            params,
        ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "last_collected_at": row["last_collected_at"],
            "platform_count": int(row["platform_count"] or 0),
        }


def clear_reviews(
    db_path: Path, app_id: str | None = None, country: str | None = None
) -> int:
    init_db(db_path)
    where_sql, params = _where_for_game(app_id, country)
    with _connect(db_path) as connection:
        row = connection.execute(
            f"SELECT COUNT(*) AS total FROM review_records {where_sql}", params
        ).fetchone()
        total = int(row["total"] or 0)
        connection.execute(f"DELETE FROM review_records {where_sql}", params)
        if app_id:
            connection.execute(
                """
                UPDATE game_profiles
                SET last_collected_at = NULL
                WHERE app_id = ? AND (? = '' OR country = ?)
                """,
                (str(app_id), str(country or ""), str(country or "").lower()),
            )
    return total


def list_game_profiles(db_path: Path) -> pd.DataFrame:
    init_db(db_path)
    with _connect(db_path) as connection:
        return pd.read_sql_query(
            """
            SELECT
                gp.app_id,
                gp.country,
                gp.platform,
                gp.app_name,
                gp.seller,
                gp.bundle_id,
                gp.track_url,
                gp.created_at,
                gp.updated_at,
                COALESCE(MAX(rr.collected_at), gp.last_collected_at) AS last_collected_at,
                COUNT(rr.id) AS total_reviews
            FROM game_profiles gp
            LEFT JOIN review_records rr
              ON rr.app_id = gp.app_id AND COALESCE(rr.country, '') = gp.country
            GROUP BY
                gp.app_id, gp.country, gp.platform, gp.app_name, gp.seller,
                gp.bundle_id, gp.track_url, gp.created_at, gp.updated_at,
                gp.last_collected_at
            ORDER BY total_reviews DESC, gp.updated_at DESC
            """,
            connection,
        )


def get_game_profile(
    db_path: Path, app_id: str | None, country: str | None
) -> dict[str, object] | None:
    if not app_id:
        return None
    profiles = list_game_profiles(db_path)
    if profiles.empty:
        return None
    match = profiles.loc[
        profiles["app_id"].astype(str).eq(str(app_id))
        & profiles["country"].astype(str).eq(str(country or "").lower())
    ]
    if match.empty:
        return None
    return match.iloc[0].to_dict()

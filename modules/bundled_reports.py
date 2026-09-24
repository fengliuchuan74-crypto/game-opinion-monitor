"""Attach a published completed report to databases seeded by that same snapshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from .storage import database


def _load_report(folder, metadata):
    summary = metadata.get('report_summary') or {}
    if summary.get('json_filename') != 'agent-report.json':
        return None
    body = (Path(folder) / 'agent-report.json').read_bytes()
    if hashlib.sha256(body).hexdigest() != summary.get('json_sha256'):
        raise ValueError('随附 Agent 完整报告校验失败，请重新下载完整项目。')
    report = json.loads(body)
    coverage = report.get('coverage') or {}
    if (report.get('model') != summary.get('model')
            or coverage.get('start_at') != summary.get('start_at')
            or coverage.get('end_at') != summary.get('end_at')
            or coverage.get('total') != summary.get('total')
            or coverage.get('analyzed') != summary.get('analyzed')
            or coverage.get('analyzed') != coverage.get('total')
            or not isinstance(coverage.get('total'), int) or coverage['total'] <= 0
            or coverage.get('pending', 0) != 0
            or not report.get('summary') or not report.get('findings')
            or not report.get('prompt_version')):
        raise ValueError('随附 Agent 完整报告与声明的评论范围不一致。')
    start, end = pd.Timestamp(coverage['start_at']), pd.Timestamp(coverage['end_at'])
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError('随附 Agent 完整报告日期范围无效。')
    from .bundled_snapshot import _read_snapshot
    from .agent_analysis import _analysis_signature, _json, _signature
    _, payload = _read_snapshot(Path(folder))
    rows = [dict(row, review_id=row['id']) for row in payload['tables']['review_records']
            if str(row['app_id']) == str(summary['app_id']) and row['country'] == summary['country']
            and start <= pd.Timestamp(row['date']) < end]
    cache = {(row['review_id'], row['content_hash']): json.loads(row['result_json'])
             for row in payload['tables']['agent_review_results']
             if row['model'] == report['model'] and row['reasoning_effort'] == report.get('reasoning_effort', '')
             and row['prompt_version'] == report['prompt_version']}
    original_scope = hashlib.sha256(_json(dict(reviews=_signature(rows), overrides=[])).encode('utf-8')).hexdigest()
    if (len(rows) != coverage['total'] or original_scope != coverage['scope_hash']
            or _analysis_signature(rows, cache) != coverage.get('analysis_hash')):
        raise ValueError('随附 Agent 完整报告与原始评论及逐条分析不匹配。')
    originals = {row['review_id']: row for row in rows}
    for evidence in report.get('evidence', []):
        original = originals.get(evidence.get('review_id'))
        if (not original or evidence.get('content_hash') != original['content_hash']
                or evidence.get('content') != original['content']
                or evidence.get('app_id') != original['app_id'] or evidence.get('country') != original['country']):
            raise ValueError('随附 Agent 报告的证据与原文不符。')
    return report


def restore_completed_report(db_path, folder, existing_metadata):
    """Upgrade older downloads without replacing comments, results or preferences.

    Only a terminal completed run is inserted. The source ID can collide with a
    local job; SQLite assigns a new ID and the report is relinked to that ID.
    """
    path = Path(folder) / 'manifest.json'
    if not path.exists():
        return existing_metadata
    metadata = json.loads(path.read_text(encoding='utf-8'))
    if metadata.get('snapshot_id') != existing_metadata.get('snapshot_id'):
        return existing_metadata
    summary = metadata.get('report_summary') or {}
    digest = summary.get('json_sha256')
    if not digest or summary.get('json_filename') != 'agent-report.json':
        return existing_metadata
    marker_key = 'bundled_report:' + digest
    with database(db_path) as connection:
        marker = connection.execute('SELECT value FROM store_meta WHERE key=?', (marker_key,)).fetchone()
        if marker:
            return existing_metadata
    report = _load_report(folder, metadata)
    if report is None:
        return existing_metadata
    coverage = report['coverage']
    with database(db_path) as connection:
        connection.execute('BEGIN IMMEDIATE')
        marker = connection.execute('SELECT value FROM store_meta WHERE key=?', (marker_key,)).fetchone()
        if marker:
            stored = connection.execute("SELECT value FROM store_meta WHERE key='bundled_snapshot'").fetchone()
            return json.loads(stored[0]) if stored else existing_metadata
        # Require provenance in the actual database, not only a caller's argument.
        stored = connection.execute("SELECT value FROM store_meta WHERE key='bundled_snapshot'").fetchone()
        if not stored or json.loads(stored[0]).get('snapshot_id') != metadata['snapshot_id']:
            return existing_metadata
        # Do not conceal a live job behind a newly appended history entry. Retry
        # this optional migration on a later start after that job finishes.
        active = connection.execute("SELECT 1 FROM agent_runs WHERE app_id=? AND country=? "
            "AND status IN ('queued','running') LIMIT 1", (str(summary['app_id']), summary['country'])).fetchone()
        if active:
            return existing_metadata
        # A terminal 'complete' entry must describe results actually present in
        # this database, otherwise normal retry logic could mistake it for cache.
        from .agent_analysis import _scope, _scope_signature, _report_cache, _analysis_signature
        local_rows = _scope(connection, summary['app_id'], summary['country'], coverage['start_at'], coverage['end_at'])
        local_cache = _report_cache(connection, local_rows, report['model'], report.get('reasoning_effort', ''))
        if (_scope_signature(connection, local_rows) != coverage['scope_hash']
                or _analysis_signature(local_rows, local_cache) != coverage.get('analysis_hash')):
            return existing_metadata
        total = int(coverage['total'])
        created = str(report['generated_at'])
        cursor = connection.execute('''INSERT INTO agent_runs (
            app_id,country,start_at,end_at,model,reasoning_effort,prompt_version,scope_hash,
            status,stage,total,selected,processed,initial_processed,max_reviews,snapshot_json,
            created_at,started_at,finished_at)
            VALUES(?,?,?,?,?,?,?,?, 'completed','随附历史分析（已完成）',?,?,?,?,0,'[]',?,?,?)''',
            (str(summary['app_id']), summary['country'], coverage['start_at'], coverage['end_at'],
             report['model'], report.get('reasoning_effort', ''), report['prompt_version'], coverage['scope_hash'],
             total,total,total,total,created,created,created))
        run_id = int(cursor.lastrowid)
        report['source_run_id'] = report.get('run_id')
        report['run_id'] = run_id
        report['bundled_snapshot_id'] = metadata['snapshot_id']
        connection.execute('UPDATE agent_runs SET report_json=? WHERE id=?',
                           (json.dumps(report, ensure_ascii=False), run_id))
        updated = dict(existing_metadata, report_summary=summary, bundled_report_run_id=run_id)
        connection.execute("UPDATE store_meta SET value=? WHERE key='bundled_snapshot'",
                           (json.dumps(updated, ensure_ascii=False),))
        connection.execute('INSERT INTO store_meta(key,value) VALUES(?,?)', (marker_key, str(run_id)))
        connection.execute("UPDATE agent_meta SET value=value+1 WHERE key='revision'")
    return updated

"""Export explicitly selected public App Store data, never an entire runtime DB."""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules.bundled_snapshot import TABLES, MAX_PAYLOAD_BYTES
from modules.agent_analysis import PROMPT_VERSION, _scope, _scope_signature, _report_cache, _analysis_signature
from modules.version_history import _load_cache
from collectors.app_store import safe_artwork_url

PUBLIC_SOURCES = {'app_store_public_feed', 'app_store_public_review_list'}
LOCAL_TZ = timezone(timedelta(hours=8))
ICON_COLUMNS = ('app_id', 'country', 'data_uri', 'source_url', 'next_check')
VERSION_COLUMNS = ('app_id', 'country', 'checked_at', 'releases', 'current_version',
                   'error', 'source_url', 'history_complete')
RELEASE_COLUMNS = ('version', 'released_at', 'precision', 'source_url')
REPORT_COLUMNS = ('summary', 'findings', 'positive', 'limitations', 'coverage', 'evidence', 'stats',
                  'investigations', 'run_id', 'model', 'reasoning_effort', 'prompt_version', 'generated_at')
COVERAGE_COLUMNS = ('all_reviews', 'analysis_hash', 'analyzed', 'end_at', 'manual_reviewed', 'max_reviews',
                    'pending', 'sampling', 'scope_hash', 'selected', 'snapshot_total', 'start_at', 'total')
FINDING_COLUMNS = ('actions', 'category', 'evidence_ids', 'hypothesis', 'observation', 'owner', 'title', 'validation')
EVIDENCE_COLUMNS = ('analysis', 'app_id', 'content', 'content_hash', 'country', 'date', 'platform',
                    'rating', 'review_id', 'title', 'topic', 'url')
ANALYSIS_COLUMNS = ('content_hash', 'demand', 'evidence_quote', 'issue_categories', 'issue_category',
                    'needs_review', 'reason', 'review_id', 'sentiment_label', 'target', 'analysis_source')
STATS_COLUMNS = ('analyzed_average_rating', 'average_rating', 'categories', 'daily', 'low_ratings',
                 'manual_reviewed', 'rated_total', 'rating_population', 'sentiments', 'uncertain')
INVESTIGATION_COLUMNS = ('query', 'current', 'previous', 'previous_start', 'previous_end', 'limitation')
SENSITIVE = re.compile(r'(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{20,}|'
                       r'github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----|'
                       r'[A-Za-z]:[\\/]Users[\\/])')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def stamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    require(result.tzinfo is not None, '导出日期必须包含时区')
    return result.astimezone(timezone.utc)


def local_date(value):
    return stamp(value).astimezone(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')


def safe_text(value):
    # Model prose and review text stay text; embedded Markdown cannot load images.
    text = html.escape(str(value or ''), quote=False)
    return re.sub(r'([\\`*_{}\[\]#!|])', r'\\\1', text)


def prose(value):
    if isinstance(value, list):
        return '\n'.join('- ' + safe_text(item) for item in value)
    return safe_text(value)


def report_markdown(run, report, profile):
    coverage = report['coverage']
    lines = [f"# {safe_text(profile['app_name'])} · 已完成 Agent 历史报告", '',
             '这是一份随评论快照保存的历史分析，不是实时监控结果。新评论及之后发生的变化未纳入本报告。', '',
             f"- 地区：{run['country'].upper()}",
             f"- 分析范围（北京时间）：{local_date(run['start_at'])} — {local_date(run['end_at'])}（结束时刻不含）",
             f"- 分析覆盖：{coverage['analyzed']} / {coverage['total']} 条；待分析 {coverage.get('pending', 0)} 条",
             f"- 模型：{safe_text(run['model'])}；强度：{safe_text(run['reasoning_effort'] or '默认')}",
             f"- 报告生成（北京时间）：{local_date(report['generated_at'])}", '',
             '## 摘要', '', prose(report['summary']), '',
             '## 正向体验', '', prose(report.get('positive')), '', '## 问题发现与处理步骤', '']
    for index, finding in enumerate(report['findings'], 1):
        lines += [f"### {index}. {safe_text(finding['title'])}", '',
                  f"类别：{safe_text(finding['category'])}", '',
                  '**观察：** ' + safe_text(finding['observation']), '',
                  '**待验证原因：** ' + safe_text(finding['hypothesis']), '',
                  '**建议协作：** ' + safe_text(finding['owner']), '', '**处理步骤：**', '']
        lines += [f'{n}. {safe_text(action)}' for n, action in enumerate(finding['actions'], 1)]
        lines += ['', '**验收方式：** ' + safe_text(finding['validation']), '',
                  '原文证据编号：' + '、'.join(str(value) for value in finding.get('evidence_ids', [])), '']
    lines += ['## 原文证据', '', '以下保留报告已选用的公开评论及分析，编号对应上述发现。', '']
    for review in report['evidence']:
        analysis = review['analysis']
        lines += [f"### 评论 #{review['review_id']} · {review['rating']} 星", '',
                  f"发布时间（北京时间）：{local_date(review['date'])}", '',
                  '**标题：** ' + safe_text(review.get('title')), '']
        lines += [('> ' + safe_text(line)).rstrip() for line in str(review['content']).splitlines()]
        lines += ['', '**判断理由：** ' + safe_text(analysis['reason']), '',
                  '**具体诉求：** ' + safe_text(analysis['demand']), '',
                  '**原文依据：** ' + safe_text(analysis['evidence_quote']), '']
    lines += ['## 分析边界', '', prose(report.get('limitations')), '',
              '公开评论是已采集样本，不能代表全部玩家或完整商店历史。模型判断和原因假设仍需结合业务记录人工核实。', '']
    return '\n'.join(lines).encode('utf-8')


def public_report(report):
    """Copy display data through a nested allowlist, never task/runtime records."""
    def pick(value, keys):
        return {key: value[key] for key in keys if key in value}
    def evidence(value):
        row = pick(value, EVIDENCE_COLUMNS)
        row['analysis'] = pick(value['analysis'], ANALYSIS_COLUMNS)
        return row
    def investigation_summary(value):
        summary = pick(value, ('total', 'classified', 'pending', 'matching', 'daily'))
        summary['evidence'] = [evidence(row) for row in value.get('evidence', [])]
        return summary
    clean = pick(report, REPORT_COLUMNS)
    clean['coverage'] = pick(report['coverage'], COVERAGE_COLUMNS)
    clean['findings'] = [pick(row, FINDING_COLUMNS) for row in report['findings']]
    clean['evidence'] = [evidence(row) for row in report['evidence']]
    clean['stats'] = pick(report.get('stats', {}), STATS_COLUMNS)
    clean['stats']['daily'] = {day: pick(value, ('total', 'sentiments', 'categories'))
                               for day, value in report.get('stats', {}).get('daily', {}).items()}
    clean['investigations'] = []
    for item in report.get('investigations', []):
        investigation = pick(item, INVESTIGATION_COLUMNS)
        investigation['query'] = pick(item.get('query', {}), ('question', 'category', 'review_ids'))
        for scope in ('current', 'previous'):
            if scope in item:
                investigation[scope] = investigation_summary(item[scope])
        clean['investigations'].append(investigation)
    return clean


def public_tables(connection):
    bad = connection.execute("SELECT COUNT(*) FROM review_records WHERE platform!='App Store' "
        "OR data_source NOT IN ('app_store_public_feed','app_store_public_review_list') "
        "OR platform IS NULL OR data_source IS NULL").fetchone()[0]
    require(bad == 0, f'发现 {bad} 条非公开 App Store 来源记录，拒绝静默过滤')
    tables = {}
    for table in ('review_records', 'game_profiles', 'agent_settings'):
        tables[table] = [dict(row) for row in connection.execute(
            f"SELECT {','.join(TABLES[table])} FROM {table} ORDER BY " +
            ('id' if table == 'review_records' else 'app_id,country'))]
    reviews = {row['id']: row for row in tables['review_records']}
    require(bool(reviews), '没有可导出的公开评论')
    pairs = {(row['app_id'], row['country']) for row in reviews.values()}
    for table in ('game_profiles', 'agent_settings'):
        tables[table] = [row for row in tables[table] if (row['app_id'], row['country']) in pairs]
    require({(row['app_id'], row['country']) for row in tables['game_profiles']} == pairs,
            '评论缺少游戏档案')
    verification = {'raw_identity_checked': 0, 'normalized_only': 0}
    for source in connection.execute('SELECT id,raw_json FROM review_records'):
        review = reviews[source['id']]
        require(review['external_id'], '公开评论缺少外部 ID')
        raw = json.loads(source['raw_json'] or '{}')
        if raw:
            identity = (raw.get('id', {}).get('label') if review['data_source'] == 'app_store_public_feed'
                        else raw.get('review', {}).get('userReviewId'))
            require(str(identity) == str(review['external_id']), '公开来源原始 ID 与记录不一致')
            verification['raw_identity_checked'] += 1
        else:
            # Downloaded snapshots intentionally omit raw payloads. Their public
            # source tag and external ID remain exportable without inventing proof.
            verification['normalized_only'] += 1
        stamp(review['date'])
    settings = {(row['app_id'], row['country']): row for row in tables['agent_settings']}
    tables['agent_review_results'] = []
    for row in connection.execute(f"SELECT {','.join(TABLES['agent_review_results'])} "
                                  'FROM agent_review_results ORDER BY review_id,model,reasoning_effort'):
        result = dict(row)
        review = reviews.get(result['review_id'])
        setting = settings.get((review['app_id'], review['country'])) if review else None
        if (not setting or result['content_hash'] != review['content_hash']
                or result['model'] != setting['model'] or result['reasoning_effort'] != setting['reasoning_effort']
                or result['prompt_version'] != PROMPT_VERSION):
            continue
        detail = json.loads(result['result_json'])
        require(detail.get('review_id') == review['id'] and detail.get('content_hash') == review['content_hash'],
                'Agent 结果内容与关联评论不一致')
        tables['agent_review_results'].append(result)
    return tables, verification


def public_caches(data_dir, profiles):
    caches = {'app_versions': {}, 'app_icons': {}}
    for profile in profiles:
        app_id, country = profile['app_id'], profile['country']
        name = f'{app_id}_{country}.json'
        require(re.fullmatch(r'[0-9]+_[a-z]{2}\.json', name), '缓存归属非法')
        version_path = data_dir / 'app_versions' / name
        if version_path.exists():
            version = _load_cache(version_path, app_id, country)
            require(version is not None, '现有版本缓存校验失败')
            clean = {key: version[key] for key in VERSION_COLUMNS}
            clean['releases'] = [{key: row[key] for key in RELEASE_COLUMNS} for row in version['releases']]
            if clean['error']:
                clean['error'] = '版本资料来自历史缓存；导出过程未联网刷新。'
            caches['app_versions'][name] = clean
        icon_path = data_dir / 'app_icons' / name
        if icon_path.exists():
            icon = json.loads(icon_path.read_text(encoding='utf-8'))
            require(icon.get('app_id') == app_id and icon.get('country') == country, '图标缓存归属不符')
            uri = str(icon.get('data_uri') or '')
            if not uri:
                continue
            require(uri.startswith('data:image/png;base64,'), '图标必须是已缓存 PNG')
            image = base64.b64decode(uri.split(',', 1)[1], validate=True)
            require(image.startswith(b'\x89PNG\r\n\x1a\n'), '图标 PNG 文件头无效')
            require(bool(safe_artwork_url(icon.get('source_url'))), '图标来源不是公开 Apple 图片地址')
            caches['app_icons'][name] = {key: icon[key] for key in ICON_COLUMNS}
    return caches


def completed_report(connection, tables):
    settings = {(row['app_id'], row['country']): row for row in tables['agent_settings']}
    profiles = {(row['app_id'], row['country']): row for row in tables['game_profiles']}
    for raw_run in connection.execute("SELECT * FROM agent_runs WHERE status='completed' "
                                      "AND report_json IS NOT NULL ORDER BY id DESC"):
        run = dict(raw_run)
        key = (run['app_id'], run['country'])
        setting = settings.get(key)
        if (not setting or run['model'] != setting['model'] or run['reasoning_effort'] != setting['reasoning_effort']
                or run['prompt_version'] != PROMPT_VERSION):
            continue
        report = json.loads(run['report_json'])
        scope = _scope(connection, *key, run['start_at'], run['end_at'])
        coverage = report['coverage']
        cache = _report_cache(connection, scope, run['model'], run['reasoning_effort'])
        if (_scope_signature(connection, scope) != coverage['scope_hash']
                or _analysis_signature(scope, cache) != coverage.get('analysis_hash')
                or coverage['total'] != len(scope) or coverage['analyzed'] != len(scope)):
            continue
        text = report_markdown(run, report, profiles[key])
        structured = json.dumps(public_report(report), ensure_ascii=False, sort_keys=True, indent=2).encode('utf-8') + b'\n'
        summary = {name: run[name] for name in ('app_id', 'country', 'start_at', 'end_at', 'model')}
        summary.update(filename='agent-report.md', sha256=hashlib.sha256(text).hexdigest(),
                       json_filename='agent-report.json', json_sha256=hashlib.sha256(structured).hexdigest(),
                       total=coverage['total'], analyzed=coverage['analyzed'])
        return text, summary, structured
    return None, None, None


def export_snapshot(database, output, *, preserve_snapshot=False):
    database, output = database.resolve(), output.resolve()
    require(database.is_file(), '必须提供已存在的评论数据库')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=15)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        tables, verification = public_tables(connection)
        report, report_summary, structured_report = completed_report(connection, tables)
        connection.rollback()
    caches = public_caches(database.parent, tables['game_profiles'])
    payload = {'format_version': 1, 'tables': tables, 'caches': caches}
    unpacked = encoded(payload)
    require(len(unpacked) <= MAX_PAYLOAD_BYTES, '快照超出应用允许的大小')
    require(not SENSITIVE.search(unpacked.decode('utf-8')), '快照出现明显凭据或机器路径，拒绝导出')
    if report:
        require(not SENSITIVE.search(report.decode('utf-8')), '报告出现明显凭据或机器路径，拒绝导出')
        require(not SENSITIVE.search(structured_report.decode('utf-8')), '结构化报告出现明显凭据或机器路径，拒绝导出')
    packed = gzip.compress(unpacked, mtime=0)
    if preserve_snapshot:
        require((output / 'snapshot.json.gz').read_bytes() == packed, '现有快照与源库不同，拒绝修改已发布快照')
        if report:
            require((output / 'agent-report.md').read_bytes() == report, '历史 Markdown 内容发生变化，拒绝改写')
    digest = hashlib.sha256(packed).hexdigest()
    created = datetime.now(timezone.utc).isoformat()
    reviews = tables['review_records']
    games = []
    for profile in tables['game_profiles']:
        selected = [row for row in reviews if (row['app_id'], row['country']) == (profile['app_id'], profile['country'])]
        dates = [stamp(row['date']) for row in selected]
        games.append({'app_id': profile['app_id'], 'country': profile['country'], 'name': profile['app_name'],
                      'count': len(selected), 'earliest_review_at': min(dates).isoformat(),
                      'latest_review_at': max(dates).isoformat()})
    preferred = max(games, key=lambda row: row['count'])
    dates = [stamp(row['date']) for row in reviews]
    manifest = {'format_version': 1, 'sha256': digest, 'snapshot_id': 'appstore-public-' + digest[:16],
                'created_at': created, 'review_count': len(reviews), 'game_count': len(games),
                'source_verification': verification,
                'agent_result_count': len(tables['agent_review_results']),
                'analyzed_review_count': len({row['review_id'] for row in tables['agent_review_results']}),
                'preferred_app_id': preferred['app_id'], 'preferred_country': preferred['country'],
                'earliest_review_at': min(dates).isoformat(), 'latest_review_at': max(dates).isoformat(), 'games': games}
    if report_summary:
        manifest['report_summary'] = report_summary
    lines = ['# 已采集公开评论快照', '',
             f"导出时间（UTC）：{created}。共 {len(reviews):,} 条 App Store 公开评论，覆盖 {len(games)} 个游戏/地区，",
             f"附带 {manifest['analyzed_review_count']} 条当前所选模型的有效 Agent 逐条分析。", '',
             '这些是历史已采集样本，不是实时数据，也不代表完整商店历史或全部玩家意见。', '',
             '| 游戏 | 地区 | 评论数 | 最早评论（北京时间） | 最新评论（北京时间） |',
             '|---|---|---:|---|---|']
    lines += [f"| {safe_text(g['name'])} | {g['country']} | {g['count']} | {local_date(g['earliest_review_at'])} | {local_date(g['latest_review_at'])} |" for g in games]
    lines += ['', '## 内容与来源', '',
              '- `snapshot.json.gz`：明确字段白名单的评论、游戏档案、已保存 Agent 结果及展示用模型设置。',
              '- `manifest.json`：快照条数、范围、SHA-256 校验和、默认游戏及报告索引。',
              '- 仅包含 Apple 公开 RSS 和公开评论列表的记录；保留公开昵称、原文、星级、时间、评论 ID 和来源。',
              f"- 来源校验：{verification['raw_identity_checked']} 条与原始响应 ID 核对；{verification['normalized_only']} 条仅核对规范化来源和外部 ID（原始响应未保留）。",
              '- 不包含原始响应 JSON、登录信息、任务队列、巡检计划、租约、运行日志、失败任务或机器路径。',
              '- 图片仅使用已缓存的官方 PNG；版本历史保留原观测时刻，按日期推定不等于评论者实际安装版本。',
              '- 首次空白数据库可初始化此快照；已有本地数据不会被覆盖。自动采集及自动分析仍默认关闭。', '']
    if report_summary:
        lines += ['## 已完成历史报告', '',
                  '[阅读 Agent 历史分析报告](agent-report.md)。该报告在导出时通过评论范围和分析结果哈希校验，',
                  f"覆盖 {report_summary['analyzed']} / {report_summary['total']} 条。报告只作为已完成历史成果，不会发起模型请求。", '',
                  '`agent-report.json` 保存同一份报告的结构化内容；完整 Agent 历史看板直接读取这份文件，',
                  '展示深度结论、处理步骤与代表评论，无需在新电脑重新调用模型。它沿用报告原始时间范围和模型标记，',
                  '与本机当前模型选择、实时统计日期分开，避免把历史分析当成新的实时报告。', '']
    lines += ['## 重复导出', '', '在仓库根目录使用已安装依赖的 Python，明确指定源数据库与输出目录：', '',
              '```powershell', 'python -X utf8 -B tools/export_public_snapshot.py --database "path/to/reviews.sqlite3" --output bundled_data',
              '```', '', '导出对数据库使用只读事务；不访问网络，不改变原始数据。来源不符合公开 App Store 要求时会报错，',
              '不会静默删除记录。压缩载荷使用固定 gzip 时间戳，相同内容可重现相同校验和。', '']
    output.mkdir(parents=True, exist_ok=True)
    (output / 'snapshot.json.gz').write_bytes(packed)
    (output / 'README.md').write_text('\n'.join(lines), encoding='utf-8')
    if report:
        (output / 'agent-report.md').write_bytes(report)
        (output / 'agent-report.json').write_bytes(structured_report)
    else:
        (output / 'agent-report.md').unlink(missing_ok=True)
        (output / 'agent-report.json').unlink(missing_ok=True)
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'review_count': manifest['review_count'], 'game_count': manifest['game_count'],
            'source_verification': verification,
            'agent_result_count': manifest['agent_result_count'], 'analyzed_review_count': manifest['analyzed_review_count'],
            'icons': len(caches['app_icons']), 'version_histories': len(caches['app_versions']),
            'compressed_bytes': len(packed), 'uncompressed_bytes': len(unpacked), 'sha256': digest,
            'report_summary': report_summary}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path, help='源评论数据库，只读打开')
    parser.add_argument('--output', required=True, type=Path, help='待发布的 bundled_data 输出目录')
    parser.add_argument('--preserve-snapshot', action='store_true', help='仅允许保持既有快照和 Markdown 字节不变的导出')
    args = parser.parse_args()
    print(json.dumps(export_snapshot(args.database, args.output, preserve_snapshot=args.preserve_snapshot), ensure_ascii=False, indent=2))

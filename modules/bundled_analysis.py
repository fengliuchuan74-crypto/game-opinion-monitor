"""Build the published historical Agent dashboard without a local database."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .bundled_reports import _load_report
from .bundled_snapshot import _read_snapshot
from .dashboard import region_comparison
from .operations import LOCAL_TZ, analyze_reviews, snapshot
from .version_attribution import attribute_versions


def _agent_data(raw, records, report):
    """Apply this report's saved interpretations without consulting model settings."""
    data = analyze_reviews(raw, None)
    # Rule fields remain available for audit, but unanalysed historical rows must
    # not contribute rule judgments to an Agent dashboard or its comparison.
    pending = dict(sentiment_label='待复核', sentiment_score=float('nan'),
        sentiment_keywords='', issue_category='未归类/待复核', issue_categories='未归类/待复核',
        issue_keywords='', analysis_basis='Agent 尚未分析；保留原文与星级，暂不判断情绪和问题类别',
        needs_review=True, analysis_source='待分析')
    for name, value in pending.items():
        data[name] = value
    cache = {(int(row['review_id']), row['content_hash']): json.loads(row['result_json'])
             for row in records if row['model'] == report['model']
             and row['reasoning_effort'] == report.get('reasoning_effort', '')
             and row['prompt_version'] == report['prompt_version']}
    for idx, row in data.iterrows():
        record = cache.get((int(row['review_id']), row['content_hash']))
        if record is None:
            continue
        assignments = dict(sentiment_label=record['sentiment_label'],
            sentiment_score={'好评': 1.0, '中评': 0.0, '差评': -1.0, '待复核': float('nan')}[record['sentiment_label']],
            sentiment_keywords='', issue_category=record['issue_category'],
            issue_categories='；'.join(record['issue_categories']), issue_keywords='',
            analysis_basis='Agent：' + record['reason'], needs_review=record['needs_review'],
            agent_analyzed=True, agent_uncertain=record['needs_review'], analysis_source='Agent',
            agent_status='待复核' if record['needs_review'] else '已分析',
            agent_reason=record['reason'], agent_demand=record['demand'], agent_target=record['target'],
            agent_quote=record['evidence_quote'], agent_model=report['model'])
        for name, value in assignments.items():
            data.at[idx, name] = value
    return data.sort_values(['date', 'review_id'], ascending=False, kind='stable')


def load_bundled_analysis(folder: Path) -> dict:
    """Return the verified snapshot's fixed historical dashboard, entirely offline.

    The bundle determines the game, region, window, model and version history.
    Local reviews, preferences, job queues and installation markers are neither
    read nor written. Callers may cache the returned independent objects.
    """
    folder = Path(folder)
    try:
        metadata, payload = _read_snapshot(folder)
        report = _load_report(folder, metadata)
    except (OSError, UnicodeError, KeyError, TypeError) as exc:
        raise ValueError('随附历史分析文件缺失或格式无效，请重新下载完整项目。') from exc
    if report is None:
        raise ValueError('随附文件中没有完整 Agent 历史报告，请重新下载完整项目。')
    summary = metadata['report_summary']
    app_id, country = str(summary['app_id']), str(summary['country'])
    profiles = [row for row in payload['tables']['game_profiles']
                if str(row['app_id']) == app_id and row['country'] == country]
    if len(profiles) != 1:
        raise ValueError('随附 Agent 历史报告没有唯一匹配的游戏及地区。')
    profile = dict(profiles[0])
    raw = pd.DataFrame([row for row in payload['tables']['review_records']
                        if str(row['app_id']) == app_id]).rename(columns={'id': 'review_id'})
    start = pd.Timestamp(report['coverage']['start_at']).tz_convert(LOCAL_TZ)
    end = pd.Timestamp(report['coverage']['end_at']).tz_convert(LOCAL_TZ)
    dates = pd.to_datetime(raw['date'], utc=True, errors='coerce', format='mixed')
    primary_dates = dates.loc[raw['country'].eq(country)]
    # Validation covers the entire published snapshot; expensive rule-field
    # preparation only needs rows displayed in the two comparison windows.
    raw = raw.loc[dates.ge(start - (end - start)) & dates.lt(end)].copy()
    data = _agent_data(raw, payload['tables']['agent_review_results'], report)
    histories = payload.get('caches', {}).get('app_versions', {})
    data = pd.concat([attribute_versions(group, histories.get(f'{app_id}_{region}.json', {}))
                      for region, group in data.groupby('country', sort=False)]).sort_index()
    scoped = data.loc[data['country'].eq(country)].sort_values(
        ['date', 'review_id'], ascending=False, kind='stable')
    view = snapshot(scoped, start=start, end=end, now=end)
    current = view['current']
    if (len(current) != report['coverage']['total']
            or int(current['agent_analyzed'].sum()) != report['coverage']['analyzed']):
        raise ValueError('随附 Agent 历史报告与可展示的评论及逐条分析数量不一致。')
    report.update(from_bundle=True, bundled_snapshot_id=metadata['snapshot_id'],
                  stale=False, stale_reason='')
    view.update(analysis_mode='Agent分析', agent_report=report, from_bundle=True, bundled_history=True,
                bundled_snapshot_id=metadata['snapshot_id'],
                undated=int(primary_dates.isna().sum()), future=int(primary_dates.gt(end).sum()))
    countries = [row['country'] for row in payload['tables']['game_profiles']
                 if str(row['app_id']) == app_id]
    regions = region_comparison(data, app_id, countries, start, end)
    icon = payload.get('caches', {}).get('app_icons', {}).get(f'{app_id}_{country}.json', {})
    return dict(metadata=metadata, profile=profile, view=view, regions=regions, icon=icon)

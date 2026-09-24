"""Auditable aggregates for the App Store dashboard and its exports."""
from __future__ import annotations

from collections import Counter
import re

import pandas as pd

from .keyword_extractor import build_dynamic_stopwords, tokenize
from .operations import LOCAL_TZ, SENTIMENTS

UNCLASSIFIED = '未归类/待复核'
SENTIMENT_COLORS = {'好评':'#50debe', '中评':'#69b7f5', '差评':'#f47ba3', '待复核':'#b7a4e8'}


def sentiments(data):
    counts = data['sentiment_label'].where(data['sentiment_label'].isin(SENTIMENTS), '待复核').value_counts()
    result = pd.DataFrame({'情绪': SENTIMENTS, '评论数': [int(counts.get(s, 0)) for s in SENTIMENTS]})
    result['占比'] = result['评论数'] / len(data) * 100 if len(data) else float('nan')
    return result


def ratings(data):
    rated = data.loc[data['rating'].between(1, 5)]
    result = pd.DataFrame({'星级': [f'{i} 星' for i in range(1, 6)],
                           '评论数': [int(rated['rating'].eq(i).sum()) for i in range(1, 6)]})
    result['占比'] = result['评论数'] / len(rated) * 100 if len(rated) else float('nan')
    return result


def negative_rows(data):
    return data.loc[data['rating'].between(1, 2) | data['sentiment_label'].eq('差评')]


def categories_for(row):
    value = row.get('issue_categories', '')
    categories = [s.strip() for s in str(value).split('；') if s.strip()] if pd.notna(value) else []
    primary = row.get('issue_category')
    if not categories and pd.notna(primary) and str(primary).strip():
        categories = [str(primary).strip()]
    return list(dict.fromkeys(categories or [UNCLASSIFIED]))


def category_mask(data, category):
    return data.apply(lambda row: category in categories_for(row), axis=1).astype(bool)


def category_counts(current, previous):
    def counts(data):
        return Counter(c for _, row in data.iterrows() for c in categories_for(row))
    now, before = counts(current), counts(previous)
    rows = [{'类别': c, '本期': now[c], '上期': before[c], '变化条数': now[c]-before[c],
             '本期占比': round(now[c]/len(current)*100, 1) if len(current) else None}
            for c in now.keys() | before.keys()]
    return pd.DataFrame(rows, columns=['类别','本期','上期','变化条数','本期占比']).sort_values(
        ['本期','上期','类别'], ascending=[False,False,True], ignore_index=True)


def keyword_counts(data, app_name='', top_n=20):
    """Document frequency: repeating a word within one review counts once."""
    counter = Counter()
    stopwords = build_dynamic_stopwords(app_name)
    for _, row in data.iterrows():
        text = ' '.join(str(row.get(k, '')) if pd.notna(row.get(k)) else '' for k in ['title', 'content'])
        counter.update(set(tokenize(text, extra_stopwords=stopwords)))
    pairs = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:top_n]
    result = pd.DataFrame(pairs, columns=['关键词','提及评论数'])
    result['占比'] = result['提及评论数']/len(data)*100 if len(data) else float('nan')
    return result


def version_label(value):
    match = re.search(r'(?:version|ver|版本)\s*[:：]\s*([^\s,，;；]+)', str(value or ''), re.I)
    return match.group(1) if match else '版本未知'


def _summary(data):
    rated = data.loc[data['rating'].between(1, 5)]
    return {'评论数':len(data), '有效星级':len(rated),
            '低星数':int(rated['rating'].le(2).sum()),
            '低星占比':float(rated['rating'].le(2).mean()*100) if len(rated) else None,
            '平均星级':float(rated['rating'].mean()) if len(rated) else None,
            **{s:int(data['sentiment_label'].eq(s).sum()) for s in SENTIMENTS}}


def versions(data):
    tagged = data.copy()
    original = tagged.get('topic',pd.Series('',index=tagged.index,dtype=str)).map(version_label)
    if 'version_display' in tagged:
        tagged['版本'] = tagged['version_display'].fillna('').astype(str).str.strip().replace('', '版本未知')
    else:
        tagged['版本'] = original
    basis = tagged.get('version_basis',pd.Series(index=tagged.index,dtype=str))
    basis = basis.fillna(original.map(lambda version:'source' if version!='版本未知' else 'unknown'))
    known = tagged['版本'].ne('版本未知')
    tagged['_version_source'] = basis.eq('source') & known
    tagged['_version_inferred'] = basis.eq('date_inferred') & known
    rows = [{'版本':v, **_summary(group),'来源提供':int(group['_version_source'].sum()),
             '按日期推定':int(group['_version_inferred'].sum())} for v, group in tagged.groupby('版本', sort=False)]
    return pd.DataFrame(rows, columns=['版本','评论数','来源提供','按日期推定','有效星级','低星数','低星占比','平均星级',*SENTIMENTS]).sort_values(
        '评论数', ascending=False, kind='stable', ignore_index=True)


def region_comparison(data, app_id, countries, start, end):
    """Same app only; a region with no sample is unknown, never zero percent."""
    if data.empty:
        scoped = data
    else:
        dates = pd.to_datetime(data['date'], utc=True, errors='coerce')
        scoped = data.loc[data['app_id'].astype(str).eq(str(app_id)) & dates.ge(start) & dates.lt(end)]
    rows = []
    for country in sorted(set(countries)):
        group = scoped.loc[scoped['country'].eq(country)] if len(scoped) else scoped
        rows.append({'地区':country.upper(), **_summary(group)})
    return pd.DataFrame(rows)


def _periods(dates, grain):
    return dates.dt.tz_convert(LOCAL_TZ).dt.tz_localize(None).dt.to_period(
        {'按日':'D', '按周':'W-SUN', '按月':'M'}[grain])


def timeline(data, start, end, grain='按日'):
    """Fill absent buckets with zero counts and missing rates, in China time."""
    bounds = pd.Series(pd.to_datetime([start, pd.Timestamp(end)-pd.Timedelta(nanoseconds=1)], utc=True))
    bound_periods = _periods(bounds, grain)
    buckets = pd.period_range(bound_periods.iloc[0], bound_periods.iloc[1], freq=bound_periods.iloc[0].freq)
    dates = pd.to_datetime(data['date'], utc=True, errors='coerce')
    tagged = data.loc[dates.ge(start)&dates.lt(end)].copy()
    tagged['周期'] = _periods(dates.loc[tagged.index], grain)
    groups = {period: group for period, group in tagged.groupby('周期')}
    empty = data.iloc[:0]
    rows = []
    for period in buckets:
        label = period.start_time.strftime('%Y-%m' if grain=='按月' else '%Y-%m-%d')
        rows.append({'时间':label, **_summary(groups.get(period, empty))})
    return pd.DataFrame(rows)


def category_timeline(data, start, end, categories, grain='按日'):
    labels=timeline(data,start,end,grain)['时间'].tolist()
    dates=pd.to_datetime(data['date'],utc=True,errors='coerce')
    scoped=data.loc[dates.ge(start)&dates.lt(end)].copy()
    scoped['bucket']=_periods(pd.to_datetime(scoped['date'],utc=True),grain).dt.start_time.dt.strftime(
        '%Y-%m' if grain=='按月' else '%Y-%m-%d')
    counts=Counter((row['bucket'],category) for _,row in scoped.iterrows() for category in categories_for(row))
    result=[{'时间':label,'类别':category,'评论数':counts[(label,category)]} for category in categories for label in labels]
    return pd.DataFrame(result, columns=['时间','类别','评论数'])


def day_reviews(data, day, start, end):
    """Local calendar day intersected with the selected window, end exclusive."""
    day_start=pd.Timestamp(day)
    day_start=day_start.tz_localize(LOCAL_TZ) if day_start.tzinfo is None else day_start.tz_convert(LOCAL_TZ)
    day_start=day_start.normalize()
    lower=max(day_start,pd.Timestamp(start))
    upper=min(day_start+pd.Timedelta(days=1),pd.Timestamp(end))
    dates=pd.to_datetime(data['date'],utc=True,errors='coerce')
    return data.loc[dates.ge(lower)&dates.lt(upper)].copy()


def chart_tables(profile, view):
    data = view['current']
    categories=category_counts(data,data.iloc[:0])['类别'].tolist()
    return {'核心指标':pd.DataFrame([view['metrics']]), '情绪分布':sentiments(data), '星级分布':ratings(data),
            '每日趋势':timeline(data, view['start'], view['end']), '版本分析':versions(data),
            '每日问题类别':category_timeline(data,view['start'],view['end'],categories),
            '全部主题类别':category_counts(data,view['previous']),
            '负面问题类别':category_counts(negative_rows(data), negative_rows(view['previous'])),
            '关键词':keyword_counts(data, profile['app_name'], top_n=30)}

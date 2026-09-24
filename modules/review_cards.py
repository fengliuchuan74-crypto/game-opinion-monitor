"""Escaped, source-grounded review cards shared by overview and daily detail."""
from __future__ import annotations
import html
from urllib.parse import urlparse
import pandas as pd
from .dashboard import categories_for,category_mask,negative_rows
from .operations import LOCAL_TZ, PLAYBOOKS
from .version_attribution import review_version_text


def featured_reviews(data):
    def representative(rows):
        rows=rows.sort_values('date',ascending=False)
        diverse=rows.drop_duplicates('issue_category').head(5)
        return pd.concat([diverse,rows.loc[~rows['review_id'].isin(diverse['review_id'])]]).head(5)
    negative=negative_rows(data)
    content_categories={'内容/玩法反馈','角色/剧情争议','养成/进度反馈','可玩性/内容单薄','社区二创/热梗'}
    content=data.loc[data.apply(lambda row:bool(set(categories_for(row))&content_categories),axis=1).astype(bool)]
    reliable_positive=data['sentiment_label'].eq('好评') & ~data['needs_review'].fillna(True).astype(bool)
    return {'典型好评 Top 5':representative(data.loc[reliable_positive]),
        '典型差评 Top 5':representative(data.loc[data['sentiment_label'].eq('差评')]),
        '最新负面 Top 5':negative.sort_values('date',ascending=False).head(5),
        '内容与玩法 Top 5':representative(content)}


def _text(value,default=''):
    return default if value is None or pd.isna(value) else str(value)


def card_html(row):
    esc=html.escape
    source=_text(row.get('analysis_source'),'规则初筛') or '规则初筛'
    waiting=source in {'待分析','待Agent分析'} or _text(row.get('agent_pending')).lower()=='true'
    sentiment=_text(row.get('sentiment_label'),'待复核')
    if waiting: sentiment='待复核'
    kind={'好评':'good','差评':'bad','中评':'neutral'}.get(sentiment,'unknown')
    date=pd.to_datetime(row.get('date'),errors='coerce',utc=True)
    date_label=date.tz_convert(LOCAL_TZ).strftime('%Y-%m-%d %H:%M') if pd.notna(date) else '日期未知'
    rating=pd.to_numeric(row.get('rating'),errors='coerce')
    stars=f'{rating:g} 星' if pd.notna(rating) and 1<=rating<=5 else '星级未知'
    title=_text(row.get('title'),'玩家反馈') or '玩家反馈'
    content=_text(row.get('content'))
    tags=['待Agent分析'] if waiting else categories_for(row)
    primary=_text(row.get('issue_category'))
    pending=bool(row.get('needs_review',False))
    if sentiment=='好评' and not pending: suggestion='保留具体体验亮点，结合更多同类评论验证，可作为产品体验与用户沟通的参考。'
    elif primary in PLAYBOOKS: suggestion=PLAYBOOKS[primary][1]+'。'
    else: suggestion='结合上下文确认具体诉求，补充问题场景后再提出处理方案。'
    if waiting:
        interpretation='<strong>待 Agent 分析</strong><br>原文已保存，尚无有效的 Agent 判断；情绪、主题和处理建议待分析后展示。'
        source='待Agent分析'
    elif source=='Agent':
        reason=_text(row.get('agent_reason'))
        demand=_text(row.get('agent_demand')) or _text(row.get('demand'))
        interpretation='<strong>Agent 解读</strong><br>'+esc(reason or '本条已有 Agent 分类，未提供额外解读。').replace('\n','<br>')
        if demand: interpretation+='<br><strong>玩家诉求：</strong>'+esc(demand).replace('\n','<br>')
    else:
        interpretation=('<strong>人工复核依据：</strong>'+esc(_text(row.get('analysis_basis'),'已人工复核')).replace('\n','<br>')+'<br>') if source=='人工复核' else ''
        interpretation+='<strong>规则处理参考：</strong>'+esc(suggestion)
    url=_text(row.get('url'))
    parsed=urlparse(url)
    link=('<a href="'+esc(url,quote=True)+'" target="_blank" rel="noopener noreferrer">查看商店来源 ↗</a>') if parsed.scheme=='https' and parsed.hostname in {'apps.apple.com','itunes.apple.com'} else ''
    full=('<details><summary>展开完整原文</summary><div class="review-full">'+esc(content).replace('\n','<br>')+'</div></details>') if len(content)>220 else ''
    body=esc(content[:220]+('…' if len(content)>220 else '')).replace('\n','<br>')
    return (f'<article class="review-card {kind}"><div class="review-top"><strong>{esc(_text(row.get("author"),"匿名玩家") or "匿名玩家")}</strong>'
        f'<time>{date_label}</time></div><div class="review-meta"><span class="platform-pill">App Store</span><span>{stars}</span></div>'
        f'<div class="review-title">{esc(title)}</div><div class="review-body">{body}</div>{full}'
        f'<div class="tag-row"><span class="review-tag {kind}">{esc("待判定" if sentiment=="待复核" else sentiment)}</span>'+
        '<span class="review-tag">判断来源：'+esc(source)+'</span>'+
        ('<span class="review-tag unknown">情绪判断有待确认</span>' if pending else '')+
        ''.join('<span class="review-tag">'+esc(tag)+'</span>' for tag in tags[:3])+
        ('<span class="review-tag">等 '+str(len(tags))+' 类</span>' if len(tags)>3 else '')+
        f'</div><div class="review-source">#{esc(_text(row.get("review_id")))} · {esc(review_version_text(row))} {link}</div>'
        f'<div class="suggestion-box">{interpretation}</div></article>')

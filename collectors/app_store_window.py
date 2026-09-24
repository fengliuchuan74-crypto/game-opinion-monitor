"""Traverse the visible public feed; a traversal is never a claim of all historical reviews."""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import requests

from .base import CollectionResult


def timestamp(value):
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError,ValueError):
        return None


def valid_next(url,app_id,country,page):
    try:
        parsed=urlparse(url)
        return (parsed.scheme=='https' and parsed.hostname=='itunes.apple.com' and not parsed.username
            and not parsed.password and parsed.port in (None,443)
            and re.fullmatch(rf'/{country}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/(xml|json)',parsed.path,re.I) is not None)
    except (TypeError,ValueError):
        return False


def read_page(collector,app_id,country,page,advertised,meta):
    root=f'https://itunes.apple.com/{country}/rss/customerreviews/'
    canonical=root+f'page={page}/id={app_id}/sortby=mostrecent/json'
    urls=[canonical+'?l=en']
    if page==1: urls.append(root+f'id={app_id}/sortBy=mostRecent/json?l=en')
    if advertised:
        urls.append(advertised)
        parts=urlparse(advertised)
        query=dict(parse_qsl(parts.query)); query.pop('urlDesc',None)
        urls.append(urlunparse(parts._replace(query=urlencode(query))))
    urls.extend([canonical,canonical.replace('/json','/xml')+'?l=en'])
    attempts=[]
    for url in dict.fromkeys(urls):
        meta['request_attempts']+=1
        try:
            response=collector.session.get(url,timeout=collector.timeout,
                headers={'User-Agent':'AppStoreOpinionWorkbench/2.2 (public reviews)'})
            status=response.status_code
            if status==429:
                attempts.append({'url':url,'http_status':status,'count':0})
                return {},[],attempts,'公开源限流，请稍后重试'
            if status!=200:
                attempts.append({'url':url,'http_status':status,'count':0})
                continue
            payload=collector._response_payload(response)
            if not isinstance(payload,dict) or not isinstance(payload.get('feed'),dict):
                raise ValueError('评论源结构异常')
            rows=collector.parse_reviews(payload,app_id,country)
            attempts.append({'url':url,'http_status':status,'count':len(rows)})
            if rows: return payload,rows,attempts,''
        except (requests.RequestException,ValueError,TypeError,AttributeError) as exc:
            attempts.append({'url':url,'error':type(exc).__name__,'count':0})
    unavailable=page>1 and attempts and all(x.get('http_status') in [400,404] for x in attempts)
    return {},[],attempts,('公开源页码上限' if unavailable else '公开源空页或请求异常，兼容重试后仍未恢复')


def collect_visible_window(collector,app_id,country,max_pages,delay,start_at=None,end_at=None,prefer_public_reviews=False):
    result=CollectionResult(platform=collector.platform)
    now=timestamp(result.started_at)
    start=timestamp(start_at) if start_at else None
    end=timestamp(end_at) if end_at else now
    if end: end=min(end,now)
    app_id,country=str(app_id).strip(),str(country).strip().lower()
    limit=max(1,min(10,int(max_pages)))
    meta=result.metadata={'requested_start':start.isoformat() if start else None,
        'requested_end':end.isoformat() if end else None,'page_limit':limit,'scanned_count':0,
        'request_attempts':0,'undated_count':0,'outside_range_count':0,'overlap_count':0,
        'boundary_reached':False,'sequence_verified':True,'page_trace':[],
        'coverage_start':None,'coverage_end':None,'coverage_status':'尚未建立覆盖记录',
        'oldest_date':None,'newest_date':None}
    if (not app_id.isascii() or not app_id.isdigit() or len(country)!=2 or not country.isascii()
            or not country.isalpha() or not end or (start_at and not start) or (start and start>=end)):
        result.errors.append('App ID、地区或采集日期范围无效；日期须包含时区且起始早于结束。')
        result.finished_at=datetime.now(timezone.utc).isoformat()
        return result
    seen=set(); fingerprints=set(); advertised=''; previous_oldest=None; all_dates=[]
    public=None; initial_attempts=[]
    if prefer_public_reviews:
        from .app_store_public import public_context,public_page
        public,context_error=public_context(collector,app_id,country,meta,initial_attempts)
        if context_error:
            result.warnings.append(context_error)
            if '限流' in context_error:
                result.errors.append(context_error)
                result.stop_reason='公开源限流'
                meta['page_trace']=[{'page':1,'attempts':initial_attempts,'read_count':0,'matched_count':0}]
                result.finished_at=datetime.now(timezone.utc).isoformat()
                collector._write_log(app_id,country,result)
                return result
    meta['source_kind']='public_review_list' if public else 'public_rss'
    if public: meta['source_total_reported']=public['total_reported']
    for page in range(1,limit+1):
        result.requested+=1
        attempts=initial_attempts if page==1 else []
        if public:
            payload,rows,has_more,error=public_page(collector,app_id,country,page,public,meta,attempts)
            if not rows and page==1 and '限流' not in error:
                result.warnings.append(error+'；尝试同地区 RSS。')
                public=None; meta['source_kind']='public_rss'; meta.pop('source_total_reported',None)
        if not public:
            payload,rows,rss_attempts,error=read_page(collector,app_id,country,page,advertised,meta)
            attempts.extend(rss_attempts)
        trace={'page':page,'attempts':attempts,'read_count':len(rows),'matched_count':0}
        meta['page_trace'].append(trace)
        if not rows:
            result.stop_reason='公开源上限' if error=='公开源页码上限' else '空页未恢复'
            result.errors.append(f'第 {page} 页：{error}。空结果不代表该游戏没有评论；已保留此前读取的数据。')
            break
        if not public and len(attempts)>1: result.warnings.append(f'第 {page} 页经同游戏、同地区的兼容地址恢复。')
        dates=[timestamp(row.date) for row in rows]
        known=[date for date in dates if date is not None]
        meta['scanned_count']+=len(rows)
        meta['undated_count']+=len(rows)-len(known)
        if len(known)!=len(rows) or any(a<b for a,b in zip(known,known[1:])) or any(d>now for d in known):
            meta['sequence_verified']=False
        if previous_oldest and known and max(known)>previous_oldest:
            meta['sequence_verified']=False
        if known:
            trace.update(oldest_date=min(known).isoformat(),newest_date=max(known).isoformat())
            previous_oldest=min(known); all_dates.extend(known)
        identities=[str(row.external_id) if row.external_id else hashlib.sha256(
            f'{row.author}|{row.date}|{row.title}|{row.content}'.encode()).hexdigest() for row in rows]
        fingerprint=frozenset(identities)
        if fingerprint in fingerprints:
            meta['sequence_verified']=False
            result.errors.append(f'第 {page} 页重复返回已读页面，无法确认后续连续性。')
            result.stop_reason='重复页面'; break
        fingerprints.add(fingerprint)
        for row,date,identity in zip(rows,dates,identities):
            if identity in seen:
                meta['overlap_count']+=1
                meta['sequence_verified']=False
                continue
            seen.add(identity)
            if date and date<end and (start is None or date>=start):
                result.reviews.append(row); trace['matched_count']+=1
            else: meta['outside_range_count']+=1
        if page==1:
            updated=payload['feed'].get('updated')
            meta['source_updated_at']=updated.get('label') if isinstance(updated,dict) else None
        if start and known and min(known)<start and meta['sequence_verified']:
            meta['boundary_reached']=True; result.stop_reason='已越过起始日期'; break
        if not public:
            links=payload.get('feed',{}).get('link',[])
            advertised=next((link['attributes'].get('href','') for link in links
                if isinstance(link,dict) and isinstance(link.get('attributes'),dict)
                and link['attributes'].get('rel')=='next'),'') if isinstance(links,list) else ''
            if advertised and not valid_next(advertised,app_id,country,page+1):
                result.errors.append('分页链接不符合当前游戏、地区和下一页页码，已停止。')
                result.stop_reason='分页异常'; meta['sequence_verified']=False; break
            has_more=bool(advertised)
        if not has_more:
            result.stop_reason='公开源末页'; break
        if page==limit:
            result.stop_reason='公开源上限' if limit==10 and not public else '页数上限'
            break
        if delay: time.sleep(max(0,float(delay)))
    if all_dates:
        earliest,latest=min(all_dates),max(all_dates)
        meta['oldest_date'],meta['newest_date']=earliest.isoformat(),latest.isoformat()
        if meta['sequence_verified']:
            covered_start=max(earliest,start) if start else earliest
            if covered_start<end:
                meta['coverage_start'],meta['coverage_end']=covered_start.isoformat(),end.isoformat()
    if not meta['sequence_verified']:
        meta['coverage_status']='分页有重叠、时间乱序或缺失，连续性待确认'
    elif meta['boundary_reached']:
        meta['coverage_status']='已遍历至起始日期之前（公开源可见范围）'
    elif start:
        meta['coverage_status']='尚未遍历到起始日期'
        result.warnings.append('尚未覆盖目标起始日期：'+result.stop_reason+'。不能将本轮数据当作该日期区间的全部评论。')
    elif all_dates:
        meta['coverage_status']='已建立本轮可见范围；更早历史未验证'
    else:
        meta['coverage_status']='公开源未返回有效评论，未建立覆盖记录'
    meta['matched_count']=result.fetched_count
    result.finished_at=datetime.now(timezone.utc).isoformat()
    collector._write_log(app_id,country,result)
    return result

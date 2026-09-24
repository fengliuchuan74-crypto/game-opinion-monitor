"""Read Apple's unauthenticated storefront review list, advertised by its review page."""
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests


# Apple storefront IDs are independent of app IDs. Unknown storefronts keep the RSS path.
STOREFRONTS = {'cn':'143465','us':'143441','hk':'143463','tw':'143470',
    'jp':'143462','kr':'143466','gb':'143444','de':'143443','fr':'143442',
    'ca':'143455','au':'143460','sg':'143464','it':'143450','br':'143503','th':'143475',
    'es':'143454','nl':'143452','nz':'143461','my':'143473','ph':'143474',
    'id':'143476','vn':'143471','in':'143467'}
ROWS_URL = 'https://itunes.apple.com/WebObjects/MZStore.woa/wa/userReviewsRow'
PAGE_SIZE = 50


def _headers(country):
    language='19' if country=='cn' else '1'
    return {'X-Apple-Store-Front':f'{STOREFRONTS[country]}-{language},29',
            'User-Agent':'AppStoreOpinionWorkbench/2.2 (public reviews)'}


def _get(collector,url,meta,attempts,headers,params=None):
    meta['request_attempts']+=1
    attempt={'url':url,'params':params or {},'count':0,
             'source':'public_review_list'}
    attempts.append(attempt)
    try:
        response=collector.session.get(url,params=params,headers=headers,timeout=collector.timeout)
    except requests.RequestException as exc:
        attempt['error']=type(exc).__name__
        raise
    attempt['http_status']=response.status_code
    if response.status_code==429:
        retry_after=getattr(response,'headers',{}).get('Retry-After')
        try:
            wait=max(1,int(retry_after))
        except (ValueError,TypeError):
            try:
                wait=max(1,int((parsedate_to_datetime(str(retry_after))-datetime.now(timezone.utc)).total_seconds()))
            except (ValueError,TypeError,OverflowError):
                wait=60
        attempt['retry_after_seconds']=wait
        meta['retry_after_seconds']=wait
        raise ValueError('公开源限流，请稍后重试')
    response.raise_for_status()
    if response.status_code!=200:
        raise ValueError(f'公开评论列表 HTTP {response.status_code}')
    data=response.json()
    if not isinstance(data,dict): raise ValueError('公开评论列表结构异常')
    return data,attempt


def public_context(collector,app_id,country,meta,attempts):
    if country not in STOREFRONTS:
        return None,'该地区暂未配置公开列表兼容入口，继续尝试 RSS'
    try:
        headers=_headers(country)
        data,_=_get(collector,f'https://itunes.apple.com/{country}/customer-reviews/id{app_id}',
            meta,attempts,headers,{'displayable-kind':'11'})
        # Never follow publishing links. Its region marker only verifies the returned storefront.
        region=parse_qs(urlparse(str(data.get('writeUserReviewUrl') or '')).query).get('cc')
        if str(data.get('adamId'))!=app_id or region!=[country]:
            raise ValueError('公开列表返回的游戏或地区不匹配')
        if data.get('userReviewsRowUrl')!=ROWS_URL or data.get('kindId')!=11:
            raise ValueError('公开评论列表地址或应用类型不匹配')
        options=data.get('userReviewsSortOptions')
        if not isinstance(options,list) or not any(isinstance(o,dict) and o.get('sortId')==4 for o in options):
            raise ValueError('公开列表未提供最新发表排序')
        return {'headers':headers,'total_reported':data.get('totalNumberOfReviews')},''
    except (requests.RequestException,ValueError,TypeError) as exc:
        return None,f'公开列表暂不可用：{exc}'


def public_page(collector,app_id,country,page,context,meta,attempts):
    params={'id':app_id,'displayable-kind':'11','startIndex':(page-1)*PAGE_SIZE,
            'endIndex':page*PAGE_SIZE,'sort':'4','appVersion':'all','cc':country}
    try:
        data,attempt=_get(collector,ROWS_URL,meta,attempts,context['headers'],params)
        items=data.get('userReviewList')
        if not isinstance(items,list) or len(items)>PAGE_SIZE:
            raise ValueError('公开评论列表结构或批量大小异常')
        entries=[]
        for row in items:
            if not isinstance(row,dict) or not str(row.get('userReviewId','')).isdigit():
                raise ValueError('公开评论缺少有效 ID')
            profile=urlparse(str(row.get('viewUsersUserReviewsUrl') or ''))
            if profile.scheme!='https' or profile.hostname!='itunes.apple.com' or profile.path!=f'/{country}/reviews':
                raise ValueError('公开评论的地区标记不匹配')
            try: rating=float(row.get('rating'))
            except (ValueError,TypeError): rating=0
            if not 1<=rating<=5 or not isinstance(row.get('body'),str) or not row['body'].strip():
                raise ValueError('公开评论缺少有效星级或正文')
            entries.append({'id':{'label':str(row['userReviewId'])},'updated':{'label':row.get('date')},
                'author':{'name':{'label':row.get('name') or '匿名用户'}},
                'title':{'label':row.get('title') or ''},'content':{'label':row['body']},
                'im:rating':{'label':str(rating)},
                'link':[{'attributes':{'href':f'https://apps.apple.com/{country}/app/id{app_id}?see-all=reviews'}}]})
        payload={'feed':{'entry':entries}}
        reviews=collector.parse_reviews(payload,app_id,country)
        for review,original in zip(reviews,items):
            review.data_source='app_store_public_review_list'
            # Store source evidence; version and reliable engagement statistics are unavailable here.
            review.raw_json={'source':'public_review_list','review':original}
        attempt['count']=len(reviews)
        if not reviews:
            return payload,[],False,'公开列表返回空内容，尚不能确认无评论或已到末尾'
        return payload,reviews,len(items)==PAGE_SIZE,''
    except (requests.RequestException,ValueError,TypeError) as exc:
        return {},[],False,f'公开列表读取失败：{exc}'

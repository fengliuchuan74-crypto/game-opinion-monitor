from __future__ import annotations
import hashlib
from io import BytesIO
import pandas as pd
from collectors.base import RawReview
from .data_loader import FIELD_ALIASES, _find_source_column, read_file
from .operations import LOCAL_TZ


def parse_import(content,filename,app_id,country):
    if len(content)>20*1024*1024: raise ValueError('单次导入限 20 MB，请按日期分批导入')
    raw=read_file(BytesIO(content),filename).fillna('')
    columns=list(raw.columns)
    mapping={key:_find_source_column(columns,aliases) for key,aliases in FIELD_ALIASES.items()}
    mapping.update({key:_find_source_column(columns,aliases) for key,aliases in {
        'app_id':['app_id','App ID','应用ID'],'country':['country','地区','国家'],
        'external_id':['external_id','review_id','评论ID'],'version':['version','版本','app_version']}.items()})
    if not mapping['content'] or not mapping['date'] or not mapping['rating']:
        raise ValueError('导入需包含正文、日期、星级；支持中文或 content/date/rating 列名')
    signature=hashlib.sha256(content).hexdigest()
    reviews,errors=[],[]
    for index,row in raw.iterrows():
        def field(key): return str(row[mapping[key]]).strip() if mapping.get(key) else ''
        try:
            if field('platform').lower() not in ('','app store','appstore','ios'): raise ValueError('仅接收 App Store 数据')
            if field('app_id') and field('app_id')!=app_id: raise ValueError('App ID 与所选游戏不符')
            if field('country') and field('country').lower()!=country: raise ValueError('地区与所选地区不符')
            if not field('content'): raise ValueError('正文为空')
            stamp=pd.Timestamp(field('date'))
            if pd.isna(stamp): raise ValueError('日期缺失或无效')
            if stamp.tzinfo is None: stamp=stamp.tz_localize(LOCAL_TZ)
            if stamp>pd.Timestamp.now(tz=LOCAL_TZ): raise ValueError('日期晚于当前时间')
            rating=float(field('rating'))
            if rating not in (1,2,3,4,5): raise ValueError('星级须为 1—5 的整数')
            reviews.append(RawReview(platform='App Store',external_id=field('external_id') or f'import:{signature}:{index}',
                date=stamp.isoformat(),author=field('author') or '匿名玩家',title=field('title'),content=field('content'),
                rating=rating,app_id=app_id,country=country,url=field('url') or None,
                topic=('version:'+field('version')) if field('version') else field('topic'),
                data_source='app_store_import',raw_json={'file':filename,'sha256':signature,'row':int(index)+2}))
        except (ValueError,TypeError,OverflowError) as exc:
            errors.append({'文件行号':int(index)+2,'原因':str(exc)})
    return reviews,errors,mapping,signature

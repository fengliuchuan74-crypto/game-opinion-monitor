"""Official App Store icons, cached separately from the review database."""
from __future__ import annotations

import base64
import html
import io
import json
import os
import tempfile
import time
from pathlib import Path

import requests
from PIL import Image, UnidentifiedImageError

from collectors.app_store import AppStoreCollector, safe_artwork_url

FRESH_SECONDS=7*24*60*60
RETRY_SECONDS=5*60
MAX_IMAGE_BYTES=3*1024*1024


def _download_icon(session, url):
    if not safe_artwork_url(url): return ''
    # No automatic redirect to a different host. Apple artwork URLs are direct image resources.
    with session.get(url,timeout=(3,5),stream=True,allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code!=200: return ''
        body=bytearray()
        for chunk in response.iter_content(64*1024):
            body.extend(chunk)
            if len(body)>MAX_IMAGE_BYTES: return ''
    with Image.open(io.BytesIO(body)) as icon:
        if icon.width*icon.height>4_000_000: return ''
        icon.thumbnail((192,192))
        output=io.BytesIO()
        icon.convert('RGBA').save(output,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(output.getvalue()).decode('ascii')


def get_app_icon(app_id, country, cache_dir, artwork_url='', *, allow_fetch=True):
    """Resolve by app/storefront, retain the last good icon offline, and retry failures later."""
    app_id,country=str(app_id).strip(),str(country).strip().lower()
    if not app_id.isascii() or not app_id.isdigit() or len(country)!=2 or not country.isascii() or not country.isalpha():
        return ''
    cache_dir=Path(cache_dir)
    path=cache_dir/f'{app_id}_{country}.json'
    cached={}
    try:
        record=json.loads(path.read_text(encoding='utf-8'))
        if (isinstance(record,dict) and record.get('app_id')==app_id and record.get('country')==country
                and isinstance(record.get('data_uri'),str) and isinstance(record.get('next_check'),(int,float))):
            cached=record
    except (OSError,ValueError):
        pass
    previous=cached.get('data_uri','')
    if previous and not previous.startswith('data:image/png;base64,'): previous=''
    now=time.time()
    source_url=safe_artwork_url(artwork_url)
    changed=bool(source_url and source_url!=cached.get('source_url'))
    if not allow_fetch or (now<cached.get('next_check',0) and not changed):
        return previous
    result=''
    try:
        with requests.Session() as session:
            if not source_url:
                collector=AppStoreCollector(cache_dir/'logs',session=session,timeout=(3,5))
                info,_=collector.lookup_app(app_id,country)
                source_url=info.get('artwork_url','') if info else ''
            if source_url: result=_download_icon(session,source_url)
    except (requests.RequestException,OSError,ValueError,UnidentifiedImageError,Image.DecompressionBombError):
        pass
    record={'app_id':app_id,'country':country,'data_uri':result or previous,
            'source_url':source_url if result else cached.get('source_url',''),
            'next_check':now+(FRESH_SECONDS if result else RETRY_SECONDS)}
    # Concurrent browser sessions may refresh the same icon; readers always see a complete file.
    pending=None
    try:
        cache_dir.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=cache_dir,suffix='.tmp',delete=False) as handle:
            pending=Path(handle.name)
            json.dump(record,handle,ensure_ascii=False)
        os.replace(pending,path)
    except OSError:
        pass
    finally:
        try:
            if pending: pending.unlink(missing_ok=True)
        except OSError:
            pass
    return result or previous


def game_hero_html(profile, icon=''):
    name=str(profile.get('app_name') or '未命名游戏')
    initial=html.escape(name.strip()[:1] or '游')
    picture=f'<span class="hero-icon-placeholder" aria-label="暂未获取游戏图标">{initial}</span>'
    if icon.startswith('data:image/png;base64,'):
        picture=f'<img class="hero-game-icon" src="{html.escape(icon,quote=True)}" alt="{html.escape(name,quote=True)}的 App Store 图标" width="104" height="104">'
    return ('<div class="hero"><div class="hero-copy"><div class="hero-kicker">APP STORE · 舆情监控与分析</div>'
            '<h1>'+html.escape(name)+' · '+html.escape(str(profile.get('country') or '').upper())+'</h1>'
            '<p>从玩家声音中识别问题，让每一个处理建议都有数据依据。</p></div>'
            '<div class="hero-icon-frame">'+picture+'</div></div>')

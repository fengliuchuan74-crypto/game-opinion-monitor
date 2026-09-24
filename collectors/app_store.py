from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import BaseCollector, CollectionResult, RawReview


SUPPORTED_COUNTRIES = ["cn", "us", "jp", "kr", "hk", "tw"]
MAX_SEARCH_RESULTS = 10
DEFAULT_CONSECUTIVE_EMPTY_PAGES = 5


def safe_artwork_url(value: Any) -> str:
    """Only use HTTPS artwork hosted by Apple's image CDN."""
    url=str(value or '')
    try:
        parsed=urlparse(url)
        host=(parsed.hostname or '').lower()
        if (parsed.scheme=='https' and (host=='mzstatic.com' or host.endswith('.mzstatic.com'))
                and not parsed.username and not parsed.password and parsed.port in (None,443)):
            return url
    except ValueError:
        pass
    return ''


def _app_info(item: Any) -> dict[str, Any] | None:
    if not isinstance(item,dict) or not str(item.get('trackId','')).isdigit():
        return None
    return {
        'app_id':str(item['trackId']), 'name':str(item.get('trackName') or ''),
        'seller':str(item.get('sellerName') or ''), 'bundle_id':str(item.get('bundleId') or ''),
        'rating':item.get('averageUserRating'), 'rating_count':item.get('userRatingCount'),
        'url':str(item.get('trackViewUrl') or ''),
        'artwork_url':next((url for field in ['artworkUrl512','artworkUrl100','artworkUrl60']
                            if (url:=safe_artwork_url(item.get(field)))),''),
    }


def _label(value: Any) -> str:
    if isinstance(value, dict):
        label = value.get("label")
        if label is not None:
            return str(label).strip()
    if value is None:
        return ""
    return str(value).strip()


def _attributes(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        attrs = value.get("attributes")
        if isinstance(attrs, dict):
            return attrs
    return {}


def _first_link_href(value: Any) -> str | None:
    if isinstance(value, list):
        for item in value:
            href = _attributes(item).get("href")
            if href:
                return str(href)
    href = _attributes(value).get("href")
    return str(href) if href else None


def _safe_float(value: Any) -> float | None:
    try:
        return float(_label(value))
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(float(_label(value)))
    except (TypeError, ValueError):
        return 0


class AppStoreCollector(BaseCollector):
    platform = "App Store"

    def __init__(
        self,
        log_dir: Path,
        session: requests.Session | None = None,
        timeout: int = 20,
    ) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or requests.Session()
        self.timeout = timeout
        if session is None:
            retry = Retry(total=1, backoff_factor=1, status_forcelist=[500,502,503,504],
                          respect_retry_after_header=False, raise_on_status=False)
            self.session.mount('https://', HTTPAdapter(max_retries=retry))

    def search_apps(
        self, term: str, country: str = "cn", limit: int = MAX_SEARCH_RESULTS
    ) -> tuple[list[dict[str, Any]], list[str]]:
        term = str(term).strip()
        country = str(country).strip().lower()
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        if not term:
            return [], ["请输入应用名称后再搜索。"]

        try:
            response = self.session.get(
                "https://itunes.apple.com/search",
                params={
                    "term": term,
                    "country": country,
                    "entity": "software",
                    "limit": limit,
                },
                timeout=self.timeout,
                headers={
                    "User-Agent": (
                        "GoosePublicOpinionMonitor/1.0 "
                        "(public App Store search)"
                    )
                },
            )
            if response.status_code != 200:
                return [], [f"应用搜索失败：HTTP {response.status_code}。"]
            payload = response.json()
        except requests.Timeout:
            return [], ["应用搜索请求超时，请稍后重试。"]
        except requests.RequestException as exc:
            return [], [f"应用搜索网络请求失败：{exc}"]
        except ValueError:
            return [], ["应用搜索返回内容不是有效 JSON。"]

        results = []
        items=payload.get('results',[]) if isinstance(payload,dict) else []
        for item in items if isinstance(items,list) else []:
            mapped=_app_info(item)
            if mapped: results.append(mapped)
        if not results:
            return [], ["未搜索到候选 App。请换一个关键词或切换国家/地区。"]
        return results, []

    def lookup_app(self, app_id: str, country: str = 'cn') -> tuple[dict[str, Any] | None, list[str]]:
        app_id,country=str(app_id).strip(),str(country).strip().lower()
        if not app_id.isdigit() or len(country)!=2 or not country.isascii() or not country.isalpha():
            return None,['请提供数字 App ID 和两位地区代码。']
        try:
            response=self.session.get('https://itunes.apple.com/lookup',
                params={'id':app_id,'country':country,'entity':'software'},timeout=self.timeout)
            response.raise_for_status()
            payload=response.json()
            items=payload.get('results',[]) if isinstance(payload,dict) else []
            for item in items if isinstance(items,list) else []:
                mapped=_app_info(item)
                if mapped and mapped['app_id']==app_id:
                    return mapped,[]
            return None,['该地区未返回当前 App ID 的应用资料。']
        except (requests.RequestException,ValueError):
            return None,['暂时无法读取 App Store 应用资料。']

    def collect(
        self,
        app_id: str,
        country: str = "cn",
        max_pages: int | None = None,
        delay_seconds: float = 1.0,
        target_reviews: int | None = None,
        consecutive_empty_limit: int = DEFAULT_CONSECUTIVE_EMPTY_PAGES,
        detect_repeated_pages: bool = False,
        follow_feed_links: bool = False,
        start_at: str | None = None,
        end_at: str | None = None,
        prefer_public_reviews: bool = False,
    ) -> CollectionResult:
        if follow_feed_links:
            from .app_store_window import collect_visible_window
            return collect_visible_window(self,app_id,country,max_pages or 10,delay_seconds,start_at,end_at,prefer_public_reviews)
        app_id = str(app_id).strip()
        country = str(country).strip().lower()
        page_limit = max(1, min(100, int(max_pages))) if max_pages is not None else 100
        delay = max(0.0, float(delay_seconds))
        target = max(1, int(target_reviews)) if target_reviews is not None else None
        empty_limit = max(1, int(consecutive_empty_limit))
        result = CollectionResult(platform=self.platform, requested=0)
        empty_pages: list[int] = []
        consecutive_empty_pages = 0

        if not app_id.isdigit():
            result.errors.append("App Store app_id 必须是数字。")
            result.finished_at = datetime.now(timezone.utc).isoformat()
            self._write_log(app_id, country, result)
            return result

        page = 1
        seen_pages = set()
        while True:
            if page_limit is not None and page > page_limit:
                result.stop_reason = "页数上限"
                break
            url = self._feed_url(app_id, country, page)
            try:
                result.requested += 1
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    headers={
                        "User-Agent": (
                            "GoosePublicOpinionMonitor/1.0 "
                            "(public App Store review feed)"
                        )
                    },
                )
                if response.status_code != 200:
                    if response.status_code == 400 and page > 1:
                        result.stop_reason = '公开源上限'
                        result.warnings.append(
                            f"第 {page} 页不可用，可能已达到 Apple 公开评论源页码上限，已停止继续请求。"
                        )
                    else:
                        result.errors.append(
                            f"第 {page} 页请求失败：HTTP {response.status_code}。"
                        )
                    break
                payload = response.json()
            except requests.Timeout:
                result.errors.append(f"第 {page} 页请求超时，请稍后重试。")
                break
            except requests.RequestException as exc:
                result.errors.append(f"第 {page} 页网络请求失败：{exc}")
                break
            except ValueError:
                result.errors.append(f"第 {page} 页返回内容不是有效 JSON。")
                break

            page_reviews = self.parse_reviews(payload, app_id=app_id, country=country)
            fingerprint = tuple((r.external_id, r.content, r.rating) for r in page_reviews)
            if detect_repeated_pages and fingerprint and fingerprint in seen_pages:
                result.stop_reason = '重复页面'
                result.warnings.append('公开源重复返回同一页，已停止；本轮不代表完整历史。')
                break
            seen_pages.add(fingerprint)
            if not page_reviews:
                empty_pages.append(page)
                consecutive_empty_pages += 1
                if page == 1 and page_limit == 1:
                    result.errors.append(
                        "未返回可解析评论。请确认 app_id、国家/地区是否正确，或该地区是否有公开评论。"
                    )
                if consecutive_empty_pages >= empty_limit:
                    result.stop_reason = '空页'
                    result.warnings.append(
                        f"连续 {consecutive_empty_pages} 页未返回可解析评论，已停止继续请求。"
                    )
                    break
                page += 1
                if delay:
                    time.sleep(delay)
                continue
            consecutive_empty_pages = 0
            result.reviews.extend(page_reviews)
            if target is not None and result.fetched_count >= target:
                result.stop_reason = '目标条数'
                if result.fetched_count > target:
                    del result.reviews[target:]
                break
            page += 1
            if delay:
                time.sleep(delay)

        if result.fetched_count == 0 and empty_pages and not result.errors:
            result.errors.append(
                "本次请求的页面未返回可解析评论。请确认 app_id、国家/地区是否正确，或该地区是否有公开评论。"
            )
        elif empty_pages:
            result.warnings.append(
                f"第 {', '.join(map(str, empty_pages))} 页未返回可解析评论，已跳过并继续尝试后续页。"
            )

        if result.errors:
            result.stop_reason = '请求失败'
        result.finished_at = datetime.now(timezone.utc).isoformat()
        self._write_log(app_id, country, result)
        return result

    @staticmethod
    def _response_payload(response):
        text = getattr(response, 'text', '')
        if not isinstance(text, str) or not text.lstrip('\ufeff \t\r\n').startswith('<'):
            return response.json()
        if '<!DOCTYPE' in text.upper():
            raise ValueError('不支持带 DTD 的评论源')
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise ValueError('无法解析公开评论 XML') from exc
        atom, im = '{http://www.w3.org/2005/Atom}', '{http://itunes.apple.com/rss}'
        entries = []
        for entry in root.findall(atom+'entry'):
            item = {}
            for key in ['id','updated','title','content']:
                element = entry.find(atom+key)
                item[key] = {'label': ''.join(element.itertext()) if element is not None else ''}
            for key in ['rating','version','voteSum']:
                element = entry.find(im+key)
                item['im:'+key] = {'label':element.text if element is not None else ''}
            author = entry.find(atom+'author/'+atom+'name')
            item['author'] = {'name':{'label':author.text if author is not None else ''}}
            item['link'] = [{'attributes':dict(link.attrib)} for link in entry.findall(atom+'link')]
            entries.append(item)
        updated=root.find(atom+'updated')
        return {'feed':{'entry':entries,'updated':{'label':updated.text if updated is not None else ''},
                        'link':[{'attributes':dict(link.attrib)} for link in root.findall(atom+'link')]}}

    @staticmethod
    def _feed_url(app_id: str, country: str, page: int) -> str:
        return (
            f"https://itunes.apple.com/{country}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortby=mostrecent/json"
        )

    def parse_reviews(
        self, payload: dict[str, Any], app_id: str, country: str
    ) -> list[RawReview]:
        entries = payload.get("feed", {}).get("entry", [])
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return []

        reviews: list[RawReview] = []
        collected_at = datetime.now(timezone.utc).isoformat()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            content = _label(entry.get("content"))
            rating = _safe_float(entry.get("im:rating"))
            if not content or rating is None:
                continue

            version = _label(entry.get("im:version"))
            reviews.append(
                RawReview(
                    platform=self.platform,
                    external_id=_label(entry.get("id")) or None,
                    date=_label(entry.get("updated")) or None,
                    author=_label(entry.get("author", {}).get("name")) or "匿名用户",
                    title=_label(entry.get("title")) or "App Store 评论",
                    content=content,
                    rating=rating,
                    likes=_safe_int(entry.get("im:voteSum")),
                    comments=0,
                    shares=0,
                    url=_first_link_href(entry.get("link")),
                    note_type="App Store 评论",
                    topic=f"version:{version}" if version else "",
                    data_source="app_store_public_feed",
                    collected_at=collected_at,
                    app_id=app_id,
                    country=country,
                    raw_json=entry,
                )
            )
        return reviews

    def _write_log(self, app_id: str, country: str, result: CollectionResult) -> None:
        log_path = self.log_dir / f"app_store_{datetime.now():%Y%m%d}.log"
        line = {
            "time": datetime.now(timezone.utc).isoformat(),
            "app_id": app_id,
            "country": country,
            "requested_pages": result.requested,
            "fetched_count": result.fetched_count,
            "failed_count": result.failed_count,
            "errors": result.errors,
            "warnings": result.warnings,
        }
        with log_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False) + "\n")

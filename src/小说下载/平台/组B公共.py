from __future__ import annotations
import asyncio
import html
import re
import urllib.parse
import aiohttp
from .公共 import ProviderError, validate_catalog_count


def identify_id(value, hosts, keys, paths):
    text = str(value or '').strip()
    if re.fullmatch(r'\d{1,30}', text):
        return text
    for candidate in re.findall(r'https?://[^\s<>"\']+', text):
        candidate = candidate.rstrip('，。；、）)]}')
        parsed = urllib.parse.urlsplit(candidate)
        host = (parsed.hostname or '').lower()
        if not any(host == item or host.endswith('.' + item) for item in hosts):
            continue
        query = urllib.parse.parse_qs(parsed.query)
        for key in keys:
            for value in query.get(key, []):
                if re.fullmatch(r'\d{1,30}', value):
                    return value
        for pattern in paths:
            match = re.search(pattern, parsed.path, re.I)
            if match:
                return match.group(1)
    return ''


def book_id(value):
    value = str(value or '').strip()
    if not re.fullmatch(r'\d{1,30}', value):
        raise ProviderError('book_id_invalid')
    return value


def plain(value, multiline=False):
    value = str(value or '').replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub(r'(?i)<br\s*/?>|</p\s*>', '\n', value)
    value = html.unescape(re.sub(r'<[^>]*>', '', value)).strip()
    return value if multiline else re.sub(r'\s+', ' ', value)


def first(source, *keys):
    if not isinstance(source, dict):
        return ''
    for key in keys:
        value = source.get(key)
        if value is not None and value != '':
            return value
    return ''


def cover(source):
    value = first(source, 'coverUrl', 'cover_url', 'big_image_link', 'image_link', 'bookCoverPicUrl', 'cover', 'coverImage', 'cover_image', 'bookCover', 'book_cover', 'bookCoverUrl', 'coverImg', 'img', 'image', 'imageUrl', 'pic', 'picUrl', 'thumb', 'imgUrl', 'largeCover')
    if isinstance(value, dict):
        value = first(value, 'url', 'uri', 'src', 'large', 'original')
    if isinstance(value, list):
        value = value[0] if value else ''
    value = str(value or '').strip()
    if value.startswith('//'):
        value = 'https:' + value
    return value if urllib.parse.urlsplit(value).scheme in {'http','https'} else ''


def metadata(identity, title, author, status, words='', chapters=0, intro='', image=''):
    title = plain(title)
    if not title:
        raise ProviderError('book_info_unavailable')
    status = plain(status)
    if status in {'完结','已完结','完本','已完本','finished','completed'} or '完' in status:
        status = '已完结'
    else:
        status = '连载中'
    try:
        count = max(0, int(str(chapters or '0').replace('章','')))
    except (ValueError, TypeError):
        count = 0
    return {'title':title,'author':plain(author) or '未知','status':status or '连载中','wordCount':str(words or ''),'chapterCount':count,'intro':plain(intro,True),'coverUrl':str(image or ''),'sourceBookId':str(identity)}


def session(headers=None):
    return aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(limit=4,limit_per_host=4))


async def retry(operation):
    for attempt in range(2):
        try:
            return await operation()
        except ProviderError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError, KeyError, TypeError):
            if attempt:
                raise ProviderError('provider_request_failed') from None
            await asyncio.sleep(0.2)


async def json_request(http, method, url, **kwargs):
    async def run():
        async with http.request(method,url,**kwargs) as response:
            response.raise_for_status()
            data = await response.json(content_type=None)
        if not isinstance(data, dict):
            raise ValueError('invalid_response')
        return data
    return await retry(run)


def checked(chapter):
    title = plain(chapter.get('title'))
    content = str(chapter.get('content') or '').strip()
    locked = ('请先购买本章','购买后阅读','订阅后阅读','本章为付费章节','内容未解锁')
    if not title or not content or (len(content)<512 and any(text in content for text in locked)):
        raise ProviderError('chapter_unavailable')
    return {'title':title,'content':content}


async def chapters_map(items, fetch, on_progress):
    if not items:
        raise ProviderError('chapter_unavailable')
    result = [None]*len(items)
    iterator = iter(enumerate(items))
    completed = 0
    if on_progress:
        on_progress(len(items), 0)
    async def worker():
        nonlocal completed
        for index,item in iterator:
            result[index] = checked(await fetch(item))
            completed += 1
            if on_progress:
                on_progress(len(items),completed)
    tasks = [asyncio.create_task(worker()) for _ in range(min(4,len(items)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    return result

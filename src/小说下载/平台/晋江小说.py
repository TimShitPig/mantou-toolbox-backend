"""晋江 Android 公开章节接口。"""
import asyncio
import html
import re
from urllib.parse import parse_qs, urlsplit

import aiohttp
from .公共 import ProviderError

PLATFORM = {
    'id': 'jinjiang', 'name': '晋江小说',
    'hosts': ['jjwxc.net'], 'aliases': ['晋江', '晋江文学城'],
    'coverHosts': ['jjwxc.net', 'jjwxc.com'], 'credentials': [],
}
BASE = 'https://app-cdn.jjwxc.net/androidapi/'


def identify(value):
    text = str(value or '').strip()
    if re.fullmatch(r'\d+', text):
        return text
    match = re.search(r'https?://[^\s<>"\']+', html.unescape(text))
    if not match:
        return ''
    url = urlsplit(match.group())
    if not (url.hostname == 'jjwxc.net' or (url.hostname or '').endswith('.jjwxc.net')):
        return ''
    query = {k.lower(): v for k, v in parse_qs(url.query).items()}
    book_id = (query.get('novelid') or [''])[0]
    if not book_id:
        found = re.search(r'/(?:book|novel)/(\d+)', url.path)
        book_id = found.group(1) if found else ''
    return book_id if re.fullmatch(r'\d+', book_id) else ''


def _text(value):
    return html.unescape(re.sub(r'<[^>]*>', '', re.sub(r'<br\s*/?>', '\n', str(value or ''), flags=re.I))).strip()


def _int(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def _session():
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(limit=4), headers={'User-Agent': 'okhttp/4.9.0'})


async def _request(session, endpoint, params):
    for attempt in range(2):
        try:
            async with session.get(BASE + endpoint, params=params) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
            if not isinstance(data, dict):
                raise ProviderError('invalid_upstream_response')
            return data
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt:
                raise ProviderError('upstream_unavailable') from None
            await asyncio.sleep(.25)


async def _detail(session, book_id):
    data = await _request(session, 'novelbasicinfo', {'novelId': book_id})
    if str(data.get('novelId') or '') != book_id or not data.get('novelName'):
        raise ProviderError('book_not_found')
    cover = data.get('novelCover') or data.get('novelcover') or data.get('cover') or data.get('coverUrl') or ''
    if str(cover).startswith('//'):
        cover = 'https:' + cover
    return {'sourceBookId': book_id, 'title': _text(data['novelName']), 'author': _text(data.get('authorName')), 'status': '已完结' if _int(data.get('novelStep')) == 2 else '连载中', 'wordCount': _int(data.get('novelSize')), 'chapterCount': _int(data.get('novelChapterCount')), 'intro': _text(data.get('novelIntro') or data.get('novelIntroShort')), 'coverUrl': str(cover)}


async def get_book(book_id):
    async with _session() as session:
        return await _detail(session, str(book_id))


async def download_book(book_id, on_progress):
    book_id = str(book_id)
    async with _session() as session:
        book = await _detail(session, book_id)
        data = await _request(session, 'chapterList', {'novelId': book_id, 'more': '0', 'whole': '1'})
        rows = data.get('chapterlist')
        if not isinstance(rows, list) or not rows:
            raise ProviderError('chapter_unavailable')
        for row in rows:
            if not isinstance(row, dict) or not str(row.get('chapterid') or row.get('chapterId') or '').isdigit():
                raise ProviderError('chapter_unavailable')
            free = str(row.get('isvip')) == '0' or (str(row.get('pointfreevip')).lower() in ('1', 'true') and _int(row.get('point')) <= 0)
            if _int(row.get('islock')) or not free:
                raise ProviderError('chapter_unavailable')
        completed = 0
        semaphore = asyncio.Semaphore(4)
        on_progress(len(rows), 0)

        async def one(index, row):
            nonlocal completed
            async with semaphore:
                data = await _request(session, 'chapterContent', {'novelId': book_id, 'chapterId': row.get('chapterid') or row.get('chapterId')})
                content = _text(data.get('content'))
                if not content:
                    raise ProviderError('chapter_unavailable')
                completed += 1
                on_progress(len(rows), completed)
                return {'title': _text(row.get('chaptername') or row.get('chapterName') or f'第{index + 1}章'), 'content': content}

        chapters = await asyncio.gather(*(one(i, row) for i, row in enumerate(rows)))
        book['chapterCount'] = len(rows)
        return {'book': book, 'chapters': chapters}

"""得间 App 详情、目录及已获授权的章节下载。"""
import asyncio
import base64
import html
import json
import os
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from .公共 import ProviderError, validate_catalog_count

PLATFORM = {
    'id': 'dejian', 'name': '得间小说',
    'hosts': ['palmestore.com', 'idejian.com', 'zhangyue.com', 'ireader.com'],
    'aliases': ['得间'],
    'coverHosts': ['palmestore.com', 'ireader.com', 'zhangyue.com', 'idujing.com'],
    'credentials': [
        {'env': 'NOVEL_DEJIAN_SESSION', 'label': '得间 App 会话', 'type': 'json', 'hint': '内置默认值；可填写自己的会话覆盖。', 'defaultAvailable': True},
        {'env': 'NOVEL_DEJIAN_SIGN_KEY', 'label': '得间 App 签名配置', 'hint': '内置默认值；可填写自己的签名配置覆盖。', 'defaultAvailable': True},
    ],
}
BASE = 'https://dj.palmestore.com'
CDN_HOSTS = ('palmestore.com', 'ireader.com', 'zhangyue.com', 'idujing.com')
DEFAULT_SIGN_KEY = 'MIICdQIBADANBgkqhkiG9w0BAQEFAASCAl8wggJbAgEAAoGBAMXGjyS3p+3AVnlBJe5VQ6tC9inh8tVBve4r+yBjC5HQD6th2n3tSyuNVYaNRAFSEq+OENwnwwhjbYUnjLWb+qZscB43K1+4/WlKdvfgwQVXm0ZQ2+jMBf+165UBEEuuWT2WqXeKkkUqPQta5lrt4eFfbo53JcOO4D5fDSGQS5bZAgMBAAECgYAor4I/AXEQXeLsKtTMxMmY77uIPi0gZdfWqUGOFhIJOw4eKZEzGp++I+MWPPVieCnT55vcTmm2zg13uP0fVykmukWqZszG/ZNpPKYleOqnZOqQj7O3au8Ywz18F/pqD++PsUzxRVeXxSOOwmjQ0D2Pe/9yutz62pyiFGAzDsaI6QJBAMn8DeBT3AtcWuONdiHL3yC4NkGJDdyBbMOaWyvrcvUUZr13uS9mZO6pLTN6v9tkmPUdvYxcPTJ9wdGR7NcNPDsCQQD6qluGI2VAlz4s5UoDnelFKrwDPeiruE3I6wsrasK6h37DsAE6OrQgx2dm4yH7ntJHUlJCZ5ay1EBNfEexgQv7AkA1r2vUwxVKY7q4nqHWa8SbgrrRAmePw0qwVreC3erJHyoLk+XBpnqPQKIF+8tAueU5yTTXOLD/WZOJazrDEf5/AkBpwG+Ggu5Xtrcbd8ynA/sDHElf0MGVmNbwOgFnWs42pa1cX6fU6ilOXvIH3TFcF6A9SMS9kThpz9QlHJaek4P7AkAavQillA/wnrha9GsK5UFmzmwNfkjLLW4psAUsXOsqFXWMoxTd0xWuSbuVOzERpbFMBl1VoZQmD9BLSVOTNe+v'
DEFAULT_P7 = '__7418529630abcdef'


def identify(value):
    text = str(value or '').strip()
    if re.fullmatch(r'\d+', text):
        return text
    found = re.search(r'https?://[^\s<>"\']+', html.unescape(text))
    if not found:
        return ''
    url = urlsplit(found.group())
    if not any(url.hostname == domain or (url.hostname or '').endswith('.' + domain) for domain in PLATFORM['hosts']):
        return ''
    query = {k.lower(): v for k, v in parse_qs(url.query).items()}
    book_id = next((query[k][0] for k in ('bid', 'bookid', 'book_id') if query.get(k)), '')
    if not book_id:
        match = re.search(r'/(?:book|detail|books)/(\d+)', url.path)
        book_id = match.group(1) if match else ''
    return book_id if book_id.isdigit() else ''


def _text(value):
    return html.unescape(re.sub(r'<[^>]*>', '', re.sub(r'<br\s*/?>', '\n', str(value or ''), flags=re.I))).strip()


def _int(value):
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def _session():
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(limit=4), headers={'User-Agent': 'okhttp/4.9.0'})


def _url(value):
    if str(value).startswith('/'):
        return BASE + str(value)
    url = urlsplit(str(value))
    if url.scheme not in ('http', 'https') or not any(url.hostname == host or (url.hostname or '').endswith('.' + host) for host in CDN_HOSTS):
        raise ProviderError('invalid_upstream_response')
    return str(value)


async def _request(session, url, *, params=None, data=None, raw=False):
    for attempt in range(2):
        try:
            async with session.request('POST' if data is not None else 'GET', _url(url), params=params, data=data, allow_redirects=False) as response:
                response.raise_for_status()
                if response.status >= 300:
                    raise ProviderError('invalid_upstream_response')
                payload = await response.read()
            return payload if raw else json.loads(payload.decode('utf-8-sig'))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt:
                raise ProviderError('upstream_unavailable') from None
            await asyncio.sleep(.25)


async def _detail(session, book_id):
    data = await _request(session, '/zybk/api/detail/index', params={'p3': '17111111', 'p2': '1', 'p4': '1', 'bid': book_id})
    if not isinstance(data, dict) or data.get('code') != 0:
        raise ProviderError('book_not_found')
    info = (data.get('body') or {}).get('bookInfo') or {}
    if str(info.get('bookId') or '') != book_id or not info.get('bookName'):
        raise ProviderError('book_not_found')
    cover = str(info.get('picUrl') or info.get('coverUrl') or '')
    if cover.startswith('//'):
        cover = 'https:' + cover
    return {'sourceBookId': book_id, 'title': _text(info['bookName']), 'author': _text(info.get('author')), 'status': '已完结' if info.get('completeState') == 'Y' else '连载中', 'wordCount': _int(info.get('wordCount') or info.get('wordNum')), 'chapterCount': _int(info.get('chapterCount') or info.get('totalChapterNum')), 'intro': _text(info.get('desc')), 'coverUrl': cover}


async def get_book(book_id):
    async with _session() as session:
        return await _detail(session, str(book_id))


def _credentials():
    params = {'p3': '25272056', 'usr': str(secrets.randbelow(90_000_000) + 10_000_000), 'p7': DEFAULT_P7, 'p31': DEFAULT_P7, 'p30': '__', 'devId': DEFAULT_P7}
    try:
        override = json.loads(os.environ.get('NOVEL_DEJIAN_SESSION') or '{}')
    except (TypeError, ValueError):
        raise ProviderError('credentials_required') from None
    if not isinstance(override, dict):
        raise ProviderError('credentials_required')
    params.update(override)
    key = os.environ.get('NOVEL_DEJIAN_SIGN_KEY') or DEFAULT_SIGN_KEY
    if not isinstance(params, dict) or not params.get('usr') or not params.get('devId') or not key:
        raise ProviderError('credentials_required')
    if any(not isinstance(value, (str, int, float, bool)) for value in params.values()):
        raise ProviderError('credentials_required')
    try:
        signer = serialization.load_der_private_key(base64.b64decode(key, validate=True), password=None)
    except (TypeError, ValueError):
        raise ProviderError('credentials_required') from None
    return {str(k): str(v) for k, v in params.items()}, signer


async def _catalog(session, book_id):
    raw = await _request(session, '/zybook/u/p/api.php', params={'Act': 'getChapterListVersion', 'p4': '501656', 'bid': book_id}, raw=True)
    text = raw.decode('utf-8-sig')
    rows = [{'id': int(cid), 'title': _text(title)} for cid, title in re.findall(r'<cp>\s*<id>(\d+)</id>.*?<cn>(.*?)</cn>', text, re.S)]
    total = re.search(r'<totalRecord>(\d+)</totalRecord>', text)
    if not rows or len({row['id'] for row in rows}) != len(rows) or (total and int(total.group(1)) != len(rows)):
        raise ProviderError('chapter_unavailable')
    return rows


async def _download_list(session, book_id, params, count):
    data = await _request(session, '/zybook3/u/p/api.php', params={**params, 'Act': 'batchDownloadChapteres', 'bid': book_id})
    address = str((data.get('body') or {}).get('downUrl') or '')
    if not address:
        raise ProviderError('chapter_unavailable')
    chapters = {}
    start = 1
    # 以目录条数限制分页，防止上游错误造成无限循环。
    for _ in range(count + 1):
        data = await _request(session, address, params={'startChapID': start})
        body = data.get('body') or {}
        rows = body.get('downInfo') or []
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            if isinstance(row, dict) and _int(row.get('chapterId')):
                chapters[_int(row['chapterId'])] = row
        last = max(chapters, default=0)
        if last < start:
            raise ProviderError('chapter_unavailable')
        if body.get('end'):
            break
        start = last + 1
    return chapters


async def _chapter(session, book_id, row, item, params, signer):
    payload = {'bookId': book_id, 'chapterId': str(row['id']), 'devId': params['devId'], 'usrName': params['usr'], 'timestamp': str(int(time.time() * 1000))}
    message = '&'.join(f'{key}={payload[key]}' for key in sorted(payload) if str(payload[key]))
    payload['sign'] = base64.b64encode(signer.sign(message.encode(), padding.PKCS1v15(), hashes.SHA1())).decode()
    payload.update({'type': '0', 'fid': '72'})
    auth = await _request(session, '/dj_drm/djdrm/getAuthChapter', params=params, data=payload)
    body = auth.get('body') or {}
    chapter_auth = body.get(f"chapter_{row['id']}") or {}
    token = chapter_auth.get('token') or body.get('token')
    if not token:
        raise ProviderError('chapter_unavailable')
    address = item.get('url') or item.get('downUrl') or item.get('downloadUrl')
    if not address:
        raise ProviderError('chapter_unavailable')
    raw = await _request(session, address, raw=True)
    from .得间解密 import 解密得间正文
    try:
        content = await asyncio.to_thread(解密得间正文, raw, str(token), params['usr'], params['devId'])
    except Exception:
        raise ProviderError('chapter_unavailable') from None
    if not str(content or '').strip():
        raise ProviderError('chapter_unavailable')
    return {'title': row['title'], 'content': str(content).strip()}


async def download_book(book_id, on_progress):
    book_id = str(book_id)
    params, signer = _credentials()
    async with _session() as session:
        book = await _detail(session, book_id)
        rows = await _catalog(session, book_id)
        validate_catalog_count(book, len(rows))
        available = await _download_list(session, book_id, params, len(rows))
        if any(row['id'] not in available for row in rows):
            raise ProviderError('chapter_unavailable')
        completed = 0
        sem = asyncio.Semaphore(4)
        on_progress(len(rows), 0)
        async def one(row):
            nonlocal completed
            async with sem:
                result = await _chapter(session, book_id, row, available[row['id']], params, signer)
                completed += 1
                on_progress(len(rows), completed)
                return result
        chapters = await asyncio.gather(*(one(row) for row in rows))
        return {'book': book, 'chapters': chapters}

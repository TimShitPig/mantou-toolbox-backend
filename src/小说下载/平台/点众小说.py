"""从现有小说 App 协议整理的独立提供器。"""
from __future__ import annotations
import asyncio
import html
import json
import logging
import random
import re
import time
import uuid
import urllib.parse
from typing import Any
import aiohttp
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from .公共 import ProviderError, validate_catalog_count
logger = logging.getLogger(__name__)

def _text(value):
    return html.unescape(re.sub(r'<[^>]*>', '', re.sub(r'<br\s*/?>', '\n', str(value or ''), flags=re.I))).strip()

def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

def _session():
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(limit=4))

KEY = b'dz#7gfy)@#ylgz&m'

IV = b'$#iupdo)8^dcr*pt'

ST = 'l1t5u51n1wk1yfor1ncrypt'

BASE = 'https://asgportal.dianzhong.com/asg-portal/portal/client'

CHARS = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'

UA = 'Mozilla/5.0 (Linux; Android 12; SM-G9900 Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Safari/537.36'

def enc(text: str) -> str:
    return AES.new(KEY, AES.MODE_CBC, IV).encrypt(pad(text.encode('utf-8'), 16)).hex()

def dec(hex_str: str) -> str:
    return unpad(AES.new(KEY, AES.MODE_CBC, IV).decrypt(bytes.fromhex(hex_str)), 16).decode('utf-8')

def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))

def gen_utdid_tmp(ts_ms=None) -> str:
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    date = time.strftime('%Y%m%d%H%M%S', time.localtime(ts_ms / 1000.0))
    ms = f'{ts_ms % 1000:03d}'
    rand6 = ''.join((random.choice(CHARS) for _ in range(6)))
    return 'A' + date + ms + rand6

def make_datas():
    now = int(time.time() * 1000)
    sid = str(uuid.uuid4())
    return {'version': '7.3.0', 'pname': 'com.dianzhong.reader', 'channelCode': 'TAXSEO1000000', 'utdidTmp': gen_utdid_tmp(now), 'token': '', 'utdid': '', 'os': 'android', 'osv': 32, 'brand': 'Samsung', 'model': 'SM-G9900', 'manu': 'Samsung', 'userId': '', 'launch': 'third', 'mchid': '', 'nchid': 'TAXSEO1000000', 'session1': sid, 'session2': sid, 'installTime': now, 'p': 20, 'sex': 1, 'launchNum': 1, 'visitor': 1, 'supportAd': 1, 'changeChidDate': now}

async def 异步调用接口(session: aiohttp.ClientSession, api: int, body: dict[str, Any], datas: dict[str, Any], *, timeout: int=20) -> dict[str, Any]:
    body_plain = dumps(body)
    headers = {'User-Agent': 'okhttp/4.10.0', 'Accept-Encoding': 'gzip', 'Content-Type': 'application/json; charset=utf-8', 'st': ST, 'datas': enc(dumps(datas))}
    请求超时 = aiohttp.ClientTimeout(total=max(1, int(timeout or 20)))
    async with session.post(f'{BASE}/{api}', data=enc(body_plain), headers=headers, timeout=请求超时) as response:
        response.raise_for_status()
        文本 = await response.text(errors='replace')
    try:
        raw = json.loads(文本) if 文本 else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError('点众接口未返回JSON') from exc
    if not isinstance(raw, dict):
        raise RuntimeError('点众接口响应格式异常')
    data_plain = None
    data_json = None
    data = raw.get('data')
    if isinstance(data, str) and data:
        try:
            data_plain = dec(data)
            data_json = json.loads(data_plain)
        except Exception:
            data_plain = data
            try:
                明文回退 = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                明文回退 = None
            if isinstance(明文回退, dict):
                data_json = 明文回退
    return {'http': response.status, 'raw': raw, 'data_json': data_json, 'data_plain': data_plain}

async def 初始化设备(session: aiohttp.ClientSession) -> dict[str, Any]:
    datas = make_datas()
    body = {'oaid': '', 'userAgent': UA, 'upgradeUserId': '', 'requestType': 1, 'ocpcSeconds': 0, 'lastLeftPage': ''}
    res = await 异步调用接口(session, 1001, body, datas)
    raw = res['raw'] if isinstance(res['raw'], dict) else {}
    user_id = raw.get('userId')
    if user_id is None and isinstance(res['data_json'], dict):
        user_id = res['data_json'].get('userId')
        if user_id is None:
            user_id = (res['data_json'].get('userInfoVo') or {}).get('userId')
    if not user_id:
        raise RuntimeError('点众设备初始化失败')
    datas['userId'] = str(user_id)
    datas['visitor'] = 0
    if raw.get('changeChidDate'):
        datas['changeChidDate'] = raw['changeChidDate']
    return datas

async def 异步获取详情(session: aiohttp.ClientSession, datas: dict[str, Any], book_id: str) -> dict[str, Any]:
    res = await 异步调用接口(session, 1111, {'bookId': str(book_id), 'chapterId': ''}, datas)
    data = res.get('data_json')
    if isinstance(data, dict):
        book = data.get('bookDetail') or data.get('bookInfo') or data.get('book') or data
        if isinstance(book, dict):
            return book
    return {}

async def 异步获取目录(session: aiohttp.ClientSession, datas: dict[str, Any], book_id: str) -> list[dict[str, Any]]:
    首页响应 = await 异步调用接口(session, 1304, {'bookId': str(book_id), 'chapterIndex': 0, 'currentChapterId': ''}, datas)
    首页数据 = 首页响应.get('data_json') if isinstance(首页响应.get('data_json'), dict) else {}
    首页 = 首页数据.get('chapterList') or 首页数据.get('chapters') or 首页数据.get('list') or []
    if not isinstance(首页, list) or not 首页:
        return []
    book_info = 首页数据.get('bookInfo') if isinstance(首页数据.get('bookInfo'), dict) else {}
    try:
        目录总数 = int(book_info.get('totalChapterNum') or 0)
    except (TypeError, ValueError):
        目录总数 = 0
    所有窗口: list[list[dict[str, Any]]] = [首页]
    if 目录总数 > len(首页):
        中心下标 = list(range(101, 目录总数 + 50, 101))
        信号量 = asyncio.Semaphore(min(最大目录并发数, len(中心下标)))

        async def 获取窗口(下标: int) -> list[dict[str, Any]]:
            try:
                async with 信号量:
                    res = await 异步调用接口(session, 1304, {'bookId': str(book_id), 'chapterIndex': 下标, 'currentChapterId': ''}, datas)
                data = res.get('data_json') if isinstance(res.get('data_json'), dict) else {}
                items = data.get('chapterList') or data.get('chapters') or data.get('list') or []
                return items if isinstance(items, list) else []
            except Exception as exc:
                logger.debug(f'点众目录窗口请求失败：书籍编号={book_id}, 序号={下标}, 错误={type(exc).__name__}')
                return []
        所有窗口.extend((窗口 for 窗口 in await asyncio.gather(*(获取窗口(下标) for 下标 in 中心下标)) if 窗口))
    章节映射: dict[str, tuple[int, dict[str, Any]]] = {}
    后备下标 = 0
    for 窗口 in 所有窗口:
        for it in 窗口:
            if not isinstance(it, dict):
                continue
            cid = str(it.get('chapterId') or it.get('id') or '').strip()
            if not cid:
                continue
            try:
                排序下标 = int(it.get('index'))
            except (TypeError, ValueError):
                排序下标 = 目录总数 + 后备下标
                后备下标 += 1
            章节映射[cid] = (排序下标, {'id': cid, 'title': str(it.get('chapterName') or it.get('title') or it.get('name') or f'章节{cid}'), 'has_lock': it.get('hasLock') is True or str(it.get('hasLock') or '').lower() in {'1', 'true'}})
    结果 = [章节 for _, 章节 in sorted(章节映射.values(), key=lambda 项: 项[0])]
    if 目录总数 and len(结果) != 目录总数:
        logger.warning(f'点众小说目录不完整：书籍编号={book_id}, 成功={len(结果)}, 总数={目录总数}')
        return []
    return 结果

def _提取正文(data: dict[str, Any]) -> str:
    for key in ('content', 'chapterContent', 'text', 'txt'):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    chapter = data.get('chapterInfo') or data.get('chapter') or {}
    if isinstance(chapter, dict):
        for key in ('content', 'chapterContent', 'text', 'txt'):
            val = chapter.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ''
PLATFORM = {'id': 'dianzhong', 'name': '点众小说', 'hosts': ['dianzhong.com'], 'aliases': ['点众'], 'coverHosts': ['dianzhong.com', 'dzbook.net', 'dzread.cn'], 'credentials': []}
最大目录并发数 = 4

def identify(value):
    text = str(value or '').strip()
    if re.fullmatch(r'\d+', text):
        return text
    found = re.search(r'https?://[^\s<>"\']+', html.unescape(text))
    if not found:
        return ''
    url = urllib.parse.urlsplit(found.group())
    if not (url.hostname == 'dianzhong.com' or (url.hostname or '').endswith('.dianzhong.com')):
        return ''
    query = {k.lower(): v for k,v in urllib.parse.parse_qs(url.query).items()}
    value = next((query[key][0] for key in ('bookid','book_id','bid') if query.get(key)), '')
    if not value:
        match = re.search(r'/(?:book|detail|chapter)/(\d+)', url.path)
        value = match.group(1) if match else ''
    return value if value.isdigit() else ''

def _metadata(book_id, data):
    title = _text(data.get('title') or data.get('bookName'))
    if not title:
        raise ProviderError('book_not_found')
    status = str(data.get('status') or data.get('serialStatus') or '')
    cover = str(data.get('coverUrl') or data.get('cover') or data.get('bookCover') or data.get('bookCoverUrl') or data.get('picUrl') or data.get('coverWap') or '')
    if cover.startswith('//'):
        cover = 'https:' + cover
    return {'sourceBookId': book_id, 'title': title, 'author': _text(data.get('author') or data.get('authorName')), 'status': '已完结' if '完' in status or str(data.get('isEnd')).lower() in ('1','true') else '连载中', 'wordCount': _int(data.get('wordCount') or data.get('words') or data.get('totalWordSize') or data.get('totalWords') or data.get('wordSize') or data.get('wordNum')), 'chapterCount': _int(data.get('totalChapterNum') or data.get('chapterCount')), 'intro': _text(data.get('intro') or data.get('description') or data.get('bookDesc') or data.get('desc')), 'coverUrl': cover}

async def get_book(book_id):
    async with _session() as session:
        device = await 初始化设备(session)
        return _metadata(str(book_id), await 异步获取详情(session, device, str(book_id)))

async def download_book(book_id, on_progress):
    book_id = str(book_id)
    async with _session() as session:
        device = await 初始化设备(session)
        book = _metadata(book_id, await 异步获取详情(session, device, book_id))
        rows = await 异步获取目录(session, device, book_id)
        validate_catalog_count(book, len(rows))
        if not rows or any(row.get('has_lock') for row in rows):
            raise ProviderError('chapter_unavailable')
        completed = 0
        sem = asyncio.Semaphore(4)
        on_progress(len(rows), 0)
        async def one(row):
            nonlocal completed
            async with sem:
                result = await 异步调用接口(session, 1303, {'bookId': book_id, 'chapterId': row['id'], 'offset': 0, 'preload': '0'}, device)
                data = result.get('data_json') or {}
                if not isinstance(data, dict) or str(data.get('status')) == '5' or data.get('orderPageVo'):
                    raise ProviderError('chapter_unavailable')
                content = _text(_提取正文(data))
                if not content:
                    raise ProviderError('chapter_unavailable')
                completed += 1
                on_progress(len(rows), completed)
                return {'title': _text(row['title']), 'content': content}
        chapters = await asyncio.gather(*(one(row) for row in rows))
        return {'book': book, 'chapters': chapters}

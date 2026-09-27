"""从现有小说 App 协议整理的独立提供器。"""
from __future__ import annotations
import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import re
import time
import urllib.parse
from typing import Any
import aiohttp
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from .公共 import ProviderError, validate_catalog_count
logger = logging.getLogger(__name__)


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

def _session():
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), connector=aiohttp.TCPConnector(limit=4))

追书包名 = 'com.ushaqi.zhuishushenqi.adfree'

追书渠道 = 'zhuishuFree'

追书应用标识 = 'F1d36851BC0e5943042b261dFcFEd0e5'

追书第三方令牌密钥 = b'5fFf6D94079904826ab080B8179E9376'



追书接口主机 = 'https://api.zhuishushenqi.com'

追书书籍接口主机 = ('https://bookapi01.zhuishushenqi.com', 'https://bookapi02.zhuishushenqi.com', 'https://bookapi03.zhuishushenqi.com', 'https://bookapi04.zhuishushenqi.com', 'https://bookapi05.zhuishushenqi.com')

追书默认章节主机 = ('https://chapter3.zhuishushenqi.com', 'https://chapterup3.zhuishushenqi.com', 'https://chapter2.zhuishushenqi.com')

追书用户代理 = 'ZhuiShuShenQi/3.45.95 (Android 9; Samsung Marlin / Samsung SM-N9760; China Mobile GSM)[preload=false;locale=zh_CN;clientidbase=]'

正文降级提示 = ('请安装最新版追书', '版权到期', '不再提供在线阅读', '该书已下架', '暂无阅读内容')

class ZhuishuError(RuntimeError):
    pass

def _需要AES() -> None:
    if AES is None or unpad is None:
        raise ZhuishuError('缺少 pycryptodome 依赖')

def _清理文本(value: Any) -> str:
    text = html.unescape(str(value or ''))
    text = re.sub('<br\\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub('<[^>]+>', '', text)
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return '\n'.join((line.strip() for line in text.split('\n'))).strip()

def _取字段(obj: Any, *keys: str, default: Any=None) -> Any:
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if value is not None and value != '':
            return value
    return default

def _安全整数(value: Any, default: int=0) -> int:
    try:
        if value is None or value == '':
            return default
        if isinstance(value, bool):
            return int(value)
        return int(float(str(value).replace(',', '').replace('，', '')))
    except Exception:
        return default

def _转布尔(value: Any, default: bool=False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == '':
        return default
    text = str(value).strip().lower()
    if text in {'1', 'true', 'yes', 'on', 'y', '是', '开启'}:
        return True
    if text in {'0', 'false', 'no', 'off', 'n', '否', '关闭'}:
        return False
    return default



def _第三方令牌() -> str:
    _需要AES()
    nonce = os.urandom(12)
    cipher = AES.new(追书第三方令牌密钥, AES.MODE_GCM, nonce=nonce)
    cipher.update(追书应用标识.encode('ascii'))
    plain = json.dumps({'time': int(time.time() * 1000)}, separators=(',', ':')).encode('utf-8')
    encrypted, tag = cipher.encrypt_and_digest(plain)
    return 追书应用标识 + ':' + (nonce + encrypted + tag).hex()

def _请求头(*, 需要令牌: bool=False, extra: dict[str, str] | None=None) -> dict[str, str]:
    headers = {'User-Agent': 追书用户代理, 'X-User-Agent': 追书用户代理, 'x-app-name': 追书渠道, 'X-Channel': 'FTencent', 'X-Uid': 追书用户标识, 'X-Device-Id': 追书设备标识, 'B-Zssq': 追书设备标识, 'x-android-id': 追书设备标识, 'weskitType': 'free', 'Accept': 'application/json, text/plain, */*', 'Accept-Encoding': 'gzip'}
    if 需要令牌:
        headers['third-token'] = _第三方令牌()
    if extra:
        headers.update(extra)
    return headers

async def _请求JSON(session: aiohttp.ClientSession, url: str, *, params: dict[str, Any] | None=None, data: Any=None, method: str='GET', headers: dict[str, str] | None=None) -> Any:
    try:
        async with session.request(method.upper(), url, params=params or None, data=data, headers=headers, allow_redirects=True) as response:
            body = await response.read()
            if response.status >= 400:
                raise ZhuishuError(f'HTTP {response.status}')
            if not body:
                raise ZhuishuError('空响应')
            try:
                result = json.loads(body.decode('utf-8', errors='replace'))
            except Exception as exc:
                raise ZhuishuError('响应不是 JSON') from exc
            if isinstance(result, dict) and result.get('errors'):
                raise ZhuishuError('接口返回错误')
            return result
    except asyncio.CancelledError:
        raise
    except ZhuishuError:
        raise
    except Exception as exc:
        raise ZhuishuError('网络请求异常') from exc


def _标准化列表(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('books', 'data', 'list', 'tocs', 'toc', 'sources'):
            value = data.get(key)
            if isinstance(value, list):
                return value
            nested = _标准化列表(value)
            if nested:
                return nested
    return []

async def 获取书籍详情(session: aiohttp.ClientSession, 书籍编号: str) -> dict[str, Any]:
    for host in (追书接口主机, *追书书籍接口主机):
        try:
            数据 = await _请求JSON(session, f'{host}/book/{书籍编号}', headers=_请求头())
            if isinstance(数据, dict) and (数据.get('_id') or 数据.get('id') or 数据.get('title')):
                return 数据
        except Exception:
            continue
    for host in 追书书籍接口主机:
        try:
            数据 = await _请求JSON(session, f'{host}/book/crypto/{书籍编号}', params={'timestamp': int(time.time() * 1000), 'token': '', 'useNewCat': 'true', 'packageName': 追书包名}, headers=_请求头(需要令牌=True))
            if isinstance(数据, dict):
                return 数据
        except Exception:
            continue
    raise ZhuishuError('详情获取失败')

async def _选择目录编号(session: aiohttp.ClientSession, 书籍编号: str) -> str:
    for host in 追书书籍接口主机:
        try:
            数据 = await _请求JSON(session, f'{host}/btoc/crypto', params={'book': 书籍编号, 'view': 'summary', 'platform': 'android', 'token': ''}, headers=_请求头(需要令牌=True))
            for item in _标准化列表(数据):
                if isinstance(item, dict):
                    编号 = _取字段(item, '_id', 'id', 'tocId', 'toc_id')
                    if 编号:
                        return str(编号)
        except Exception:
            continue
    for endpoint in ('atoc', 'ctoc'):
        try:
            数据 = await _请求JSON(session, f'{追书接口主机}/{endpoint}', params={'book': 书籍编号, 'view': 'summary', 'platform': 'android'}, headers=_请求头())
            for item in _标准化列表(数据):
                if isinstance(item, dict):
                    编号 = _取字段(item, '_id', 'id', 'tocId', 'toc_id')
                    if 编号:
                        return str(编号)
        except Exception:
            continue
    return ''

async def 获取目录(session: aiohttp.ClientSession, 书籍编号: str) -> dict[str, Any]:
    目录编号 = await _选择目录编号(session, 书籍编号)
    候选 = []
    if 目录编号:
        for host in 追书书籍接口主机:
            候选.append((f'{host}/dtoc/crypto/{书籍编号}/{目录编号}', {'view': 'chapters', 'platform': 'android', 'token': ''}, _请求头(需要令牌=True)))
        for endpoint in ('atoc', 'ctoc'):
            候选.append((f'{追书接口主机}/{endpoint}/{目录编号}', {'view': 'chapters', 'platform': 'android'}, _请求头()))
    for host in 追书书籍接口主机:
        for identifier in (目录编号, 书籍编号):
            if not identifier:
                continue
            候选.append((f'{host}/dtoc/{identifier}', {'view': 'chapters', 'platform': 'android', 'token': '', 'packageName': 追书包名}, _请求头(需要令牌=True)))
    for url, params, headers in 候选:
        try:
            数据 = await _请求JSON(session, url, params=params, headers=headers)
            if not isinstance(数据, dict):
                continue
            if isinstance(数据.get('data'), dict) and 数据['data'].get('chapters'):
                数据 = dict(数据['data'])
            if 数据.get('chapters'):
                if 目录编号:
                    数据.setdefault('_id', 目录编号)
                return 数据
        except Exception:
            continue
    raise ZhuishuError('目录获取失败')




def _章节内容对象(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    for key in ('chapter', 'data'):
        if isinstance(data.get(key), dict):
            return data[key]
    return data

def _正文是降级提示(text: Any) -> bool:
    value = _清理文本(text)
    return any((marker in value for marker in 正文降级提示))

def _提取密文(data: Any) -> tuple[str, str]:
    chapter = _章节内容对象(data)
    cp = _取字段(chapter, 'cpContent', 'content', default='')
    if cp:
        return ('cipher', str(cp))
    images = _取字段(chapter, 'images', default='')
    if images:
        return ('cipher', str(images))
    body = _取字段(chapter, 'body', 'text', default='')
    return ('plain', str(body or ''))

PLATFORM = {'id': 'zhuishu', 'name': '追书小说', 'hosts': ['zhuishushenqi.com'], 'aliases': ['追书','追书神器'], 'coverHosts': ['zhuishushenqi.com'], 'credentials': []}
追书设备标识 = base64.b64encode(os.urandom(16)).decode('ascii')
追书用户标识 = hashlib.md5(追书设备标识.encode('ascii')).hexdigest()[:24]

def identify(value):
    text = str(value or '').strip()
    if re.fullmatch(r'[a-fA-F0-9]{24}', text):
        return text
    found = re.search(r'https?://[^\s<>"\']+', html.unescape(text))
    if not found:
        return ''
    url = urllib.parse.urlsplit(found.group())
    if not (url.hostname == 'zhuishushenqi.com' or (url.hostname or '').endswith('.zhuishushenqi.com')):
        return ''
    query = {k.lower(): v for k,v in urllib.parse.parse_qs(url.query).items()}
    value = next((query[k][0] for k in ('bookid','book_id','id') if query.get(k)), '')
    if not value:
        match = re.search(r'/(?:books?|novel)/([a-fA-F0-9]{24})', url.path)
        value = match.group(1) if match else ''
    return value if re.fullmatch(r'[a-fA-F0-9]{24}', value) else ''

def _metadata(book_id, data):
    title = _清理文本(_取字段(data,'title','name','book_name'))
    if not title:
        raise ProviderError('book_not_found')
    cover = str(_取字段(data,'cover','coverUrl','cover_url',default='') or '')
    if cover.startswith('/agent/'):
        cover = urllib.parse.unquote(cover[len('/agent/'):])
    if cover.startswith('//'):
        cover = 'https:' + cover
    elif cover.startswith('/'):
        cover = 'https://statics.zhuishushenqi.com' + cover
    return {'sourceBookId':book_id, 'title':title, 'author':_清理文本(_取字段(data,'author','originalAuthor')), 'status':'连载中' if _转布尔(_取字段(data,'isSerial','is_serial'), True) else '已完结', 'wordCount':_安全整数(_取字段(data,'wordCount','word_count','words')), 'chapterCount':_安全整数(_取字段(data,'chaptersCount','chapterCount')), 'intro':_清理文本(_取字段(data,'longIntro','shortIntro','intro','description')), 'coverUrl':cover}

async def get_book(book_id):
    async with _session() as session:
        return _metadata(str(book_id), await 获取书籍详情(session, str(book_id)))

async def _chapter_data(session, row):
    link = str(row.get('link') or row.get('url') or '')
    url = urllib.parse.urlsplit(link)
    if not link or not (url.hostname == 'zhuishushenqi.com' or (url.hostname or '').endswith('.zhuishushenqi.com')):
        raise ProviderError('chapter_unavailable')
    endpoint = 'picture2' if url.hostname == 'picture.zhuishushenqi.com' else 'chapter2'
    for host in 追书默认章节主机:
        try:
            data = await _请求JSON(session, host + '/' + endpoint + '/' + urllib.parse.quote(link, safe=''), headers=_请求头())
            kind, content = _提取密文(data)
            if content:
                return kind, content
        except ZhuishuError:
            continue
    raise ProviderError('chapter_unavailable')

async def download_book(book_id, on_progress):
    book_id = str(book_id)
    async with _session() as session:
        book = _metadata(book_id, await 获取书籍详情(session, book_id))
        catalog = await 获取目录(session, book_id)
        rows = catalog.get('chapters')
        if not isinstance(rows, list) or not rows:
            raise ProviderError('chapter_unavailable')
        validate_catalog_count(book, len(rows))
        for row in rows:
            if not isinstance(row,dict) or any(_转布尔(row.get(k),False) for k in ('isVip','isvip','isLocked','locked','isLock')):
                raise ProviderError('chapter_unavailable')
        declared = _int(catalog.get('chaptersCount') or catalog.get('chapterCount'))
        if declared and declared != len(rows):
            raise ProviderError('chapter_unavailable')
        completed = 0
        sem = asyncio.Semaphore(4)
        on_progress(len(rows),0)
        async def one(index,row):
            nonlocal completed
            async with sem:
                kind, content = await _chapter_data(session,row)
                if kind == 'cipher':
                    raise ProviderError('chapter_unavailable')
                content = _清理文本(content)
                if not content or _正文是降级提示(content):
                    raise ProviderError('chapter_unavailable')
                completed += 1
                on_progress(len(rows),completed)
                return {'title':_清理文本(_取字段(row,'title','name',default=f'第{index+1}章')), 'content':content}
        chapters = await asyncio.gather(*(one(i,row) for i,row in enumerate(rows)))
        return {'book':book,'chapters':chapters}

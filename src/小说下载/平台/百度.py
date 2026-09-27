from __future__ import annotations
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import asyncio, html, json, logging, re, urllib.parse
from typing import Any
import aiohttp
from .公共 import ProviderError
from .组B公共 import book_id as _book_id, cover, metadata, validate_catalog_count, session as _session, retry as _retry, chapters_map
logger = logging.getLogger(__name__)
PLATFORM = {'id': 'baidu', 'name': '百度小说', 'hosts': ['boxnovel.baidu.com', 'novel.baidu.com', 'novelapi.baidu.com'], 'aliases': ['百度'], 'coverHosts': ['dss0.baidu.com', 'imgsrc.baidu.com', 'imgcdn.bcebos.com', 'novel-pic.cdn.bcebos.com', 't7.baidu.com', 'ss0.bdstatic.com', 'ss1.bdstatic.com'], 'credentials': []}
百度搜索地址 = 'https://novelapi.baidu.com/boxnovel/cors'
百度详情地址 = 百度搜索地址
百度目录地址 = 'https://novelapi.baidu.com/searchbox'
百度正文地址 = 百度目录地址
百度允许域名 = {'mr.baidu.com', 'boxnovel.baidu.com', 'novel.baidu.com'}
百度请求头 = {'User-Agent': 'Mozilla/5.0 (Linux; Android 10; V1838T Build/QP1A.190711.020; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 Mobile Safari/537.36 baiduboxapp/12.8.0.10', 'Accept': 'application/json, text/plain, */*', 'Referer': 'https://boxnovel.baidu.com/'}
百度固定UID = 'juB18g8oHi_-aH88lPHl8g8nHi_ju2avgi25ugi3Sf8R9WMxpiWmuYMaA'
百度固定UA = '_a-qiyuuvigyNE64I5me6NN0v8oZu-I4_C2Hiyat2iqlC'
百度AES密钥 = b'D0CD8B760CE07BC3'
百度AES向量 = b'2011121211143000'
百度请求重试次数 = 2

def _清理来源(值: Any) -> str:
    文本 = html.unescape(str(值 or '')).replace('\\/', '/').strip()
    return 文本.rstrip('"\'`，。；;]}>）)')

def _数字文本(值: Any) -> str:
    文本 = str(值 or '').strip()
    return 文本 if re.fullmatch('\\d{5,30}', 文本) else ''

def 解析百度书籍编号(来源: Any) -> str:
    文本 = _清理来源(来源)
    try:
        解析 = urllib.parse.urlsplit(文本)
    except Exception:
        return ''
    if (解析.hostname or '').lower() not in 百度允许域名:
        return ''
    查询: dict[str, list[str]] = {}
    for 部分 in (解析.query, urllib.parse.unquote(解析.fragment).lstrip('#?')):
        try:
            for 键, 值 in urllib.parse.parse_qs(部分, keep_blank_values=True).items():
                查询.setdefault(键.lower(), []).extend(值)
        except Exception:
            continue
    for 键 in ('gid', 'bookid', 'book_id', 'bookgid', 'novel_book_id'):
        for 值 in 查询.get(键, []):
            书籍编号 = _数字文本(值)
            if 书籍编号:
                return 书籍编号
    for 值列表 in 查询.get('data', []):
        当前 = 值列表
        for _ in range(2):
            当前 = urllib.parse.unquote_plus(str(当前))
        try:
            数据 = json.loads(当前)
        except Exception:
            数据 = None
        if isinstance(数据, dict):
            for 键 in ('gid', 'bookid', 'book_id', 'bookGid', 'novel_book_id', 'novelBookId'):
                书籍编号 = _数字文本(数据.get(键))
                if 书籍编号:
                    return 书籍编号
    路径匹配 = re.search('(?:book|novel|detail|reader)[^0-9]{0,20}(\\d{5,30})', 解析.path, re.IGNORECASE)
    if 路径匹配:
        return 路径匹配.group(1)
    原文匹配 = re.search('(?:gid|bookid|book_id)%?3?d%?22?%?3a?%?22?(\\d{5,30})', 文本, re.IGNORECASE)
    return 原文匹配.group(1) if 原文匹配 else ''

async def _请求JSON(会话: aiohttp.ClientSession, 地址: str, 参数: dict[str, Any]) -> dict[str, Any]:
    最后异常: Exception | None = None
    for 次数 in range(百度请求重试次数):
        try:
            async with 会话.get(地址, params=参数) as 响应:
                响应.raise_for_status()
                数据 = await 响应.json(content_type=None)
            return 数据 if isinstance(数据, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as 异常:
            最后异常 = 异常
            if 次数 + 1 < 百度请求重试次数:
                await asyncio.sleep(0.25 * (次数 + 1))
    raise RuntimeError('百度接口请求失败') from 最后异常

def _百度成功(数据: Any) -> bool:
    return isinstance(数据, dict) and str(数据.get('errno', '0')) == '0'

def _安全整数(值: Any, 默认值: int=0) -> int:
    if isinstance(值, bool):
        return 默认值
    try:
        return int(str(值).replace(',', '').strip())
    except (TypeError, ValueError):
        return 默认值

def _取详情字段(数据: dict[str, Any]) -> dict[str, Any]:
    节点 = 数据.get('novel', {}).get('detail', {}).get('data', {})
    if not isinstance(节点, dict):
        return {}
    return {'title': str(节点.get('title') or '未知').strip(), 'author': str(节点.get('author') or '未知').strip(), 'intro': str(节点.get('summary') or '').strip(), 'status': str(节点.get('status') or '连载').strip(), 'word_count': 节点.get('words_num') or 节点.get('wordCount') or '', 'chapter_count': _安全整数(节点.get('chapter_num')), 'coverUrl': cover(节点)}

async def 获取百度详情(会话: aiohttp.ClientSession, 书籍编号: str) -> dict[str, Any]:
    数据 = await _请求JSON(会话, 百度详情地址, {'osname': 'bdboxnovelsdk', 'action': 'novel', 'type': 'detail', 'tojsondata': '1', 'data': json.dumps({'gid': 书籍编号, 'frombox': True}, separators=(',', ':'))})
    if not _百度成功(数据):
        return {}
    return _取详情字段(数据)

async def 获取百度目录(会话: aiohttp.ClientSession, 书籍编号: str) -> list[dict[str, Any]]:
    数据 = await _请求JSON(会话, 百度目录地址, {'action': 'novel', 'type': 'chapter', 'data': json.dumps({'gid': 书籍编号}, separators=(',', ':'))})
    if not _百度成功(数据):
        return []
    项目列表 = 数据.get('data', {}).get('novel', {}).get('chapter', {}).get('dataset', {}).get('items', [])
    if not isinstance(项目列表, list):
        return []
    目录: list[dict[str, Any]] = []
    已见: set[str] = set()
    for 项目 in 项目列表:
        if not isinstance(项目, dict):
            continue
        编号 = str(项目.get('cid') or '').strip()
        标题 = str(项目.get('title') or '').strip()
        if 编号 and 标题 and (编号 not in 已见):
            已见.add(编号)
            目录.append({'id': 编号, 'title': 标题})
    return 目录

def _解密百度正文(密文: bytes) -> str:
    if not 密文 or AES is None or unpad is None:
        return ''
    try:
        明文 = unpad(AES.new(百度AES密钥, AES.MODE_CBC, 百度AES向量).decrypt(密文), AES.block_size)
        return 明文.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n').strip()
    except Exception:
        return ''

async def _下载百度章节(会话: aiohttp.ClientSession, 书籍编号: str, 章节: dict[str, Any], 信号量: asyncio.Semaphore) -> str:
    编号 = str(章节.get('id') or '')
    async with 信号量:
        for 次数 in range(百度请求重试次数):
            try:
                元数据 = await _请求JSON(会话, 百度正文地址, {'action': 'novel', 'type': 'content', 'uid': 百度固定UID, 'ua': 百度固定UA, 'ctv': '2', 'cen': 'ua_uid', 'data': json.dumps({'gid': 书籍编号, 'cid': 编号}, separators=(',', ':'))})
                内容地址 = 元数据.get('data', {}).get('novel', {}).get('content', {}).get('dataset', {}).get('content_url')
                if not isinstance(内容地址, str) or urllib.parse.urlsplit(内容地址).scheme not in {'http', 'https'}:
                    raise RuntimeError('content url missing')
                async with 会话.get(内容地址) as 响应:
                    响应.raise_for_status()
                    密文 = await 响应.read()
                if len(密文) > 16 * 1024 * 1024:
                    raise RuntimeError('content too large')
                正文 = await asyncio.to_thread(_解密百度正文, 密文)
                if 正文:
                    return 正文
                raise RuntimeError('empty content')
            except Exception as 异常:
                logger.debug('百度小说章节获取失败：章节=%s, 错误=%s', 编号, type(异常).__name__)
                if 次数 + 1 < 百度请求重试次数:
                    await asyncio.sleep(0.25 * (次数 + 1))
    return ''

def identify(value):
    text = str(value or '').strip()
    if re.fullmatch('\\d{1,30}', text):
        return text
    for candidate in re.findall('https?://[^\\s<>"\\\']+', text):
        found = 解析百度书籍编号(candidate.rstrip('，。；、）)]}'))
        if found:
            return found
    return ''

async def _load_book(http, identity):
    detail = await _retry(lambda: 获取百度详情(http, identity))
    if not detail:
        raise ProviderError('book_info_unavailable')
    return metadata(identity, detail.get('title'), detail.get('author'), detail.get('status'), detail.get('word_count'), detail.get('chapter_count'), detail.get('intro'), detail.get('coverUrl'))

async def get_book(book_id):
    async with _session(headers=百度请求头) as http:
        return await _load_book(http, _book_id(book_id))

async def download_book(book_id, on_progress=None):
    identity = _book_id(book_id)
    async with _session(headers=百度请求头) as http:
        book = await _load_book(http, identity)
        catalog = await _retry(lambda: 获取百度目录(http, identity))
        validate_catalog_count(book, len(catalog))
        semaphore = asyncio.Semaphore(4)

        async def fetch(chapter):
            content = await _下载百度章节(http, identity, chapter, semaphore)
            return {'title': chapter.get('title'), 'content': content}
        chapters = await chapters_map(catalog, fetch, on_progress)
        return {'book': book, 'chapters': chapters}

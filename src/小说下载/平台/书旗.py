from __future__ import annotations
import base64, hashlib, html, logging, re, time, urllib.parse
from dataclasses import dataclass
from typing import Any
import aiohttp
from .公共 import ProviderError
from .组B公共 import identify_id, book_id as _book_id, cover, metadata, validate_catalog_count, session as _session, retry as _retry, json_request, chapters_map
logger = logging.getLogger(__name__)
PLATFORM = {'id': 'shuqi', 'name': '书旗小说', 'hosts': ['shuqi.com', 'shuqireader.com'], 'aliases': ['书旗'], 'coverHosts': ['img-tail.shuqireader.com', 'img.shuqi.com', 'img.shuqireader.com'], 'credentials': []}
IOS目录URL = 'https://ocean.shuqireader.com/api/bcspub/iosapi/book/chapterlist'
IOS目录UID = '8000000'
IOS目录盐值 = '37e81a9d8f02596e1b895d07c171d5c9'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36'

class ShuqiError(RuntimeError):
    pass

@dataclass
class Chapter:
    index: int
    chapter_id: str
    name: str
    content_url: str
    word_count: int = 0

@dataclass
class Book:
    book_id: str
    book_name: str
    author_name: str
    chapter_num: int
    word_count: int
    intro: str
    status_text: str
    chapters: list[Chapter]
    raw: dict[str, Any]
    is_short: bool = False

async def 获取书籍(session: aiohttp.ClientSession, 书籍编号: str, 是否短篇: bool=False) -> Book:
    时间戳 = str(int(time.time()))
    目录参数 = {'reqEncryptType': '-1', 'resEncryptType': '-1', 'user_id': IOS目录UID, 'bookId': str(书籍编号), 'timestamp': 时间戳, 'sign': hashlib.md5(f'{书籍编号}{时间戳}{IOS目录UID}{IOS目录盐值}'.encode()).hexdigest()}
    响应 = await 请求JSON(session, IOS目录URL, params=目录参数)
    return 解析目录响应(书籍编号, 响应, 是否短篇)

def 解析目录响应(书籍编号: str, 响应: dict[str, Any], 是否短篇: bool=False) -> Book:
    状态 = str(响应.get('state') or 响应.get('status') or '')
    if 状态 and 状态 not in {'200', '0'}:
        raise ShuqiError(f'目录接口异常：state={状态}')
    数据 = 响应.get('data') if isinstance(响应.get('data'), dict) else {}
    if not 数据:
        raise ShuqiError('目录接口 data 为空')
    章节列表: list[Chapter] = []
    for 分卷 in 数据.get('chapterList') or []:
        if not isinstance(分卷, dict):
            continue
        for 项 in 分卷.get('volumeList') or []:
            if not isinstance(项, dict):
                continue
            章节编号 = str(项.get('chapterId') or '').strip()
            if not 章节编号:
                continue
            if 是否短篇:
                内容前缀 = str(数据.get('shortContUrlPrefix') or 数据.get('freeContUrlPrefix') or '')
                内容后缀 = str(项.get('shortContUrlSuffix') or 项.get('contUrlSuffix') or 项.get('freeContUrlSuffix') or '')
            else:
                内容前缀 = str(数据.get('freeContUrlPrefix') or '')
                内容后缀 = str(项.get('contUrlSuffix') or 项.get('freeContUrlSuffix') or 项.get('shortContUrlSuffix') or '')
            if 内容后缀.startswith('http://') or 内容后缀.startswith('https://'):
                正文地址 = 内容后缀
            elif 内容前缀 and 内容后缀:
                正文地址 = 内容前缀.rstrip('/') + (内容后缀 if 内容后缀.startswith('?') else '/' + 内容后缀.lstrip('/'))
            else:
                正文地址 = ''
            章节列表.append(Chapter(index=len(章节列表) + 1, chapter_id=章节编号, name=清理网页文本(项.get('chapterName') or f'第{len(章节列表) + 1}章'), content_url=正文地址, word_count=安全整数(项.get('wordCount') or 项.get('chapterWordCount'), 0)))
    if not 章节列表:
        raise ShuqiError('目录章节为空')
    目录章节数 = 安全整数(数据.get('chapterNum'), len(章节列表)) or len(章节列表)
    return Book(book_id=str(书籍编号), book_name=清理网页文本(数据.get('bookName') or f'书旗小说{书籍编号}'), author_name=清理网页文本(数据.get('authorName') or '未知') or '未知', chapter_num=目录章节数, word_count=获取书旗原始字数(数据, 章节列表), intro=获取书旗简介(数据), status_text=解析书旗状态(数据), chapters=章节列表, raw=数据, is_short=是否短篇)

def _生成书旗字符变换表() -> dict[int, int]:
    表: dict[int, int] = {}
    for 字符码 in range(ord('A'), ord('Z') + 1):
        偏移 = (字符码 + 32 - 83) % 26 or 26
        表[字符码] = 偏移 + 64
    for 字符码 in range(ord('a'), ord('z') + 1):
        偏移 = (字符码 - 83) % 26 or 26
        表[字符码] = 偏移 + 96
    return 表
书旗字符变换表 = str.maketrans(_生成书旗字符变换表())

def _书旗字符变换(密文: str) -> str:
    文本 = str(密文 or '')
    if 文本.isascii():
        return 文本.translate(书旗字符变换表)
    结果: list[str] = []
    for 字符 in 文本:
        if 字符.isalpha():
            大写 = 字符.isupper()
            偏移 = (ord(字符.lower()) - 83) % 26 or 26
            结果.append(chr(偏移 + (64 if 大写 else 96)))
        else:
            结果.append(字符)
    return ''.join(结果)

def _解码书旗正文(密文: str) -> str:
    if not str(密文 or '').strip():
        raise ShuqiError('章节正文为空')
    try:
        编码文本 = _书旗字符变换(密文)
        原始正文 = base64.b64decode(编码文本, validate=True).decode('utf-8')
    except Exception as exc:
        raise ShuqiError('章节正文解密失败') from exc
    正文 = html.unescape(原始正文).replace('<br/>', '\n')
    正文 = 正文.replace('\r\n', '\n').replace('\r', '\n')
    正文 = '\n'.join((行.lstrip(' \u3000') for 行 in 正文.split('\n'))).strip()
    if not 正文:
        raise ShuqiError('章节正文为空')
    return 正文

def 获取书旗原始字数(数据: dict[str, Any], 章节列表: list[Chapter]) -> int:
    for 字段名 in ('realTimeWordCount', 'wordCount', 'words', 'totalWordCount'):
        字数 = 安全整数(数据.get(字段名), 0)
        if 字数 > 0:
            return 字数
    return sum((章节.word_count for 章节 in 章节列表))

def 获取书旗简介(数据: dict[str, Any]) -> str:
    for 字段名 in ('intro', 'desc', 'description', 'bookDesc', 'summary'):
        简介 = 清理网页文本(数据.get(字段名))
        if 简介:
            return 简介
    return ''

def 解析书旗状态(数据: dict[str, Any]) -> str:
    for 字段名 in ('statusText', 'statusName', 'bookStatus', 'updateStatus'):
        文本 = 清理网页文本(数据.get(字段名))
        if '完结' in 文本 or '已完' in 文本:
            return '完结'
        if '连载' in 文本 or '更新' in 文本:
            return '连载'
    状态值 = str(数据.get('state') or 数据.get('updateType') or '').strip()
    return '完结' if 状态值 == '2' else '连载'

def 清理网页文本(文本: Any) -> str:
    return html.unescape(re.sub('<[^>]+>', '', str(文本 or ''))).strip()

def 安全整数(值: Any, 默认值: int=0) -> int:
    try:
        return int(值)
    except Exception:
        return 默认值

def identify(value):
    return identify_id(value, PLATFORM['hosts'], ['bid', 'bookId', 'book_id'], ['/(?:catalog|cover|book)/(\\d+)', '/v2/query/(\\d+)'])

async def 请求JSON(session, url, *, params=None):
    return await json_request(session, 'GET', url, params=params, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})

def _metadata(book):
    return metadata(book.book_id, book.book_name, book.author_name, book.status_text, book.word_count, book.chapter_num, book.intro, cover(book.raw))

async def get_book(book_id):
    if not IOS目录UID:
        raise ProviderError('credentials_required')
    async with _session() as http:
        return _metadata(await _retry(lambda: 获取书籍(http, _book_id(book_id))))

async def download_book(book_id, on_progress=None):
    if not IOS目录UID:
        raise ProviderError('credentials_required')
    identity = _book_id(book_id)
    async with _session() as http:
        detail = await _retry(lambda: 获取书籍(http, identity))
        book = _metadata(detail)
        validate_catalog_count(book, len(detail.chapters))

        async def fetch(chapter):
            if not chapter.content_url:
                raise ProviderError('chapter_unavailable')
            parsed = urllib.parse.urlsplit(chapter.content_url)
            if parsed.scheme not in {'http', 'https'}:
                raise ProviderError('chapter_unavailable')
            response = await json_request(http, 'GET', chapter.content_url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'})
            state = str(response.get('state') or response.get('status') or '')
            if state not in {'', '0', '200'} or not response.get('ChapterContent'):
                raise ProviderError('chapter_unavailable')
            try:
                content = _解码书旗正文(str(response['ChapterContent']))
            except Exception:
                raise ProviderError('chapter_unavailable') from None
            return {'title': chapter.name, 'content': content}
        chapters = await chapters_map(detail.chapters, fetch, on_progress)
        return {'book': book, 'chapters': chapters}

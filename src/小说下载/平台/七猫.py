from __future__ import annotations
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import base64, hashlib, html, json, logging, random, re, secrets
from typing import Any
import aiohttp
from .公共 import ProviderError
from .组B公共 import plain, identify_id, book_id as _book_id, first, cover, metadata, validate_catalog_count, session as _session, retry as _retry, json_request, checked
logger = logging.getLogger(__name__)
PLATFORM = {'id': 'qimao', 'name': '七猫小说', 'hosts': ['qimao.com', 'wtzw.com'], 'aliases': ['七猫'], 'coverHosts': ['cdn.wtzw.com', 'img.qimao.com', 'static.qimao.com'], 'credentials': []}
签名密钥 = 'd3dGiJc651gSQ8w1'
应用ID = 'com.kmxs.reader'
渠道名 = 'qm-guanfang_lf'
应用版本列表 = ['79105']
解密密钥 = bytes.fromhex('32343263636238323330643730396531')
QM参数字符映射 = {'+': 'P', '/': 'X', '0': 'M', '1': 'U', '2': 'l', '3': 'E', '4': 'r', '5': 'Y', '6': 'W', '7': 'b', '8': 'd', '9': 'J', 'A': '9', 'B': 's', 'C': 'a', 'D': 'I', 'E': '0', 'F': 'o', 'G': 'y', 'H': '_', 'I': 'H', 'J': 'G', 'K': 'i', 'L': 't', 'M': 'g', 'N': 'N', 'O': 'A', 'P': '8', 'Q': 'F', 'R': 'k', 'S': '3', 'T': 'h', 'U': 'f', 'V': 'R', 'W': 'q', 'X': 'C', 'Y': '4', 'Z': 'p', 'a': 'm', 'b': 'B', 'c': 'O', 'd': 'u', 'e': 'c', 'f': '6', 'g': 'K', 'h': 'x', 'i': '5', 'j': 'T', 'k': '-', 'l': '2', 'm': 'z', 'n': 'S', 'o': 'Z', 'p': '1', 'q': 'V', 'r': 'v', 's': 'j', 't': 'Q', 'u': '7', 'v': 'D', 'w': 'w', 'x': 'n', 'y': 'L', 'z': 'e'}

async def 获取小说详情(session: aiohttp.ClientSession, 书籍编号: str, 是否短篇: bool=False) -> dict[str, Any]:
    if 是否短篇:
        数据 = await 请求JSON(session, 'https://api-bc.wtzw.com/api/v1/story/detail', {}, 生成请求头(书籍编号, 'api-bc.wtzw.com'), 方法='POST', 表单=签名参数({'bookid': 书籍编号, 'book_privacy': '0', 'ex_bookids': ''}))
    else:
        数据 = await 请求JSON(session, 'https://api-bc.wtzw.com/api/v1/reader/detail', 签名参数({'id': 书籍编号}), 生成请求头(书籍编号, 'api-bc.wtzw.com'))
    详情 = 数据.get('data') if isinstance(数据, dict) else {}
    if not isinstance(详情, dict) or not 详情:
        raise RuntimeError('小说详情接口没有返回有效数据')
    if isinstance(详情.get('book'), dict):
        详情 = {**详情.get('book', {}), **详情}
    title = 清理网页文本(读取首个字段(详情, ('title', 'book_name', 'name', 'share_title')))
    if not title:
        raise ProviderError('book_info_unavailable')
    return {'title': title, 'author': 清理网页文本(读取首个字段(详情, ('author', 'author_name', 'pen_name')) or 读取字段路径(详情, ('author_info', 'name')) or '未知'), 'intro': plain(读取首个字段(详情, ('intro', 'description', 'desc', 'book_intro')) or '', True), 'words_num': 读取首个字段(详情, ('words_num', 'word_count', 'words', 'total_words')) or '', 'is_over': 读取首个字段(详情, ('is_over', 'is_finish', 'finish', 'completed')) or ('1' if 是否短篇 else ''), 'chapters': 读取首个字段(详情, ('chapters', 'chapter_count', 'chapter_num', 'total_chapters')) or '', 'coverUrl': cover(详情), 'chapter_list_desc': 清理网页文本(详情.get('chapter_list_desc') or ''), 'category_over_words': 清理网页文本(详情.get('category_over_words') or ''), 'tags': '、'.join((清理网页文本(标签.get('title') or '') for 标签 in 详情.get('book_tag_list', []) if isinstance(标签, dict) and 标签.get('title')))}

async def 获取小说目录(session: aiohttp.ClientSession, 书籍编号: str, 是否短篇: bool=False) -> list[dict[str, Any]]:
    数据 = await 请求JSON(session, 'https://api-ks.wtzw.com/api/v1/chapter/chapter-list', 签名参数({'chapter_ver': '0', 'id': 书籍编号, 'reader_type': '4' if 是否短篇 else '0'}), 生成请求头(书籍编号, 'api-ks.wtzw.com'))
    章节列表 = 读取字段路径(数据, ('data', 'chapter_lists')) or []
    目录 = [章节 for 章节 in 章节列表 if isinstance(章节, dict) and 章节.get('id')]
    return sorted(目录, key=lambda 章节: int(章节.get('chapter_sort') or 0))

async def 获取批量章节正文(session: aiohttp.ClientSession, 书籍编号: str, 章节编号列表: list[str], 是否短篇: bool=False) -> dict[str, str]:
    if not 章节编号列表:
        return {}
    参数 = {'id': 书籍编号, 'chapterIds': ','.join(章节编号列表)}
    if 是否短篇:
        参数['reader_agent'] = '1'
    数据 = await 请求JSON(session, 'https://api-ks.wtzw.com/api/v1/chapter/preload-chapter-content', 签名参数(参数), 生成请求头(书籍编号, 'api-ks.wtzw.com'))
    章节列表 = 读取字段路径(数据, ('data', 'chapter_contents')) or 读取字段路径(数据, ('data', 'chapter_content')) or []
    正文映射: dict[str, str] = {}
    for 项目 in 章节列表:
        if not isinstance(项目, dict):
            continue
        章节编号 = str(读取首个字段(项目, ('id', 'chapter_id', 'chapterId')) or '')
        加密正文 = 读取首个字段(项目, ('content', 'chapter_content', 'body'))
        if 章节编号 and 加密正文:
            正文映射[章节编号] = 解密正文(str(加密正文))
    return 正文映射

async def 获取章节正文(session: aiohttp.ClientSession, 书籍编号: str, 章节编号: str, 是否短篇: bool=False) -> str:
    参数 = {'id': 书籍编号, 'chapterId': 章节编号}
    if 是否短篇:
        参数['reader_agent'] = '1'
    数据 = await 请求JSON(session, 'https://api-ks.wtzw.com/api/v1/chapter/content', 签名参数(参数), 生成请求头(书籍编号, 'api-ks.wtzw.com'))
    加密正文 = 读取字段路径(数据, ('data', 'content'))
    if not 加密正文:
        错误 = 读取字段路径(数据, ('errors', 'details')) or '章节正文为空'
        raise RuntimeError(str(错误))
    return 解密正文(str(加密正文))

def 签名参数(参数: dict[str, Any]) -> dict[str, Any]:
    结果 = dict(参数)
    待签名 = ''.join((f'{键}={转换请求值(结果[键])}' for 键 in sorted(结果))) + 签名密钥
    结果['sign'] = hashlib.md5(待签名.encode('utf-8')).hexdigest()
    return 结果

def 生成请求头(书籍编号: str, 主机: str='') -> dict[str, str]:
    随机源 = random.Random(书籍编号)
    请求头 = {'AUTHORIZATION': '', 'app-version': 随机源.choice(应用版本列表), 'application-id': 应用ID, 'channel': 渠道名, 'is-white': '0', 'net-env': '1', 'platform': 'android', 'qm-params': 生成QM参数(主机), 'reg': '0'}
    待签名 = ''.join((f'{键}={请求头[键]}' for 键 in sorted(请求头))) + 签名密钥
    请求头['sign'] = hashlib.md5(待签名.encode('utf-8')).hexdigest()
    请求头['no-permiss'] = '0'
    请求头['User-Agent'] = f'Android 7.91.5 {应用ID}'
    return 请求头

def 转换请求值(值: Any) -> str:
    if 值 is True:
        return '1'
    if 值 is False:
        return '0'
    return '' if 值 is None else str(值)

def 生成QM参数(主机: str='') -> str:
    参数 = {'uuid': '', 'imei': '', 'qimei': '', 'uid': '', 'oaid-no-cache': '', 'oaid': '', 'smid': '', 'mac': '', 'brand': 'samsung', 'sub-brand': '', 'phone-level': '', 'model': 'SM-G9750', 'sys-ver': '9', 'android-id': secrets.token_hex(8), 'sourceuid': '', 'static_score': '', 'oaid_status': '', 'session-id': '', 'cf': '0'}
    if 主机 == 'api-bc.wtzw.com':
        参数['refresh-type'] = '0'
    原始 = json.dumps(参数, ensure_ascii=False, separators=(',', ':'))
    编码 = base64.b64encode(原始.encode('utf-8')).decode('utf-8').replace('+', '-').replace('/', '_')
    return ''.join((QM参数字符映射.get(字符, 字符) for 字符 in 编码))

def 解密正文(加密正文: str) -> str:
    原始内容 = base64.b64decode(加密正文)
    cipher = AES.new(解密密钥, AES.MODE_CBC, iv=原始内容[:16])
    解密内容 = unpad(cipher.decrypt(原始内容[16:]), AES.block_size)
    return 解密内容.decode('utf-8').strip()

def 清理网页文本(文本: Any) -> str:
    文本 = re.sub('<[^>]+>', '', str(文本 or ''))
    return html.unescape(文本).strip()

def 读取字段路径(数据: Any, 路径: tuple[str, ...]) -> Any:
    当前 = 数据
    for 字段 in 路径:
        if not isinstance(当前, dict):
            return None
        当前 = 当前.get(字段)
    return 当前

def 读取首个字段(数据: dict[str, Any], 字段列表: tuple[str, ...]) -> Any:
    if not isinstance(数据, dict):
        return None
    for 字段 in 字段列表:
        值 = 数据.get(字段)
        if 值 not in (None, ''):
            return 值
    return None

def identify(value):
    return identify_id(value, PLATFORM['hosts'], ['id', 'bookid', 'book_id'], ['/shuku/(\\d+)', '/(?:article-detail|book-detail|short-story-detail)/(\\d+)'])

async def 请求JSON(session, 地址, 参数, 请求头, 方法='GET', 表单=None):
    data = await json_request(session, 方法, 地址, params=参数 or None, headers=请求头, **{'data': 表单 or {}} if 方法 == 'POST' else {})
    if data.get('errors'):
        raise ProviderError('provider_request_failed')
    return data

async def _load_book(http, identity):
    detail = await _retry(lambda: 获取小说详情(http, identity))
    return metadata(identity, detail.get('title'), detail.get('author'), '已完结' if str(detail.get('is_over')) == '1' else '连载中', detail.get('words_num'), detail.get('chapters'), detail.get('intro'), detail.get('coverUrl'))

async def get_book(book_id):
    identity = _book_id(book_id)
    async with _session() as http:
        return await _load_book(http, identity)

async def download_book(book_id, on_progress=None):
    identity = _book_id(book_id)
    async with _session() as http:
        book = await _load_book(http, identity)
        catalog = await _retry(lambda: 获取小说目录(http, identity))
        if not catalog:
            raise ProviderError('chapter_unavailable')
        validate_catalog_count(book, len(catalog))
        chapters = []
        if on_progress:
            on_progress(len(catalog), 0)
        for start in range(0, len(catalog), 30):
            batch = catalog[start:start + 30]
            ids = [str(row['id']) for row in batch]
            try:
                contents = await _retry(lambda: 获取批量章节正文(http, identity, ids))
            except ProviderError:
                contents = {}
            for row in batch:
                cid = str(row['id'])
                content = contents.get(cid)
                if not content:
                    try:
                        content = await _retry(lambda: 获取章节正文(http, identity, cid))
                    except ProviderError:
                        raise ProviderError('chapter_unavailable') from None
                chapters.append(checked({'title': first(row, 'title', 'chapter_name', 'name') or '第%d章' % (len(chapters) + 1), 'content': content}))
                if on_progress:
                    on_progress(len(catalog), len(chapters))
        return {'book': book, 'chapters': chapters}

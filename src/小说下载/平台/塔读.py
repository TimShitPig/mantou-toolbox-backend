from __future__ import annotations
import asyncio, base64, hashlib, hmac, html, json, logging, re, time, urllib.parse, uuid
from dataclasses import dataclass
from typing import Any, List, Mapping, Sequence
import aiohttp
from .公共 import ProviderError
from .组B公共 import identify_id, book_id as _book_id, first, cover, metadata, validate_catalog_count, session as _session, retry as _retry, chapters_map
logger = logging.getLogger(__name__)
PLATFORM = {'id': 'tadu', 'name': '塔读小说', 'hosts': ['tadu.com'], 'aliases': ['塔读'], 'coverHosts': ['media3.tadu.com', 'img.tadu.com', 'image.tadu.com'], 'credentials': []}

def pkcs7_pad(data: bytes, block_size: int) -> bytes:
    n = block_size - len(data) % block_size
    return data + bytes([n]) * n

def _xtime(x: int) -> int:
    x <<= 1
    return (x ^ 283) & 255 if x & 256 else x & 255

def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p & 255

def _gf_pow(a: int, n: int) -> int:
    r = 1
    while n:
        if n & 1:
            r = _gmul(r, a)
        a = _gmul(a, a)
        n >>= 1
    return r

def _rot8(x: int, n: int) -> int:
    return (x << n | x >> 8 - n) & 255

def _make_sbox() -> List[int]:
    box: List[int] = []
    for x in range(256):
        inv = 0 if x == 0 else _gf_pow(x, 254)
        box.append((inv ^ _rot8(inv, 1) ^ _rot8(inv, 2) ^ _rot8(inv, 3) ^ _rot8(inv, 4) ^ 99) & 255)
    return box
AES_SBOX = _make_sbox()
AES_RCON = [0, 1, 2, 4, 8, 16, 32, 64, 128, 27, 54]

def _aes_key_expand(key: bytes) -> List[List[int]]:
    if len(key) != 16:
        raise ValueError('AES-128 key must be 16 bytes')
    words: List[List[int]] = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = words[i - 1][:]
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [AES_SBOX[b] for b in temp]
            temp[0] ^= AES_RCON[i // 4]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(words[i:i + 4], []) for i in range(0, 44, 4)]

def _aes_shift_rows(s: List[int]) -> None:
    old = s[:]
    for r in range(4):
        for c in range(4):
            s[r + 4 * c] = old[r + 4 * ((c + r) % 4)]

def _aes_mix_columns(s: List[int]) -> None:
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = (s[i], s[i + 1], s[i + 2], s[i + 3])
        s[i] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        s[i + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        s[i + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        s[i + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)

def aes128_encrypt_block(block: bytes, key: bytes) -> bytes:
    if len(block) != 16:
        raise ValueError('AES block must be 16 bytes')
    rks = _aes_key_expand(key)
    s = list(block)
    for i in range(16):
        s[i] ^= rks[0][i]
    for rnd in range(1, 10):
        for i in range(16):
            s[i] = AES_SBOX[s[i]]
        _aes_shift_rows(s)
        _aes_mix_columns(s)
        for i in range(16):
            s[i] ^= rks[rnd][i]
    for i in range(16):
        s[i] = AES_SBOX[s[i]]
    _aes_shift_rows(s)
    for i in range(16):
        s[i] ^= rks[10][i]
    return bytes(s)

def aes128_ecb_pkcs7_encrypt(data: bytes, key: bytes) -> bytes:
    data = pkcs7_pad(data, 16)
    return b''.join((aes128_encrypt_block(data[i:i + 16], key) for i in range(0, len(data), 16)))
DES_IP = [58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7]
DES_FP = [40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25]
DES_E = [32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1]
DES_P = [16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25]
DES_PC1 = [57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36, 63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4]
DES_PC2 = [14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2, 41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32]
DES_SHIFTS = [1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1]
DES_SBOX = [[[14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7], [0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8], [4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0], [15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13]], [[15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10], [3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5], [0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15], [13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9]], [[10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8], [13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1], [13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7], [1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12]], [[7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15], [13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9], [10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4], [3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14]], [[2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9], [14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6], [4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14], [11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3]], [[12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11], [10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8], [9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6], [4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13]], [[4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1], [13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6], [1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2], [6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12]], [[13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7], [1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2], [7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8], [2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11]]]

def _bits(data: bytes) -> List[int]:
    return [b >> i & 1 for b in data for i in range(7, -1, -1)]

def _unbits(bits: Sequence[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(bits), 8):
        v = 0
        for bit in bits[i:i + 8]:
            v = v << 1 | bit & 1
        out.append(v)
    return bytes(out)

def _perm(bits: Sequence[int], table: Sequence[int]) -> List[int]:
    return [bits[i - 1] for i in table]

def _rot(bits: List[int], n: int) -> List[int]:
    return bits[n:] + bits[:n]

def _des_subkeys(key: bytes) -> List[List[int]]:
    if len(key) != 8:
        raise ValueError('DES key must be 8 bytes')
    k = _perm(_bits(key), DES_PC1)
    c, d = (k[:28], k[28:])
    out: List[List[int]] = []
    for sh in DES_SHIFTS:
        c = _rot(c, sh)
        d = _rot(d, sh)
        out.append(_perm(c + d, DES_PC2))
    return out

def _des_f(r: Sequence[int], subkey: Sequence[int]) -> List[int]:
    x = [a ^ b for a, b in zip(_perm(r, DES_E), subkey)]
    s_out: List[int] = []
    for i in range(8):
        c = x[i * 6:(i + 1) * 6]
        row = c[0] << 1 | c[5]
        col = c[1] << 3 | c[2] << 2 | c[3] << 1 | c[4]
        v = DES_SBOX[i][row][col]
        s_out.extend([v >> 3 & 1, v >> 2 & 1, v >> 1 & 1, v & 1])
    return _perm(s_out, DES_P)

def des_encrypt_block(block: bytes, key: bytes) -> bytes:
    if len(block) != 8:
        raise ValueError('DES block must be 8 bytes')
    l_r = _perm(_bits(block), DES_IP)
    l, r = (l_r[:32], l_r[32:])
    for sk in _des_subkeys(key):
        l, r = (r, [a ^ b for a, b in zip(l, _des_f(r, sk))])
    return _unbits(_perm(r + l, DES_FP))

def des_ecb_pkcs7_encrypt(data: bytes, key: bytes) -> bytes:
    data = pkcs7_pad(data, 8)
    return b''.join((des_encrypt_block(data[i:i + 8], key) for i in range(0, len(data), 8)))

def 解密_tadu正文(data: bytes) -> str:
    """使用内嵌纯 Python 库解析 TDZ 容器并还原 UTF-16LE 正文。"""
    raw = bytes(data or b'')
    if raw[:4] == b'tadu':
        import gzip
        if len(raw) < 32:
            raise ValueError('TDZ 容器头不完整')
        meta_len = int.from_bytes(raw[8:16], 'little')
        pos = 16 + meta_len
        if pos + 16 > len(raw):
            raise ValueError('TDZ 容器索引不完整')
        offset = int.from_bytes(raw[pos:pos + 8], 'little')
        length = int.from_bytes(raw[pos + 8:pos + 16], 'little')
        if offset <= 0 or length <= 0 or offset + length > len(raw):
            raise ValueError('TDZ 容器正文范围无效')
        raw = gzip.decompress(raw[offset:offset + length])
    elif raw.startswith(b'\x1f\x8b'):
        import gzip
        raw = gzip.decompress(raw)
    return raw.decode('utf-16le', errors='ignore').replace('\ufeff', ' ').replace('\r', '\n').strip()
塔读API = 'http://reader.tadu.com'
塔读目录并发上限 = 4
APP_VERSION = '6.11.02.800019'
VERSION_CODE = 1321
ANDROID_RELEASE = '10'
ANDROID_SDK_INT = 29
SCREEN_SIZE = '1080*1920'
DEVICE_TYPE = 'Pixel 4'
DEVICE_MAKE = 'Google'
PACKAGE_NAME = 'zhuishu'
USER_AGENT = 'Mozilla/5.0 (Linux; Android 10; Pixel 4 Build/QP1A.190711.020; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/120.0.0.0 Mobile Safari/537.36'
TDCN_SECRET = 'UYHMKJ%$#&21918djduw^&*()_+^$%kjdsk28dkdj236^'
TDCN_DES_KEY = b'LAP^%O$8'
BASE62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
OAUTH_SECRET_1 = 'cd2yj5352pu927mrsn5kmut0saloniy'
OAUTH_HMAC_KEY = b'1a8154132fg4a784fad101661z1854181ac1qed7'

@dataclass
class 塔读会话状态:
    sessionid: str = ''
    token: str = ''
    refresh_token: str = ''
    expire: Any = None
    early_time: Any = None
    expire_time: Any = None
_塔读会话 = 塔读会话状态()
_塔读会话锁 = asyncio.Lock()

def _当前毫秒() -> int:
    return int(time.time() * 1000)

def _md5_hex(value: str | bytes) -> str:
    raw = value.encode('utf-8') if isinstance(value, str) else value
    return hashlib.md5(raw).hexdigest()

def _base62_encode(number: int, length: int=16) -> str:
    out: list[str] = []
    while True:
        if number <= 61:
            out.append(BASE62[number])
            break
        out.append(BASE62[number % 62])
        number //= 62
    return ''.join(reversed(out)).rjust(length, '0')

def _aes_b64(text: str, key_text: str) -> str:
    return base64.b64encode(aes128_ecb_pkcs7_encrypt(text.encode('utf-8'), key_text.encode('utf-8'))).decode('ascii')

def _des_hex(text: str) -> str:
    return des_ecb_pkcs7_encrypt(text.encode('utf-8'), TDCN_DES_KEY).hex().upper()

def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')

def _紧凑JSON(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

def _构造Bearer(sessionid: str='', ts: int | None=None) -> str:
    current = int(ts if ts is not None else _当前毫秒())
    aes_key = _base62_encode(current, 16)
    issuer = f'tadu:app:android:{aes_key}'
    jti = _md5_hex(f'{_aes_b64(OAUTH_SECRET_1, aes_key)}:{_aes_b64(issuer, aes_key)}:{_aes_b64(str(current), aes_key).lower()}')
    header = {'alg': 'HS256', 'typ': 'JWT'}
    claims = {'sdk': _aes_b64(str(ANDROID_SDK_INT), aes_key), 'sessionid': _aes_b64(sessionid or '', aes_key), 'clientTime': current, 'iss': issuer, 'iat': current // 1000, 'jti': jti}
    signing = (_base64url(_紧凑JSON(header)) + '.' + _base64url(_紧凑JSON(claims))).encode('ascii')
    signature = hmac.new(OAUTH_HMAC_KEY, signing, hashlib.sha256).digest()
    jwt = signing.decode('ascii') + '.' + _base64url(signature)
    return base64.b64encode(jwt.encode('utf-8')).decode('ascii')

def _参数值串(params: Mapping[str, Any] | None) -> str:
    if not params:
        return ''
    values: list[str] = []
    for key in sorted(params.keys(), key=lambda item: str(item).lower()):
        value = params.get(key)
        if value is None:
            values.append('')
        elif isinstance(value, (list, tuple)):
            values.append(''.join(('' if item is None else str(item) for item in value)))
        else:
            values.append(str(value))
    return ''.join(values)

def _构造Tdcn(params: Mapping[str, Any] | None, rn: str) -> str:
    sdk = urllib.parse.quote_plus(ANDROID_RELEASE, safe='')
    device_type = urllib.parse.quote_plus(DEVICE_TYPE, safe='')
    raw = _参数值串(params) + TDCN_SECRET + rn + '' + APP_VERSION + '' + '' + sdk + SCREEN_SIZE + device_type + '' + ''
    return _des_hex(_md5_hex(raw))

def _构造XClient(params: Mapping[str, Any] | None=None) -> str:
    rn = ''.join((str(uuid.uuid4().int % 10) for _ in range(10)))
    tdcn = _构造Tdcn(params, rn)
    fields = [('sdk', urllib.parse.quote_plus(ANDROID_RELEASE, safe='')), ('sdkVersion', str(ANDROID_SDK_INT)), ('screenSize', SCREEN_SIZE), ('type', urllib.parse.quote_plus(DEVICE_TYPE, safe='')), ('imei', ''), ('imsi', ''), ('version', APP_VERSION), ('versionCode', str(VERSION_CODE)), ('rootPath', ''), ('rn', rn), ('tdcn', tdcn), ('android_id_new', ''), ('localTime', str(_当前毫秒())), ('shuZiId', ''), ('tdUUID', str(uuid.uuid4())), ('oaid', ''), ('isGuestMode', '0'), ('readLike', '0'), ('tagIds', ''), ('make', DEVICE_MAKE), ('package_name', PACKAGE_NAME)]
    return ';'.join((f'{key}={value}' for key, value in fields)) + ';'

def _接口成功(response: Mapping[str, Any]) -> bool:
    try:
        return int(response.get('code', -1)) == 100
    except Exception:
        return False

def _数据对象(response: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        return {}
    data = response.get('data')
    if isinstance(data, Mapping):
        return data
    return {}

def _绝对地址(domain: str, url: str) -> str:
    value = str(url or '').strip()
    if not value:
        return ''
    if value.startswith(('http://', 'https://')):
        return value
    return str(domain or '').rstrip('/') + '/' + value.lstrip('/')

def _提取列表(data: Mapping[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, Mapping):
            nested = _提取列表(value, ('books', 'bookList', 'chapters', 'chapterList', 'list', 'records'))
            if nested:
                return nested
    return []

def _格式化字数(value: Any) -> str:
    text = str(value or '').strip()
    if not text:
        return '未知'
    number_text = re.sub('[\\s,，]', '', text)
    if number_text.endswith('字'):
        number_text = number_text[:-1]
    if number_text.replace('.', '', 1).isdigit():
        try:
            number = int(float(number_text))
        except Exception:
            return text
        return f'{round(number / 10000, 1)}万字' if number >= 10000 else f'{number}字'
    return text

def _清理正文(text: Any) -> str:
    value = html.unescape(str(text or '')).replace('\r\n', '\n').replace('\r', '\n')
    value = re.sub('[ \\t\\u3000]+', ' ', value)
    return value.strip()

def 提取塔读正文(data: bytes) -> str:
    return _清理正文(解密_tadu正文(data))

def 解析塔读批量章节(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    data = _数据对象(response)
    domain = str(data.get('domain') or '')
    rows = _提取列表(data, ('chapters', 'chapterList', 'list'))
    result: list[dict[str, Any]] = []
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, Mapping):
            continue
        chapter_id = str(item.get('chapterId') or item.get('chapter_id') or item.get('id') or '').strip()
        chapter_num = item.get('chapterNum') or item.get('chapterNumber') or item.get('num') or index
        try:
            chapter_num = int(chapter_num)
        except Exception:
            chapter_num = index
        title = str(item.get('chapterName') or item.get('chapterTitle') or item.get('title') or item.get('name') or f'第{chapter_num}章').strip()
        url = _绝对地址(str(item.get('domain') or domain), str(item.get('downloadUrl') or item.get('chapterUrl') or item.get('chapterDownloadUrl') or item.get('url') or ''))
        result.append({'chapter_id': chapter_id, 'chapter_num': chapter_num, 'title': title or f'第{chapter_num}章', 'url': url})
    return result

def _塔读请求头(sign_params: Mapping[str, Any] | None=None, content_type: str='') -> dict[str, str]:
    state = _塔读会话
    headers = {'User-Agent': USER_AGENT, 'X-Client': _构造XClient(sign_params), 'COOKIE': f'sessionid={state.sessionid};token={state.token};refreshToken={state.refresh_token};bearer={_构造Bearer(state.sessionid)}', 'token': state.token, 'Accept': 'application/json, text/plain, */*', 'Connection': 'keep-alive'}
    if content_type:
        headers['Content-Type'] = content_type
    return headers

async def _请求塔读接口(session: aiohttp.ClientSession, method: str, path: str, *, params: Mapping[str, Any] | None=None, form: Mapping[str, Any] | None=None, ensure_session: bool=True) -> dict[str, Any]:
    if ensure_session:
        await 确保塔读会话(session)
    method = method.upper()
    sign_params = params if method == 'GET' else form
    url = path if path.startswith('http') else 塔读API.rstrip('/') + '/' + path.lstrip('/')
    headers = _塔读请求头(sign_params, 'application/x-www-form-urlencoded' if method == 'POST' else '')
    async with session.request(method, url, params=params if method == 'GET' else None, data=form if method == 'POST' else None, headers=headers) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    if not isinstance(payload, dict):
        raise RuntimeError('塔读接口响应格式异常')
    return payload

def _更新塔读会话(data: Mapping[str, Any]) -> None:
    """吸收注册/续期响应，仅把游客登录态保存在当前进程内。"""
    _塔读会话.sessionid = str(data.get('sessionId') or data.get('sessionid') or _塔读会话.sessionid or '')
    _塔读会话.token = str(data.get('token') or _塔读会话.token or '')
    _塔读会话.refresh_token = str(data.get('refreshToken') or data.get('refresh_token') or _塔读会话.refresh_token or '')
    if data.get('expire') not in (None, ''):
        _塔读会话.expire = data.get('expire')
    if data.get('earlyTime') not in (None, ''):
        _塔读会话.early_time = data.get('earlyTime')
    if data.get('expireTime') not in (None, ''):
        _塔读会话.expire_time = data.get('expireTime')
    try:
        expire_seconds = int(data.get('expire') or 0)
    except (TypeError, ValueError):
        expire_seconds = 0
    if expire_seconds > 0:
        _塔读会话.expire_time = _当前毫秒() + expire_seconds * 1000

def _塔读会话需要续期() -> bool:
    if not _塔读会话.sessionid or not _塔读会话.token:
        return False
    try:
        expire_time = int(_塔读会话.expire_time)
    except (TypeError, ValueError):
        return True
    try:
        early_time = max(0, int(_塔读会话.early_time or 0))
    except (TypeError, ValueError):
        early_time = 0
    return _当前毫秒() >= expire_time - early_time * 1000

def _应用塔读Token响应(response: Mapping[str, Any] | None) -> bool:
    if not isinstance(response, Mapping) or not _接口成功(response):
        return False
    data = response.get('data')
    if not isinstance(data, Mapping):
        data = response
    token = data.get('token')
    if not token:
        return False
    _更新塔读会话(data)
    return True

async def _注册塔读会话(session: aiohttp.ClientSession) -> None:
    response = await _请求塔读接口(session, 'POST', '/user/api/register', form={'readType': 0}, ensure_session=False)
    data = _数据对象(response)
    if not _接口成功(response) or not data:
        raise RuntimeError('塔读游客会话初始化失败')
    _更新塔读会话(data)
    if not _塔读会话.sessionid or not _塔读会话.token:
        raise RuntimeError('塔读游客会话字段不完整')

async def 确保塔读会话(session: aiohttp.ClientSession) -> None:
    if _塔读会话.sessionid and _塔读会话.token and (not _塔读会话需要续期()):
        return
    async with _塔读会话锁:
        if _塔读会话.sessionid and _塔读会话.token and (not _塔读会话需要续期()):
            return
        if not _塔读会话.sessionid or not _塔读会话.token:
            await _注册塔读会话(session)
            return
        try:
            response = await _请求塔读接口(session, 'GET', '/user/api/token/get', ensure_session=False)
            if _应用塔读Token响应(response):
                return
        except Exception as exc:
            logger.debug('塔读游客Token续期失败：阶段=refresh, 错误类型=%s', type(exc).__name__)
        await _注册塔读会话(session)

async def _获取详情(session: aiohttp.ClientSession, book_id: str) -> dict[str, Any]:
    response = await _请求塔读接口(session, 'GET', '/book/info/titlePage', params={'bookId': book_id})
    data = _数据对象(response)
    for key in ('bookInfo', 'bookDetail', 'book', 'info'):
        value = data.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return dict(data)

async def _获取单章地址(session: aiohttp.ClientSession, book_id: str, chapter: Mapping[str, Any]) -> str:
    response = await _请求塔读接口(session, 'GET', '/book/chapter/getChapterTdz', params={'book_id': book_id, 'chapter_num': chapter.get('chapter_num') or 0, 'chapter_id': chapter.get('chapter_id') or ''})
    data = _数据对象(response)
    chapter_info = data.get('chapterInfo') if isinstance(data.get('chapterInfo'), Mapping) else data
    return _绝对地址(str(chapter_info.get('domain') or data.get('domain') or ''), str(chapter_info.get('chapterUrl') or chapter_info.get('chapterDownloadUrl') or chapter_info.get('url') or ''))

async def _获取目录(session: aiohttp.ClientSession, book_id: str) -> list[dict[str, Any]]:
    batch_response = await _请求塔读接口(session, 'GET', '/book/batchdownload/listNew', params={'book_id': book_id})
    chapters = 解析塔读批量章节(batch_response)
    if not chapters:
        catalog_response = await _请求塔读接口(session, 'GET', '/book/directory/list', params={'bookId': book_id, 'sort': 'asc'})
        data = _数据对象(catalog_response)
        rows = _提取列表(data, ('chapters', 'chapterList', 'list'))
        chapters = 解析塔读批量章节({'data': {'chapters': rows}})
    if not chapters:
        return []
    missing = [chapter for chapter in chapters if not chapter.get('url')]
    if missing:
        sem = asyncio.Semaphore(min(塔读目录并发上限, max(1, len(missing))))

        async def fill(chapter: dict[str, Any]) -> None:
            async with sem:
                chapter['url'] = await _获取单章地址(session, book_id, chapter)
        await asyncio.gather(*(fill(chapter) for chapter in missing))
    return chapters

async def _下载章节字节(session: aiohttp.ClientSession, url: str) -> bytes:
    value = str(url or '')
    if value.startswith('https://media') and '.tadu.com/' in value:
        value = 'http://' + value[len('https://'):]
    headers = {'User-Agent': USER_AGENT, 'Accept': '*/*', 'Connection': 'keep-alive'}
    async with session.get(value, headers=headers) as response:
        response.raise_for_status()
        return await response.read()

def _详情字段(detail: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = detail.get(key)
        if value not in (None, ''):
            return value
    return ''

def 解析塔读书籍详情(detail: Mapping[str, Any] | None) -> dict[str, str]:
    """统一解析塔读 titlePage 返回的书名、作者、状态和真实字数。"""
    data = detail if isinstance(detail, Mapping) else {}
    title = str(_详情字段(data, 'bookName', 'bookTitle', 'title', 'name') or '未知')
    author = str(_详情字段(data, 'bookAuthor', 'authorName', 'author', 'writer') or '未知')
    status_text = str(_详情字段(data, 'status', 'serialStatus', 'bookStatus') or '')
    is_end = str(_详情字段(data, 'isEnd', 'isFinished', 'finish') or '').lower()
    status = '完结' if '完' in status_text or is_end in {'1', 'true', 'yes'} else '连载'
    word_count = _格式化字数(_详情字段(data, 'bookTotalSize', 'wordCount', 'bookWordCount', 'wordNum', 'totalWordCount', 'totalWords', 'words'))
    return {'title': title, 'author': author, 'status': status, 'word_count': word_count}

def identify(value):
    return identify_id(value, PLATFORM['hosts'], ['bookId', 'book_id', 'id'], ['/book/(\\d+)', '/reader/(\\d+)'])

async def _load_book(http, identity):
    raw = await _retry(lambda: _获取详情(http, identity))
    data = 解析塔读书籍详情(raw)
    title = data.get('title')
    if isinstance(raw.get('isSerial'), bool):
        data['status'] = '连载中' if raw['isSerial'] else '已完结'
    if title == '未知':
        raise ProviderError('book_info_unavailable')
    return metadata(identity, title, data.get('author'), data.get('status'), data.get('word_count'), first(raw, 'chapterCount', 'chapterNum', 'chapterTotal', 'chapterTotalSize'), first(raw, 'intro', 'bookIntro', 'description', 'summary', 'bookIntroduction'), cover(raw))

async def get_book(book_id):
    async with _session() as http:
        return await _load_book(http, _book_id(book_id))

async def download_book(book_id, on_progress=None):
    identity = _book_id(book_id)
    async with _session() as http:
        book = await _load_book(http, identity)
        catalog = await _retry(lambda: _获取目录(http, identity))
        validate_catalog_count(book, len(catalog))

        async def fetch(chapter):
            url = chapter.get('url')
            if not url:
                raise ProviderError('chapter_unavailable')
            data = await _retry(lambda: _下载章节字节(http, url))
            try:
                content = await asyncio.to_thread(提取塔读正文, data)
            except Exception:
                raise ProviderError('chapter_unavailable') from None
            return {'title': chapter.get('title'), 'content': content}
        chapters = await chapters_map(catalog, fetch, on_progress)
        return {'book': book, 'chapters': chapters}

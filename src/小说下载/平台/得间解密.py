from __future__ import annotations

import base64
import hashlib
import re
import struct
import zlib
from functools import lru_cache
from typing import List, Optional, Tuple

import gmpy2
import numpy as np
from Crypto.Util.strxor import strxor

TOKEN_KEY_BASE0 = bytes.fromhex("5a0b1252b41e6bf509dd542a66d25a47")
TOKEN_KEY_BASE1 = bytes.fromhex("16a7f4c45ec7a517d82f84e753fc5ecd")
NATIVE_AES_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cfd0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdbe0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9ee1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
NATIVE_AES_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)
NATIVE_RSA_N = int(
    "bd95ed3c46e9cc7e5174db00493f54c9fcd307a689260aeac7c9ca1fb635b45083d54dce90b00a4d98f8baa508edb4aa14efce8d6cbf73f6c0bb9fddf522699a"
    "e0106c19bfc2bd84147d1d20ecafd4796d01b4d7f8d785f58408aa0fc91c30be2198c14a45bb7714ae2bd03bc571d4d5e7dbf8e24b60a48e936076ec1e1216d1",
    16,
)
NATIVE_RSA_E = 65537
IV_XOR_CONST = 0xC83C4ED0


def _rol3(x: int) -> int:
    return (((x << 3) & 0xFF) | (x >> 5)) & 0xFF


ZHANGYUE_CTR_POST_XOR = bytes((~_rol3(value)) & 0xFF for value in range(256))


def _gf_xtime(a: int) -> int:
    return (((a << 1) & 0xFF) ^ (0x1B if a & 0x80 else 0)) & 0xFF


def _gf_mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _gf_xtime(a)
        b >>= 1
    return out & 0xFF


def _ror32(v: int, n: int) -> int:
    return ((v >> n) | ((v & ((1 << n) - 1)) << (32 - n))) & 0xFFFFFFFF


def _native_t_tables() -> Tuple[List[int], List[int], List[int], List[int]]:
    t0 = [
        (_gf_mul(s, 3) << 24) | (_gf_mul(s, 10) << 16) | (s << 8) | _gf_mul(s, 9)
        for s in NATIVE_AES_SBOX
    ]
    return (
        t0,
        [_ror32(v, 8) for v in t0],
        [_ror32(v, 16) for v in t0],
        [_ror32(v, 24) for v in t0],
    )


_NATIVE_T = _native_t_tables()
_NATIVE_T_ARRAYS = tuple(np.asarray(table, dtype=np.uint32) for table in _NATIVE_T)
_NATIVE_SBOX_ARRAY = np.frombuffer(NATIVE_AES_SBOX, dtype=np.uint8).astype(np.uint32)


@lru_cache(maxsize=512)
def _native_key_schedule(key: bytes) -> Tuple[int, ...]:
    if len(key) != 16:
        raise ValueError("bad key")
    words = [int.from_bytes(key[i : i + 4], "big") for i in range(0, 16, 4)]
    for rcon in NATIVE_AES_RCON:
        t = words[-1]
        rot = ((t << 8) & 0xFFFFFFFF) | (t >> 24)
        sub = 0
        for shift in (24, 16, 8, 0):
            sub |= NATIVE_AES_SBOX[(rot >> shift) & 0xFF] << shift
        sub ^= rcon << 24
        words.append(words[-4] ^ sub)
        words.append(words[-4] ^ words[-1])
        words.append(words[-4] ^ words[-1])
        words.append(words[-4] ^ words[-1])
    return tuple(x & 0xFFFFFFFF for x in words)


def _native_block(round_keys: Tuple[int, ...], block16: bytes) -> bytes:
    if len(block16) != 16:
        raise ValueError("bad block")
    t0, t1, t2, t3 = _NATIVE_T
    s0 = round_keys[0] ^ int.from_bytes(block16[0:4], "big")
    s1 = round_keys[1] ^ int.from_bytes(block16[4:8], "big")
    s2 = round_keys[2] ^ int.from_bytes(block16[8:12], "big")
    s3 = round_keys[3] ^ int.from_bytes(block16[12:16], "big")
    for r in range(1, 10):
        n0 = (
            t0[s0 >> 24]
            ^ t1[(s1 >> 16) & 0xFF]
            ^ t2[(s2 >> 8) & 0xFF]
            ^ t3[s3 & 0xFF]
            ^ round_keys[4 * r]
        )
        n1 = (
            t0[s1 >> 24]
            ^ t1[(s2 >> 16) & 0xFF]
            ^ t2[(s3 >> 8) & 0xFF]
            ^ t3[s0 & 0xFF]
            ^ round_keys[4 * r + 1]
        )
        n2 = (
            t0[s2 >> 24]
            ^ t1[(s3 >> 16) & 0xFF]
            ^ t2[(s0 >> 8) & 0xFF]
            ^ t3[s1 & 0xFF]
            ^ round_keys[4 * r + 2]
        )
        n3 = (
            t0[s3 >> 24]
            ^ t1[(s0 >> 16) & 0xFF]
            ^ t2[(s1 >> 8) & 0xFF]
            ^ t3[s2 & 0xFF]
            ^ round_keys[4 * r + 3]
        )
        s0, s1, s2, s3 = (
            n0 & 0xFFFFFFFF,
            n1 & 0xFFFFFFFF,
            n2 & 0xFFFFFFFF,
            n3 & 0xFFFFFFFF,
        )
    out = bytearray(16)
    final = round_keys[40:44]
    selectors = (
        (s0 >> 24, 24, 0),
        ((s1 >> 16) & 0xFF, 16, 0),
        ((s2 >> 8) & 0xFF, 8, 0),
        (s3 & 0xFF, 0, 0),
        (s1 >> 24, 24, 1),
        ((s2 >> 16) & 0xFF, 16, 1),
        ((s3 >> 8) & 0xFF, 8, 1),
        (s0 & 0xFF, 0, 1),
        (s2 >> 24, 24, 2),
        ((s3 >> 16) & 0xFF, 16, 2),
        ((s0 >> 8) & 0xFF, 8, 2),
        (s1 & 0xFF, 0, 2),
        (s3 >> 24, 24, 3),
        ((s0 >> 16) & 0xFF, 16, 3),
        ((s1 >> 8) & 0xFF, 8, 3),
        (s2 & 0xFF, 0, 3),
    )
    for i, (src, shift, key_index) in enumerate(selectors):
        out[i] = NATIVE_AES_SBOX[src & 0xFF] ^ ((final[key_index] >> shift) & 0xFF)
    return bytes(out)


def _native_ctr_batch(data: bytes, round_keys: Tuple[int, ...], iv: bytes) -> bytes:
    """将同一自定义轮变换按 uint32 批量执行；计数器和末轮与单块协议一致。"""
    块数 = (len(data) + 15) // 16
    计数器 = (np.arange(块数, dtype=np.uint64) + int.from_bytes(iv[10:14], "big")) & 0xFFFFFFFF
    状态 = np.empty((4, 块数), dtype=np.uint32)
    状态[0] = round_keys[0] ^ int.from_bytes(iv[:4], "big")
    状态[1] = round_keys[1] ^ int.from_bytes(iv[4:8], "big")
    状态[2] = np.uint32(round_keys[2]) ^ (
        (np.uint32(int.from_bytes(iv[8:10], "big")) << 16)
        | (计数器 >> 16).astype(np.uint32)
    )
    状态[3] = np.uint32(round_keys[3]) ^ (
        (计数器.astype(np.uint32) << 16)
        | np.uint32(int.from_bytes(iv[14:16], "big"))
    )
    t0, t1, t2, t3 = _NATIVE_T_ARRAYS
    轮密钥 = np.asarray(round_keys, dtype=np.uint32).reshape(11, 4, 1)
    for r in range(1, 10):
        状态 = (
            t0[状态 >> 24] ^ t1[(状态[[1, 2, 3, 0]] >> 16) & 255]
            ^ t2[(状态[[2, 3, 0, 1]] >> 8) & 255]
            ^ t3[状态[[3, 0, 1, 2]] & 255] ^ 轮密钥[r]
        )
    box = _NATIVE_SBOX_ARRAY
    words = (
        (box[状态 >> 24] << 24) ^ (box[(状态[[1, 2, 3, 0]] >> 16) & 255] << 16)
        ^ (box[(状态[[2, 3, 0, 1]] >> 8) & 255] << 8)
        ^ box[状态[[3, 0, 1, 2]] & 255] ^ 轮密钥[10]
    )
    密钥流 = words.T.astype(">u4").tobytes()[:len(data)]
    return strxor(data, 密钥流).translate(ZHANGYUE_CTR_POST_XOR)


def zhangyue_native_ctr(data: bytes, key: bytes, iv: bytes) -> bytes:
    if len(key) != 16 or len(iv) != 16:
        raise ValueError("bad ctr args")
    if not data:
        return b""
    if len(data) >= 256:
        if len(data) > 65536:
            输出 = bytearray()
            初始计数 = int.from_bytes(iv[10:14], "big")
            计数IV = bytearray(iv)
            round_keys = _native_key_schedule(key)
            for 起点 in range(0, len(data), 65536):
                计数IV[10:14] = ((初始计数 + 起点 // 16) & 0xFFFFFFFF).to_bytes(4, "big")
                输出.extend(_native_ctr_batch(data[起点:起点 + 65536], round_keys, bytes(计数IV)))
            return bytes(输出)
        return _native_ctr_batch(data, _native_key_schedule(key), iv)
    counter = bytearray(iv)
    key_stream = bytearray(len(data))
    round_keys = _native_key_schedule(key)
    for off in range(0, len(data), 16):
        end = min(off + 16, len(data))
        key_stream[off:end] = _native_block(round_keys, bytes(counter))[: end - off]
        for idx in (13, 12, 11, 10):
            counter[idx] = (counter[idx] + 1) & 0xFF
            if counter[idx]:
                break
    return strxor(data, key_stream).translate(ZHANGYUE_CTR_POST_XOR)


def native_rsa_unwrap(cipher: bytes) -> bytes:
    if len(cipher) != 128:
        raise ValueError("bad token length")
    m = int(
        gmpy2.powmod(int.from_bytes(cipher, "big"), NATIVE_RSA_E, NATIVE_RSA_N)
    ).to_bytes(128, "big")
    if not m.startswith(b"\x00\x01"):
        raise ValueError("bad token padding")
    sep = m.find(b"\x00", 2)
    if sep < 0:
        raise ValueError("bad token sep")
    return m[sep + 1 :]


def _token_first_layer_key(seed4: bytes) -> bytes:
    return bytes(
        (TOKEN_KEY_BASE0[i] + TOKEN_KEY_BASE1[i] + seed4[i & 3]) & 0xFF
        for i in range(16)
    )


def unwrap_dejian_token(raw: bytes) -> bytes:
    if len(raw) < 12:
        raise ValueError("bad raw token")
    struct_len = int.from_bytes(raw[:4], "little")
    if struct_len <= 0 or struct_len > 0x400:
        raise ValueError("bad token header")
    key = _token_first_layer_key(raw[4:8])
    body = zhangyue_native_ctr(
        raw[8:], key, bytes((~key[(i + 5) & 15]) & 0xFF for i in range(16))
    )
    return raw[:8] + body


def derive_stage1_key(raw_token: bytes, usr: str, dev: str) -> bytes:
    token = unwrap_dejian_token(raw_token)
    if len(token) < 0x4C:
        raise ValueError("token too short")
    iv = bytes.fromhex("000001018b0000000000000000000000")
    slot0 = token[0x0C:0x1C]
    check = token[0x2C:0x3C]
    key = zhangyue_native_ctr(slot0, hashlib.md5(usr.encode("utf-8")).digest(), iv)
    if hashlib.md5(key).digest() != check:
        raise ValueError("token check failed")
    return key


def iv_from_stage1(key16: bytes) -> bytes:
    iv = bytearray()
    for i in range(0, 16, 4):
        d = struct.unpack_from("<I", key16, i)[0] ^ IV_XOR_CONST
        iv += struct.pack("<I", d)
    return bytes(iv)


def parse_zip_stored(data: bytes):
    off = 0
    while off + 30 <= len(data) and data[off : off + 4] == b"PK\x03\x04":
        _sig, _ver, flag, method, _mt, _md, _crc, csize, _usize, nlen, xlen = (
            struct.unpack_from("<IHHHHHIIIHH", data, off)
        )
        name = data[off + 30 : off + 30 + nlen].decode("utf-8", "replace")
        data_off = off + 30 + nlen + xlen
        payload = data[data_off : data_off + csize]
        yield name, method, payload
        off = data_off + csize
        if flag & 8:
            off += 16 if data[off : off + 4] == b"PK\x07\x08" else 12


def decrypt_payload(payload: bytes, key: bytes) -> bytes:
    dec = zhangyue_native_ctr(payload, key, iv_from_stage1(key))
    last_err: Optional[Exception] = None
    for skip in range(8):
        try:
            return zlib.decompress(dec[skip:], -15)
        except Exception as e:
            last_err = e
    raise RuntimeError(f"inflate failed: {last_err}")


def strip_zy_header(raw: bytes) -> bytes:
    if raw.startswith(b"<?xml") or raw.startswith(b"<"):
        return raw
    idx = raw.find(b"<?xml")
    if 0 < idx <= 16:
        return raw[idx:]
    return raw


def html_to_text(html: str) -> str:
    # 删除 class="text-title-1" 的 h1 标签
    html = re.sub(
        r'<h1[^>]*class="text-title-1"[^>]*>.*?</h1>',
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    t = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", " ", t, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</p\s*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)

    # 合并连续换行为一个
    t = re.sub(r"\n{2,}", "\n", t)

    for a, b in [
        ("&nbsp;", " "),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&amp;", "&"),
        ("&quot;", '"'),
        ("&#39;", "'"),
    ]:
        t = t.replace(a, b)
    t = t.replace("\r", "")
    t = t.strip()
    return t


def decrypt_epub_text(epub_data: bytes, key: bytes) -> str:
    text = ""
    for name, method, payload in parse_zip_stored(epub_data):
        if name == "mimetype" or name.endswith("encryption.xml"):
            continue
        body = strip_zy_header(decrypt_payload(payload, key))
        if name.endswith((".xhtml", ".html")):
            text = html_to_text(body.decode("utf-8", "replace"))
    if not text:
        raise RuntimeError("no text")
    return text


def 解密得间正文(正文数据: bytes, 授权令牌: str, 用户名: str, 设备号: str) -> str:
    原始令牌 = native_rsa_unwrap(base64.b64decode(授权令牌))
    密钥 = derive_stage1_key(原始令牌, 用户名, 设备号)
    return decrypt_epub_text(正文数据, 密钥).strip()

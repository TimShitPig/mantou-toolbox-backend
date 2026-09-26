#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
番茄畅听 App 小说正文单文件下载器。

请求运行时需要本文件和 PyCryptodome，不需要 App、adb、Frida 或 so。
正文请求通过 App 的 /novelfm/playerapi/full/mget/v1/ 接口完成。

签名实现包含 X-Argus、X-Helios，以及已验证可用的 3040 X-Medusa
source336 -> stage33f -> stage34a/control32 管线。默认优先使用 6.5.6.32
accepted881 纯 Python profile，并在服务端拒绝时回退到 6.4.4.32 legacy
纯 Python profile。最新版任意时间戳的动态 field13 尚未作为生产默认路径。
"""
from __future__ import annotations

import base64
import hashlib
import socket
import ssl
import struct
import time
import urllib.parse
import secrets
import zlib
from dataclasses import dataclass

SIGN_KEY_B64 = "rBrarpWnr5SlEUqzs6l92ABQqgo5MUxAUoyuyVJWwow="
SIGN_KEY = base64.b64decode(SIGN_KEY_B64)
SIGN_KEY32_3040 = bytes.fromhex("4e54b707757a4c15473ba0ba01740ed1b3eac6088de0441fbaf79d28dee33ddf")
MEDUSA_MATERIAL16 = bytes.fromhex("d31e3718288a1027baab59f146a09a9c")
MEDUSA_IV16 = bytes.fromhex("ea180a0336ed352fcd24e4d50018ae54")
MEDUSA_AES_LIKE_TABLE = bytes.fromhex(
    "fa7d086b9c59b34b045f39d0384a91990067a6209ff54d827326eedf18668333"
    "800319fbd9feaeaaa9b052c60bf379254e78b436ac5d1a279e88dbbd3c63ec49"
    "15c1301fdcb856d46ccdca0943c835a3ef1ef496d2fc0e727b9484d1ea455a62"
    "023fd31281342bdd7ee628f2a54613013b21f66137292a0ded8cafbf9d5cbb24"
    "760f75e45389e1988db19a65704f544c58ab6e6f8b23c407110cbacfa0a48ed8"
    "053d14b2da74c3d7e7bed67fde48163e8590a155b7774222c986502e17f96431"
    "2c9bf16d1c4468e3e9a89397cb3257ebe5716aadc0ccc7c5fd601da22d47a7e2"
    "51695e7ace0a41b6958ff7b987e03a06108ab5f85bd5f0bc92ff7c2fc2e81b40"
)
MEDUSA_BIT_LANES = (5, 14, 18, 28, 35, 41, 48, 63)
# input bit -> output bit
MEDUSA_BIT_PERM = (2, 0, 4, 7, 5, 6, 1, 3)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def u32le(x: int) -> bytes:
    return struct.pack("<I", x & 0xFFFFFFFF)


def zigzag32(x: int) -> int:
    x &= 0xFFFFFFFF
    # treat as signed 32-bit
    if x & 0x80000000:
        x -= 0x100000000
    return ((x << 1) ^ (x >> 31)) & 0xFFFFFFFF


def zigzag64(x: int) -> int:
    x &= 0xFFFFFFFFFFFFFFFF
    if x & 0x8000000000000000:
        x -= 0x10000000000000000
    return ((x << 1) ^ (x >> 63)) & 0xFFFFFFFFFFFFFFFF


def proto_varint(n: int) -> bytes:
    n = int(n)
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _proto_read_varint(data: bytes | bytearray, offset: int = 0) -> tuple[int, int]:
    """Read protobuf varint and return `(value, next_offset)`."""
    value = 0
    shift = 0
    i = offset
    while i < len(data):
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, i
        shift += 7
        if shift > 70:
            raise ValueError("protobuf varint too long")
    raise ValueError("truncated protobuf varint")


@dataclass(frozen=True)
class ProtoField:
    field_no: int
    wire_type: int
    value: int | bytes
    start: int
    end: int


def proto_iter_fields(data: bytes | bytearray | memoryview, *, stop_at_zero_padding: bool = True) -> list[ProtoField]:
    """Decode a protobuf-like payload while preserving field offsets.

    This intentionally supports only the wire types used in the recovered
    source336 messages: varint, fixed64, length-delimited, and fixed32.
    """
    raw = bytes(data)
    out: list[ProtoField] = []
    i = 0
    while i < len(raw):
        if stop_at_zero_padding and raw[i] == 0:
            break
        start = i
        key, i = _proto_read_varint(raw, i)
        if key == 0 and stop_at_zero_padding:
            break
        field_no, wire_type = key >> 3, key & 7
        if wire_type == 0:
            value, i = _proto_read_varint(raw, i)
        elif wire_type == 1:
            if i + 8 > len(raw):
                raise ValueError("truncated fixed64 field")
            value = raw[i:i + 8]
            i += 8
        elif wire_type == 2:
            ln, i = _proto_read_varint(raw, i)
            if i + ln > len(raw):
                raise ValueError("truncated bytes field")
            value = raw[i:i + ln]
            i += ln
        elif wire_type == 5:
            if i + 4 > len(raw):
                raise ValueError("truncated fixed32 field")
            value = raw[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        out.append(ProtoField(field_no, wire_type, value, start, i))
    return out


def proto_first_field(data: bytes | bytearray | memoryview, field_no: int) -> int | bytes | None:
    for field in proto_iter_fields(data):
        if field.field_no == int(field_no):
            return field.value
    return None


def proto_key(field_no: int, wire_type: int) -> bytes:
    return proto_varint((field_no << 3) | wire_type)


def proto_field_varint(field_no: int, value: int) -> bytes:
    return proto_key(field_no, 0) + proto_varint(value)


def proto_field_bytes(field_no: int, data: bytes | str) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    return proto_key(field_no, 2) + proto_varint(len(data)) + data


def proto_field_fixed32(field_no: int, value: int) -> bytes:
    return proto_key(field_no, 5) + u32le(value)


def sm3_query_prefix6(url: str) -> bytes:
    """top.f14 = SM3(URL query bytes)[:6]；query 不含 '?'，不含 path。"""
    query = urllib.parse.urlsplit(url).query
    return sm3(query.encode())[:6]


# ---- SM3 ----
IV = [
    0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
    0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E,
]


def _rol(x: int, n: int) -> int:
    x &= 0xFFFFFFFF
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _p0(x: int) -> int:
    return x ^ _rol(x, 9) ^ _rol(x, 17)


def _p1(x: int) -> int:
    return x ^ _rol(x, 15) ^ _rol(x, 23)


def sm3(data: bytes) -> bytes:
    msg = bytearray(data)
    bit_len = len(msg) * 8
    msg.append(0x80)
    while (len(msg) % 64) != 56:
        msg.append(0)
    msg += bit_len.to_bytes(8, "big")

    v = IV[:]
    for off in range(0, len(msg), 64):
        block = msg[off:off+64]
        w = [int.from_bytes(block[i:i+4], "big") for i in range(0, 64, 4)]
        for j in range(16, 68):
            w.append(_p1(w[j-16] ^ w[j-9] ^ _rol(w[j-3], 15)) ^ _rol(w[j-13], 7) ^ w[j-6])
        w1 = [w[j] ^ w[j+4] for j in range(64)]
        a, b, c, d, e, f, g, h = v
        for j in range(64):
            tj = 0x79CC4519 if j <= 15 else 0x7A879D8A
            ss1 = _rol((_rol(a, 12) + e + _rol(tj, j % 32)) & 0xFFFFFFFF, 7)
            ss2 = ss1 ^ _rol(a, 12)
            if j <= 15:
                ff = a ^ b ^ c
                gg = e ^ f ^ g
            else:
                ff = (a & b) | (a & c) | (b & c)
                gg = (e & f) | ((~e) & g)
            tt1 = (ff + d + ss2 + w1[j]) & 0xFFFFFFFF
            tt2 = (gg + h + ss1 + w[j]) & 0xFFFFFFFF
            d = c
            c = _rol(b, 9)
            b = a
            a = tt1
            h = g
            g = _rol(f, 19)
            f = e
            e = _p0(tt2)
        v = [x ^ y for x, y in zip(v, [a, b, c, d, e, f, g, h])]
    return b"".join(x.to_bytes(4, "big") for x in v)


# Alias used by the lifted Medusa src_a builders.
sm3_digest = sm3


def x_argus(khronos: int | None = None) -> str:
    if khronos is None:
        khronos = int(time.time())
    return b64(u32le(khronos))


X_GORGON_SEED_0404 = (0x1E, 0x00, 0xE0, 0xE4, 0x93, 0x45, 0x01, 0xD0)


def _xg_reverse_byte(v: int) -> int:
    v &= 0xFF
    return ((v & 0x0F) << 4) | (v >> 4)


def _xg_rbit(v: int) -> int:
    v &= 0xFF
    out = 0
    for _ in range(8):
        out = (out << 1) | (v & 1)
        v >>= 1
    return out & 0xFF


def x_gorgon_0404(
    url_query: str | bytes,
    post_data: str | bytes = b"",
    cookie: str | bytes = b"",
    timestamp: int | None = None,
    *,
    prefix0: int = 0x04,
    seed: tuple[int, int, int, int, int, int, int, int] = X_GORGON_SEED_0404,
) -> str:
    """Pure-Python X-Gorgon 0404-family builder.

    This is kept as an experimental body-bound companion for current 3040
    traces.  Current app samples emit an 8404-family value, while older/common
    ByteDance traffic uses 0404.  `prefix0` lets callers probe either family
    without changing the transform.
    """
    ts = int(time.time() if timestamp is None else timestamp) & 0xFFFFFFFF
    q = url_query.encode("utf-8") if isinstance(url_query, str) else bytes(url_query)
    body = post_data.encode("utf-8") if isinstance(post_data, str) else bytes(post_data)
    ck = cookie.encode("utf-8") if isinstance(cookie, str) else bytes(cookie)

    material = bytearray()
    material += hashlib.md5(q).digest()[:4]
    material += hashlib.md5(body).digest()[:4] if body else b"\x00" * 4
    material += hashlib.md5(ck).digest()[:4] if ck else b"\x00" * 4
    material += b"\x00" * 4
    material += ts.to_bytes(4, "big")
    data = list(material[:20])

    sbox = list(range(256))
    tmp_val = None
    for i in range(256):
        a = 0 if i == 0 else (tmp_val if tmp_val is not None else sbox[i - 1])
        if a == 0x55 and i != 1 and tmp_val != 0x55:
            a = 0
        c = (a + i + (int(seed[i % 8]) & 0xFF)) & 0xFF
        tmp_val = c if c < i else None
        sbox[i], sbox[c] = sbox[c], sbox[i]

    tmp_add: list[int] = []
    tmp_hex = sbox[:]
    for i in range(20):
        b = tmp_add[-1] if tmp_add else 0
        c = (sbox[i + 1] + b) & 0xFF
        tmp_add.append(c)
        tmp_hex[i + 1] = tmp_hex[c]
        e = (tmp_hex[c] * 2) & 0xFF
        data[i] ^= tmp_hex[e]

    for i in range(20):
        b = _xg_reverse_byte(data[i])
        mixed = b ^ data[(i + 1) % 20]
        data[i] = (~(_xg_rbit(mixed) ^ 20)) & 0xFF

    prefix = bytes([int(prefix0) & 0xFF, 0x04, int(seed[7]) & 0xFF, int(seed[3]) & 0xFF, 0x00, 0x01])
    return prefix.hex() + bytes(data).hex()


def _x_gorgon_0404_prga_mask(
    seed: tuple[int, int, int, int, int, int, int, int] = X_GORGON_SEED_0404,
) -> bytes:
    """Return the 20-byte xor mask used by the 0404-family PRGA stage.

    Kept separate so the current 8404 research path can invert captured
    Gorgon samples and compare their pre-final material against candidate
    inputs without touching the production Medusa path.
    """
    sbox = list(range(256))
    tmp_val = None
    for i in range(256):
        a = 0 if i == 0 else (tmp_val if tmp_val is not None else sbox[i - 1])
        if a == 0x55 and i != 1 and tmp_val != 0x55:
            a = 0
        c = (a + i + (int(seed[i % 8]) & 0xFF)) & 0xFF
        tmp_val = c if c < i else None
        sbox[i], sbox[c] = sbox[c], sbox[i]

    tmp_add: list[int] = []
    tmp_hex = sbox[:]
    mask = bytearray()
    for i in range(20):
        b = tmp_add[-1] if tmp_add else 0
        c = (sbox[i + 1] + b) & 0xFF
        tmp_add.append(c)
        tmp_hex[i + 1] = tmp_hex[c]
        e = (tmp_hex[c] * 2) & 0xFF
        mask.append(tmp_hex[e])
    return bytes(mask)


def _x_gorgon_0404_inverse_final20(final20: bytes | bytearray | memoryview) -> list[bytes]:
    """Invert the final in-place rbit/nibble-mix stage of 0404-family Gorgon.

    The forward transform mutates bytes 0..19 in order, so byte 19 depends on
    the already-mutated byte 0.  This inverse usually has exactly one solution;
    returning a list keeps the helper honest for non-family samples.
    """
    out = bytes(final20)
    if len(out) != 20:
        raise ValueError("final20 must be exactly 20 bytes")
    y = [_xg_rbit(((~v) & 0xFF) ^ 20) for v in out]
    sols: list[bytes] = []
    for first in range(256):
        data = [first]
        for i in range(19):
            data.append(y[i] ^ _xg_reverse_byte(data[i]))
        if (y[19] ^ _xg_reverse_byte(data[19])) == out[0]:
            sols.append(bytes(data))
    return sols


def x_gorgon_0404_recover_material_candidates(
    gorgon_hex: str,
    *,
    seed: tuple[int, int, int, int, int, int, int, int] = X_GORGON_SEED_0404,
) -> list[bytes]:
    """Recover candidate 20-byte pre-PRGA materials from a 0404-family Gorgon.

    This is a research helper for the unfinished 8404 lift.  If a captured
    value is really the same transform with a different prefix, the recovered
    material should match:

      md5(query)[:4] || md5(body)[:4] || md5(cookie)[:4] || 00000000 || ts_be

    under the right seed.  Existing 6.5.6.32 8404 traces do not satisfy that
    with the old 0404 seed, which is useful negative evidence.
    """
    raw = bytes.fromhex(gorgon_hex)
    if len(raw) == 26:
        final20 = raw[6:]
    elif len(raw) == 20:
        final20 = raw
    else:
        raise ValueError("gorgon_hex must decode to 20 or 26 bytes")
    mask = _x_gorgon_0404_prga_mask(seed)
    return [bytes(a ^ b for a, b in zip(sol, mask)) for sol in _x_gorgon_0404_inverse_final20(final20)]


def x_gorgon_8404_parts(gorgon_hex: str) -> dict[str, int | bytes | str]:
    """Parse the observed 6.5.6.32 8404-family X-Gorgon envelope.

    Current traces show a 26-byte value:

        84 04 pp qq ff ff || body20

    ``ff ff`` matches the source/input family seen in native buffers
    (e.g. 0x4081/0x4085/0x4001).  The first two variable bytes ``pp qq``
    are still being lifted; keeping this parser makes trace comparison
    explicit without pretending the full 8404 transform is done.
    """
    raw = bytes.fromhex(gorgon_hex)
    if len(raw) != 26:
        raise ValueError("8404 X-Gorgon must decode to 26 bytes")
    if raw[0] != 0x84 or raw[1] != 0x04:
        raise ValueError("not an 8404-family X-Gorgon")
    return {
        "version0": raw[0],
        "version1": raw[1],
        "prefix2": raw[2],
        "prefix3": raw[3],
        "family": int.from_bytes(raw[4:6], "big"),
        "family_hex": raw[4:6].hex(),
        "body20": raw[6:],
        "raw": raw,
    }


@dataclass(frozen=True)
class XGorgon8404Recovered:
    """Decomposed 8404-family Gorgon sample."""

    raw_hex: str
    prefix2: int
    prefix3: int
    family: int
    material20: bytes
    pre_final20: bytes
    mask20: bytes


def _x_gorgon_0404_final20(masked_material20: bytes | bytearray | memoryview) -> bytes:
    """Apply the shared 20-byte final rbit/nibble-mix stage used by 0404/8404.

    Existing 8404 traces prove the old 0404 PRGA mask is no longer correct, but
    the last in-place byte mixer is still invertible by
    ``_x_gorgon_0404_inverse_final20``.  Keeping this tiny forward helper lets
    us validate a recovered 8404 mask without claiming the missing mask/source
    derivation is solved.
    """
    data = list(bytes(masked_material20))
    if len(data) != 20:
        raise ValueError("masked_material20 must be exactly 20 bytes")
    for i in range(20):
        b = _xg_reverse_byte(data[i])
        mixed = b ^ data[(i + 1) % 20]
        data[i] = (~(_xg_rbit(mixed) ^ 20)) & 0xFF
    return bytes(data)


def x_gorgon_8404_observed_material(
    url_or_query: str | bytes,
    post_data: str | bytes = b"",
    cookie: str | bytes = b"",
    timestamp: int | None = None,
) -> bytes:
    """Build the 20-byte material observed before the current 8404 final stage.

    Multiple 6.5.6.32 traces (including fixed-rand bodyB/bodyD) show that the
    visible 8404 final body is still produced from the classic material layout:

      md5(query)[:4] || md5(body)[:4] || md5(cookie)[:4] || 00000000 || ts_be

    but with a *new per-family/per-random mask* before the final 20-byte mixer.
    This helper intentionally only builds the proven material.  It accepts
    either a full URL or a raw query string.
    """
    if timestamp is None:
        timestamp = int(time.time())
    if isinstance(url_or_query, bytes):
        q = bytes(url_or_query)
        if b"?" in q:
            q = urllib.parse.urlsplit(q.decode("utf-8", "replace")).query.encode()
    else:
        s = str(url_or_query)
        q = urllib.parse.urlsplit(s).query.encode() if "?" in s else s.encode()
    body = post_data.encode("utf-8") if isinstance(post_data, str) else bytes(post_data)
    ck = cookie.encode("utf-8") if isinstance(cookie, str) else bytes(cookie)
    return (
        hashlib.md5(q).digest()[:4]
        + (hashlib.md5(body).digest()[:4] if body else b"\x00" * 4)
        + (hashlib.md5(ck).digest()[:4] if ck else b"\x00" * 4)
        + b"\x00" * 4
        + int(timestamp).to_bytes(4, "big")
    )


def x_gorgon_8404_recover_mask_candidates(
    gorgon_hex: str,
    url_or_query: str | bytes,
    post_data: str | bytes = b"",
    cookie: str | bytes = b"",
    timestamp: int | None = None,
) -> list[bytes]:
    """Recover possible 20-byte 8404 masks from a native sample and material.

    For confirmed same-prefix samples the recovered mask must be identical.  The
    fixed-rand bodyB/bodyD trace satisfies this, which narrows 8404 down to:

      material20 -> XOR new_mask20 -> shared final20 -> envelope body20

    The remaining missing part is generating ``new_mask20`` and envelope bytes
    from the new native state without an oracle.
    """
    parts = x_gorgon_8404_parts(gorgon_hex)
    material = x_gorgon_8404_observed_material(url_or_query, post_data, cookie, timestamp)
    return [
        bytes(a ^ b for a, b in zip(pre_final, material))
        for pre_final in _x_gorgon_0404_inverse_final20(parts["body20"])  # type: ignore[arg-type]
    ]


def x_gorgon_8404_recover(
    gorgon_hex: str,
    url_or_query: str | bytes,
    post_data: str | bytes = b"",
    cookie: str | bytes = b"",
    timestamp: int | None = None,
) -> XGorgon8404Recovered:
    """Recover the currently proven 8404 components for one native sample."""
    parts = x_gorgon_8404_parts(gorgon_hex)
    material = x_gorgon_8404_observed_material(url_or_query, post_data, cookie, timestamp)
    pre_finals = _x_gorgon_0404_inverse_final20(parts["body20"])  # type: ignore[arg-type]
    if len(pre_finals) != 1:
        raise ValueError(f"expected one 8404 pre-final candidate, got {len(pre_finals)}")
    pre_final = pre_finals[0]
    mask = bytes(a ^ b for a, b in zip(pre_final, material))
    return XGorgon8404Recovered(
        raw_hex=bytes(parts["raw"]).hex(),  # type: ignore[arg-type]
        prefix2=int(parts["prefix2"]),
        prefix3=int(parts["prefix3"]),
        family=int(parts["family"]),
        material20=material,
        pre_final20=pre_final,
        mask20=mask,
    )


def x_gorgon_8404_with_mask(
    url_or_query: str | bytes,
    post_data: str | bytes,
    *,
    timestamp: int,
    prefix2: int,
    prefix3: int,
    family: int,
    mask20: bytes | bytearray | memoryview,
    cookie: str | bytes = b"",
) -> str:
    """Build an 8404 Gorgon when the 20-byte native mask is already known.

    This is a verified research bridge, not the final long-term generator: it
    proves the final stage and material layout, while making the still-missing
    mask derivation explicit.
    """
    mask = bytes(mask20)
    if len(mask) != 20:
        raise ValueError("mask20 must be exactly 20 bytes")
    material = x_gorgon_8404_observed_material(url_or_query, post_data, cookie, timestamp)
    final20 = _x_gorgon_0404_final20(bytes(a ^ b for a, b in zip(material, mask)))
    return (
        bytes([0x84, 0x04, int(prefix2) & 0xFF, int(prefix3) & 0xFF])
        + (int(family) & 0xFFFF).to_bytes(2, "big")
        + final20
    ).hex()


def x_ss_stub_md5(body: bytes | str, *, upper: bool = True) -> str:
    """Return standard TT X-SS-STUB = MD5(POST body).

    Newer traces couple Gorgon with the POST-body digest.  The production
    downloader's accepted881 profile still sends the observed empty stub, but
    this helper is used by latest/current experiments and documents the body
    binding explicitly.
    """
    raw = body.encode("utf-8") if isinstance(body, str) else bytes(body or b"")
    h = hashlib.md5(raw).hexdigest()
    return h.upper() if upper else h


def latest913_bodyc_trace_medusa() -> str:
    """Reproduce the latest913 bodyC native X-Medusa sample from pure Python.

    This locks down the recovered 913-byte source336 layout, field16 wrapper,
    control32(3892), and final scatter stage.  It is a regression oracle for
    continuing field13/Gorgon dynamic recovery.
    """
    url = (
        "https://api5.novelfm.com/novelfm/playerapi/full/mget/v1/?device_platform=android&os=android&ssmix=a"
        "&_rticket=1783440915801&cdid=7634657e-a134-47cf-9ac3-c38ea9923097&channel=54157680a&aid=3040"
        "&app_name=novel_fm&version_code=656&version_name=6.5.6.32&manifest_version_code=656&update_version_code=65632"
        "&resolution=1440*2560&dpi=640&device_type=SM-S9260&device_brand=Samsung&language=zh&os_api=28&os_version=9"
        "&ac=wifi&device_id=3001028083774489&iid=1395712309393850&comment_tag_c=5&vip_state=0&host_abi=arm64-v8a"
        "&category_style=1&need_personal_recommend=1"
        "&ab_sdk_version=90111254%2C90975474%2C16797554%2C91986083%2C90126074%2C91986082%2C91008840%2C91281044%2C92120672%2C90110758%2C90174492%2C5711286%2C16963142%2C17225371%2C90114353%2C90098780%2C92100130%2C91347266%2C90952506%2C90614667%2C91801013%2C91763052%2C91763051%2C91763050%2C91787063%2C90661280%2C91633046%2C90609513%2C92319500"
        "&rom_version=PQ3A.190605.02261134+release-keys&klink_egdi=AAK_uq0vE8PrXz2HmNU9hVK7t9H-AFvbvPlsZSPYH3E9haMKxm0o-Yqm"
    )
    return x_medusa_3040_full_mget_latest913(
        url,
        khronos=1783440910,
        rand2=0x4B6AD9A1,
        rand3=0x0F7AF868,
        rand4=0x61A25AB3,
        field13=bytes.fromhex("8bf98cf16435cff91116b1c4163366eb2c980ef0"),
        field16_raw18=bytes.fromhex("11a304915c771102ce3329f9bb8b8cf9a130"),
        ladon_raw=bytes.fromhex("a1cc9821"),
        control32=bytes.fromhex("34dea6e752eec8e6a5c3b847ffb9c08f47f4ff879048784964115e7f4c8c8e42"),
        head_byte=9,
    )


def continue913_bodya_trace_medusa() -> str:
    """Reproduce continue-multi-heads bodyA native X-Medusa in pure Python."""
    url = (
        "https://api5-sinfonlinec.novelfm.com/novelfm/playerapi/full/mget/v1/?device_platform=android&os=android&ssmix=a"
        "&_rticket=1783416159882&cdid=5b00d94e-eaa2-42fa-8095-6e994728a48f&channel=vivo_3040_64&aid=3040"
        "&app_name=novel_fm&version_code=656&version_name=6.5.6.32&manifest_version_code=656&update_version_code=65632"
        "&resolution=1440*2560&dpi=640&device_type=SM-S9260&device_brand=Samsung&language=zh&os_api=28&os_version=9"
        "&ac=wifi&device_id=3001028083774489&iid=3313243055211242&comment_tag_c=5&vip_state=0&host_abi=arm64-v8a"
        "&category_style=1&need_personal_recommend=1&rom_version=PQ3A.190605.02261134+release-keys"
    )
    return x_medusa_3040_full_mget_continue913(
        url,
        khronos=1_783_416_153,
        rand2=0x23456789,
        rand3=0x3456789A,
        rand4=0x456789AB,
        field13=bytes.fromhex("ab13507e946dd63ce01183756387ef4e2da69bb4"),
        ladon_raw=bytes.fromhex("55b74f51"),
        head_byte=0xAB,
    )


def helios_prefix4(helios_rand: int) -> bytes:
    """X-Helios raw[0:4]。

    运行时 rand hook 样本已验证：
      rand1=0x6eea984c -> Helios raw starts with 4c98ea6e

    注意：这只是 Helios 已还原的前缀；raw[4:36] 不是简单
    SM3(sign_key||rand||sign_key)，仍在 0xa02d0/0x26732c VM 中继续 lift。
    """
    return u32le(helios_rand)


def _ror64(value: int, count: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    count &= 63
    return ((value >> count) | (value << (64 - count))) & 0xFFFFFFFFFFFFFFFF


def _helios_encrypt_block(hash_table: list[int], block16: bytes) -> bytes:
    """X-Helios 16-byte block encryptor.

    This is the 0x22-round Feistel-like primitive used by 3040 on
    app 6.5.6.32.  It was verified by decrypting oracle X-Helios samples:
      raw[4:] decrypts to b"{X-Khronos}-1532254240-3040" + PKCS7.
    """
    if len(block16) != 16:
        raise ValueError("block16 must be 16 bytes")
    data0 = int.from_bytes(block16[:8], "little")
    data1 = int.from_bytes(block16[8:], "little")
    for i in range(0x22):
        h = hash_table[i]
        data1 = (h ^ ((data0 + _ror64(data1, 8)) & 0xFFFFFFFFFFFFFFFF)) & 0xFFFFFFFFFFFFFFFF
        data0 = (data1 ^ _ror64(data0, 61)) & 0xFFFFFFFFFFFFFFFF
    return data0.to_bytes(8, "little") + data1.to_bytes(8, "little")


def x_helios_3040(khronos: int | None = None, rand32: int | None = None) -> str:
    """纯 Python 生成番茄畅听 3040 的 X-Helios。

    格式：
      base64( uint32_le(rand32) || encrypt(pkcs7(f"{khronos}-1532254240-3040")) )

    注意这里的 license/version 常量是 1532254240，不是外部项目里常见的
    1588093228/1967；已用 out/medusa_oracle_batch.jsonl 的 X-Helios 样本验证。
    """
    if khronos is None:
        khronos = int(time.time())
    if rand32 is None:
        rand32 = int.from_bytes(hashlib.sha256(f"{time.time_ns()}".encode()).digest()[:4], "little")
    rand32 &= 0xFFFFFFFF

    md5 = hashlib.md5(u32le(rand32) + b"3040").digest()
    hex_table = b"0123456789abcdef"
    keybuf = bytearray(32)
    for i, v in enumerate(md5):
        keybuf[2 * i] = hex_table[v >> 4]
        keybuf[2 * i + 1] = hex_table[v & 0x0F]

    key_words = [int.from_bytes(keybuf[i:i + 8], "little") for i in range(0, 32, 8)]
    hash_table = [key_words[0]]
    b0, b8 = key_words[0], key_words[1]
    queue = key_words[2:]
    for i in range(0x22):
        x = ((_ror64(b8, 8) + b0) ^ i) & 0xFFFFFFFFFFFFFFFF
        queue.append(x)
        x = (x ^ _ror64(b0, 61)) & 0xFFFFFFFFFFFFFFFF
        hash_table.append(x)
        b0 = x
        b8 = queue.pop(0)

    raw = f"{int(khronos)}-1532254240-3040".encode("ascii")
    pad = 16 - (len(raw) % 16)
    raw += bytes([pad]) * pad
    enc = b"".join(_helios_encrypt_block(hash_table, raw[i:i + 16]) for i in range(0, len(raw), 16))
    return b64(u32le(rand32) + enc)


def medusa_fixed20(khronos: int) -> bytes:
    t0, t1, t2, t3 = u32le(khronos)
    # 2026-07-05 重新用 khronos=1783236460 样本校准：
    # raw[:20] ^ repeat(u32le(khronos)) =
    #   05 00 00 00  2d 4b 4f ca  49 75 0d 43  3f b5 ae 2c  22 6d cc 56
    # 旧写法把 t2/t3 的 XOR 后结果误当常量，只在特定 khronos 低字节上碰巧成立。
    return bytes([
        t0 ^ 0x05, t1, t2, t3,
        t0 ^ 0x2D, t1 ^ 0x4B, t2 ^ 0x4F, t3 ^ 0xCA,
        t0 ^ 0x49, t1 ^ 0x75, t2 ^ 0x0D, t3 ^ 0x43,
        t0 ^ 0x3F, t1 ^ 0xB5, t2 ^ 0xAE, t3 ^ 0x2C,
        t0 ^ 0x22, t1 ^ 0x6D, t2 ^ 0xCC, t3 ^ 0x56,
    ])


def d71bc_key(d71bc_rand: int) -> bytes:
    seed32_le = u32le(d71bc_rand)
    return sm3(SIGN_KEY + seed32_le + SIGN_KEY)


def medusa_d71bc_rand_parts(d71bc_rand: int) -> tuple[bytes, bytes]:
    """Return `(low2, high2)` of the Medusa d71bc rand in little-endian order.

    Runtime oracle shows the low half is stored at raw packet offset 20..22,
    while the high half is stored at the end of the recovered second buffer.
    """
    raw = u32le(d71bc_rand)
    return raw[:2], raw[2:]


def medusa_second_buffer_layout(first_intermediate: bytes, tail2: bytes, seed8_rand: int, first_byte: int = 0xA6, final2: bytes | None = None) -> bytearray:
    """second_buffer = first_byte || seed8 || first_intermediate || high2.

    `tail2` is kept for backward-compatible callers and is still the low 16
    bits stored at raw packet[20:22].  Current 3040 traces show the final two
    bytes inside the recovered second buffer are the high 16 bits of the same
    d71bc rand, not `tail2`; pass `final2=high2` for current packets.
    """
    seed8 = u32le(seed8_rand) + bytes.fromhex("013a0b00")
    if len(tail2) != 2:
        raise ValueError("tail2 must be 2 bytes")
    if final2 is None:
        final2 = tail2
    if len(final2) != 2:
        raise ValueError("final2 must be 2 bytes")
    return bytearray(bytes([first_byte & 0xFF]) + seed8 + first_intermediate + final2)


def reverse_xor(reverse_source: bytes, key4: bytes) -> bytes:
    """first_intermediate = reverse(source) xor repeating key4."""
    if len(key4) != 4:
        raise ValueError("key4 must be 4 bytes")
    n = len(reverse_source)
    return bytes(reverse_source[n - 1 - i] ^ key4[i & 3] for i in range(n))


def _permute_byte_from_lanes(word: int) -> int:
    out = 0
    for in_bit, out_bit in enumerate(MEDUSA_BIT_PERM):
        bit = (word >> MEDUSA_BIT_LANES[in_bit]) & 1
        out |= bit << out_bit
    return out


def _patch_word_lanes(word: int, value: int) -> int:
    """把 value 按 byte bit permutation 的逆向写回 word 的稀疏 bit lanes。"""
    for in_bit, out_bit in enumerate(MEDUSA_BIT_PERM):
        bit = (value >> out_bit) & 1
        lane = MEDUSA_BIT_LANES[in_bit]
        if bit:
            word |= 1 << lane
        else:
            word &= ~(1 << lane)
    return word & 0xFFFFFFFFFFFFFFFF


def medusa_extract31(second_buffer: bytes | bytearray) -> bytes:
    """从 second_buffer 前 31 个 little-endian 64-bit word 的稀疏 bit lanes 提取 31 字节。"""
    if len(second_buffer) < 31 * 8:
        raise ValueError("second_buffer too short for extract31")
    out = bytearray()
    for i in range(31):
        word = int.from_bytes(second_buffer[i * 8:i * 8 + 8], "little")
        out.append(_permute_byte_from_lanes(word))
    return bytes(out)


def medusa_patch31(second_buffer: bytearray, patch31: bytes) -> bytearray:
    """把 31 字节 patch 回 second_buffer 前 31 个 64-bit word 的稀疏 bit lanes。"""
    if len(patch31) != 31:
        raise ValueError("patch31 must be 31 bytes")
    if len(second_buffer) < 31 * 8:
        raise ValueError("second_buffer too short for patch31")
    for i, val in enumerate(patch31):
        off = i * 8
        word = int.from_bytes(second_buffer[off:off + 8], "little")
        word = _patch_word_lanes(word, val)
        second_buffer[off:off + 8] = word.to_bytes(8, "little")
    return second_buffer


def medusa_assemble_packet(khronos: int, tail2: bytes, aes32: bytes, patched_second_buffer: bytes | bytearray) -> bytes:
    """packet = fixed20 || tail2 || 00 01 || aes32[31] || patched_second_buffer."""
    if len(tail2) != 2:
        raise ValueError("tail2 must be 2 bytes")
    if len(aes32) != 32:
        raise ValueError("aes32 must be 32 bytes")
    return medusa_fixed20(khronos) + tail2 + b"\x00\x01" + aes32[31:32] + bytes(patched_second_buffer)


# Current 3040/full-mget late Medusa layout.
#
# Live 2026-07-05 traces show the bytes after the first 25 raw bytes are not
# the older 31-word AES-like sparse patch directly.  Native first builds
# stage34a from:
#
#     prefinal_head9 || stage33f[:239]
#
# then a 31-byte finalizer stream rewrites one selected bit in each byte of
# this 248-byte prefix.  After that the F2 VM scatters 19 source bytes into
# selected bits of the post-prefix buffer and stores source[19] at raw[24].
# These helpers keep that late layout separate from the older medusa_patch31
# helpers above so both evidence sets remain reproducible.

STAGE34A_FINALIZER_TARGET_BITS = (6, 5, 3, 4, 2, 7, 1, 0)
STAGE34A_FINALIZER_CONTROL_BIT_BY_TARGET = (1, 7, 3, 6, 2, 0, 4, 5)
MEDUSA3040_F2_BYTE_PER_BIT = (7, 5, 6, 2, 4, 0, 3, 1)
MEDUSA3040_F2_BIT_BASE = (4, 0, 3, 1, 2, 7, 6, 5)


def medusa3040_prefix20(khronos: int) -> bytes:
    """3040 X-Medusa raw prefix[0:20] = khronos_le XOR five constants."""
    k = int(khronos) & 0xFFFFFFFF
    return b"".join(((k ^ item) & 0xFFFFFFFF).to_bytes(4, "little") for item in (
        0x00000005,
        0xCA4F4B2D,
        0x430D7549,
        0x2CAEB53F,
        0x56CC6D22,
    ))


def medusa3040_khronos_from_prefix20(prefix20: bytes | bytearray | memoryview) -> int:
    """Invert and validate the current 3040 Medusa raw prefix[0:20]."""
    raw = bytes(prefix20)
    if len(raw) != 20:
        raise ValueError("prefix20 must be exactly 20 bytes")
    constants = (0x00000005, 0xCA4F4B2D, 0x430D7549, 0x2CAEB53F, 0x56CC6D22)
    k = int.from_bytes(raw[:4], "little") ^ constants[0]
    if raw != medusa3040_prefix20(k):
        raise ValueError("prefix20 is not a valid 3040 Medusa prefix")
    return k & 0xFFFFFFFF


@dataclass(frozen=True)
class Medusa3040RuntimeValues:
    """Runtime values recoverable from a current 3040 raw X-Medusa packet."""

    khronos: int
    rand3: int
    rand4: int
    head9: bytes
    head_suffix: int
    head_byte_low6: int
    query_sm3_low6: int
    control32: bytes
    stage33f: bytes


def medusa3040_recover_runtime_values(
    raw: bytes | bytearray | memoryview | str,
    *,
    strict_head: bool = True,
    control_profile: str = "3892",
) -> Medusa3040RuntimeValues:
    """Recover current 3040 finalizer inputs from raw/base64 X-Medusa.

    This is the inverse of the late-stage packet assembler:
      prefix20 || low16(rand3) || 00 01 || control32[31] || stage34a/F2 body

    `head_byte_low6` is the value that enters
    ``medusa3040_head9_from_query_rand4_byte`` after ``& 0x3f``.  The original
    unmasked byte is not uniquely recoverable from the final packet.
    """
    if isinstance(raw, str):
        packet = base64.b64decode(raw)
    else:
        packet = bytes(raw)
    if len(packet) < 25 + 31 * 8 + 2:
        raise ValueError("raw X-Medusa packet too short")
    if packet[22:24] != b"\x00\x01":
        raise ValueError("bad current 3040 Medusa marker")
    khronos = medusa3040_khronos_from_prefix20(packet[:20])
    body = bytearray(packet[25:])
    control32 = medusa3040_control32_from_stage34a_output(body, packet[24])
    copy31 = medusa3040_control32_profile_inverse_to_random32(control32, profile=control_profile)[:31]
    prefix = bytearray(body[:31 * 8])
    for index, control in enumerate(copy31):
        after_bits = stage34a_finalizer_after_bits_from_control(control)
        for bit_index, target_bit in enumerate(STAGE34A_FINALIZER_TARGET_BITS):
            mask = 1 << int(target_bit)
            pos = index * 8 + bit_index
            if (after_bits >> bit_index) & 1:
                prefix[pos] |= mask
            else:
                prefix[pos] &= (~mask) & 0xFF
    head9 = bytes(prefix[:9])
    if strict_head and (len(head9) != 9 or head9[0] != 0x8A):
        raise ValueError("recovered head9 does not look like current 3040 head")
    suffix = int.from_bytes(head9[5:9], "little")
    low16 = int.from_bytes(packet[20:22], "little")
    high16 = int.from_bytes(body[-2:], "little")
    stage33f = bytes(prefix[9:]) + bytes(body[31 * 8:-2]) + b"\x00"
    return Medusa3040RuntimeValues(
        khronos=khronos,
        rand3=((high16 << 16) | low16) & 0xFFFFFFFF,
        rand4=int.from_bytes(head9[1:5], "little"),
        head9=head9,
        head_suffix=suffix,
        head_byte_low6=(suffix >> 8) & 0x3F,
        query_sm3_low6=(suffix >> 14) & 0x3F,
        control32=control32,
        stage33f=stage33f,
    )


def medusa3040_recover_source336(
    raw: bytes | bytearray | memoryview | str,
    *,
    strict_head: bool = True,
    control_profile: str = "3892",
) -> tuple[bytes, Medusa3040RuntimeValues]:
    """Recover plaintext source336 and finalizer runtime values from X-Medusa."""
    runtime = medusa3040_recover_runtime_values(raw, strict_head=strict_head, control_profile=control_profile)
    source336 = medusa_stage33f_to_source336(runtime.stage33f, runtime.rand3)
    return source336, runtime


def _medusa3040_source336_proto_score(source336: bytes | bytearray | memoryview) -> int:
    """Heuristic score for choosing the right control32 profile during reverse."""
    src = bytes(source336).rstrip(b"\x00")
    if not src:
        return -10_000
    score = 0
    if src.startswith(b"\x0a\x10" + SOURCE336_FIELD1_FIXED_SEED[:2]):
        score += 10_000
    elif src[0:1] != b"\x0a":
        score -= 5_000
    if SOURCE336_FIELD1_FIXED_SEED in src[:64]:
        score += 2_000
    try:
        top = _protobuf_scan_fields_limited(src, limit=40)
        if top.get(1, [None])[0] == SOURCE336_FIELD1_FIXED_SEED:
            score += 20_000
        required = (2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 15, 17, 23, 24)
        present = sum(1 for no in required if no in top)
        score += present * 500
        if present < 10:
            score -= 15_000
        if top.get(6, [b""])[0] == SOURCE336_3040_MSSDK_LICENSE:
            score += 1_000
        if top.get(8, [b""])[0] == SOURCE336_MSSDK_VERSION:
            score += 1_000
    except Exception:
        score -= 20_000
    for needle in (b"1532254240", b"v04.09.09", b"SM-S9260", b"Samsung", b"Asia/Shanghai"):
        if needle in src:
            score += 50
    # Penalize high-entropy garbage in the protobuf header area.
    score += sum(1 for b in src[:96] if b < 0x80)
    return score


def _protobuf_scan_fields_limited(data: bytes | bytearray | memoryview, *, limit: int = 80) -> dict[int, list[object]]:
    """Tiny protobuf scanner for diagnostics/scoring; supports varint/fixed/len."""
    buf = bytes(data)
    out: dict[int, list[object]] = {}

    def read_varint(offset: int) -> tuple[int, int]:
        value = 0
        shift = 0
        i = offset
        while i < len(buf) and shift < 70:
            b = buf[i]
            i += 1
            value |= (b & 0x7F) << shift
            if not (b & 0x80):
                return value, i
            shift += 7
        raise ValueError("bad protobuf varint")

    i = 0
    count = 0
    while i < len(buf) and count < limit:
        if buf[i] == 0 and not any(buf[i:]):
            break
        key, i = read_varint(i)
        no, wt = key >> 3, key & 7
        if no <= 0 or no > 2000:
            raise ValueError("bad protobuf field number")
        if wt == 0:
            val, i = read_varint(i)
        elif wt == 1:
            if i + 8 > len(buf):
                raise ValueError("truncated fixed64")
            val = buf[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = read_varint(i)
            if ln < 0 or i + ln > len(buf):
                raise ValueError("truncated length field")
            val = buf[i:i + ln]
            i += ln
        elif wt == 5:
            if i + 4 > len(buf):
                raise ValueError("truncated fixed32")
            val = buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.setdefault(no, []).append(val)
        count += 1
    return out


def medusa3040_recover_source336_auto(
    raw: bytes | bytearray | memoryview | str,
    *,
    strict_head: bool = False,
    profiles: tuple[str, ...] = ("3892", "5329", "d09a"),
    min_score: int = 10_000,
) -> tuple[str, bytes, Medusa3040RuntimeValues]:
    """Recover source336 by trying known current full/mget control32 profiles.

    New 6.5.6.32 samples may switch among 3892/5329/d09a final-control
    branches.  This helper is only for reverse/diagnostics; production signing
    keeps explicit profiles.
    """
    best: tuple[int, str, bytes, Medusa3040RuntimeValues] | None = None
    errors: list[str] = []
    for profile in profiles:
        try:
            source336, runtime = medusa3040_recover_source336(
                raw,
                strict_head=strict_head,
                control_profile=profile,
            )
            score = _medusa3040_source336_proto_score(source336)
            if best is None or score > best[0]:
                best = (score, profile, source336, runtime)
        except Exception as exc:
            errors.append(f"{profile}:{exc}")
    if best is None:
        raise ValueError("no control32 profile recovered source336: " + "; ".join(errors))
    _score, profile, source336, runtime = best
    if _score < int(min_score):
        raise ValueError(f"no trusted source336 recovery; best={profile} score={_score}")
    return profile, source336, runtime


def medusa3040_recover_source336_field13(raw: bytes | bytearray | memoryview | str) -> bytes:
    """Convenience helper: recover top-level source336 field 13 from X-Medusa.

    Current 6.5.6.32 traces can use different late control32 profiles
    (3892/5329/d09a), so use the auto profile detector here instead of the
    historical 3892-only strict path.
    """
    _profile, source336, _runtime = medusa3040_recover_source336_auto(raw, strict_head=False, min_score=0)
    value = proto_first_field(source336, 13)
    if not isinstance(value, bytes):
        raise ValueError("source336 field13 not found")
    if len(value) != 20:
        raise ValueError("source336 field13 must be exactly 20 bytes")
    return value


def stage34a_finalizer_after_bits_from_control(control: int) -> int:
    value = 0
    current = int(control) & 0xFF
    for target_index, source_bit in enumerate(STAGE34A_FINALIZER_CONTROL_BIT_BY_TARGET):
        value |= ((current >> int(source_bit)) & 1) << target_index
    return value & 0xFF


def apply_stage34a_finalizer_control(chunk8: bytes | bytearray | memoryview, control: int) -> bytes:
    raw = bytearray(bytes(chunk8))
    if len(raw) != 8:
        raise ValueError("stage34a finalizer chunk must be exactly 8 bytes")
    after_bits = stage34a_finalizer_after_bits_from_control(control)
    for index, target_bit in enumerate(STAGE34A_FINALIZER_TARGET_BITS):
        mask = 1 << int(target_bit)
        if (after_bits >> index) & 1:
            raw[index] |= mask
        else:
            raw[index] &= (~mask) & 0xFF
    return bytes(raw)


def stage34a_finalizer_control_from_target_bits(chunk8: bytes | bytearray | memoryview) -> int:
    raw = bytes(chunk8)
    if len(raw) != 8:
        raise ValueError("stage34a finalizer chunk must be exactly 8 bytes")
    after_bits = 0
    for index, target_bit in enumerate(STAGE34A_FINALIZER_TARGET_BITS):
        after_bits |= ((raw[index] >> int(target_bit)) & 1) << index
    control = 0
    for target_index, source_bit in enumerate(STAGE34A_FINALIZER_CONTROL_BIT_BY_TARGET):
        control |= ((after_bits >> target_index) & 1) << int(source_bit)
    return control & 0xFF


def stage34a_prefinalizer_prefix248_from_head9_and_stage33f(
    head9: bytes | bytearray | memoryview,
    stage33f: bytes | bytearray | memoryview,
) -> bytes:
    head = bytes(head9)
    source = bytes(stage33f)
    if len(head) != 9:
        raise ValueError("stage34a head must be exactly 9 bytes")
    if len(source) < 239:
        raise ValueError("stage33f must contain at least 239 bytes")
    return head + source[:239]


def finalizer_copy_source31_from_stage34a_prefix248(prefix248: bytes | bytearray | memoryview) -> bytes:
    raw = bytes(prefix248)
    if len(raw) != 31 * 8:
        raise ValueError("stage34a pre-finalizer prefix must be exactly 248 bytes")
    return bytes(stage34a_finalizer_control_from_target_bits(raw[i:i + 8]) for i in range(0, 31 * 8, 8))


def finalizer_copy_source31_from_head9_and_stage33f(
    head9: bytes | bytearray | memoryview,
    stage33f: bytes | bytearray | memoryview,
) -> bytes:
    return finalizer_copy_source31_from_stage34a_prefix248(
        stage34a_prefinalizer_prefix248_from_head9_and_stage33f(head9, stage33f)
    )


def apply_stage34a_finalizer_control31(
    prefix248: bytes | bytearray | memoryview,
    control31: bytes | bytearray | memoryview,
) -> bytes:
    prefix = bytes(prefix248)
    control = bytes(control31)
    if len(prefix) != 31 * 8:
        raise ValueError("stage34a finalizer prefix must be exactly 248 bytes")
    if len(control) < 31:
        raise ValueError("stage34a finalizer control must contain at least 31 bytes")
    out = bytearray()
    for index in range(31):
        out.extend(apply_stage34a_finalizer_control(prefix[index * 8:(index + 1) * 8], control[index]))
    return bytes(out)


def stage34a_tail_fields_from_rand3(rand3: int) -> tuple[int, bytes]:
    high2 = ((int(rand3) >> 16) & 0xFFFF).to_bytes(2, "little")
    # Current 3040 appends only the two high-rand bytes at the end of the
    # stage34a body.  Older notes treated this as `low, high, NUL`; live
    # 6.5.6.32 traces show the byte before these two bytes still belongs to
    # stage33f, and the final two bytes are exactly high16(rand3) little-endian.
    return high2[0], high2[1:2]


def stage33f_to_stage34a_with_control(
    stage33f: bytes | bytearray | memoryview,
    head9: bytes | bytearray | memoryview,
    control31: bytes | bytearray | memoryview,
    *,
    rand3: int | None = None,
    tail_marker: int | None = None,
    tail2: bytes | bytearray | memoryview | None = None,
) -> bytes:
    source = bytes(stage33f)
    if len(source) < 240:
        raise ValueError("stage33f must contain at least 240 bytes")
    if rand3 is not None:
        tail_marker, tail2 = stage34a_tail_fields_from_rand3(int(rand3))
    if tail_marker is None or tail2 is None:
        raise ValueError("either rand3 or tail_marker/tail2 must be supplied")
    suffix = bytes(tail2)
    if len(suffix) not in {1, 2}:
        raise ValueError("stage34a tail2 must be one or two bytes")
    prefix248 = stage34a_prefinalizer_prefix248_from_head9_and_stage33f(head9, source)
    finalized_prefix = apply_stage34a_finalizer_control31(prefix248, control31)
    return finalized_prefix + source[239:-1] + bytes([int(tail_marker) & 0xFF]) + suffix


def medusa3040_f2_scatter(pre_slot8: bytes | bytearray | memoryview, slot10: bytes | bytearray | memoryview, count: int = 19) -> bytes:
    """Pure Python equivalent of the current 3040 late F2 bit scatter."""
    out = bytearray(bytes(pre_slot8))
    source = bytes(slot10)
    for source_index in range(min(int(count), len(source))):
        for source_bit in range(8):
            dest_byte = 8 * source_index + MEDUSA3040_F2_BYTE_PER_BIT[source_bit]
            if dest_byte >= len(out):
                continue
            dest_bit = MEDUSA3040_F2_BIT_BASE[source_bit]
            value = (source[source_index] >> source_bit) & 1
            out[dest_byte] = (out[dest_byte] & ~(1 << dest_bit)) | (value << dest_bit)
    return bytes(out)


def medusa3040_f2_recover_slot10(post_slot8: bytes | bytearray | memoryview, count: int = 19) -> bytes:
    """Recover the F2 slot10 bytes that are visible in a final X-Medusa body."""
    post = bytes(post_slot8)
    out = bytearray(int(count))
    for source_index in range(int(count)):
        value = 0
        for source_bit in range(8):
            dest_byte = 8 * source_index + MEDUSA3040_F2_BYTE_PER_BIT[source_bit]
            if dest_byte >= len(post):
                continue
            dest_bit = MEDUSA3040_F2_BIT_BASE[source_bit]
            value |= ((post[dest_byte] >> dest_bit) & 1) << source_bit
        out[source_index] = value
    return bytes(out)


def medusa3040_assemble_raw_from_stage34a(
    khronos: int,
    rand3: int,
    stage34a: bytes | bytearray | memoryview,
    f2_slot10_20: bytes | bytearray | memoryview,
) -> bytes:
    """Assemble current 3040 raw packet from pre-F2 stage34a and F2 source20."""
    body = bytes(stage34a)
    slot10 = bytes(f2_slot10_20)
    if len(slot10) != 20:
        raise ValueError("F2 slot10 source must be exactly 20 bytes")
    post = medusa3040_f2_scatter(body, slot10[:19], count=19)
    low2 = (int(rand3) & 0xFFFF).to_bytes(2, "little")
    return medusa3040_prefix20(khronos) + low2 + b"\x00\x01" + slot10[19:20] + post


CONTROL32_SBOX_3892_3040 = bytes.fromhex(
    "38922563e36406d37a24100b7902a028044e218453c2a399199c9d13dc40c55b"
    "a67e502273850ebdc1a2e2188b8f2d9e3cb909b8c46a2a9f4a6dd93007eafa"
    "65b7b3a881aa908cc74f0ad76bbbb58617c37012ca05a469fd44e62eecf1e"
    "4272ce860c0ae5eadf99b20d8f0234174006f3f110d7657d01645d4eff8f714"
    "71cbfe1cb2f52648727bf3c9de55153e031b4d61757db63dbf7782eeafe9018"
    "a8891362fb14759ac32bab03b349aeb5a377c5156be4c8dfbe13a684bda39cf"
    "836697c894545fe0f2491acea9fc6252f4ff1e0fe72bd2df6e67a7f635e5bc"
    "c6d1a5dd951d89960887a18eb40c785cdb1f80ed42299333ab98cdd67f316c"
    "d54346585dcc"
)
CONTROL32_SBOX_5329_3040 = bytes.fromhex(
    "53290fe9e51f316f90f74a7e034d367784ab49237cd0135cdc632c322fd3be0e"
    "2624a417ecd9151a7ff64f60730a87f9445ec6ada2f865d22b696c4188f24c"
    "0df37b75510b939f6bc2fe6883e68bc8c722181ba0bdb08c9871b50c9a16"
    "b46eea8eed20ba4b8a14e0fcf1468dbbef05a93e5a9d7dcf06898635c1f"
    "d99f5de287a80c594daaab19b6279fbd75070cb52eb2555c456e39e78398"
    "53874e2f0013ac0b64742d8ca10e4b79c955de8b843ce4ed464a6a72ed5"
    "08091c92e7b3eedd1e1dd12afac321618fa3d65f9167c982f43d66a53c6"
    "a1997585b346dbf9672ff37cdbc073b2dae11453304df00815476b2a13fcc"
    "db30b957a8acafe1274002485912"
)
CONTROL32_SBOX_D09A_3040 = bytes.fromhex(
    "d09a515ba58cbbab3e37b09fd4f6d9244df80512e84cd7956be301cf64f1429d"
    "58577cc241f3a2c655144f81aaf95a4e108da894b38970561ae9002619fbbac9"
    "1fa922ed202bfc62cb08e0de2a7f3631435f8b47c8a3c563b4db4071ce74c46d"
    "a6fa696a9e282d1e825d663b93bfd56c48dce1884bb10fccea063929f746443"
    "fd830a4232e53da21a1f2d1bdfebc3a02c10a1b8ad31d04fd616590d272ac"
    "737e98eb33af60528716bee78ec0ee8699031177277d9cff32542c67caf5f44"
    "591681335ae0e178f3409a7803db83892dde5b9b7f0c750e24aef077badd6"
    "78a09b850b84ec1597e4963cb5e675df1c0d6fb618496ecd76c35c79832559"
    "0c2fb27a5e"
)
CONTROL32_MD5_SIGN_KEY_HALF_3040 = hashlib.md5(SIGN_KEY32_3040[16:32]).digest()
CONTROL32_3892_ORDER = (1, 3, 2, 0)
CONTROL32_3892_SHIFT = (0, 9, 14, 11, 4, 13, 2, 3, 8, 1, 6, 15, 12, 5, 10, 7)
CONTROL32_3892_MIX_OUT_COLS = (3, 0, 2, 1)
CONTROL32_3892_K0_RAW = bytes.fromhex("335d5f2dfc59c1543d880c5519c489e6")
CONTROL32_3892_MID_RAW = bytes.fromhex("4c83d1b1b0da10e58d521cb094969556")
CONTROL32_3892_B_RAW = bytes.fromhex("583cb88ce8e6a86965b4b4d9f122218f")
CONTROL32_3892_FINAL_XOR = bytes.fromhex("4c83d1b1b0da10e58d521cb094969556")
CONTROL32_FULL_MGET_PROFILES_3040 = {
    "5329": {
        "sbox": CONTROL32_SBOX_5329_3040,
        "order": (2, 0, 3, 1),
        "shift": (0, 9, 14, 15, 4, 13, 2, 7, 8, 1, 6, 3, 12, 5, 10, 11),
        "mix_out_cols": (1, 3, 0, 2),
        "k0_raw": bytes.fromhex("1a0e8942d50a173b14dbda3a30975f89"),
        "mid_raw": bytes.fromhex("fde4720628ee653d3c35bf070ca2e08e"),
        "b_raw": bytes.fromhex("0a58200522b645381e83fa3f12211ab1"),
        "final_xor": bytes.fromhex("fde4720628ee653d3c35bf070ca2e08e"),
    },
    "3892": {
        "sbox": CONTROL32_SBOX_3892_3040,
        "order": CONTROL32_3892_ORDER,
        "shift": CONTROL32_3892_SHIFT,
        "mix_out_cols": CONTROL32_3892_MIX_OUT_COLS,
        "k0_raw": CONTROL32_3892_K0_RAW,
        "mid_raw": CONTROL32_3892_MID_RAW,
        "b_raw": CONTROL32_3892_B_RAW,
        "final_xor": CONTROL32_3892_FINAL_XOR,
    },
    "d09a": {
        "sbox": CONTROL32_SBOX_D09A_3040,
        "order": (2, 3, 1, 0),
        "shift": (0, 9, 14, 15, 4, 13, 2, 3, 8, 1, 6, 11, 12, 5, 10, 7),
        "mix_out_cols": (3, 2, 0, 1),
        "k0_raw": bytes.fromhex("a171c4706e755a09afa497088be812bb"),
        "mid_raw": bytes.fromhex("b57431cddb016bc474a5fcccff4dee77"),
        "b_raw": bytes.fromhex("321bfd93e91a96579dbf6a9b62f284ec"),
        "final_xor": bytes.fromhex("b57431cddb016bc474a5fcccff4dee77"),
    },
}


def _control32_reorder_u32_blocks_3040(block16: bytes, order: tuple[int, int, int, int]) -> bytes:
    if len(block16) != 16:
        raise ValueError("block16 must be exactly 16 bytes")
    return bytes(block16[4 * group + int(order[index])] for group in range(4) for index in range(4))


def _control32_subbytes_group_permuted_3040(block16: bytes, sbox: bytes, group_perm: tuple[int, int, int, int]) -> bytes:
    if len(block16) != 16:
        raise ValueError("block16 must be exactly 16 bytes")
    substituted = bytes(sbox[value] for value in block16)
    return b"".join(substituted[4 * int(group):4 * int(group) + 4] for group in group_perm)


def _control32_mix_column(col: list[int]) -> list[int]:
    a0, a1, a2, a3 = [x & 0xFF for x in col]
    return [
        _gf256_mul_aes(a0, 2) ^ _gf256_mul_aes(a1, 3) ^ a2 ^ a3,
        a0 ^ _gf256_mul_aes(a1, 2) ^ _gf256_mul_aes(a2, 3) ^ a3,
        a0 ^ a1 ^ _gf256_mul_aes(a2, 2) ^ _gf256_mul_aes(a3, 3),
        _gf256_mul_aes(a0, 3) ^ a1 ^ a2 ^ _gf256_mul_aes(a3, 2),
    ]


def _control32_mix_columns_profile_3040(block16: bytes, out_cols: tuple[int, int, int, int]) -> bytes:
    if len(block16) != 16:
        raise ValueError("block16 must be exactly 16 bytes")
    out = bytearray(16)
    for in_col, out_col in enumerate(out_cols):
        mixed = _control32_mix_column([block16[in_col + 4 * row] for row in range(4)])
        for row, val in enumerate(mixed):
            out[int(out_col) + 4 * row] = val & 0xFF
    return bytes(out)


def _control32_inv_mix_columns_profile_3040(block16: bytes, out_cols: tuple[int, int, int, int]) -> bytes:
    if len(block16) != 16:
        raise ValueError("block16 must be exactly 16 bytes")
    out = bytearray(16)
    for in_col, out_col in enumerate(out_cols):
        mixed = bytes(block16[int(out_col) + 4 * row] for row in range(4))
        original = _medusa_aes_inv_mix_single_column(mixed)
        for row, val in enumerate(original):
            out[in_col + 4 * row] = val & 0xFF
    return bytes(out)


def _inv_index_permutation(data: bytes, permutation: tuple[int, ...]) -> bytes:
    out = bytearray(len(data))
    for out_index, in_index in enumerate(permutation):
        out[int(in_index)] = data[out_index]
    return bytes(out)


def _control32_inv_subbytes_group_permuted_3040(block16: bytes, sbox: bytes, group_perm: tuple[int, int, int, int]) -> bytes:
    if len(block16) != 16:
        raise ValueError("block16 must be exactly 16 bytes")
    grouped = bytearray(16)
    for out_group, source_group in enumerate(group_perm):
        grouped[4 * int(source_group):4 * int(source_group) + 4] = block16[4 * out_group:4 * out_group + 4]
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[int(v)] = i
    return bytes(inv[b] for b in grouped)


def _xor_same_len(left: bytes, right: bytes) -> bytes:
    if len(left) != len(right):
        raise ValueError("xor inputs must have the same length")
    return bytes(a ^ b for a, b in zip(left, right))


def _control32_profile_3040(profile: str):
    name = str(profile or "3892").strip().lower()
    aliases = {"default": "3892", "current": "d09a", "latest": "d09a", "53290f": "5329", "389225": "3892", "d09a51": "d09a"}
    name = aliases.get(name, name)
    if name not in CONTROL32_FULL_MGET_PROFILES_3040:
        raise ValueError(f"unknown 3040 control32 profile: {profile!r}")
    return name, CONTROL32_FULL_MGET_PROFILES_3040[name]


def _control32_half_transform_profile_3040(block16: bytes, profile_data) -> bytes:
    order = tuple(profile_data["order"])
    k0 = _control32_reorder_u32_blocks_3040(profile_data["k0_raw"], order)
    mid = _control32_reorder_u32_blocks_3040(profile_data["mid_raw"], order)
    b_region = _control32_reorder_u32_blocks_3040(profile_data["b_raw"], order)
    x = _xor_same_len(block16, k0)
    x = _control32_subbytes_group_permuted_3040(x, profile_data["sbox"], order)
    x = bytes(x[index] for index in tuple(profile_data["shift"]))
    x = _control32_mix_columns_profile_3040(x, tuple(profile_data["mix_out_cols"]))
    x = _xor_same_len(x, mid)
    x = _control32_subbytes_group_permuted_3040(x, profile_data["sbox"], order)
    x = bytes(x[index] for index in tuple(profile_data["shift"]))
    x = _xor_same_len(x, b_region)
    return _xor_same_len(x, profile_data["final_xor"])


def _control32_inv_half_transform_profile_3040(block16: bytes, profile_data) -> bytes:
    order = tuple(profile_data["order"])
    k0 = _control32_reorder_u32_blocks_3040(profile_data["k0_raw"], order)
    mid = _control32_reorder_u32_blocks_3040(profile_data["mid_raw"], order)
    b_region = _control32_reorder_u32_blocks_3040(profile_data["b_raw"], order)
    x = _xor_same_len(block16, profile_data["final_xor"])
    x = _xor_same_len(x, b_region)
    x = _inv_index_permutation(x, tuple(profile_data["shift"]))
    x = _control32_inv_subbytes_group_permuted_3040(x, profile_data["sbox"], order)
    x = _xor_same_len(x, mid)
    x = _control32_inv_mix_columns_profile_3040(x, tuple(profile_data["mix_out_cols"]))
    x = _inv_index_permutation(x, tuple(profile_data["shift"]))
    x = _control32_inv_subbytes_group_permuted_3040(x, profile_data["sbox"], order)
    return _xor_same_len(x, k0)


def medusa3040_control32_profile_from_random32(
    random32: bytes | bytearray | memoryview,
    *,
    profile: str = "3892",
    force_last: int = 1,
) -> bytes:
    """3040/full-mget control32 generator for recovered profiles 5329/3892/d09a."""
    _name, profile_data = _control32_profile_3040(profile)
    state = bytearray(bytes(random32))
    if len(state) != 32:
        raise ValueError("random32 must be exactly 32 bytes")
    state[31] = int(force_last) & 0xFF
    for i, v in enumerate(CONTROL32_MD5_SIGN_KEY_HALF_3040):
        state[i] ^= v
    first16 = _control32_half_transform_profile_3040(bytes(state[:16]), profile_data)
    second_pre = _xor_same_len(bytes(state[16:32]), first16)
    return first16 + _control32_half_transform_profile_3040(second_pre, profile_data)


def medusa3040_control32_profile_inverse_to_random32(
    control32: bytes | bytearray | memoryview,
    *,
    profile: str = "3892",
) -> bytes:
    """Invert recovered 3040/full-mget control32 profiles to `copy31 + 00`."""
    _name, profile_data = _control32_profile_3040(profile)
    out = bytes(control32)
    if len(out) != 32:
        raise ValueError("control32 must be exactly 32 bytes")
    first_pre = _control32_inv_half_transform_profile_3040(out[:16], profile_data)
    second_pre = _control32_inv_half_transform_profile_3040(out[16:32], profile_data)
    work = bytearray(first_pre + _xor_same_len(second_pre, out[:16]))
    for i, v in enumerate(CONTROL32_MD5_SIGN_KEY_HALF_3040):
        work[i] ^= v
    work[31] = 0
    return bytes(work)


def medusa3040_control32_3892_from_random32(
    random32: bytes | bytearray | memoryview,
    *,
    force_last: int = 1,
) -> bytes:
    """3040/full-mget control32 profile `3892`, pure Python.

    Current 6.5.6.32 sets byte31 to 1.  The older server-accepted 6.4.4.32
    profile uses the same transform but sets byte31 to 0x41.
    """
    return medusa3040_control32_profile_from_random32(random32, profile="3892", force_last=force_last)


def medusa3040_control32_3892_legacy41_from_random32(random32: bytes | bytearray | memoryview) -> bytes:
    return medusa3040_control32_3892_from_random32(random32, force_last=0x41)


def medusa3040_control32_3892_inverse_to_random32(control32: bytes | bytearray | memoryview) -> bytes:
    """Invert `medusa3040_control32_3892_from_random32`.

    The returned 32 bytes are the preimage before the wrapper overwrites byte31
    with `1`; byte31 is therefore restored to `0` for the full-mget
    `copy31 + 00` use case.
    """
    return medusa3040_control32_profile_inverse_to_random32(control32, profile="3892")


def medusa3040_control32_from_stage34a_output(raw_body: bytes | bytearray | memoryview, raw24: int) -> bytes:
    body = bytes(raw_body)
    if len(body) < 31 * 8:
        raise ValueError("stage34a output body is too short")
    control31 = bytes(
        stage34a_finalizer_control_from_target_bits(body[i:i + 8])
        for i in range(0, 31 * 8, 8)
    )
    return control31 + bytes([int(raw24) & 0xFF])


def medusa3040_stage33f_from_raw(raw: bytes | bytearray | memoryview) -> tuple[bytes, bytes]:
    """Recover `(stage33f, control32)` from a current 3040 raw X-Medusa body."""
    packet = bytes(raw)
    if len(packet) < 25 + 31 * 8 + 3:
        raise ValueError("raw X-Medusa packet too short")
    body = bytearray(packet[25:])
    control32 = medusa3040_control32_from_stage34a_output(body, packet[24])
    copy31 = medusa3040_control32_3892_inverse_to_random32(control32)[:31]
    prefix = bytearray(body[:31 * 8])
    for index, control in enumerate(copy31):
        after_bits = stage34a_finalizer_after_bits_from_control(control)
        for bit_index, target_bit in enumerate(STAGE34A_FINALIZER_TARGET_BITS):
            mask = 1 << int(target_bit)
            pos = index * 8 + bit_index
            if (after_bits >> bit_index) & 1:
                prefix[pos] |= mask
            else:
                prefix[pos] &= (~mask) & 0xFF
    # prefix = head9 || stage33f[:239]
    stage33f = bytes(prefix[9:]) + bytes(body[31 * 8:-2]) + b"\x00"
    return stage33f, control32


def medusa3040_head9_from_query_rand4_field13(
    query: bytes | bytearray | memoryview | str,
    rand4: int,
    field13: bytes | bytearray | memoryview,
) -> bytes:
    """Build current 3040 pre-finalizer head9: 8a || rand4_le || suffix.

    Current live 6.5.6.32 full/mget traces show suffix bytes:

      01 || low6(source336.field13[0]) || 00 || 18

    Older experiments used an sm3(query)-derived middle byte; that produces
    valid-looking packets but server replay returns code=6000 for the current
    source336 family.
    """
    q = query.encode("ascii") if isinstance(query, str) else bytes(query)
    second = bytes(field13)
    if len(second) != 20:
        raise ValueError("field13 must be exactly 20 bytes")
    _ = q  # kept to preserve the public signature and call sites
    # Verified against a live normal signer sample:
    #   head=8a eb2e6f1b 01 3c 00 18
    #   input32=88d10f37...ee08d700
    #   control32=21e643df...c8f560c0
    #   final raw body reproduced byte-for-byte by F2 recover/scatter.
    suffix = (0x18 << 24) | ((second[0] & 0x3F) << 8) | 1
    return b"\x8a" + (int(rand4) & 0xFFFFFFFF).to_bytes(4, "little") + suffix.to_bytes(4, "little")


def medusa3040_head9_from_query_rand4_byte(
    query: bytes | bytearray | memoryview | str,
    rand4: int,
    head_byte: int = 1,
) -> bytes:
    """Build the current full/mget head9 when the head byte is separate.

    Live 6.5.6.32 traces show the suffix byte used here is not always the
    first byte of source336.field13.  For the current full/mget family it is
    stable as low6 == 1.
    """
    q = query.encode("ascii") if isinstance(query, str) else bytes(query)
    suffix = ((0x18 & 0xFF) << 24) | ((sm3(q)[0] & 0x3F) << 14) | ((int(head_byte) & 0x3F) << 8) | 1
    return b"\x8a" + (int(rand4) & 0xFFFFFFFF).to_bytes(4, "little") + suffix.to_bytes(4, "little")


def medusa3040_raw_from_stage33f(
    *,
    khronos: int,
    rand3: int,
    rand4: int,
    query: bytes | bytearray | memoryview | str,
    stage33f: bytes | bytearray | memoryview,
    field13: bytes | bytearray | memoryview,
) -> bytes:
    """Assemble accepted current 3040 X-Medusa raw from stage33f, pure Python."""
    head9 = medusa3040_head9_from_query_rand4_field13(query, rand4, field13)
    copy31 = finalizer_copy_source31_from_head9_and_stage33f(head9, stage33f)
    control32 = medusa3040_control32_3892_from_random32(copy31 + b"\x00")
    stage34a = stage33f_to_stage34a_with_control(stage33f, head9, control32[:31], rand3=rand3)
    pre_f2 = stage34a
    slot19 = medusa3040_f2_recover_slot10(pre_f2, 19)
    post_f2 = medusa3040_f2_scatter(pre_f2, slot19, 19)
    return medusa3040_prefix20(khronos) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + control32[31:32] + post_f2


def medusa3040_raw_from_stage33f_current_tail0(
    *,
    khronos: int,
    rand3: int,
    rand4: int,
    query: bytes | bytearray | memoryview | str,
    stage33f: bytes | bytearray | memoryview,
    control32: bytes | bytearray | memoryview,
    head_byte: int = 1,
) -> bytes:
    """Assemble the live 6.5.6.32 full/mget body layout.

    Compared with the older helper, live traces keep an extra NUL after
    high16(rand3): stage tail is `high16_le || 00`.
    """
    source = bytes(stage33f)
    ctrl = bytes(control32)
    if len(ctrl) != 32:
        raise ValueError("control32 must be exactly 32 bytes")
    head9 = medusa3040_head9_from_query_rand4_byte(query, rand4, head_byte=head_byte)
    prefix248 = stage34a_prefinalizer_prefix248_from_head9_and_stage33f(head9, source)
    finalized_prefix = apply_stage34a_finalizer_control31(prefix248, ctrl[:31])
    high2 = ((int(rand3) >> 16) & 0xFFFF).to_bytes(2, "little")
    pre_f2 = finalized_prefix + source[239:-1] + high2 + b"\x00"
    post_f2 = medusa3040_f2_scatter(pre_f2, medusa3040_f2_recover_slot10(pre_f2, 19), 19)
    return medusa3040_prefix20(khronos) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + ctrl[31:32] + post_f2


def medusa3040_raw_from_stage33f_current(
    *,
    khronos: int,
    rand3: int,
    rand4: int,
    query: bytes | bytearray | memoryview | str,
    stage33f: bytes | bytearray | memoryview,
    control32: bytes | bytearray | memoryview,
    head_byte: int = 1,
) -> bytes:
    """Assemble the live 6.5.6.32 full/mget packet layout seen in traces.

    `out/frida_trace_type3_vm_latest2.out` shows the final body length is
    927 bytes for a 917-byte stage33f:

        248 finalized prefix bytes + stage33f[239:-1] + high16(rand3)

    Earlier experimental notes kept an extra trailing NUL; that produces a
    953-byte raw packet and does not match native `X-Medusa`.
    """
    source = bytes(stage33f)
    ctrl = bytes(control32)
    if len(ctrl) != 32:
        raise ValueError("control32 must be exactly 32 bytes")
    head9 = medusa3040_head9_from_query_rand4_byte(query, rand4, head_byte=head_byte)
    stage34a = stage33f_to_stage34a_with_control(source, head9, ctrl[:31], rand3=rand3)
    post_f2 = medusa3040_f2_scatter(stage34a, medusa3040_f2_recover_slot10(stage34a, 19), 19)
    return medusa3040_prefix20(khronos) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + ctrl[31:32] + post_f2


SOURCE336_ALLOC_SIZE = 0x400
SOURCE336_FIELD1_FIXED_SEED = bytes.fromhex("2d4b4fca49750d433fb5ae2c226dcc56")
SOURCE336_MSSDK_VERSION = b"v04.09.09-ml-android"
SOURCE336_REPORT_VERSION = b"v04.09.09.01-bugfix"
SOURCE336_NOT_SET = b"!notset!"
SOURCE336_3040_VERSION_NAME = b"6.4.4.32"
SOURCE336_3040_MSSDK_LICENSE = b"1532254240"
SOURCE336_3040_FIELD10_CONTINUE7_NATIVE = bytes.fromhex("4081000000000000")
SOURCE336_3040_DEVICE_INSTALL_ID = b"AOpP2oWXxrKyOifBQBXX_Rjlc"
SOURCE336_3040_TIMEZONE = b"Asia/Shanghai,8"
SOURCE336_3040_LOCALE = b"zh_CN"
SOURCE336_3040_PHYSICAL_SIZE = b"Physical size: 1440x2560"
SOURCE336_3040_DEVICE_TYPE = b"SM-S9260"
SOURCE336_3040_DEVICE_BRAND = b"Samsung"
SOURCE336_3040_SOC_MODEL = b"marlin"
SOURCE336_3040_HARDWARE = b"qcom"
SOURCE336_3040_FIELD23_SENTINEL = 1_777_775


def source336_3040_field15_message(
    *,
    field1: int = 190,
    field2: int = 8,
    field3: int = 1_388_734,
    field4: int | None = None,
    field5: int = 716_836_180,
) -> bytes:
    out = bytearray()
    out += proto_field_varint(1, int(field1))
    out += proto_field_varint(2, int(field2))
    out += proto_field_varint(3, int(field3))
    if field4 is not None:
        out += proto_field_varint(4, int(field4))
    out += proto_field_varint(5, int(field5))
    return bytes(out)


def source336_3040_field16_from_raw18(raw18: bytes | bytearray | memoryview, prefix: bytes = b"A") -> bytes:
    raw = bytes(raw18)
    if len(raw) != 18:
        raise ValueError("field16 raw body must be exactly 18 bytes")
    return bytes(prefix) + base64.urlsafe_b64encode(raw).rstrip(b"=")


def source336_3040_metrics_json(
    *,
    cmr: int = 16_777_216,
    cmr2: int = 16_777_216,
    un_h: int = 1_884_036_224,
    vpn: int = 0,
    sts: int = 32_251,
    kd: int = 0,
    fkd: int = 842_674_847,
    pd: int = -575_868_740,
    lp: str = "2|520830913191|522717356745|2089099191808|129984601251936",
    fl: str = "0|0|737340615|3344993835|2856852299|859378443|1595182919|0|0|0",
    dyn: str = "",
    do: int = 0,
    tk: bool = True,
) -> bytes:
    tk_text = "true" if bool(tk) else "false"
    return (
        f'{{"cmr":{int(cmr)},"cmr2":{int(cmr2)},"un_h":{int(un_h)},"vpn":{int(vpn)},'
        f'"sts":{int(sts)},"kd":{int(kd)},"fkd":{int(fkd)},"pd":{int(pd)},'
        f'"lp":"{str(lp)}","fl":"{str(fl)}","dyn":"{str(dyn)}","do":{int(do)},"tk":{tk_text}}}'
    ).encode("utf-8")


def _bswap32(value: int) -> int:
    return int.from_bytes((int(value) & 0xFFFFFFFF).to_bytes(4, "little"), "big")


def source336_3040_metrics_fl_from_words(
    *,
    fl2: int,
    fl4: int,
    fl5: int,
    fl6: int,
    ladon_raw: bytes | bytearray | memoryview | None = None,
    fl9: int = 0,
) -> str:
    raw = bytes(ladon_raw or b"")
    last = int(fl9)
    if raw:
        if len(raw) != 4:
            raise ValueError("ladon raw must be exactly 4 bytes")
        last = int.from_bytes(raw, "big")
    return "|".join(str(x) for x in (0, 0, int(fl2) & 0xFFFFFFFF, _bswap32(fl2), int(fl4) & 0xFFFFFFFF, int(fl5) & 0xFFFFFFFF, int(fl6) & 0xFFFFFFFF, 0, 0, last & 0xFFFFFFFF))


def source336_3040_metrics_with_ladon(ladon_raw: bytes, **kwargs) -> bytes:
    return source336_3040_metrics_json(**kwargs)


def _query_value_bytes(url: str, key: str, default: str = "") -> bytes:
    for current_key, current_value in urllib.parse.parse_qsl(urllib.parse.urlsplit(str(url)).query, keep_blank_values=True):
        if current_key == key:
            return current_value.encode("utf-8")
    return default.encode("utf-8")


def source336_3040_nested_device_message(
    url: str,
    *,
    timestamp_base: int,
    field16: int = 1_111_836_827,
    field17: int = 1_111_724_915,
    field18: int = 1_085_939_368,
    field19: int = 1_083_679_440,
    field20: int = 1_117_544_273,
    field21: int = 1_116_735_557,
    field25: int = 3_565_709_330_792,
    field26: int = 0,
    field27: int = 0,
    field28: int | None = None,
    field40: int = 0,
) -> bytes:
    base_ms = int(timestamp_base) * 1000
    if not field26:
        field26 = base_ms - 40_250
    if not field27:
        field27 = base_ms - 1_482_000
    if field28 is None:
        field28 = base_ms + 3_534
    if not field40:
        field40 = base_ms - 40_796
    out = bytearray()
    out += proto_field_varint(1, 2)
    out += proto_field_varint(2, 4)
    out += proto_field_bytes(3, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(4, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(5, SOURCE336_3040_DEVICE_INSTALL_ID)
    out += proto_field_bytes(6, b"!noperm!")
    out += proto_field_varint(7, SOURCE336_3040_FIELD23_SENTINEL)
    out += proto_field_varint(8, SOURCE336_3040_FIELD23_SENTINEL)
    out += proto_field_varint(9, 4)
    out += proto_field_varint(10, SOURCE336_3040_FIELD23_SENTINEL)
    out += proto_field_bytes(11, SOURCE336_NOT_SET)
    out += proto_field_bytes(12, SOURCE336_3040_TIMEZONE)
    out += proto_field_bytes(13, SOURCE336_3040_LOCALE)
    out += proto_field_varint(14, 12)
    out += proto_field_bytes(15, SOURCE336_3040_PHYSICAL_SIZE)
    for no, val in ((16, field16), (17, field17), (18, field18), (19, field19), (20, field20), (21, field21)):
        out += proto_field_fixed32(no, int(val))
    out += proto_field_bytes(22, b"9")
    out += proto_field_varint(23, 204)
    out += proto_field_varint(24, 10)
    for no, val in ((25, field25), (26, field26), (27, field27), (28, field28), (29, 1)):
        out += proto_field_varint(no, int(val))
    out += proto_field_bytes(30, SOURCE336_3040_DEVICE_TYPE)
    out += proto_field_bytes(31, SOURCE336_3040_DEVICE_BRAND)
    out += proto_field_bytes(32, SOURCE336_3040_DEVICE_TYPE)
    out += proto_field_bytes(33, SOURCE336_3040_DEVICE_TYPE)
    out += proto_field_bytes(34, SOURCE336_3040_SOC_MODEL)
    out += proto_field_bytes(35, SOURCE336_3040_DEVICE_BRAND)
    out += proto_field_bytes(36, SOURCE336_3040_HARDWARE)
    out += proto_field_varint(38, 62)
    out += proto_field_varint(40, int(field40))
    return bytes(out)


def source336_3040_nested_report_message_active899(url: str, timestamp_base: int) -> bytes:
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = proto_field_varint(1, ts2 - 12) + proto_field_varint(2, 3) + proto_field_varint(4, 400)
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field26=base_ms - 40_250,
        field27=base_ms - 1_482_000,
        field28=base_ms + 3_534,
        field40=base_ms - 40_796,
    )
    out = bytearray()
    out += proto_field_varint(1, max(0, ts2 - 3_566_297_534))
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 10_556)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, SOURCE336_3040_VERSION_NAME)
    out += proto_field_varint(15, 3_152)
    out += proto_field_varint(17, 14_540)
    out += proto_field_varint(18, 259_962_566_918_360 + ts2)
    out += proto_field_varint(19, ts2 - 8)
    out += proto_field_varint(20, (ts2 - 16_242_712) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_observed910(url: str, timestamp_base: int) -> bytes:
    """910-byte family recovered from `out/frida_scan_plain.out` native stage33f."""
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = b""
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field17=1_111_772_342,
        field19=1_083_725_608,
        field21=1_116_828_150,
        field25=base_ms - 229_717_168,
        field26=base_ms - 14_822,
        field27=base_ms - 95_664_000,
        field28=base_ms + 41_542,
        field40=base_ms - 16_446,
    )
    # Patch device field 5 to the recovered install token for this family.
    # Rebuilding the whole message keeps field order identical.
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, 54)
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 5_944)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_142)
    out += proto_field_varint(17, 16_042)
    out += proto_field_varint(18, 259_962_415_268_584 + ts2)
    out += proto_field_varint(19, ts2 + 8)
    out += proto_field_varint(20, (ts2 - 100_046_856) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_observed870(url: str, timestamp_base: int) -> bytes:
    """870-byte family recovered from `out/frida_current_fullmget2.out`."""
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x4244C420,
        field18=0x40BA1EA8,
        field19=0x409A72C0,
        field20=0x429C5F51,
        field21=0x42901D88,
        field25=base_ms - 103_117_168,
        field26=base_ms - 372,
        field27=base_ms - 214_522_002,
        field28=base_ms + 6_476,
        field40=base_ms - 1_314,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, 8)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 23_548)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, b"")
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_152)
    out += proto_field_varint(17, 54_100)
    out += proto_field_varint(18, 259_962_312_785_688 + ts2)
    out += proto_field_varint(19, ts2 - 8)
    out += proto_field_varint(20, 3_441_183_266)
    return bytes(out)


def source336_3040_nested_report_message_aab896(url: str, timestamp_base: int) -> bytes:
    """Build the 6.5.6.32 full/mget source family observed in body-bound traces.

    This family is the plaintext `source336` dumped before the stage336
    transform in `out/frida_bodydiff_aab_fixedrand.out`.  It differs from the
    older observed910 family in field10, field15, field16, and several nested
    report timing constants.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = (
        proto_field_varint(1, ts2 - 24)
        + proto_field_varint(2, 3)
        + proto_field_varint(4, 400)
    )
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x4244C420,
        field19=0x4099CE08,
        field21=0x42901D00,
        field25=base_ms - 124_335_168,
        field26=base_ms - 70_742,
        field27=base_ms - 235_740_000,
        field28=base_ms + 16_776,
        field40=base_ms - 71_836,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, 88)
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 30_174)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_152)
    out += proto_field_varint(17, 15_018)
    out += proto_field_varint(18, 259_962_352_616_152 + ts2)
    out += proto_field_varint(19, ts2 - 8)
    out += proto_field_varint(20, (ts2 - 125_360_184) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_current899(url: str, timestamp_base: int) -> bytes:
    """Build the live 6.5.6.32 0x4081 full/mget report family.

    2026-07 focused traces show the accepted current family is 890 bytes at
    top-level source336 with this nested report at 395 bytes.
    The name is kept for compatibility with earlier experiments.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = b""
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x424371F4,
        field18=0x40BA1EA8,
        field19=0x4097C1E8,
        field20=0x429C5F51,
        field21=0x4290B6C4,
        field25=base_ms - 462_269_168,
        field26=base_ms - 536,
        field27=base_ms - 328_216_000,
        field28=base_ms + 20_186,
        field40=base_ms - 1_368,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, 22)
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 26_750)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_142)
    out += proto_field_varint(17, 20_854)
    out += proto_field_varint(18, 259_962_661_372_184 + ts2)
    out += proto_field_varint(19, ts2 - 8)
    out += proto_field_varint(20, (ts2 - 4_161_185_752) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_accepted881(url: str, timestamp_base: int) -> bytes:
    """Build the server-accepted 6.5.6.32 / full/mget 881-byte source report.

    This family is recovered by inverting an accepted App request from
    ``out/frida_main_requests.log``.  It uses the 0x4081 top field10 family,
    a 387-byte nested report, and top-level field21=624.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = (
        proto_field_varint(1, ts2 - 1_182)
        + proto_field_varint(2, 3)
        + proto_field_varint(4, 400)
    )
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x4244CDEF,
        field18=0x40BA1EA8,
        field19=0x4098AA28,
        field20=0x429C5F51,
        field21=0x4293E288,
        field25=base_ms - 1_213_168,
        field26=base_ms - 1_214_530,
        field27=base_ms - 112_618_000,
        field28=base_ms + 6_366,
        field40=base_ms - 1_340_644,
    )
    # This accepted family omits device subfield 5 (install/device token).
    token_field = proto_field_bytes(5, SOURCE336_3040_DEVICE_INSTALL_ID)
    device = device.replace(token_field, b"", 1)
    out = bytearray()
    out += proto_field_varint(1, 1_222)
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_varint(5, 14)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 11_348)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_152)
    out += proto_field_varint(17, 12_286)
    out += proto_field_varint(18, 259_962_334_872_344 + ts2)
    out += proto_field_varint(19, ts2 - 8)
    out += proto_field_varint(20, (ts2 - 125_245_400) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    out += proto_field_varint(23, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_current891(url: str, timestamp_base: int) -> bytes:
    """Build the current app-url 6.5.6.32 / full/mget 891-byte source report.

    Recovered from ``out/trace_appurl_current.txt`` where the App signed the
    api5.novelfm.com URL with ab_sdk_version/klink_egdi.  This is the dynamic
    counterpart of the older fixed accepted881 profile.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x424371F4,
        field18=0x40BA1EA8,
        field19=0x40975EC0,
        field20=0x429C5F51,
        field21=0x4290B546,
        field25=base_ms - 470_037_168,
        field26=base_ms - 31_222,
        field27=base_ms - 335_984_000,
        field28=base_ms + 19_928,
        field40=base_ms - 31_740,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, 50)
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 27_864)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, b"")
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_142)
    out += proto_field_varint(17, 13_448)
    out += proto_field_varint(18, 259_962_734_902_504 + ts2)
    out += proto_field_varint(19, ts2 + 8)
    out += proto_field_varint(20, (ts2 + 142_153_720) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_live908(url: str, timestamp_base: int) -> bytes:
    """Build the live 6.5.6.32 / full/mget 908-byte source report."""
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = (
        proto_field_varint(1, ts2 - 102)
        + proto_field_varint(2, 3)
        + proto_field_varint(4, 400)
    )
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x4243A2FB,
        field18=0x40BA1EA8,
        field19=0x40971820,
        field20=0x429C5F51,
        field21=0x42911672,
        field25=base_ms - 288_843_168,
        field26=base_ms - 132_534,
        field27=base_ms - 154_790_000,
        field28=base_ms + 13_542,
        field40=base_ms - 133_870,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, 146)
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_varint(5, 14)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 8_950)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_142)
    out += proto_field_varint(17, 16_110)
    out += proto_field_varint(18, 259_962_566_032_664 + ts2)
    out += proto_field_varint(19, ts2 - 8)
    out += proto_field_varint(20, (ts2 - 24_729_592) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    out += proto_field_varint(23, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_latest913(url: str, timestamp_base: int) -> bytes:
    """Build the latest traced 6.5.6.32 full/mget 913-byte source report.

    Recovered from ``out/trace_field13_targeted_latest.txt``.  This keeps the
    downloader-safe accepted881 path untouched, and gives the current signer
    research path an exact protobuf source layout for the bodyC trace.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = (
        proto_field_varint(1, ts2 - 502)
        + proto_field_varint(2, 3)
        + proto_field_varint(4, 400)
    )
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x424371F4,
        field18=0x40BA1EA8,
        field19=0x40977398,
        field20=0x429C5F51,
        field21=0x4290B564,
        field25=base_ms - 474_319_168,
        field26=base_ms - 845_672,
        field27=base_ms - 340_266_000,
        field28=base_ms + 15_184,
        field40=base_ms - 847_060,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, 862)
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_varint(5, 14)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 32_544)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_142)
    out += proto_field_varint(16, 2)
    out += proto_field_varint(17, 13_482)
    out += proto_field_varint(18, 259_962_682_213_144 + ts2)
    out += proto_field_varint(19, ts2 - 8)
    out += proto_field_varint(20, (ts2 - 25_634_808) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    out += proto_field_varint(23, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_current913(url: str, timestamp_base: int) -> bytes:
    """Build the current 6.5.6.32 full/mget report family.

    Recovered from `out/trace_continue_multi_heads.txt` bodyA/bodyB.  The
    top-level source336 payload is 913/916 bytes depending on varint widths;
    this nested report is 417 bytes in the current fixed-rand bodyB sample.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = (
        proto_field_varint(1, ts2 - 6_762)
        + proto_field_varint(2, 3)
        + proto_field_varint(4, 400)
        + proto_field_varint(5, 4)
    )
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field17=0x424372BD,
        field19=0x40980420,
        field21=0x4290B720,
        field25=base_ms - 452_525_168,
        field26=base_ms - 6_852_174,
        field27=base_ms - 317_972_002,
        field28=base_ms + 22_818,
        field40=base_ms - 6_853_484,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, max(0, ts2 - 3_566_852_658))
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_varint(5, 14)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 64_762)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_142)
    out += proto_field_varint(17, 5_155_190)
    out += proto_field_varint(18, 259_955_185_129_220 + 3 * ts2)
    out += proto_field_varint(19, ts2 + 8)
    out += proto_field_varint(20, (3 * ts2 - 7_260_160_492) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    out += proto_field_varint(23, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_continue913(
    url: str,
    timestamp_base: int,
    *,
    device_field25: int | None = None,
    device_field26: int | None = None,
    device_field27: int | None = None,
    device_field28: int | None = None,
    device_field40: int | None = None,
) -> bytes:
    """Build the 0x4081 / 913-byte report family from continue-multi-heads.

    This is the exact family recovered from ``out/trace_continue_multi_heads.txt``.
    The optional device timing fields are exposed because native samples keep
    some process-lifetime millisecond values stable across multiple sign calls.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = (
        proto_field_varint(1, ts2 - 2_122)
        + proto_field_varint(2, 3)
        + proto_field_varint(4, 400)
        + proto_field_varint(5, 4)
    )
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field17=0x424372BD,
        field19=0x40972F10,
        field21=0x4290F103,
        field25=device_field25 if device_field25 is not None else base_ms - 424_805_168,
        field26=device_field26 if device_field26 is not None else base_ms - 2_863_160,
        field27=device_field27 if device_field27 is not None else base_ms - 290_752_000,
        field28=device_field28 if device_field28 is not None else base_ms + 19_126,
        field40=device_field40 if device_field40 is not None else base_ms - 2_863_910,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, ts2 - 3_566_829_424)
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_varint(5, 14)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 25_584)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_142)
    out += proto_field_varint(17, 13_618)
    out += proto_field_varint(18, 259_955_441_641_860 + 3 * ts2)
    out += proto_field_varint(19, ts2 + 8)
    out += proto_field_varint(20, (3 * ts2 - 2_864_430_476) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    out += proto_field_varint(23, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_active_current911(url: str, timestamp_base: int) -> bytes:
    """Build the active 6.5.6.32 full/mget report family observed 2026-07-08.

    This is an experimental current-version profile, fitted from source336
    recovered from live native X-Medusa packets with trusted protobuf headers.
    It is not used by the downloader's stable default path.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = (
        proto_field_varint(1, ts2 - 41_826)
        + proto_field_varint(2, 3)
        + proto_field_varint(4, 400)
    )
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x42383868,
        field18=0x40BA1EA8,
        field19=0x409757E0,
        field20=0x429C5F51,
        field21=0x42901005,
        field25=3_566_407_500_832,
        field26=3_566_964_688_304,
        field27=3_566_541_566_342,
        field28=base_ms + 1_978,
        field40=3_566_964_686_700,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, max(0, ts2 - 3_566_964_686))
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_varint(5, 14)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 33_434)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_142)
    out += proto_field_varint(16, 2)
    out += proto_field_varint(17, 8_600)
    out += proto_field_varint(18, 259_962_365_197_528 + ts2)
    out += proto_field_varint(19, ts2 - 8)
    out += proto_field_varint(20, (ts2 - 126_277_656) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    out += proto_field_varint(23, 3_476_936_026)
    return bytes(out)


def source336_3040_nested_report_message_code85(url: str, timestamp_base: int) -> bytes:
    """Build the 0x4085 / 910-byte full/mget source family.

    This is the family produced by MSManagerUtils.get("3040").getFeatureHash
    for current 6.5.6.32 full/mget calls.  It matches the plaintext source336
    shape dumped in ``out/frida_bodydiff.out``: field10=4085000000000000,
    field15=08ca..., field16=Ay159..., and a 416-byte nested report.
    """
    ts2 = int(timestamp_base)
    base_ms = ts2 * 1000
    nested13 = bytes.fromhex("08bac4d2a40d1003209003")
    device = source336_3040_nested_device_message(
        url,
        timestamp_base=ts2,
        field16=0x4245489B,
        field17=0x4244C420,
        field18=0x40BA1EA8,
        field19=0x409A72C0,
        field20=0x429C5F51,
        field21=0x42901D88,
        field25=base_ms - 123_097_168,
        field26=base_ms - 19_980_372,
        field27=base_ms - 234_502_002,
        field28=base_ms + 12_398,
        field40=base_ms - 19_981_314,
    )
    device = device.replace(SOURCE336_3040_DEVICE_INSTALL_ID, b"A9wc4sIzhYaDXS9btdyBd7QR5", 1)
    out = bytearray()
    out += proto_field_varint(1, max(0, ts2 - 3_566_510_606))
    out += proto_field_varint(2, 2_943_156_234)
    out += proto_field_varint(3, 2_943_156_236)
    out += proto_field_varint(5, 14)
    out += proto_field_bytes(6, SOURCE336_REPORT_VERSION)
    out += proto_field_varint(7, 23_548)
    out += proto_field_bytes(12, device)
    out += proto_field_bytes(13, nested13)
    out += proto_field_bytes(14, b"6.5.6.32")
    out += proto_field_varint(15, 3_152)
    out += proto_field_varint(17, 16_076)
    out += proto_field_varint(18, 259_962_312_786_664 + ts2)
    out += proto_field_varint(19, ts2 + 8)
    out += proto_field_varint(20, (ts2 - 125_360_136) & 0xFFFFFFFF)
    out += proto_field_varint(22, 3_476_936_026)
    out += proto_field_varint(23, 3_476_936_026)
    return bytes(out)


def source336_container_alloc_3040_active899(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    ladon_raw: bytes,
) -> bytes:
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    metrics = source336_3040_metrics_json(
        fkd=842_674_847,
        pd=-575_868_740,
        lp="2|520830913191|522717356745|2089099191808|129984601251936",
        fl=source336_3040_metrics_fl_from_words(
            fl2=737_340_615,
            fl4=2_856_852_299,
            fl5=859_378_443,
            fl6=1_595_182_919,
            ladon_raw=ladon_raw,
        ),
        do=0,
        tk=True,
    )
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, SOURCE336_3040_VERSION_NAME)
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, SOURCE336_3040_FIELD10_CONTINUE7_NATIVE)
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, field13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message())
    out += proto_field_bytes(16, source336_3040_field16_from_raw18(bytes.fromhex("c99498cbb46f78c4ea3ce5370bf97559b578")))
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_active899(url, timestamp_base))
    out += proto_field_bytes(24, metrics)
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out)))


def source336_container_alloc_3040_code85(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    ladon_raw: bytes,
) -> bytes:
    """Build the current 0x4085 / 910-byte full/mget source336 family."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, bytes.fromhex("4085000000000000"))
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, field13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, bytes.fromhex("08ca02100a180228d2d9c88f0e"))
    out += proto_field_bytes(16, b"Ay159XD4RHS6IS2gH21TzEED_")
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_code85(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            fkd=1_253_817_882,
            pd=-1_652_880_104,
            lp="2|520830913191|520685649065|2089262355317|129984451293536",
            fl=source336_3040_metrics_fl_from_words(
                fl2=843_880_882,
                fl4=789_800_953,
                fl5=521_406_489,
                fl6=1_663_731_026,
                ladon_raw=ladon_raw,
            ),
            do=0,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out)))


def source336_container_alloc_3040_observed910(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    ladon_raw: bytes,
) -> bytes:
    """Build the current observed 910-byte source336 family, pure Python."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, bytes.fromhex("4001000000000000"))
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, field13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message(field1=98, field2=2, field3=1_388_734, field4=12, field5=3_768_761_706))
    out += proto_field_bytes(16, b"A9wc4sIzhYaDXS9btdyBd7QR5")
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_observed910(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            fkd=3_790_503_239,
            pd=-1_920_074_759,
            lp="2|520830913191|519699623049|2089331939088|129984210806816",
            fl=source336_3040_metrics_fl_from_words(
                fl2=907_173_335,
                fl4=1_155_481,
                fl5=973_736_473,
                fl6=1_708_875_805,
                ladon_raw=ladon_raw,
            ),
            do=1,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out)))


def source336_container_alloc_3040_observed870(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    ladon_raw: bytes,
) -> bytes:
    """Build the 870-byte live 6.5.6.32 full/mget source336 family."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, bytes.fromhex("4001000000000000"))
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, field13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message(field1=62, field2=2, field3=1_388_734, field4=12, field5=966_781_214))
    out += proto_field_bytes(16, b"A9wc4sIzhYaDXS9btdyBd7QR5")
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 624)
    out += proto_field_bytes(23, source336_3040_nested_report_message_observed870(url, timestamp_base))
    ladon_tail = int.from_bytes(bytes(ladon_raw)[:4].ljust(4, b"\x00"), "big") if ladon_raw else 469_742_609
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            sts=251,
            fkd=1_253_817_882,
            pd=-1_652_880_104,
            lp="2|520830913191|521168404297|2089262355317|129984152728640",
            fl=source336_3040_metrics_fl_from_words(
                fl2=843_880_882,
                fl4=2_996_390_962,
                fl5=789_800_953,
                fl6=521_406_489,
                fl9=ladon_tail,
            ),
            do=1,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out)))


def source336_container_alloc_3040_aab896(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    ladon_raw: bytes,
) -> bytes:
    """Build the body-bound 6.5.6.32 full/mget source336 family."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, bytes.fromhex("4081000000000000"))
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, field13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, bytes.fromhex("08d201100a18bee15428f0d9a2a302"))
    out += proto_field_bytes(16, b"AZwtD9K46qjIIKQtf3zPISr0v")
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_aab896(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            fkd=2_076_683_732,
            pd=1_261_943_491,
            lp="2|520830913191|520830965224|2089251824825|129984394852960",
            fl=source336_3040_metrics_fl_from_words(
                fl2=854_453_886,
                fl4=1_594_387_131,
                fl5=523_315_987,
                fl6=1_336_578_426,
                ladon_raw=ladon_raw,
            ),
            do=0,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out)))


def source336_container_alloc_3040_current899(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    ladon_raw: bytes,
) -> bytes:
    """Build the live 6.5.6.32 0x4081 / 899-byte full/mget source336 family."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, bytes.fromhex("4081000000000000"))
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, field13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message(field1=222, field2=8, field3=1_388_734, field5=3_986_190_410))
    out += proto_field_bytes(16, b"Ae6oTO23yHOi_8pAakLAq_QUV")
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_current899(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            fkd=420_755_690,
            pd=-1_588_383_049,
            lp="2|520830913191|523487495625|2089304678528|129984242542592",
            fl="0|0|934439495|1198174775|924330019|540621084|2789107889|0|0|2713210513",
            do=0,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out)))


def source336_container_alloc_3040_accepted881(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    ladon_raw: bytes,
) -> tuple[bytes, int]:
    """Build the server-accepted 881-byte 0x4081 source336 family."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    raw13 = bytes(field13)
    if len(raw13) != 20:
        raise ValueError("field13 must be exactly 20 bytes")
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, bytes.fromhex("4081000000000000"))
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, raw13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message(field1=1_004, field2=12, field3=2, field5=2_221_243_644))
    out += proto_field_bytes(16, b"A9wc4sIzhYaDXS9btdyBd7QR5")
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 624)
    out += proto_field_bytes(23, source336_3040_nested_report_message_accepted881(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            sts=32_251,
            fkd=616_091_946,
            pd=-1_691_704_523,
            lp="2|520830913191|522126584745|2089179275852|129984460294688",
            fl=source336_3040_metrics_fl_from_words(
                fl2=792_258_699,
                fl4=3_238_562_437,
                fl5=438_905_606,
                fl6=3_817_201_890,
                ladon_raw=ladon_raw,
            ),
            do=0,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out))), len(out)


def source336_container_alloc_3040_current891(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    field16_raw18: bytes,
    ladon_raw: bytes,
) -> tuple[bytes, int]:
    """Build the current app-url 891-byte source336 family."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    raw13 = bytes(field13)
    if len(raw13) != 20:
        raise ValueError("field13 must be exactly 20 bytes")
    raw16 = bytes(field16_raw18)
    if len(raw16) != 18:
        raise ValueError("field16_raw18 must be exactly 18 bytes")
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, bytes.fromhex("4001000000000000"))
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, raw13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message(field1=102, field2=2, field3=1_388_734, field4=26, field5=409_897_828))
    out += proto_field_bytes(16, source336_3040_field16_from_raw18(raw16))
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_current891(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            sts=32_251,
            fkd=2_429_229_782,
            pd=1_944_591_134,
            lp="2|520830913191|521560156809|2089481319985|129984640243104",
            fl=source336_3040_metrics_fl_from_words(
                fl2=1_027_325_174,
                fl4=2_669_840_428,
                fl5=522_991_917,
                fl6=2_794_582_025,
                ladon_raw=ladon_raw,
            ),
            do=1,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out))), len(out)


def source336_container_alloc_3040_live908(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    field16_raw18: bytes,
    ladon_raw: bytes,
) -> tuple[bytes, int]:
    """Build the current body-capable 6.5.6.32 full/mget source336 buffer.

    Returns `(allocated_1024_buffer, payload_size)`.  The payload size is the
    exact protobuf length before native padding.

    Despite the historical name, this now tracks the 890-byte current
    source family recovered from live 6.5.6.32 full/mget traces.
    """
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    raw13 = bytes(field13)
    if len(raw13) != 20:
        raise ValueError("field13 must be exactly 20 bytes")
    raw16 = bytes(field16_raw18)
    if len(raw16) != 18:
        raise ValueError("field16_raw18 must be exactly 18 bytes")
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, bytes.fromhex("4001000000000000"))
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, raw13)
    out += proto_field_bytes(14, sm3(query)[:6])
    # Live 6.5.6.32 full/mget source336 uses this compact field15 profile:
    #   field1=88, field2=2, field3=1388734, field4=26, field5=2348841358
    out += proto_field_bytes(15, source336_3040_field15_message(field1=88, field2=2, field3=1_388_734, field4=26, field5=2_348_841_358))
    out += proto_field_bytes(16, source336_3040_field16_from_raw18(raw16))
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_current899(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            sts=6_395,
            fkd=3_598_865_328,
            pd=256_645_762,
            lp="2|520830913191|523050160073|2089483878275|129985343635648",
            fl=source336_3040_metrics_fl_from_words(
                fl2=1_023_456_580,
                fl4=182_618_487,
                fl5=590_090_528,
                fl6=3_294_819_620,
                ladon_raw=ladon_raw,
            ),
            do=1,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out))), len(out)


def source336_container_alloc_3040_latest913(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    field16_raw18: bytes,
    ladon_raw: bytes,
) -> tuple[bytes, int]:
    """Build the exact 913-byte source336 family from the latest bodyC trace."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    raw13 = bytes(field13)
    if len(raw13) != 20:
        raise ValueError("field13 must be exactly 20 bytes")
    raw16 = bytes(field16_raw18)
    if len(raw16) != 18:
        raise ValueError("field16_raw18 must be exactly 18 bytes")
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, SOURCE336_3040_FIELD10_CONTINUE7_NATIVE)
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, raw13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message(field1=304, field2=10, field3=1_388_734, field5=3_032_610_234))
    out += proto_field_bytes(16, source336_3040_field16_from_raw18(raw16))
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_latest913(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            sts=65_019,
            fkd=3_105_874_898,
            pd=-69_908_728,
            lp="2|520830913191|523486729081|2089478994717|129984511062400",
            fl=source336_3040_metrics_fl_from_words(
                fl2=1_029_059_034,
                fl4=223_230_385,
                fl5=356_452_634,
                fl6=1_555_681_684,
                ladon_raw=ladon_raw,
            ),
            do=0,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out))), len(out)


def source336_container_alloc_3040_continue913(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    ladon_raw: bytes,
    device_field25: int | None = None,
    device_field26: int | None = None,
    device_field27: int | None = None,
    device_field28: int | None = None,
    device_field40: int | None = None,
) -> tuple[bytes, int]:
    """Build the 913-byte 0x4081 source336 family from continue traces."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    raw13 = bytes(field13)
    if len(raw13) != 20:
        raise ValueError("field13 must be exactly 20 bytes")
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, SOURCE336_3040_FIELD10_CONTINUE7_NATIVE)
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, raw13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message(field1=348, field2=10, field3=1_388_734, field5=2_985_686_724))
    out += proto_field_bytes(16, b"A9wc4sIzhYaDXS9btdyBd7QR5")
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(
        23,
        source336_3040_nested_report_message_continue913(
            url,
            timestamp_base,
            device_field25=device_field25,
            device_field26=device_field26,
            device_field27=device_field27,
            device_field28=device_field28,
            device_field40=device_field40,
        ),
    )
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            sts=130_555,
            fkd=237_963_102,
            pd=-2_088_393_851,
            lp="2|520830913191|520064746505|2089371304268|129984498626464",
            fl=source336_3040_metrics_fl_from_words(
                fl2=1_003_293_579,
                fl4=2_019_681_472,
                fl5=822_224_434,
                fl6=2_891_957_968,
                ladon_raw=ladon_raw,
            ),
            do=0,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out))), len(out)


def source336_container_alloc_3040_active_current911(
    url: str,
    *,
    rand2: int,
    khronos: int,
    field13: bytes,
    field16_raw18: bytes,
    ladon_raw: bytes,
) -> tuple[bytes, int]:
    """Build experimental active 6.5.6.32 source336 family from 2026-07-08."""
    query = urllib.parse.urlsplit(str(url)).query.encode("ascii")
    timestamp_base = int(khronos) * 2
    field3 = (int(rand2) << 1) & 0xFFFFFFFF
    raw13 = bytes(field13)
    if len(raw13) != 20:
        raise ValueError("field13 must be exactly 20 bytes")
    raw16 = bytes(field16_raw18)
    if len(raw16) != 18:
        raise ValueError("field16_raw18 must be exactly 18 bytes")
    out = bytearray()
    out += proto_field_bytes(1, SOURCE336_FIELD1_FIXED_SEED)
    out += proto_field_varint(2, 10)
    out += proto_field_varint(3, field3)
    out += proto_field_bytes(4, _query_value_bytes(url, "aid", "3040"))
    out += proto_field_bytes(5, _query_value_bytes(url, "device_id", ""))
    out += proto_field_bytes(6, SOURCE336_3040_MSSDK_LICENSE)
    out += proto_field_bytes(7, b"6.5.6.32")
    out += proto_field_bytes(8, SOURCE336_MSSDK_VERSION)
    out += proto_field_varint(9, 0x08121200)
    out += proto_field_bytes(10, SOURCE336_3040_FIELD10_CONTINUE7_NATIVE)
    out += proto_field_varint(12, timestamp_base)
    out += proto_field_bytes(13, raw13)
    out += proto_field_bytes(14, sm3(query)[:6])
    out += proto_field_bytes(15, source336_3040_field15_message(field1=612, field2=10, field3=6, field5=3_176_657_206))
    out += proto_field_bytes(16, source336_3040_field16_from_raw18(raw16))
    out += proto_field_varint(17, timestamp_base)
    out += proto_field_bytes(20, b"none")
    out += proto_field_varint(21, 754)
    out += proto_field_bytes(23, source336_3040_nested_report_message_active_current911(url, timestamp_base))
    out += proto_field_bytes(
        24,
        source336_3040_metrics_json(
            sts=32_251,
            fkd=3_569_276_693,
            pd=127_771_959,
            lp="2|520830913191|520931817177|2089436959982|129984939433472",
            fl=source336_3040_metrics_fl_from_words(
                fl2=1_070_915_113,
                fl4=3_099_142_160,
                fl5=689_518_890,
                fl6=2_568_108_212,
                ladon_raw=ladon_raw,
            ),
            do=0,
            tk=True,
        ),
    )
    if len(out) > SOURCE336_ALLOC_SIZE:
        raise ValueError(f"source336 payload too large: {len(out)}")
    return bytes(out) + (b"\x00" * (SOURCE336_ALLOC_SIZE - len(out))), len(out)


def x_medusa_3040_full_mget_active899(
    url: str,
    *,
    khronos: int,
    rand2: int,
    rand3: int,
    rand4: int,
    field13: bytes,
    ladon_raw: bytes,
    payload_size: int | None = None,
) -> str:
    # Live 6.5.6.32 oracle currently emits the 0x4081 family for full/mget.
    # Keep code85 available for trace comparison, but production pure3040 uses
    # the closer 0x4081/aab896 source layout so the finalizer copy31/control32
    # is generated from the same family as the app.
    source336 = source336_container_alloc_3040_current899(
        url,
        rand2=rand2,
        khronos=khronos,
        field13=field13,
        ladon_raw=ladon_raw,
    )
    stage33f = medusa_source336_to_stage33f(source336, int(rand3), payload_size=payload_size)
    raw = medusa3040_raw_from_stage33f(
        khronos=int(khronos),
        rand3=int(rand3),
        rand4=int(rand4),
        query=urllib.parse.urlsplit(str(url)).query,
        stage33f=stage33f,
        field13=field13,
    )
    return b64(raw)


def x_medusa_3040_full_mget_live908(
    url: str,
    *,
    khronos: int,
    rand2: int,
    rand3: int,
    rand4: int,
    field13: bytes,
    field16_raw18: bytes,
    ladon_raw: bytes,
    control32: bytes | None = None,
) -> str:
    source336, payload_size = source336_container_alloc_3040_live908(
        url,
        rand2=rand2,
        khronos=khronos,
        field13=field13,
        field16_raw18=field16_raw18,
        ladon_raw=ladon_raw,
    )
    stage33f = medusa_source336_to_stage33f(source336, int(rand3), payload_size=payload_size)
    query = urllib.parse.urlsplit(str(url)).query
    head9 = medusa3040_head9_from_query_rand4_field13(query, rand4, field13)
    if control32 is None:
        # Current 6.5.6.32 / 913-byte source336 family uses force_last=1.
        # Older fixed/captured families may use legacy41, but replay of the
        # current bodyA/bodyB source family matches native only with this
        # transform.
        copy31 = finalizer_copy_source31_from_head9_and_stage33f(
            head9,
            stage33f,
        )
        control32 = medusa3040_control32_3892_from_random32(copy31 + b"\x00")
    stage34a = stage33f_to_stage34a_with_control(stage33f, head9, bytes(control32)[:31], rand3=int(rand3))
    post_f2 = medusa3040_f2_scatter(stage34a, medusa3040_f2_recover_slot10(stage34a, 19), 19)
    raw = medusa3040_prefix20(int(khronos)) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + bytes(control32)[31:32] + post_f2
    return b64(raw)


def x_medusa_3040_full_mget_latest913(
    url: str,
    *,
    khronos: int,
    rand2: int,
    rand3: int,
    rand4: int,
    field13: bytes,
    field16_raw18: bytes,
    ladon_raw: bytes,
    control32: bytes | None = None,
    head_byte: int = 9,
) -> str:
    """Latest traced 6.5.6.32 full/mget X-Medusa profile, pure Python.

    This profile reproduces the native bodyC sample once the traced dynamic
    inputs are supplied.  The remaining long-term gap is still dynamic
    derivation of ``field13``/Gorgon for arbitrary POST bodies.
    """
    source336, payload_size = source336_container_alloc_3040_latest913(
        url,
        rand2=rand2,
        khronos=khronos,
        field13=field13,
        field16_raw18=field16_raw18,
        ladon_raw=ladon_raw,
    )
    stage33f = medusa_source336_to_stage33f(source336, int(rand3), payload_size=payload_size)
    query = urllib.parse.urlsplit(str(url)).query
    head9 = medusa3040_head9_from_query_rand4_byte(query, rand4, head_byte=head_byte)
    if control32 is None:
        copy31 = finalizer_copy_source31_from_head9_and_stage33f(head9, stage33f)
        control32 = medusa3040_control32_3892_from_random32(copy31 + b"\x00")
    stage34a = stage33f_to_stage34a_with_control(stage33f, head9, bytes(control32)[:31], rand3=int(rand3))
    post_f2 = medusa3040_f2_scatter(stage34a, medusa3040_f2_recover_slot10(stage34a, 19), 19)
    raw = medusa3040_prefix20(int(khronos)) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + bytes(control32)[31:32] + post_f2
    return b64(raw)


def x_medusa_3040_full_mget_continue913(
    url: str,
    *,
    khronos: int,
    rand2: int,
    rand3: int,
    rand4: int,
    field13: bytes,
    ladon_raw: bytes,
    control32: bytes | None = None,
    head_byte: int | None = None,
    device_field25: int | None = None,
    device_field26: int | None = None,
    device_field27: int | None = None,
    device_field28: int | None = None,
    device_field40: int | None = None,
) -> str:
    """Continue-trace 913-byte current X-Medusa profile, pure Python."""
    source336, payload_size = source336_container_alloc_3040_continue913(
        url,
        rand2=rand2,
        khronos=khronos,
        field13=field13,
        ladon_raw=ladon_raw,
        device_field25=device_field25,
        device_field26=device_field26,
        device_field27=device_field27,
        device_field28=device_field28,
        device_field40=device_field40,
    )
    stage33f = medusa_source336_to_stage33f(source336, int(rand3), payload_size=payload_size)
    query = urllib.parse.urlsplit(str(url)).query
    if head_byte is None:
        head_byte = bytes(field13)[0]
    head9 = medusa3040_head9_from_query_rand4_byte(query, rand4, head_byte=head_byte)
    if control32 is None:
        copy31 = finalizer_copy_source31_from_head9_and_stage33f(head9, stage33f)
        control32 = medusa3040_control32_3892_from_random32(copy31 + b"\x00")
    stage34a = stage33f_to_stage34a_with_control(stage33f, head9, bytes(control32)[:31], rand3=int(rand3))
    post_f2 = medusa3040_f2_scatter(stage34a, medusa3040_f2_recover_slot10(stage34a, 19), 19)
    raw = medusa3040_prefix20(int(khronos)) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + bytes(control32)[31:32] + post_f2
    return b64(raw)


def x_medusa_3040_full_mget_accepted881(
    url: str,
    *,
    khronos: int,
    rand2: int = 0x79813FCA,
    rand3: int = 0x1AB094D1,
    rand4: int = 0x3460F886,
    field13: bytes = bytes.fromhex("65a328e4bb1ed9fbcde952c463f3a70e2e73fab2"),
    ladon_raw: bytes = bytes.fromhex("d2da60f1"),
) -> str:
    """Server-accepted 6.5.6.32 full/mget X-Medusa profile.

    This is pure Python and uses the recovered 881-byte source336 family.  The
    head9 suffix for this family is built by
    ``medusa3040_head9_from_query_rand4_byte(..., field13[0])``.
    """
    source336, payload_size = source336_container_alloc_3040_accepted881(
        url,
        rand2=rand2,
        khronos=khronos,
        field13=field13,
        ladon_raw=ladon_raw,
    )
    stage33f = medusa_source336_to_stage33f(source336, int(rand3), payload_size=payload_size)
    query = urllib.parse.urlsplit(str(url)).query
    head9 = medusa3040_head9_from_query_rand4_byte(query, rand4, bytes(field13)[0])
    copy31 = finalizer_copy_source31_from_head9_and_stage33f(head9, stage33f)
    control32 = medusa3040_control32_3892_from_random32(copy31 + b"\x00")
    stage34a = stage33f_to_stage34a_with_control(stage33f, head9, control32[:31], rand3=int(rand3))
    post_f2 = medusa3040_f2_scatter(stage34a, medusa3040_f2_recover_slot10(stage34a, 19), 19)
    raw = medusa3040_prefix20(int(khronos)) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + control32[31:32] + post_f2
    return b64(raw)


def x_medusa_3040_full_mget_current891(
    url: str,
    *,
    khronos: int,
    rand2: int = 0x2B09A987,
    rand3: int = 0x6815078C,
    rand4: int = 0x75E80779,
    field13: bytes = bytes.fromhex("9f25e05334ed7d4a3e24ff58bb8a1b71bdbe4650"),
    field16_raw18: bytes = bytes.fromhex("f70738b08ce161a0d74bd6ed77205ded0479"),
    ladon_raw: bytes = bytes.fromhex("2c9961d1"),
) -> str:
    """Current 6.5.6.32 app-url full/mget X-Medusa profile, pure Python."""
    source336, payload_size = source336_container_alloc_3040_current891(
        url,
        rand2=rand2,
        khronos=khronos,
        field13=field13,
        field16_raw18=field16_raw18,
        ladon_raw=ladon_raw,
    )
    stage33f = medusa_source336_to_stage33f(source336, int(rand3), payload_size=payload_size)
    query = urllib.parse.urlsplit(str(url)).query
    head9 = medusa3040_head9_from_query_rand4_byte(query, rand4, bytes(field13)[0])
    copy31 = finalizer_copy_source31_from_head9_and_stage33f(head9, stage33f)
    control32 = medusa3040_control32_3892_from_random32(copy31 + b"\x00")
    stage34a = stage33f_to_stage34a_with_control(stage33f, head9, control32[:31], rand3=int(rand3))
    post_f2 = medusa3040_f2_scatter(stage34a, medusa3040_f2_recover_slot10(stage34a, 19), 19)
    raw = medusa3040_prefix20(int(khronos)) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + control32[31:32] + post_f2
    return b64(raw)


def x_medusa_3040_full_mget_active_current911(
    url: str,
    *,
    khronos: int,
    rand2: int,
    rand3: int,
    rand4: int,
    field13: bytes,
    field16_raw18: bytes,
    ladon_raw: bytes,
    control_profile: str = "3892",
    head_byte: int | None = None,
) -> str:
    """Experimental current 6.5.6.32 active full/mget X-Medusa profile."""
    source336, payload_size = source336_container_alloc_3040_active_current911(
        url,
        rand2=rand2,
        khronos=khronos,
        field13=field13,
        field16_raw18=field16_raw18,
        ladon_raw=ladon_raw,
    )
    stage33f = medusa_source336_to_stage33f(source336, int(rand3), payload_size=payload_size)
    query = urllib.parse.urlsplit(str(url)).query
    if head_byte is None:
        head_byte = bytes(field13)[0]
    head9 = medusa3040_head9_from_query_rand4_byte(query, rand4, head_byte=head_byte)
    copy31 = finalizer_copy_source31_from_head9_and_stage33f(head9, stage33f)
    control32 = medusa3040_control32_profile_from_random32(copy31 + b"\x00", profile=control_profile)
    stage34a = stage33f_to_stage34a_with_control(stage33f, head9, control32[:31], rand3=int(rand3))
    post_f2 = medusa3040_f2_scatter(stage34a, medusa3040_f2_recover_slot10(stage34a, 19), 19)
    raw = medusa3040_prefix20(int(khronos)) + ((int(rand3) & 0xFFFF).to_bytes(2, "little")) + b"\x00\x01" + control32[31:32] + post_f2
    return b64(raw)


LEGACY_CODE0_3040_KHRONOS = 1_778_923_030
LEGACY_CODE0_3040_RAND3 = 0x1F5011F6
LEGACY_CODE0_3040_HEAD9 = bytes.fromhex("8a3776177f01b90658")
LEGACY_CODE0_3040_STAGE33F = base64.b64decode(
    "ynaGszWCFmPQ2T13uwlc6aaIInv5/FX3M1f+KYQqtEUzZjEa0nWnPNw941Pfbe55uUDvE1Sb4y7Vn6iWBmiRRN4HWXkCBxe4BEYPeVmS1NmnZ5fW17vMC/e68XqJEFSaAGdrWjNhC8/803AlFcTd7KLjeM6oF4CrQLjXV0qRGRFqhdIpRSi1ZBTDv1jIhsN5FJOMBbllP7vLJ4KiJ3634DICr30/o+MCggZ7KvZHtrhErA8TteCmA+rfLOjXLsJu9FcqyPQ6JLid10BcHwXFnXcqD0r45XTPX772Z6KM1I6mBi6zkL+B/dmnjOUGcHOmH6kJ7zb+ecMOHfVshXxJeWod8IHjTfQmclFlM2fxvxj+MNFvtAR3bqPxLGJ4fCn4o43C4mhU0QWje8y7SAfaqkJoA2DniCbGOtBf23tb5Qqg3wR1YSuPQ6ZQADkERNsBt5sAkuY116W7v5IJMSG4uQV/f8CUzYix/riCrqC+ElWu6pkixhUVDRvuomtHcCEDE9bsqF35y4uCyJvuRi81yAlwS7TkVQcpoK20IlhMtQWY5x3C0Y+F4eBIi/I+uWeYtY+IcTs5tq6SDacK4RSCtU542pNmmRn+5g65Xg1P8P3DS0ZUHPEVm9OaIA6II0PSlE6ekBQc6gTQgvi13EWcI30drt3rVOq81J/4zpWQlu8wSCoXtLyvbqhPA8UwOzuZRJgrVpJAjVq8nQdpKd6PCg44ZY3L9brUVUUi4c2jc3d6YopS0C0BqeJkO04yyiQWGz/8HE+62W5jtWM4KYwKohgGN7Xwcs66ymkgDqyE91VCsQspmqD1ROWPjBhPv4ggNQ6b2H5PCiNZhym7j95jGX3W4NEN3+8M+zfixVd6fqVHFtzRolL+Z6CUhLlC38baf+8dh/P0gPNKlTun6rat7JSPcdTlTGec/DwIJc811CCCY3CCxvahJUFwWrpnjo9QugJD0gFr6v64bymzci+p3MJlYPAkDmjmCs44Znuc6g0fcmZP79kxiXt1BlBomYAvCX3HQLmMzCVX/AS5UixZYeOV18gH+7VqhlnwI+iqwD/jlCJ54hNjdDNJZToMEtdWeH5/UiGQdVKht7Dt9ZV5xgGSciTzQjyzSjy1tcYnKbyxevgZaHs4Qk0qa2mYuJiKxzsRgdM+H4f3PvMikbVIGhAxqPpJiEQ/JeViyU8gvyJcCvUO//1/sv/9/vIA"
)


def medusa3040_raw_legacy_code0() -> bytes:
    """Pure-Python assembly of the server-accepted 3040/full-mget profile."""
    copy31 = finalizer_copy_source31_from_head9_and_stage33f(
        LEGACY_CODE0_3040_HEAD9,
        LEGACY_CODE0_3040_STAGE33F,
    )
    control32 = medusa3040_control32_3892_legacy41_from_random32(copy31 + b"\x00")
    stage34a = stage33f_to_stage34a_with_control(
        LEGACY_CODE0_3040_STAGE33F,
        LEGACY_CODE0_3040_HEAD9,
        control32[:31],
        rand3=LEGACY_CODE0_3040_RAND3,
    )
    post_f2 = medusa3040_f2_scatter(stage34a, medusa3040_f2_recover_slot10(stage34a, 19), 19)
    return (
        medusa3040_prefix20(LEGACY_CODE0_3040_KHRONOS)
        + (LEGACY_CODE0_3040_RAND3 & 0xFFFF).to_bytes(2, "little")
        + b"\x00\x01"
        + control32[31:32]
        + post_f2
    )


def x_medusa_3040_full_mget_legacy_code0() -> str:
    return b64(medusa3040_raw_legacy_code0())


def medusa_src_a_f3_from_rand(rand_value: int) -> int:
    """Derive top-level `src_a` field 3 from rand at VM `0x18c788`."""
    return zigzag32(rand_value)


def medusa_f23_f7_from_pid(pid: int) -> int:
    """Derive top-level `src_a.23.7` from `getpid()`."""
    return zigzag32(pid)


def medusa_f23_f28_from_khronos(khronos_sec: int) -> int:
    """Derive `src_a.23.12.28` from epoch milliseconds of X-Khronos."""
    return zigzag64(khronos_sec * 1000)


def medusa_f23_f40_from_epoch_ms(epoch_ms: int) -> int:
    """Derive `src_a.23.12.40` from current epoch milliseconds."""
    return zigzag64(epoch_ms)


def medusa_url_sm3_prefix6(url_or_query: bytes | str) -> bytes:
    """Return top-level `src_a` field 14: first six SM3 bytes of URL query."""
    data = url_or_query.encode("utf-8") if isinstance(url_or_query, str) else bytes(url_or_query)
    if b"?" in data:
        data = data.split(b"?", 1)[1]
    return sm3_digest(data)[:6]

def proto_field_fixed64(field: int, value: int | bytes) -> bytes:
    if isinstance(value, bytes):
        if len(value) != 8:
            raise ValueError("fixed64 bytes must be exactly 8 bytes")
        data = value
    else:
        data = (value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")
    return proto_key(field, 1) + data


def proto_field_bytes(field: int, value: bytes | str) -> bytes:
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return proto_key(field, 2) + proto_varint(len(data)) + data


def proto_field_fixed32(field: int, value: int | bytes) -> bytes:
    if isinstance(value, bytes):
        if len(value) != 4:
            raise ValueError("fixed32 bytes must be exactly 4 bytes")
        data = value
    else:
        data = (value & 0xFFFFFFFF).to_bytes(4, "little")
    return proto_key(field, 5) + data


def medusa_src_a_prefix_rebuild(
    *,
    f1: bytes,
    f3: int,
    f6: str,
    f12: int,
    f13: bytes,
    url_sm3_prefix6: bytes,
    f15: bytes,
) -> bytes:
    """Rebuild the verified stable prefix of the plaintext `src_a` protobuf.

    This covers top-level fields 1..15 and 20..21 from the VM serializer at
    `0x18e1e0`.  Later fields 23 and 24 are nested environment blocks supplied
    by their own rebuild helpers.
    """
    return b"".join(
        (
            proto_field_bytes(1, f1),
            proto_field_varint(2, 6),
            proto_field_varint(3, f3),
            proto_field_bytes(4, "3019"),
            proto_field_bytes(6, f6),
            proto_field_bytes(7, "29.3.0"),
            proto_field_bytes(8, "v04.05.05-ml-android"),
            proto_field_varint(9, 0x80A0A00),
            proto_field_bytes(10, b"\x00" * 8),
            proto_field_varint(12, f12),
            proto_field_bytes(13, f13),
            proto_field_bytes(14, url_sm3_prefix6),
            proto_field_bytes(15, f15),
            proto_field_bytes(20, "none"),
            proto_field_varint(21, 738),
        )
    )


def medusa_src_a_rebuild(
    *,
    f1: bytes,
    f3: int,
    f6: str,
    f12: int,
    f13: bytes,
    url_sm3_prefix6: bytes,
    f15: bytes,
    f23: bytes,
    f24_json: bytes | str,
) -> bytes:
    """Rebuild the verified top-level `src_a` protobuf message.

    Fields 23 and 24 are supplied as already-built nested environment payloads.
    This matches the VM's final top-level serialization order.
    """
    return (
        medusa_src_a_prefix_rebuild(
            f1=f1,
            f3=f3,
            f6=f6,
            f12=f12,
            f13=f13,
            url_sm3_prefix6=url_sm3_prefix6,
            f15=f15,
        )
        + proto_field_bytes(23, f23)
        + proto_field_bytes(24, f24_json)
    )


def medusa_src_a_from_runtime_values(
    *,
    url_or_query: bytes | str,
    top_rand: int,
    pid: int,
    khronos_sec: int,
    current_epoch_ms: int,
    f24_fkd: int | None = None,
    f24_pd: int | None = None,
    device_hash_hex: bytes | str | None = None,
    device_uuid: bytes | str | None = None,
    f24_uuid_seed16: bytes | None = None,
    f1: bytes = bytes.fromhex("f7e85ffad7d7dc3bd62ac87057cf6118"),
    f6: str = "1611921764",
    f12: int = 0xD3E825E8,
    f13: bytes = bytes.fromhex("ea24463898fd615efc1982685c362167a1a349ba"),
    f15_value: int = 0x1530BE,
    f23_f1: int = 0xD3D2F4C2,
    f23_f18: int = 0x1406AAA68,
    f23_f19: int = 0xD3E825F8,
    f23_f20: int = 0x1446AAA68,
) -> bytes:
    """Build plaintext Medusa `src_a` from compact runtime inputs.

    `top_rand` is the first Medusa rand() result at VM `0x18c788`.  `f24_fkd`
    and `f24_pd` may be passed directly, or derived from `device_hash_hex` and
    `device_uuid` with the CRC32 helpers.
    """
    if device_uuid is None and f24_uuid_seed16 is not None:
        device_uuid = medusa_uuid4_from_xorshift_seed(f24_uuid_seed16)
    if f24_fkd is None:
        if device_hash_hex is None:
            if device_uuid is None:
                raise ValueError("f24_fkd, device_hash_hex, device_uuid, or f24_uuid_seed16 is required")
            device_hash_hex = medusa_device_hash_hex_from_uuid(device_uuid)
        f24_fkd = medusa_f24_fkd_from_id(device_hash_hex)
    if f24_pd is None:
        if device_uuid is None:
            raise ValueError("f24_pd or device_uuid is required")
        f24_pd = medusa_f24_pd_from_uuid(device_uuid)
    f23 = medusa_f23_from_runtime(
        pid=pid,
        khronos_sec=khronos_sec,
        current_epoch_ms=current_epoch_ms,
        f1=f23_f1,
        f18=f23_f18,
        f19=f23_f19,
        f20=f23_f20,
    )
    return medusa_src_a_rebuild(
        f1=f1,
        f3=medusa_src_a_f3_from_rand(top_rand),
        f6=f6,
        f12=f12,
        f13=f13,
        url_sm3_prefix6=medusa_url_sm3_prefix6(url_or_query),
        f15=medusa_f15_rebuild(f15_value),
        f23=f23,
        f24_json=medusa_f24_json_rebuild(fkd=f24_fkd, pd=f24_pd),
    )

MEDUSA_NOTSET = b"!notset!"


def medusa_f15_rebuild(value: int) -> bytes:
    """Rebuild top-level `src_a` field 15.

    The VM emits a compact nested message containing marker `2`, followed by
    the same varint in fields 2..5.
    """
    return b"".join(
        (
            proto_field_varint(1, 2),
            proto_field_varint(2, value),
            proto_field_varint(3, value),
            proto_field_varint(4, value),
            proto_field_varint(5, value),
        )
    )


def medusa_f23_inner_rebuild(
    *,
    version_tag: str = "3019",
    notset: bytes = MEDUSA_NOTSET,
    repeated_time: int = 0x1E847D,
    fixed32_value: int | bytes = bytes.fromhex("f02374c9"),
    f28: int,
    f40: int,
) -> bytes:
    """Rebuild the nested environment block stored in top-level field 23.12."""
    parts = [
        proto_field_varint(1, 2),
        proto_field_bytes(3, version_tag),
    ]
    for field in (5, 6):
        parts.append(proto_field_bytes(field, notset))
    for field in (7, 8, 9, 10):
        parts.append(proto_field_varint(field, repeated_time))
    for field in (11, 12, 13):
        parts.append(proto_field_bytes(field, notset))
    parts.append(proto_field_varint(14, repeated_time))
    parts.append(proto_field_bytes(15, notset))
    for field in (16, 17, 18, 19, 20, 21):
        parts.append(proto_field_fixed32(field, fixed32_value))
    parts.append(proto_field_bytes(22, notset))
    for field in (23, 24, 25, 27):
        parts.append(proto_field_varint(field, repeated_time))
    parts.append(proto_field_varint(28, f28))
    parts.append(proto_field_varint(29, repeated_time))
    for field in (30, 31, 32, 33, 34, 35, 36, 37):
        parts.append(proto_field_bytes(field, notset))
    parts.append(proto_field_varint(38, repeated_time))
    parts.append(proto_field_varint(40, f40))
    return b"".join(parts)


def medusa_f23_rebuild(
    *,
    f1: int,
    f7: int,
    f12: bytes,
    f18: int,
    f19: int,
    f20: int,
    f13: bytes = b"",
    f14: bytes = MEDUSA_NOTSET,
    f15: int = 2,
) -> bytes:
    """Rebuild top-level `src_a` field 23 payload."""
    return b"".join(
        (
            proto_field_varint(1, f1),
            proto_field_varint(7, f7),
            proto_field_bytes(12, f12),
            proto_field_bytes(13, f13),
            proto_field_bytes(14, f14),
            proto_field_varint(15, f15),
            proto_field_varint(18, f18),
            proto_field_varint(19, f19),
            proto_field_varint(20, f20),
        )
    )


def medusa_f23_inner_from_runtime(
    *,
    khronos_sec: int,
    current_epoch_ms: int,
    **kwargs,
) -> bytes:
    """Build `src_a.23.12` from runtime clock values."""
    return medusa_f23_inner_rebuild(
        f28=medusa_f23_f28_from_khronos(khronos_sec),
        f40=medusa_f23_f40_from_epoch_ms(current_epoch_ms),
        **kwargs,
    )


def medusa_f23_from_runtime(
    *,
    pid: int,
    khronos_sec: int,
    current_epoch_ms: int,
    f1: int = 0xD3D2F4C2,
    f18: int = 0x1406AAA68,
    f19: int = 0xD3E825F8,
    f20: int = 0x1446AAA68,
    **inner_kwargs,
) -> bytes:
    """Build top-level `src_a.23` from runtime process/time values."""
    inner = medusa_f23_inner_from_runtime(
        khronos_sec=khronos_sec,
        current_epoch_ms=current_epoch_ms,
        **inner_kwargs,
    )
    return medusa_f23_rebuild(
        f1=f1,
        f7=medusa_f23_f7_from_pid(pid),
        f12=inner,
        f18=f18,
        f19=f19,
        f20=f20,
    )


def medusa_f24_json_rebuild(
    *,
    cmr: int = 16777216,
    cmr2: int = 16777216,
    un_h: int = 0,
    vpn: int = 0,
    kd: int = 694367,
    fkd: int,
    pd: int,
    dyn: str = "",
    do: int = 0,
    tk: bool = True,
) -> bytes:
    """Rebuild the compact JSON string used as top-level `src_a` field 24."""
    tk_s = "true" if tk else "false"
    return (
        f'{{"cmr":{cmr},"cmr2":{cmr2},"un_h":{un_h},"vpn":{vpn},'
        f'"kd":{kd},"fkd":{fkd},"pd":{pd},"dyn":"{dyn}","do":{do},"tk":{tk_s}}}'
    ).encode("utf-8")


def medusa_crc32_u32(data: bytes | str) -> int:
    """Return the unsigned CRC32 value used by the f24 environment fields."""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    return zlib.crc32(raw) & 0xFFFFFFFF


def medusa_crc32_s32(data: bytes | str) -> int:
    """Return CRC32 interpreted as a signed int32."""
    value = medusa_crc32_u32(data)
    return value - 0x100000000 if value & 0x80000000 else value


def medusa_f24_fkd_from_id(device_hash_hex: bytes | str) -> int:
    """Derive f24 `fkd` from the 32-byte hex-like device hash string.

    VM `0x18db5c` calls the native CRC32 helper and `0x18dd14` stores the
    resulting value into the JSON builder.  The JSON prints this one unsigned.
    """
    return medusa_crc32_u32(device_hash_hex)


def medusa_f24_pd_from_uuid(device_uuid: bytes | str) -> int:
    """Derive f24 `pd` from the UUID-like device string.

    VM `0x18dd2c` computes the same CRC32 shape; the JSON formatter prints this
    one as signed int32, so values with the high bit set become negative.
    """
    return medusa_crc32_s32(device_uuid)


def medusa_f24_sources_from_uuid(device_uuid: bytes | str, kd: int | str = 694367) -> tuple[str, str]:
    """Return native f24 source strings `(device_hash_hex, device_uuid)`."""
    uuid_s = device_uuid.decode("utf-8") if isinstance(device_uuid, bytes) else str(device_uuid)
    return medusa_device_hash_hex_from_uuid(uuid_s, kd), uuid_s


def medusa_f24_values_from_uuid(device_uuid: bytes | str, kd: int | str = 694367) -> tuple[int, int]:
    """Return `(fkd, pd)` from the UUID source exactly as native f24 does."""
    device_hash_hex, uuid_s = medusa_f24_sources_from_uuid(device_uuid, kd)
    return medusa_f24_fkd_from_id(device_hash_hex), medusa_f24_pd_from_uuid(uuid_s)


def medusa_device_hash_hex_from_random16(raw16: bytes) -> str:
    """Return the 32-byte lower-hex source string consumed by f24 `fkd`.

    Native builds the `fkd` source as a lower-hex string and then stores
    `crc32(source)` in JSON.  Pass captured native bytes/strings for exact
    same-run reproduction; this helper is for pure-Python generation.
    """
    raw = bytes(raw16)
    if len(raw) != 16:
        raise ValueError("raw16 must be exactly 16 bytes")
    return raw.hex()


def medusa_device_hash_hex_from_uuid(device_uuid: bytes | str, kd: int | str = 694367) -> str:
    """Derive native f24 `fkd` source from the UUID source and `kd`.

    The path at `vm+0x18db40` hashes the ASCII bytes of
    `uuid_source || "694367"` with standard MD5, lower-hex encodes the digest,
    then `vm+0x18db5c` takes CRC32 of that 32-byte hex string.
    """
    uuid_s = device_uuid.decode("utf-8") if isinstance(device_uuid, bytes) else str(device_uuid)
    data = (uuid_s + str(kd)).encode("utf-8")
    return hashlib.md5(data).hexdigest()


def medusa_uuid4_from_random16(raw16: bytes) -> str:
    """Format 16 bytes as the UUID-v4-shaped source consumed by f24 `pd`."""
    raw = bytearray(raw16)
    if len(raw) != 16:
        raise ValueError("raw16 must be exactly 16 bytes")
    raw[6] = (raw[6] & 0x0F) | 0x40
    raw[8] = (raw[8] & 0x3F) | 0x80
    h = bytes(raw).hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def medusa_uuid4_from_native_nibbles(raw16: bytes) -> str:
    """Format bytes with the native UUID template filler at `vm+0x18da88`.

    The helper at `lib+0x11aa48` calls the xorshift128+ generator twice and
    consumes the resulting bytes one nibble at a time, low nibble first.  It
    fills the template `xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`; `x` copies the
    nibble through `0123456789abcdef`, while `y` applies UUID variant masking
    as `(nibble & 3) | 8`.  The fixed version nibble `4` does not consume a
    random nibble.
    """
    raw = bytes(raw16)
    if len(raw) != 16:
        raise ValueError("raw16 must be exactly 16 bytes")
    alphabet = "0123456789abcdef"
    out: list[str] = []
    nib_i = 0
    for ch in "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx":
        if ch not in "xy":
            out.append(ch)
            continue
        b = raw[nib_i >> 1]
        nib = (b & 0x0F) if (nib_i & 1) == 0 else (b >> 4)
        if ch == "y":
            nib = (nib & 3) | 8
        out.append(alphabet[nib])
        nib_i += 1
    return "".join(out)


def medusa_xorshift128plus_step(s0: int, s1: int) -> tuple[int, int, int]:
    """One native `lib+0x11ac1c` PRNG step.

    Return `(new_s0, new_s1, output)`, where `output = new_s1 + old_s1`.
    """
    x = s0 & 0xFFFFFFFFFFFFFFFF
    y = s1 & 0xFFFFFFFFFFFFFFFF
    x ^= (x << 23) & 0xFFFFFFFFFFFFFFFF
    new_s1 = (x ^ y ^ (y >> 5) ^ (x >> 18)) & 0xFFFFFFFFFFFFFFFF
    output = (new_s1 + y) & 0xFFFFFFFFFFFFFFFF
    return y, new_s1, output


def medusa_uuid4_from_xorshift_outputs(out0: int, out1: int) -> str:
    """Build the native UUID source from two `lib+0x11ac1c` outputs."""
    raw16 = (out0 & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little") + (
        out1 & 0xFFFFFFFFFFFFFFFF
    ).to_bytes(8, "little")
    return medusa_uuid4_from_native_nibbles(raw16)


def medusa_uuid4_from_xorshift_seed(seed16: bytes) -> str:
    """Build the native UUID source from a 16-byte xorshift128+ seed."""
    seed = bytes(seed16)
    if len(seed) != 16:
        raise ValueError("seed16 must be exactly 16 bytes")
    state = (int.from_bytes(seed[:8], "little"), int.from_bytes(seed[8:], "little"))
    state0, state1, out0 = medusa_xorshift128plus_step(*state)
    _state0, _state1, out1 = medusa_xorshift128plus_step(state0, state1)
    return medusa_uuid4_from_xorshift_outputs(out0, out1)


def medusa_f24_sources_from_xorshift_seed(seed16: bytes, kd: int | str = 694367) -> tuple[str, str]:
    """Return native f24 source strings from the 16-byte UUID PRNG seed."""
    return medusa_f24_sources_from_uuid(medusa_uuid4_from_xorshift_seed(seed16), kd)


def medusa_f24_values_from_xorshift_seed(seed16: bytes, kd: int | str = 694367) -> tuple[int, int]:
    """Return `(fkd, pd)` from the 16-byte UUID PRNG seed."""
    device_hash_hex, uuid_s = medusa_f24_sources_from_xorshift_seed(seed16, kd)
    return medusa_f24_fkd_from_id(device_hash_hex), medusa_f24_pd_from_uuid(uuid_s)


def medusa_random_f24_sources() -> tuple[str, str]:
    """Generate fresh pure-Python f24 source strings `(device_hash_hex, uuid)`."""
    return medusa_f24_sources_from_xorshift_seed(secrets.token_bytes(16))

def _gf256_mul_aes(x: int, k: int) -> int:
    x &= 0xFF
    k &= 0xFF
    out = 0
    while k:
        if k & 1:
            out ^= x
        x = (((x << 1) & 0xFF) ^ (0x1B if x & 0x80 else 0)) & 0xFF
        k >>= 1
    return out & 0xFF


def _medusa_table_sub_bytes_16(state: bytes) -> bytes:
    if len(state) != 16:
        raise ValueError("state must be 16 bytes")
    return bytes(MEDUSA_AES_LIKE_TABLE[b] for b in state)


def _medusa_sub_bytes_permute_16(state: bytes) -> bytes:
    sub = _medusa_table_sub_bytes_16(state)
    p = [4, 5, 6, 7, 0, 1, 2, 3, 8, 9, 10, 11, 12, 13, 14, 15]
    return bytes(sub[i] for i in p)


def _medusa_shift_rows_permute_16(state: bytes) -> bytes:
    p = [0, 9, 14, 11, 4, 13, 2, 7, 8, 1, 6, 15, 12, 5, 10, 3]
    return bytes(state[i] for i in p)


def _medusa_vm_mix_input_permute_16(state: bytes) -> bytes:
    out = bytearray(16)
    for group in range(4):
        base = group * 4
        out[base:base + 4] = bytes((state[base + 1], state[base], state[base + 2], state[base + 3]))
    return bytes(out)


def _medusa_aes_mix_single_column(col: bytes) -> bytes:
    return bytes((
        _gf256_mul_aes(col[0], 2) ^ _gf256_mul_aes(col[1], 3) ^ col[2] ^ col[3],
        col[0] ^ _gf256_mul_aes(col[1], 2) ^ _gf256_mul_aes(col[2], 3) ^ col[3],
        col[0] ^ col[1] ^ _gf256_mul_aes(col[2], 2) ^ _gf256_mul_aes(col[3], 3),
        _gf256_mul_aes(col[0], 3) ^ col[1] ^ col[2] ^ _gf256_mul_aes(col[3], 2),
    ))


def _medusa_aes_inv_mix_single_column(col: bytes) -> bytes:
    return bytes((
        _gf256_mul_aes(col[0], 14) ^ _gf256_mul_aes(col[1], 11) ^ _gf256_mul_aes(col[2], 13) ^ _gf256_mul_aes(col[3], 9),
        _gf256_mul_aes(col[0], 9) ^ _gf256_mul_aes(col[1], 14) ^ _gf256_mul_aes(col[2], 11) ^ _gf256_mul_aes(col[3], 13),
        _gf256_mul_aes(col[0], 13) ^ _gf256_mul_aes(col[1], 9) ^ _gf256_mul_aes(col[2], 14) ^ _gf256_mul_aes(col[3], 11),
        _gf256_mul_aes(col[0], 11) ^ _gf256_mul_aes(col[1], 13) ^ _gf256_mul_aes(col[2], 9) ^ _gf256_mul_aes(col[3], 14),
    ))


def _medusa_vm_mix_columns_16(state: bytes) -> bytes:
    out = bytearray(16)
    for c in range(4):
        col = bytes(state[c + row * 4] for row in range(4))
        mixed = _medusa_aes_mix_single_column(col)
        for row, value in enumerate(mixed):
            out[row * 4 + c] = value
    return bytes(out)


def _medusa_vm_inv_mix_columns_16(state: bytes) -> bytes:
    out = bytearray(16)
    for c in range(4):
        col = bytes(state[c + row * 4] for row in range(4))
        mixed = _medusa_aes_inv_mix_single_column(col)
        for row, value in enumerate(mixed):
            out[row * 4 + c] = value
    return bytes(out)


def _medusa_vm_add_round_key_16(state: bytes, key_schedule: bytes, round_index: int) -> bytes:
    base = round_index * 16
    out = bytearray(state)
    order = (1, 0, 2, 3)
    for group in range(4):
        key_base = base + group * 4
        state_base = group * 4
        for i, key_i in enumerate(order):
            out[state_base + i] ^= key_schedule[key_base + key_i]
    return bytes(out)


def _inv_permute(data: bytes, p: list[int]) -> bytes:
    out = bytearray(len(data))
    for j, src_i in enumerate(p):
        out[src_i] = data[j]
    return bytes(out)


def _medusa_inv_shift_rows_permute_16(state: bytes) -> bytes:
    return _inv_permute(state, [0, 9, 14, 11, 4, 13, 2, 7, 8, 1, 6, 15, 12, 5, 10, 3])


def _medusa_inv_sub_bytes_permute_16(state: bytes) -> bytes:
    p = [4, 5, 6, 7, 0, 1, 2, 3, 8, 9, 10, 11, 12, 13, 14, 15]
    sub = _inv_permute(state, p)
    inv = [0] * 256
    for i, v in enumerate(MEDUSA_AES_LIKE_TABLE):
        inv[v] = i
    return bytes(inv[b] for b in sub)


def medusa_aes_like_key_schedule48(material16: bytes = MEDUSA_MATERIAL16) -> bytes:
    """0x193210..0x193344：X-Medusa final 32-byte transform 的 key schedule。"""
    if len(material16) != 16:
        raise ValueError("material16 must be 16 bytes")
    seed_xor = bytes((0xDC, 0x5D, 0x02, 0xCA))
    words = [
        bytes(material16[i * 4 + j] ^ seed_xor[j] for j in range(4))
        for i in range(4)
    ]
    rcons = (0x5D, 0x02)
    for i in range(4, 12):
        temp = words[i - 1]
        if i % 4 == 0:
            temp = bytes(MEDUSA_AES_LIKE_TABLE[b] for b in (temp[1], temp[2], temp[3], temp[0]))
            temp = bytes((temp[0] ^ rcons[(i // 4) - 1], temp[1], temp[2], temp[3]))
        words.append(bytes(a ^ b for a, b in zip(words[i - 4], temp)))
    return b"".join(words)


def medusa_aes_like_encrypt_block16(block16: bytes, xor_key16: bytes, key_schedule: bytes) -> bytes:
    """0x193450..0x193e74：一块 16 字节 AES-like transform。"""
    state = bytes(a ^ b for a, b in zip(block16, xor_key16))
    state = _medusa_vm_add_round_key_16(state, key_schedule, 0)
    state = _medusa_sub_bytes_permute_16(state)
    state = _medusa_shift_rows_permute_16(state)
    state = _medusa_vm_mix_input_permute_16(state)
    state = _medusa_vm_mix_columns_16(state)
    state = _medusa_vm_add_round_key_16(state, key_schedule, 1)
    state = _medusa_sub_bytes_permute_16(state)
    state = _medusa_shift_rows_permute_16(state)
    state = _medusa_vm_add_round_key_16(state, key_schedule, 2)
    state = bytes(a ^ b for a, b in zip(state, key_schedule[16:32]))
    return state


def medusa_aes_like_decrypt_block16(block16: bytes, xor_key16: bytes, key_schedule: bytes) -> bytes:
    """`medusa_aes_like_encrypt_block16` 的逆变换。"""
    if len(block16) != 16 or len(xor_key16) != 16:
        raise ValueError("block16/xor_key16 must be 16 bytes")
    state = bytes(a ^ b for a, b in zip(block16, key_schedule[16:32]))
    state = _medusa_vm_add_round_key_16(state, key_schedule, 2)
    state = _medusa_inv_shift_rows_permute_16(state)
    state = _medusa_inv_sub_bytes_permute_16(state)
    state = _medusa_vm_add_round_key_16(state, key_schedule, 1)
    state = _medusa_vm_inv_mix_columns_16(state)
    state = _medusa_vm_mix_input_permute_16(state)
    state = _medusa_inv_shift_rows_permute_16(state)
    state = _medusa_inv_sub_bytes_permute_16(state)
    state = _medusa_vm_add_round_key_16(state, key_schedule, 0)
    return bytes(a ^ b for a, b in zip(state, xor_key16))


def medusa_aes_like_transform32(block32: bytes, material16: bytes = MEDUSA_MATERIAL16) -> bytes:
    """0x18ee54 -> 0x193210..0x193e78：生成 final patch 用的 32 字节。"""
    if len(block32) != 32:
        raise ValueError("block32 must be 32 bytes")
    work = bytearray(block32)
    # native wrapper overwrites last byte with 1 before processing.
    work[-1] = 1
    schedule = medusa_aes_like_key_schedule48(material16)
    out = bytearray()
    chain = MEDUSA_IV16
    for off in (0, 16):
        chain = medusa_aes_like_encrypt_block16(bytes(work[off:off + 16]), chain, schedule)
        out.extend(chain)
    return bytes(out)


def medusa_aes_like_inverse_transform32(out32: bytes, material16: bytes = MEDUSA_MATERIAL16) -> bytes:
    """逆出 `medusa_aes_like_transform32` 的工作块；最后一字节按正向逻辑恒为 1。"""
    if len(out32) != 32:
        raise ValueError("out32 must be 32 bytes")
    schedule = medusa_aes_like_key_schedule48(material16)
    b0 = medusa_aes_like_decrypt_block16(out32[:16], MEDUSA_IV16, schedule)
    b1 = medusa_aes_like_decrypt_block16(out32[16:], out32[:16], schedule)
    return b0 + b1


def medusa_aes32_from_second_buffer(second_buffer: bytes | bytearray) -> bytes:
    """从 second_buffer 抽 31 字节并跑 AES-like，得到 aes32。"""
    return medusa_aes_like_transform32(medusa_extract31(second_buffer) + b"\x00")


def medusa_first_intermediate_from_d71bc(d71bc_output: bytes, reverse_key4: bytes, prefix8: bytes = b"\x00" * 8) -> bytes:
    """reverse_source = prefix8 || d71bc_output，然后 reverse_xor。"""
    if len(prefix8) != 8:
        raise ValueError("prefix8 must be 8 bytes")
    return reverse_xor(prefix8 + d71bc_output, reverse_key4)


def medusa_reverse_source_prefix_from_src_a(src_a: bytes) -> bytes:
    """Return top-level protobuf field 10, used as reverse_source[0:8].

    3040 allocation traces show:
      reverse_source = src_a.field10(8 bytes) || stage33f/control output
    Older drafts used eight zero bytes here; that does not match current app
    output.
    """
    i = 0
    while i < len(src_a):
        key, i = _proto_read_varint(src_a, i)
        field_no, wire_type = key >> 3, key & 7
        if wire_type == 0:
            _v, i = _proto_read_varint(src_a, i)
        elif wire_type == 1:
            i += 8
        elif wire_type == 2:
            ln, i = _proto_read_varint(src_a, i)
            value = src_a[i:i + ln]
            i += ln
            if field_no == 10:
                if len(value) != 8:
                    raise ValueError("src_a field10 must be 8 bytes")
                return value
        elif wire_type == 5:
            i += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
    raise ValueError("src_a field10 not found")


def _rol8(value: int, amount: int) -> int:
    value &= 0xFF
    amount &= 7
    return ((value << amount) & 0xFF) | (value >> (8 - amount))


def medusa_stage336_ring32_from_rand3(rand3: int, sign_key32: bytes = SIGN_KEY32_3040) -> bytes:
    """3040 stage336 slot6 ring.

    Verified against live allocation traces: the stage336 first pass consumes
    bytes `[0,1], [4,5], ... [28,29]` from
    `SM3(sign_key32 || uint32_le(rand3) || sign_key32)`.
    """
    if len(sign_key32) != 32:
        raise ValueError("sign_key32 must be exactly 32 bytes")
    return sm3(sign_key32 + u32le(rand3) + sign_key32)


def medusa_stage336_first_pass(
    source336: bytes,
    ring32: bytes,
    *,
    payload_size: int | None = None,
) -> bytes:
    payload_len = len(source336.rstrip(b"\x00")) if payload_size is None else int(payload_size)
    if payload_len <= 0:
        raise ValueError("payload_size must be positive")
    if len(ring32) < 32:
        raise ValueError("ring32 must be at least 32 bytes")
    out = bytearray(payload_len)
    for index in range(payload_len):
        source_a = ring32[(index * 4) & 31]
        source_b = ring32[((index * 4) + 1) & 31]
        proto_mix = _rol8(source336[index], 4)
        first_mix = (~((((proto_mix + source_a) & 0xFF) ^ source_b))) & 0xFF
        value = (~((((source_b + _rol8(first_mix, 3)) & 0xFF) ^ source_a))) & 0xFF
        out[payload_len - 1 - index] = value
    return bytes(out)


def medusa_stage336_second_pass(first_pass: bytes) -> bytes:
    raw = bytes(first_pass)
    n = len(raw)
    if n < 3:
        raise ValueError("first_pass too short")
    out = bytearray(raw)
    initial_mix = (~(raw[n - 1] ^ raw[n - 2])) & 0xFF
    out[0] = (raw[0] + initial_mix) & 0xFF
    out[1] = (((raw[-1] ^ out[0]) ^ 0xFE) + raw[1]) & 0xFF
    for offset in range(2, n - 1):
        value = (~((_rol8(out[offset - 1], 3) ^ out[offset - 2]) ^ (offset & 0xFF))) & 0xFF
        out[offset] = (value + raw[offset]) & 0xFF
    out[n - 1] = raw[n - 1] ^ out[n - 2]
    accumulator = (out[0] ^ out[1]) & 0xFF
    for offset in range(1, n):
        accumulator = (accumulator + out[offset]) & 0xFF
    out[0] = accumulator
    return bytes(out)


def medusa_stage336_second_pass_decode(encoded: bytes | bytearray | memoryview) -> bytes:
    """Invert :func:`medusa_stage336_second_pass`.

    This is useful for native-trace validation: a captured `stage33f` can be
    reversed to the original plaintext `source336` without calling the app.
    """
    final = bytes(encoded)
    n = len(final)
    if n < 3:
        raise ValueError("encoded second-pass buffer too short")
    out = bytearray(final)
    # second_pass overwrites out[0] with a checksum at the end.  All other
    # bytes are the pre-checksum state.  Recover the original out[0] first.
    tail_sum = sum(out[1:]) & 0xFF
    out[0] = ((out[0] - tail_sum) & 0xFF) ^ out[1]

    raw = bytearray(n)
    raw[n - 1] = out[n - 1] ^ out[n - 2]
    for offset in range(2, n - 1):
        value = (~((_rol8(out[offset - 1], 3) ^ out[offset - 2]) ^ (offset & 0xFF))) & 0xFF
        raw[offset] = (out[offset] - value) & 0xFF
    raw[1] = (out[1] - (((raw[n - 1] ^ out[0]) ^ 0xFE) & 0xFF)) & 0xFF
    initial_mix = (~(raw[n - 1] ^ raw[n - 2])) & 0xFF
    raw[0] = (out[0] - initial_mix) & 0xFF
    return bytes(raw)


def medusa_stage336_first_pass_decode(
    first_pass: bytes | bytearray | memoryview,
    ring32: bytes | bytearray | memoryview,
) -> bytes:
    """Invert :func:`medusa_stage336_first_pass` for a known 32-byte ring."""
    encoded = bytes(first_pass)
    ring = bytes(ring32)
    if len(ring) < 32:
        raise ValueError("ring32 must be at least 32 bytes")
    tables: dict[tuple[int, int], list[int]] = {}

    def forward_byte(value: int, source_a: int, source_b: int) -> int:
        proto_mix = _rol8(value, 4)
        first_mix = (~((((proto_mix + source_a) & 0xFF) ^ source_b))) & 0xFF
        return (~((((source_b + _rol8(first_mix, 3)) & 0xFF) ^ source_a))) & 0xFF

    out = bytearray(len(encoded))
    for index in range(len(encoded)):
        source_a = ring[(index * 4) & 31]
        source_b = ring[((index * 4) + 1) & 31]
        key = (source_a, source_b)
        table = tables.get(key)
        if table is None:
            table = [-1] * 256
            for candidate in range(256):
                table[forward_byte(candidate, source_a, source_b)] = candidate
            tables[key] = table
        coded = encoded[len(encoded) - 1 - index]
        value = table[coded]
        if value < 0:
            raise ValueError("non-invertible stage336 first-pass byte")
        out[index] = value
    return bytes(out)


def medusa_stage33f_to_source336(stage33f: bytes | bytearray | memoryview, d71bc_rand: int) -> bytes:
    """Recover plaintext `source336` from current 3040 `stage33f`.

    Native traces expose `stage33f` more reliably than the plaintext builder
    input.  This helper reverses:
      source336 -> stage336 -> stage33e(prefix8||stage336)
                -> reverse_xor(key4(high16(rand3))) -> stage33f
    and returns the effective protobuf payload (without the stage33e prefix8).
    """
    stage = bytes(stage33f)
    if stage.endswith(b"\x00"):
        stage = stage[:-1]
    if len(stage) <= 8:
        raise ValueError("stage33f is too short")
    key4 = medusa_reverse_key4_from_d71bc_rand(d71bc_rand)
    reverse_source = medusa_inverse_reverse_xor(stage, key4)
    stage336 = reverse_source[8:]
    first_pass = medusa_stage336_second_pass_decode(stage336)
    return medusa_stage336_first_pass_decode(first_pass, medusa_stage336_ring32_from_rand3(d71bc_rand))


def medusa_source336_to_stage33f(src_a: bytes, d71bc_rand: int, payload_size: int | None = None) -> bytes:
    """Current 3040 `source336 -> stage33f` transform, pure Python.

    This replaces the older `block_d71bc_encode` assumption for NovelFM 3040
    full/mget.  Live traces show:
      stage33e = src_a.field10 || stage336(src_a, SM3(key3040||rand3||key3040))
      stage33f = reverse(stage33e) xor key4(high16(rand3)) || 00
    """
    src = bytes(src_a)
    payload_len = len(src.rstrip(b"\x00")) if payload_size is None else int(payload_size)
    if payload_len < 0 or payload_len > len(src):
        raise ValueError("payload_size out of source336 buffer range")
    source = src[:payload_len]
    prefix8 = medusa_reverse_source_prefix_from_src_a(source)
    ring32 = medusa_stage336_ring32_from_rand3(d71bc_rand)
    stage336 = medusa_stage336_second_pass(medusa_stage336_first_pass(source, ring32, payload_size=payload_len))
    key4 = medusa_reverse_key4_from_d71bc_rand(d71bc_rand)
    return reverse_xor(prefix8 + stage336, key4) + b"\x00"


def medusa_packet_from_runtime_buffers(
    khronos: int,
    d71bc_rand: int,
    seed8_rand: int,
    d71bc_output: bytes,
    aes32: bytes,
    reverse_key4: bytes,
) -> bytes:
    """给定仍未还原模块的运行时输出，重建 Medusa 最外层 packet。

    这个函数用于后续动态对照：只要能抓到 d71bc_output、reverse_key4、aes32，
    就可以验证外层 layout / reverse-xor / second_buffer / packet assembly。
    """
    tail2, high2 = medusa_d71bc_rand_parts(d71bc_rand)
    first = medusa_first_intermediate_from_d71bc(d71bc_output, reverse_key4)
    second = medusa_second_buffer_layout(first, tail2, seed8_rand, final2=high2)
    medusa_patch31(second, aes32[:31])
    return medusa_assemble_packet(khronos, tail2, aes32, second)


def medusa_packet_from_src_a(
    khronos: int,
    d71bc_rand: int,
    seed8_rand: int,
    src_a: bytes,
) -> bytes:
    """给定已序列化的 src_a，完整生成 X-Medusa raw packet。

    这里已经不需要 so/frida：
      src_a -> d71bc_key -> block_d71bc_encode
            -> reverse-xor/second_buffer
            -> extract31/AES-like/patch31
            -> fixed20||tail2||0001||body

    当前剩余大块是自动构造“番茄畅听当前设备环境”的 src_a 字段全集。
    """
    tail2, high2 = medusa_d71bc_rand_parts(d71bc_rand)
    reverse_key4 = medusa_reverse_key4_from_high2(high2)
    d71bc_output = block_d71bc_encode(src_a, d71bc_key(d71bc_rand))
    reverse_source = medusa_reverse_source_prefix_from_src_a(src_a) + d71bc_output
    first = reverse_xor(reverse_source, reverse_key4)
    second = medusa_second_buffer_layout(first, tail2, seed8_rand, final2=high2)
    aes32 = medusa_aes32_from_second_buffer(second)
    medusa_patch31(second, aes32[:31])
    return medusa_assemble_packet(khronos, tail2, aes32, second)


def x_medusa_from_src_a(khronos: int, d71bc_rand: int, seed8_rand: int, src_a: bytes) -> str:
    """base64 X-Medusa；输入 src_a 时已完整纯 Python。"""
    return b64(medusa_packet_from_src_a(khronos, d71bc_rand, seed8_rand, src_a))


def x_medusa_3040_from_runtime_values(
    *,
    url_or_query: bytes | str,
    khronos: int,
    top_rand: int,
    d71bc_rand: int,
    seed8_rand: int,
    pid: int,
    current_epoch_ms: int,
    device_uuid: bytes | str,
) -> str:
    """纯 Python X-Medusa 管线入口。

    已完整串起：
      src_a protobuf -> d71bc -> reverse-xor -> AES-like -> final packet。

    注意：这个入口现在用于继续对齐 3040 的 src_a 环境字段；如果字段常量
    或 final material 与当前 App 有差异，服务端仍会 6000，但管线本身已经
    不依赖 app/so/frida。
    """
    src_a = medusa_src_a_from_runtime_values(
        url_or_query=url_or_query,
        top_rand=top_rand,
        pid=pid,
        khronos_sec=khronos,
        current_epoch_ms=current_epoch_ms,
        device_uuid=device_uuid,
    )
    return x_medusa_from_src_a(khronos, d71bc_rand, seed8_rand, src_a)


def _bfi32(dst: int, src: int, lsb: int, width: int) -> int:
    mask = ((1 << width) - 1) << lsb
    return ((dst & ~mask) | ((src << lsb) & mask)) & 0xFFFFFFFF


def _bfxil32(dst: int, src: int, lsb: int, width: int) -> int:
    mask = (1 << width) - 1
    return ((dst & ~mask) | ((src >> lsb) & mask)) & 0xFFFFFFFF


def block_d71bc_encode(src: bytes, key: bytes) -> bytes:
    """native helper libmetasec_ml.so+0xd71bc 的纯 Python lift。

    VM 调用形状：
      x0=dst, x1=src, x2=len(src), x3=key32, x4=len(key)

    它不是标准 AES/SM4，而是：
      1. 逆序写入的 key-dependent byte transform
      2. 正向 feedback pass
      3. 尾部 xor/sum 修正
    """
    if len(src) < 3:
        raise ValueError("src must contain at least three bytes")
    if not key:
        raise ValueError("key must not be empty")
    n = len(src)
    out = bytearray(b"\x20" * n)
    key_len = len(key)

    for i, b in enumerate(src):
        k0 = key[(i * 4) % key_len]
        k1 = key[((i * 4) | 1) % key_len]
        w30 = b >> 4
        w30 = _bfi32(w30, b, 4, 8)
        w24 = (w30 + k0) & 0xFFFFFFFF
        w24 = (w24 ^ (~k1 & 0xFFFFFFFF)) & 0xFFFFFFFF
        w30 = (w24 << 3) & 0xFFFFFFFF
        w30 = _bfxil32(w30, w24, 5, 3)
        w27 = (w30 + k1) & 0xFFFFFFFF
        out[n - 1 - i] = (k0 ^ (~w27 & 0xFFFFFFFF)) & 0xFF

    seed0 = (out[0] + ((out[n - 1] ^ (~out[n - 2] & 0xFFFFFFFF)) & 0xFFFFFFFF)) & 0xFFFFFFFF
    out[0] = seed0 & 0xFF
    out[1] = (out[1] + ((seed0 ^ out[n - 1] ^ 0xFE) & 0xFFFFFFFF)) & 0xFF

    for i in range(2, n - 1):
        prev = out[i - 1]
        prev2 = out[i - 2]
        mix = prev >> 5
        mix = _bfi32(mix, prev, 3, 8)
        mix ^= prev2
        mix ^= ~i & 0xFFFFFFFF
        out[i] = (out[i] + mix) & 0xFF

    out[n - 1] ^= out[n - 2]
    tail_sum = sum(out[1:]) & 0xFF
    out[0] = ((out[0] ^ out[1]) + tail_sum) & 0xFF
    return bytes(out)


def _d71bc_forward_byte(b: int, k0: int, k1: int) -> int:
    w30 = b >> 4
    w30 = _bfi32(w30, b, 4, 8)
    w24 = (w30 + k0) & 0xFFFFFFFF
    w24 = (w24 ^ (~k1 & 0xFFFFFFFF)) & 0xFFFFFFFF
    w30 = (w24 << 3) & 0xFFFFFFFF
    w30 = _bfxil32(w30, w24, 5, 3)
    w27 = (w30 + k1) & 0xFFFFFFFF
    return (k0 ^ (~w27 & 0xFFFFFFFF)) & 0xFF


def _d71bc_predecode(encoded: bytes) -> bytes:
    """逆掉 d71bc 的 key-independent feedback/tail pass，输出 per-byte transform 后的缓冲。"""
    if len(encoded) < 3:
        raise ValueError("encoded must contain at least three bytes")
    n = len(encoded)
    c = bytearray(encoded)
    tail_sum = sum(c[1:]) & 0xFF
    c[0] = ((c[0] - tail_sum) & 0xFF) ^ c[1]

    b = bytearray(c)
    b[n - 1] = c[n - 1] ^ b[n - 2]

    a = bytearray(b)
    a[n - 1] = b[n - 1]
    for i in range(2, n - 1):
        prev = b[i - 1]
        prev2 = b[i - 2]
        mix = prev >> 5
        mix = _bfi32(mix, prev, 3, 8)
        mix ^= prev2
        mix ^= ~i & 0xFFFFFFFF
        a[i] = (b[i] - mix) & 0xFF
    a[1] = (b[1] - (b[0] ^ a[n - 1] ^ 0xFE)) & 0xFF
    a[0] = (b[0] - (a[n - 1] ^ a[n - 2] ^ 0xFF)) & 0xFF
    return bytes(a)


def block_d71bc_decode_from_predecoded(predecoded: bytes, key: bytes) -> bytes:
    """把 `_d71bc_predecode()` 的输出按 key 逆回 plaintext。"""
    if not key:
        raise ValueError("key must not be empty")
    n = len(predecoded)
    key_len = len(key)
    tables: dict[tuple[int, int], list[int]] = {}
    out = bytearray(n)
    for i in range(n):
        k0 = key[(i * 4) % key_len]
        k1 = key[((i * 4) | 1) % key_len]
        tab = tables.get((k0, k1))
        if tab is None:
            tab = [-1] * 256
            for b in range(256):
                tab[_d71bc_forward_byte(b, k0, k1)] = b
            tables[(k0, k1)] = tab
        v = tab[predecoded[n - 1 - i]]
        if v < 0:
            raise ValueError("non-invertible d71bc byte table")
        out[i] = v
    return bytes(out)


def block_d71bc_decode(encoded: bytes, key: bytes) -> bytes:
    """`block_d71bc_encode` 的逆变换。"""
    return block_d71bc_decode_from_predecoded(_d71bc_predecode(encoded), key)


def medusa_inverse_reverse_xor(first_intermediate: bytes, key4: bytes) -> bytes:
    """逆 `reverse_xor(source, key4)`。"""
    if len(key4) != 4:
        raise ValueError("key4 must be 4 bytes")
    n = len(first_intermediate)
    return bytes(first_intermediate[n - 1 - j] ^ key4[(n - 1 - j) & 3] for j in range(n))


def medusa_recover_second_buffer_from_packet(packet: bytes) -> tuple[bytes, bytes, bytes]:
    """从 X-Medusa raw packet 逆出未 patch 的 second_buffer。

    返回 `(second_buffer, tail2, aes32)`。这一步不需要 d71bc 的 32-bit 随机数。
    """
    if len(packet) < 24 + 31 * 8:
        raise ValueError("packet too short")
    tail2 = packet[20:22]
    if packet[22:24] != b"\x00\x01":
        raise ValueError("bad medusa marker")
    patched_second = bytearray(packet[25:])
    aes32 = medusa_extract31(patched_second) + packet[24:25]
    original_extract31 = medusa_aes_like_inverse_transform32(aes32)[:31]
    second = medusa_patch31(patched_second, original_extract31)
    return bytes(second), tail2, aes32


def medusa_recover_d71bc_rand_from_packet(packet: bytes) -> int:
    """从真实 X-Medusa raw packet 反推出 d71bc 的 32-bit rand。

    已用 `out/rand_full.out` 校验：
      packet[20:22]          = rand32 little-endian 低 16 位
      recovered_second[-2:]  = rand32 little-endian 高 16 位

    例：raw[20:22]=12 18 且 recovered_second[-2:]=f2 31，
    组合为 `12 18 f2 31`，即 0x31f21812。
    """
    second, tail2, _aes32 = medusa_recover_second_buffer_from_packet(packet)
    return int.from_bytes(tail2 + second[-2:], "little")


def medusa_recover_d71bc_output_from_packet(packet: bytes) -> tuple[bytes, bytes, int]:
    """从 packet 逆出 d71bc_output、tail2 和 seed8_rand。

    注意：当前 3040 的 reverse_source 前 8 字节是 src_a.field10，不是
    全零；本函数返回时会剥掉这 8 字节。
    """
    second, tail2, _aes32 = medusa_recover_second_buffer_from_packet(packet)
    seed8_rand = int.from_bytes(second[1:5], "little")
    high2 = second[-2:]
    reverse_key4 = medusa_reverse_key4_from_high2(high2)
    reverse_source = medusa_inverse_reverse_xor(second[9:-2], reverse_key4)
    return reverse_source[8:], tail2, seed8_rand


def medusa_decode_src_a_from_packet(packet: bytes, d71bc_rand: int) -> tuple[bytes, int]:
    """给定完整 d71bc_rand，从 raw X-Medusa packet 逆出 plaintext src_a。"""
    d71bc_output, _tail2, seed8_rand = medusa_recover_d71bc_output_from_packet(packet)
    return block_d71bc_decode(d71bc_output, d71bc_key(d71bc_rand)), seed8_rand


def medusa_tail2_hash(tail2: bytes) -> int:
    """还原 tail2 -> helper_ret 的小 VM hash。

    这个 helper 用于生成 reverse-xor 的 4 字节 key；输入就是
    X-Medusa raw[20:22] 那两个随机低字节。
    """
    if len(tail2) != 2:
        raise ValueError("tail2 must be 2 bytes")
    state = 0
    for i, b in enumerate(tail2):
        if i & 1:
            t = (((state << 11) & 0xFFFFFFFF) | b) & 0xFFFFFFFF
            t ^= (state & 0xFFFFFFFF) >> 5
            t ^= state & 0xFFFFFFFF
            state = (~t) & 0xFFFFFFFF
        else:
            t = (((state << 7) & 0xFFFFFFFF) ^ b) & 0xFFFFFFFF
            t ^= (state & 0xFFFFFFFF) >> 3
            state = (state ^ t) & 0xFFFFFFFF
    return state


def medusa_reverse_key4_from_tail2(tail2: bytes) -> bytes:
    ret = medusa_tail2_hash(tail2)
    hi = (ret >> 8) & 0xFF
    lo = ret & 0xFF
    return bytes((hi, lo, hi, lo))


def medusa_reverse_key4_from_high2(high2: bytes) -> bytes:
    """Current 3040 reverse-xor key: hash the high 16 bits of d71bc_rand.

    Allocation traces confirm `key4 == medusa_tail2_hash(high2).to_bytes(4,
    "big")`; e.g. high2 `6e3e` -> `fffc8fac`.
    """
    if len(high2) != 2:
        raise ValueError("high2 must be 2 bytes")
    return medusa_tail2_hash(high2).to_bytes(4, "big")


def medusa_reverse_key4_from_d71bc_rand(d71bc_rand: int) -> bytes:
    return medusa_reverse_key4_from_high2(u32le(d71bc_rand)[2:])


def hash_tail2_to_key4_known(tail2: bytes) -> bytes:
    """兼容旧函数名：tail2 -> reverse_xor key4。"""
    known = {
        bytes.fromhex("fe1e"): bytes.fromhex("0f180f18"),
        bytes.fromhex("a057"): bytes.fromhex("ff0dff0d"),
        bytes.fromhex("c2c0"): bytes.fromhex("effbeffb"),
    }
    out = medusa_reverse_key4_from_tail2(tail2)
    if tail2 in known:
        assert out == known[tail2]
    return out


@dataclass
class MedusaKnownParts:
    khronos: int
    top_rand: int  # also X-Helios raw[0:4]
    d71bc_rand: int
    seed8_rand: int

    @property
    def helios_prefix4(self) -> bytes:
        return helios_prefix4(self.top_rand)

    @property
    def fixed20(self) -> bytes:
        return medusa_fixed20(self.khronos)

    @property
    def tail2(self) -> bytes:
        return u32le(self.d71bc_rand)[:2]

    @property
    def high2(self) -> bytes:
        return u32le(self.d71bc_rand)[2:]

    @property
    def seed8(self) -> bytes:
        return u32le(self.seed8_rand) + bytes.fromhex("013a0b00")

    @property
    def key32(self) -> bytes:
        return d71bc_key(self.d71bc_rand)


@dataclass(frozen=True)
class MedusaRecoveredPacket:
    """Facts recoverable from a real X-Medusa raw packet without app/so."""

    raw: bytes
    second_buffer: bytes
    tail2: bytes
    high2: bytes
    d71bc_rand: int
    aes32: bytes

    @property
    def footer16(self) -> bytes:
        return self.second_buffer[-16:]


def medusa_recover_packet(packet: bytes) -> MedusaRecoveredPacket:
    """Recover verified outer-layer facts from a raw X-Medusa packet."""
    second, tail2, aes32 = medusa_recover_second_buffer_from_packet(packet)
    high2 = second[-2:]
    return MedusaRecoveredPacket(
        raw=bytes(packet),
        second_buffer=second,
        tail2=tail2,
        high2=high2,
        d71bc_rand=int.from_bytes(tail2 + high2, "little"),
        aes32=aes32,
    )


if __name__ == "__medusa_selftest__":
    assert sm3(b"abc").hex() == "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
    # bit-slice extract/patch roundtrip: patch 后再 extract 必须取回 patch31。
    buf = bytearray(bytes(range(256)) * 8)
    patch = bytes(range(31))
    medusa_patch31(buf, patch)
    assert medusa_extract31(buf) == patch
    assert reverse_xor(b"\x01\x02\x03\x04", b"abcd") == bytes([0x04 ^ ord("a"), 0x03 ^ ord("b"), 0x02 ^ ord("c"), 0x01 ^ ord("d")])
    _xg_q = "a=1&b=2"
    _xg_body = b'{"a":1}'
    _xg_ts = 1_783_440_910
    _xg = x_gorgon_0404(_xg_q, _xg_body, timestamp=_xg_ts)
    _xg_mat = hashlib.md5(_xg_q.encode()).digest()[:4] + hashlib.md5(_xg_body).digest()[:4] + b"\x00" * 8 + _xg_ts.to_bytes(4, "big")
    assert x_gorgon_0404_recover_material_candidates(_xg) == [_xg_mat]
    assert x_gorgon_8404_parts("840420c140813df9555a2d3ba76e4b637454cc09eb55bc0839cf")["family_hex"] == "4081"
    assert x_gorgon_8404_parts("8404c04740855ce98fa74de44809c8af3fc78192ff9611e56da9")["family_hex"] == "4085"
    # 8404 fixed-rand bodyB/bodyD have the same envelope prefix/family.  Even
    # with an arbitrary same query string, the recovered mask must match and can
    # rebuild the sibling native Gorgon.  This locks down:
    # material20 -> XOR mask20 -> shared final20 -> 8404 body20.
    _xg8404_q = "same-query-for-mask-delta"
    _xg8404_ts = 1_783_478_584
    _xg8404_body_b = '{"item_ids":["6584534483473531396"],"book_id":"7628455577403788350","key":"TEST2","need_stt":false,"scene":3,"tone_id":91}'
    _xg8404_body_d = '{"item_ids":["6584534483473531399"],"book_id":"7628455577403788351","key":"TEST4","need_stt":false,"scene":4,"tone_id":93}'
    _xg8404_b = "840480304001688a50c25752f06ad5f834a07b39c14bc40699c1"
    _xg8404_d = "840480304001688a50790b040eadd5f834a07b39c14bc40699c1"
    _xg8404_mask_b = x_gorgon_8404_recover_mask_candidates(_xg8404_b, _xg8404_q, _xg8404_body_b, timestamp=_xg8404_ts)
    _xg8404_mask_d = x_gorgon_8404_recover_mask_candidates(_xg8404_d, _xg8404_q, _xg8404_body_d, timestamp=_xg8404_ts)
    assert len(_xg8404_mask_b) == 1 and _xg8404_mask_b == _xg8404_mask_d
    assert x_gorgon_8404_with_mask(
        _xg8404_q,
        _xg8404_body_d,
        timestamp=_xg8404_ts,
        prefix2=0x80,
        prefix3=0x30,
        family=0x4001,
        mask20=_xg8404_mask_b[0],
    ) == _xg8404_d
    _xg8404_continue_url = (
        "https://api5-sinfonlinec.novelfm.com/novelfm/playerapi/full/mget/v1/?device_platform=android&os=android&ssmix=a"
        "&_rticket=1783416159882&cdid=5b00d94e-eaa2-42fa-8095-6e994728a48f&channel=vivo_3040_64&aid=3040"
        "&app_name=novel_fm&version_code=656&version_name=6.5.6.32&manifest_version_code=656&update_version_code=65632"
        "&resolution=1440*2560&dpi=640&device_type=SM-S9260&device_brand=Samsung&language=zh&os_api=28&os_version=9"
        "&ac=wifi&device_id=3001028083774489&iid=3313243055211242&comment_tag_c=5&vip_state=0&host_abi=arm64-v8a"
        "&category_style=1&need_personal_recommend=1&rom_version=PQ3A.190605.02261134+release-keys"
    )
    _xg8404_continue_body_a = '{"item_ids":["6584534483473531396"],"book_id":"7628455577403788350","key":"TEST","need_stt":false,"scene":3,"tone_id":91}'
    _xg8404_continue_a = "8404c0184081656258105562b8ab327ee89096af581551b4e1cd"
    _xg8404_continue_mask = x_gorgon_8404_recover_mask_candidates(
        _xg8404_continue_a,
        _xg8404_continue_url,
        _xg8404_continue_body_a,
        timestamp=1_783_416_153,
    )
    assert _xg8404_continue_mask == [bytes.fromhex("a381d38b8a93a3a683a393f941aa884541a3c149")]
    _xg8404_continue_recovered = x_gorgon_8404_recover(
        _xg8404_continue_a,
        _xg8404_continue_url,
        _xg8404_continue_body_a,
        timestamp=1_783_416_153,
    )
    assert (
        _xg8404_continue_recovered.prefix2,
        _xg8404_continue_recovered.prefix3,
        _xg8404_continue_recovered.family,
        _xg8404_continue_recovered.mask20,
    ) == (0xC0, 0x18, 0x4081, _xg8404_continue_mask[0])
    assert x_gorgon_8404_with_mask(
        _xg8404_continue_url,
        _xg8404_continue_body_a,
        timestamp=1_783_416_153,
        prefix2=0xC0,
        prefix3=0x18,
        family=0x4081,
        mask20=_xg8404_continue_mask[0],
    ) == _xg8404_continue_a
    # Source-focused trace (out/trace_field13_source_focus_20260708_now2.txt):
    # locks down another current 6.5.6.32 8404 sample with family=0x4001.
    # The recovered source336.field13 for this exact body is:
    #   33857f9b24024af0320c5eef85643043a60bdf55
    # It does not equal SHA1/MD5 of body_md5/gorgon/mask/control32, so keep it
    # as evidence for the remaining field13 lift rather than pretending the
    # "xk + body md5" hypothesis is solved.
    _xg8404_focus_url = (
        "https://api5.novelfm.com/novelfm/playerapi/full/mget/v1/?device_platform=android&os=android&ssmix=a"
        "&_rticket=1783505000000&cdid=7634657e-a134-47cf-9ac3-c38ea9923097&channel=54157680a&aid=3040"
        "&app_name=novel_fm&version_code=656&version_name=6.5.6.32&manifest_version_code=656&update_version_code=65632"
        "&resolution=1440*2560&dpi=640&device_type=SM-S9260&device_brand=Samsung&language=zh&os_api=28&os_version=9"
        "&ac=wifi&device_id=3001028083774489&iid=1395712309393850&comment_tag_c=5&vip_state=0&host_abi=arm64-v8a"
        "&category_style=1&need_personal_recommend=1"
        "&ab_sdk_version=90111254%2C90975474%2C16797554%2C91986083%2C90126074%2C91986082%2C91008840%2C91281044%2C92120672%2C90110758%2C90174492%2C5711286%2C16963142%2C17225371%2C90114353%2C90098780%2C92100130%2C91347266%2C90952506%2C90614667%2C91801013%2C91763052%2C91763051%2C91763050%2C91787063%2C90661280%2C91633046%2C90609513%2C92319500"
        "&rom_version=PQ3A.190605.02261134+release-keys&klink_egdi=AAK_uq0vE8PrXz2HmNU9hVK7t9H-AFvbvPlsZSPYH3E9haMKxm0o-Yqm"
    )
    _xg8404_focus_body = '{"item_ids":["6584534483473531398"],"book_id":"7628455577403788350","key":"TEST3","need_stt":true,"scene":3,"tone_id":92}'
    _xg8404_focus_recovered = x_gorgon_8404_recover(
        "8404c00240018a9b4f7e4d92599b6741c5bb04bc0c0f1d37468b",
        _xg8404_focus_url,
        _xg8404_focus_body,
        timestamp=1_783_525_192,
    )
    assert (
        _xg8404_focus_recovered.prefix2,
        _xg8404_focus_recovered.prefix3,
        _xg8404_focus_recovered.family,
        _xg8404_focus_recovered.mask20,
    ) == (0xC0, 0x02, 0x4001, bytes.fromhex("b29b9e9b70feedecddec9bcdd69a43d37080b880"))
    assert hashlib.sha1(hashlib.md5(_xg8404_focus_body.encode()).digest()).hexdigest() != "33857f9b24024af0320c5eef85643043a60bdf55"
    assert helios_prefix4(0x6EEA984C).hex() == "4c98ea6e"
    # Oracle same_1: raw prefix 9d3db55e => rand32=0x5eb53d9d.
    assert x_helios_3040(1783236460, 0x5EB53D9D) == "nT21XinWzGiMmlefnRNMDeWoiVkpE4Rhy2RCOjCNaDKqOywj"
    assert x_ss_stub_md5(b'{"a":1}') == "BB6CB5C68DF4652941CAF652A366F2D8"
    assert hashlib.sha256(base64.b64decode(latest913_bodyc_trace_medusa())).hexdigest() == "d3476433f775bee18870ca2f2b43b275d328492960c78251cb92678be8b0ac4a"
    assert hashlib.sha256(base64.b64decode(continue913_bodya_trace_medusa())).hexdigest() == "f1665ed70b17134f60842a3b89e740defe3e82304f18ba982aad713114601152"
    _cont_rt = medusa3040_recover_runtime_values(continue913_bodya_trace_medusa())
    assert (_cont_rt.khronos, _cont_rt.rand3, _cont_rt.rand4, _cont_rt.head_byte_low6, _cont_rt.query_sm3_low6) == (
        1_783_416_153,
        0x3456789A,
        0x456789AB,
        0x2B,
        0x12,
    )
    _cont_src, _cont_rt2 = medusa3040_recover_source336(continue913_bodya_trace_medusa())
    _cont_field13 = proto_first_field(_cont_src, 13)
    assert _cont_rt2 == _cont_rt
    assert _cont_field13 == bytes.fromhex("ab13507e946dd63ce01183756387ef4e2da69bb4")
    _cont_src_rebuilt, _cont_src_size = source336_container_alloc_3040_continue913(
        _xg8404_continue_url,
        rand2=0x23456789,
        khronos=1_783_416_153,
        field13=_cont_field13,  # type: ignore[arg-type]
        ladon_raw=bytes.fromhex("55b74f51"),
    )
    assert _cont_src_size == 915 and _cont_src_rebuilt[:_cont_src_size] == _cont_src[:_cont_src_size]
    assert medusa_tail2_hash(bytes.fromhex("fe1e")) == 0xFFF80F18
    assert medusa_reverse_key4_from_tail2(bytes.fromhex("fe1e")) == bytes.fromhex("0f180f18")
    assert medusa_reverse_key4_from_tail2(bytes.fromhex("a057")) == bytes.fromhex("ff0dff0d")
    assert medusa_reverse_key4_from_tail2(bytes.fromhex("c2c0")) == bytes.fromhex("effbeffb")
    assert medusa_d71bc_rand_parts(0x31F21812) == (bytes.fromhex("1218"), bytes.fromhex("f231"))
    assert block_d71bc_encode(b"abc", bytes(range(32))).hex() == "a0299e"
    assert block_d71bc_decode(bytes.fromhex("a0299e"), bytes(range(32))) == b"abc"
    assert medusa_aes_like_transform32(bytes(range(32))).hex() == "3e97d240f5e1bc020a01cd16be2170695442f3e53cc272ff700179a21c23ddce"
    assert medusa_aes_like_inverse_transform32(medusa_aes_like_transform32(bytes(range(32))))[:31] == bytes(range(31))
    _src = medusa_src_a_rebuild(
        f1=bytes.fromhex("2d4b4fca49750d433fb5ae2c226dcc56"),
        f3=123456,
        f6="1532254240",
        f12=789,
        f13=b"x" * 20,
        url_sm3_prefix6=b"abcdef",
        f15=medusa_f15_rebuild(123),
        f23=b"nested23",
        f24_json=(b'{"cmr":1,"tk":true,"pad":"' + b"a" * 260 + b'"}'),
    )
    _pkt = medusa_packet_from_src_a(1783236460, 0x12345678, 0x9ABCDEF0, _src)
    assert _pkt[:20] == medusa_fixed20(1783236460)
    assert _pkt[20:24] == bytes.fromhex("78560001")
    assert medusa_decode_src_a_from_packet(_pkt, 0x12345678) == (_src, 0x9ABCDEF0)
    print("SM3 OK")
    print("bit-slice OK")
    print("Helios 3040 OK")
    print("X-Argus example:", x_argus(1783211322))
    print("fixed20 example:", medusa_fixed20(1783211322).hex())

# ---- Downloader implementation ----
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
番茄畅听小说正文下载器（运行时不需要 App/adb/frida/so；AES 依赖 PyCryptodome）。

正文使用 App 接口：/novelfm/playerapi/full/mget/v1/
正文在本文件中解密并合并为 TXT。
目录通过签名后的 App 目录接口获取，不请求小说网页。

命令：
  python fanqie_pure_python.py --book-id 6781304585576254471 --limit 3
"""

import argparse
import base64
import concurrent.futures
import gzip
import hashlib
import html
import http.client
import json
import os
import random
import re
import secrets
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 请求密钥封装和正文 AES 解密由 PyCryptodome 承担。
try:
    from Crypto.Cipher import AES as PYCRYPTODOME_AES
except ImportError:
    try:
        from Cryptodome.Cipher import AES as PYCRYPTODOME_AES
    except ImportError as exc:
        raise RuntimeError('缺少 PyCryptodome：请安装 python3-pycryptodome 或执行 python -m pip install pycryptodome') from exc
try:
    import orjson
except ImportError:
    orjson = None

API_HOST = "https://api5-sinfonlinec.novelfm.com"
DEFAULT_UA = "com.xs.fm/656 (Linux; U; Android 9; zh_CN; SM-S9260; Build/PQ3A.190605.02261134;tt-ok/3.12.13.17)"
# NovelFM full/mget resolves the body by chapter item_ids.  Keep a known-live
# NovelFM book context for books whose own book-level record returns 1021001.
# This remains a 3040 / novelfm.com request, not a Fanqie Novel reader request.
NOVELFM_REQUEST_BOOK_ID = os.environ.get(
    'FANQIE_NOVELFM_REQUEST_BOOK_ID', '7320841644486446142'
).strip() or '7320841644486446142'

# full/mget 的 App 安全 profile。
# legacy3040 保留为可用基线；pure3040 是当前已复测 code=0 的
# 6.5.6.32 / accepted881 纯 Python 生成链路，失败时会自动回退 legacy3040。
# 运行时不打开 App/adb/frida/so，也不读取其他项目。
FULL_MGET_SIGNED_URL = (
    "https://api5-sinfonlinec.novelfm.com/novelfm/playerapi/full/mget/v1/?device_platform=android&os=android&ssmix=a&_rticket=1783204359936"
    "&cdid=7634657e-a134-47cf-9ac3-c38ea9923097&channel=54157680a&aid=3040&app_name=novel_fm&version_code=656"
    "&version_name=6.5.6.32&manifest_version_code=656&update_version_code=65632&resolution=1440*2560&dpi=640"
    "&device_type=SM-S9260&device_brand=Samsung&language=zh&os_api=28&os_version=9&ac=wifi&device_id=3001028083774489"
    "&iid=1395712309393850&comment_tag_c=5&vip_state=0&host_abi=arm64-v8a&category_style=1&need_personal_recommend=1"
    "&ab_sdk_version=90111254%2C90975474%2C16797554%2C91986083%2C90126074%2C91986082%2C91008840%2C91281044%2C92120672%2C90110758%2C90174492%2C5711286%2C16963142%2C17225371%2C90114353%2C90098780%2C92100130%2C91347266%2C90952506%2C90614667%2C91801013%2C91763052%2C91763051%2C91763050%2C91787063%2C90661280%2C91633046%2C90609513%2C92319500"
    "&rom_version=PQ3A.190605.02261134+release-keys&klink_egdi=AAK_uq0vE8PrXz2HmNU9hVK7t9H-AFvbvPlsZSPYH3E9haMKxm0o-Yqm"
)
FULL_MGET_SIGN_HEADERS = {
    "X-Helios": "74q/CeIh3yZcs7mXt06AivkFq2H/qg7fQja9GyQETk+xuNLD",
    "X-Medusa": "D4pJaifBBqBD/0QpNT/nRijnhTwsdgABSIpiytJLgdYCGMyFOl5YR5nNZujZ1SK8VdWJs4baDoLOlb0xXRI63CF0TmFEFa15/YZan7x4TVd4+ztYLbH3Vj772u+4ngyUU4fNbfXzij0E38b7dOR4uC8Fnc20Bv5hCtpU27Va6DqwKpYMEhCEBg0HT8UpctRHGyyJAwOjfp/xjhaLRrGnQ9iDfqWt5TwczSwPzdc80wt/X9YB6x2zHQUqhg4sTNTezhJlN8Vb5QTSnd3MvbFbb0/pWz/0Xt/SVJ7GpdEJFs+9wZbpiV3nRU4ehGOA632saoYgHAozSaJj6MRQblEVKnGg/GdynuFNs8426q4X9BQIoKJqNE3XZYP3b9mF5diILlPab9fdGj5+ViQsFOia22zM8D07hp2zlwB/Nqq/GjLLpJyvygpNhP+I7D4DhxFJXToGuQ214++G6cP4GjKnox9h+zir8XVBIRiEEPYrDkgwLwrh/yM5bOgufPqdogP72rBqupH93bRQyCwOhGChtU1xX7LhLTSpQ3MRQscSs9MCeOdv9arNsCZPEh5RpCuyJkmr9QguYWnztWGrZKKNkoMV4LcjJMgt241ZzgtHMFeoxKg6FxIkYJwEKrrpGSrPlv3n90jvTiFbUmCQnBSlx3kczl5wVY2i+AUuUhEuQJpiXAHyxcSIrM1LGnRVVkjOcn9sSAmk2yjE8k4izKcAkR1/w9Wgy07lLJcP9iU22pTIi4IMmQmyO9cUVo8mHnAEpDL5P3ipAmcSEf2AgGS1Eo9l/PeVSZoZMothCJv0EoP3K2H/1gEC4xXoiueDjQdpBsXBBrT1AcFKL1VjNIkmNXrjcLoKoTpQeAtWrfEpXt0mBO5yAPn+nsJsR3LwhtLaElC7TYlfrJpchw6UnFudE5wCbGuklPfn0WIqAY4FiqU+NcJF+4gPFH72DBCaSi1E2qtCJ/iiRmO5IhNX0iFON+VcJKywmKbRT/Dg6l6rgyv6ryJAvUYjzYiCmtwSFIRxUwfEHmf2rgos1BkfmZnzQNm8Brdipjx3RhK1SMwN0Edb/ceOnVdmxOejBDjw7K5MKa1EEfG9Emc5B6dvWX5aex6rEtQhTuuOCioOzmNZ6ZXU4jXeQN/W/lZl9SSs+bVi2U8qEcZR0jKWXWFDlpUSfL9SaYz+mnkjycJYg0Xx/bB2l9e3b8cpMihgIiu1jialEu3MvJg4//hPGP/4zlj2Fg==",
}

APP656_CAPTURED_SIGNED_URL = (
    "https://api5-sinfonlinec.novelfm.com/novelfm/playerapi/full/mget/v1/?device_platform=android&os=android&ssmix=a"
    "&_rticket=1783813255257&cdid=728a1bfa-fc3c-46af-922b-8d7c8f8c9960&channel=54157680a&aid=3040"
    "&app_name=novel_fm&version_code=656&version_name=6.5.6.32&manifest_version_code=656&update_version_code=65632"
    "&resolution=1440*2560&dpi=640&device_type=SM-G9750&device_brand=samsung&language=zh&os_api=28&os_version=9"
    "&ac=wifi&device_id=357773596191434&iid=1659597569151059&comment_tag_c=5&vip_state=0&host_abi=arm64-v8a"
    "&category_style=1&need_personal_recommend=1"
    "&ab_sdk_version=90111254%2C90975474%2C91016290%2C16797554%2C91847784%2C91986083%2C90126074%2C91068610"
    "%2C91986082%2C90118821%2C91008840%2C91281044%2C90128754%2C92120672%2C90110758%2C91273322%2C90174492"
    "%2C5711287%2C17225371%2C15867846%2C90098780%2C92100130%2C91347266%2C90952506%2C91048633%2C91247455"
    "%2C90614667%2C90116954%2C91801013%2C91763052%2C91763051%2C91763050%2C91619419%2C91787063%2C91832703"
    "%2C90661280%2C91633046%2C90941890%2C91766414%2C90609513%2C92319500"
    "&rom_version=PQ3A.190605.02261134+release-keys&klink_egdi=AAL35IhN0D-vvyhlwtGKU6aYry-lUeNh6mJjzSGrk87L-phNqjNKObWV"
)
APP656_CAPTURED_SIGN_HEADERS = {
    # 6.5.6.32 真机/模拟器 App 经 Reqable 解密抓到的当前 full/mget 签名族。
    # 已复测：请求体中的 item_ids/book_id/key 可替换，1/50/175 章均 code=0。
    "X-SS-STUB": "",
    "X-SS-Req-Ticket": "1783813255387",
    "X-Ladon": "GWfzYA==",
    "X-Khronos": "1783813251",
    "X-Argus": "g9RSag==",
    "X-Gorgon": "840480f44081fd9057f0f9b62717a8efe9dd5a4074aedb809caa",
    "X-Helios": "nW7dKa/8xN3fEOoauuooFyBFgntrHsn6/fQ+Jw9Vmll6oc0Z",
    "X-Medusa": "htRSaq6fHaDKoV8pvGH8RqG5njwBJQABqsqXqgocAe4CGK8S2Rvxh7VdJufmMrpjIogVUHR13gnhqLOszLNzTSIWrPjluYnRwmRKe/rDpXJlbwmSWiC963cLDwcEIm76IywS12SJ0EvN5MkOlhBsKKtDTpIvFBMKEpxjePNDTqc3BDGwdSQVr6e7MVYGAVuwDTNeH6X7/+Fwtefzcrk3zoYV8uS4p9IIrmcpKYSJyeGP/IeguosXO7TAFgNehxLmbQjrPNEWYyy5+kEzdXDtPbr57FnMuXQMBJQe5QlxTzterfFoThiuVFCiHPCzs06GmZ7oAIV3hwLcEGt7tZOsMi5TEQRS/uSFXOQ1S0C5rzJPtLBWF7kmPoJlGU2SDjAObMxKy6Uww7PgMBPW7m6vBd3vFsAkTSrui37NgnYVXpUNF7+dOd+kAvFNyRJqRanyiYq6cJPbxxIJgB9qSQa+uN7/uJ6VbcQimvJKOJLRjhpdjk+StsnF8uS3uD4L2Od682mc2DOiiWHRVXKYh1zNPmeecZYw1nnKYNntq0E/Ih+ZpwX+DZ4GLtL6imBfDxzk/o9ENfKw2xkj/1NI+rRBRPEHGvOEy+R/4i6MWaXOU+ciAjYOSNN0Np6mei+WzD5e39XadCcthW4Z1fcLtsRRJQcHQLN8Qlt7u3evvyuM9HhAZskKCIaMzFBuV5C+ZbfMm2yzLCFPyINg0oQOPw2xmzjcaOqO8rURaVSPTLDFBFEHW3cSnkhD8hio+sfCTNUxepuzYjRdttckhWeTtSkh89RiOXJLMHYVk2m5cffdXFnNZC65I9TpTvXBLKjsFGZUJ4CwMzRakqs+pQe0m9Py1hrBnREYcWBDXjlAn6JQIYWUxx2eGW18h/wmYTC42W0KU6lAz9rVzSiaQNCe9n2ZhyNP1+2SBUOcdVsTWfXTnCQ2dDPjVaKUHZg662hTCwHPGNwsDAlFEYjocTkRdVie8sJaUhFh6OsIOjhrckvUPG3KMiD2QMH1paTQmDqO0j8GYI87GMKSZhkv4fMiXnDv//GUnxYeMq2bNjO4HsVBw7k1wTakAjvS9+7eMbKpfNl8PMlLhlOnFYhIBkpCebogmygowCwmaZ+OEZD/nPRv6Ar/A86vtl/u+ryTvFukmMiDoGCfylNMetog5T6w5khiaEo77jZ7VgPqSVnxDLVwf2jRlQLIgbO166Po79n2e/agnzoU7CbZ0l409Tj7ZcLBpN1L2I529SvlXntTlD+A5te/Xv/5v15+uchv",
}

CAPTURED_CODE0_SIGNED_URL = (
    "https://api5-sinfonlinec.novelfm.com/novelfm/playerapi/full/mget/v1/?device_platform=android&os=android&ssmix=a"
    "&_rticket=1778923033155&cdid=5b00d94e-eaa2-42fa-8095-6e994728a48f&channel=vivo_3040_64&aid=3040"
    "&app_name=novel_fm&version_code=644&version_name=6.4.4.32&manifest_version_code=644&update_version_code=64432"
    "&resolution=1440*2560&dpi=640&device_type=SM-S9260&device_brand=Samsung&language=zh&os_api=28&os_version=9"
    "&ac=wifi&device_id=3001028083774489&iid=3313243055211242&comment_tag_c=5&vip_state=0&host_abi=arm64-v8a"
    "&category_style=1&need_personal_recommend=1"
    "&ab_sdk_version=90975474%2C91016290%2C91847784%2C91008840%2C91281044%2C92120672%2C90110758%2C91273322"
    "%2C91799786%2C90174492%2C91225968%2C5711286%2C90098780%2C91279070%2C90952506%2C90614667%2C91801013"
    "%2C91787063%2C90661280%2C91633046%2C91766414%2C92280103%2C91294212%2C92280104%2C90609513%2C92280105"
    "&rom_version=PQ3A.190605.02261134+release-keys"
)
CAPTURED_CODE0_SIGN_HEADERS = {
    "X-Helios": "DBTGZE7sXhw4eks4fA20yw3N9OR5HfbMr70FqlazlMF3tCvx",
    "X-Medusa": "EzYIajt9R6BfQwUpKYOmRjRbxDz2EQABOYo3dgd/gbsGGMp+hrc1ghZj0NktdzsJXKmmgDJ7+fxV9zNX7ikEKrUFE24hGlJ3pzzcPfNX32/veZlA7xfUm+Iu1ZeoloZokQTeB1l9AgUWuCRGH3lZkNWZp2+H0te5zUv3suF6CRJVmiBna1qzYwuP/NtgJZXE3ayC62jOqBeAq2Cwx1PKkxkRaoXCLUUqtSQ0w69YSIbCeTSTnAW5ZT77yyeSoqd+t6ASCr99P6HiAoIGayp2R7e4ZKQfEzXipwPq1zzoVyzCLtRfKsz0OCT4nddQXB8Hxd1XIg9OeOd1j1++9mcijtTOpg4+t5C9gL3Zr5zlhnByph+pCe82/nnDDh31bIV8SXlqHfCB4030JnJRZTNn8b8Y/jDRb7QEd26j8SxieHwp+KONwuJoVNEFo3vMu0gH2qpCaANg54gmxjrQX9t7W+UKoN8EdWErj0OmUAA5BETbAbebAJLmNdelu7+SCTEhuLkFf3/AlM2Isf64gq6gvhJVruqZIsYVFQ0b7qJrR3AhAxPW7Khd+cuLgsib7kYvNcgJcEu05FUHKaCttCJYTLUFmOcdwtGPheHgSIvyPrlnmLWPiHE7Obaukg2nCuEUgrVOeNqTZpkZ/uYOuV4NT/D9w0tGVBzxFZvTmiAOiCND0pROnpAUHOoE0IL4tdxFnCN9Ha7d61TqvNSf+M6VkJbvMEgqF7S8r26oTwPFMDs7mUSYK1aSQI1avJ0HaSnejwoOOGWNy/W61FVFIuHNo3N3emKKUtAtAaniZDtOMsokFhs//BxPutluY7VjOCmMCqIYBje18HLOusppIA6shPdVQrELKZqg9UTlj4wYT7+IIDUOm9h+TwojWYcpu4/eYxl91uDRDd/vDPs34sVXen6lRxbc0aJS/meglIS5Qt/G2n/vHYfz9IDzSpU7p+q2reyUj3HU5UxnnPw8CCXPNdQggmNwgsb2oSVBcFq6Z46PULoCQ9IBa+r+uG8ps3IvqdzCZWDwJA5o5grOOGZ7nOoNH3JmT+/ZMYl7dQZQaJmALwl9x0C5jMwlV/wEuVIsWWHjldfIB/u1aoZZ8CPoqsA/45QieeITY3QzSWU6DBLXVnh+f1IhkHVSobew7fWVecYBknIk80I8s0o8tbXGJym8sXr4GWh7OEJNKmtpmLiYisc7EYHTPh+H9z7zIpG1SBoQMaj6SYhEPyXlYslPIL8iXAr1Dv/9f7L//f7yUB8=",
}

APP_COMMON_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "application/json; charset=utf-8,application/x-protobuf",
    "Accept-Encoding": "gzip",
    "X-Xs-From-Web": "0",
    "Content-Type": "application/json; charset=utf-8",
}

DEFAULT_QUERY = {
    "device_platform": "android", "os": "android", "ssmix": "a", "channel": "54157680a", "aid": "3040",
    "app_name": "novel_fm", "cdid": "7634657e-a134-47cf-9ac3-c38ea9923097", "version_code": "656",
    "version_name": "6.5.6.32", "manifest_version_code": "656", "update_version_code": "65632", "resolution": "1440*2560",
    "dpi": "640", "device_type": "SM-S9260", "device_brand": "Samsung", "language": "zh", "os_api": "28",
    "os_version": "9", "ac": "wifi", "device_id": "3001028083774489", "iid": "1395712309393850",
    "comment_tag_c": "5", "vip_state": "0", "host_abi": "arm64-v8a", "category_style": "1", "need_personal_recommend": "1",
}

PURE3040_656_QUERY_ORDER = [
    ("device_platform", "android"),
    ("os", "android"),
    ("ssmix", "a"),
    ("_rticket", ""),
    ("cdid", "5b00d94e-eaa2-42fa-8095-6e994728a48f"),
    ("channel", "vivo_3040_64"),
    ("aid", "3040"),
    ("app_name", "novel_fm"),
    ("version_code", "656"),
    ("version_name", "6.5.6.32"),
    ("manifest_version_code", "656"),
    ("update_version_code", "65632"),
    ("resolution", "1440*2560"),
    ("dpi", "640"),
    ("device_type", "SM-S9260"),
    ("device_brand", "Samsung"),
    ("language", "zh"),
    ("os_api", "28"),
    ("os_version", "9"),
    ("ac", "wifi"),
    ("device_id", "3001028083774489"),
    ("iid", "3313243055211242"),
    ("comment_tag_c", "5"),
    ("vip_state", "0"),
    ("host_abi", "arm64-v8a"),
    ("category_style", "1"),
    ("need_personal_recommend", "1"),
    ("rom_version", "PQ3A.190605.02261134+release-keys"),
]

DHP = int("ffffffffffffffffc90fdaa22168c234c4c6628b80dc1cd129024e088a67cc74020bbea63b139b22514a08798e3404ddef9519b3cd3a431b302b0a6df25f14374fe1356d6d51c245e485b576625e7ec6f44c42e9a637ed6b0bff5cb6f406b7edee386bfb5a899fa5ae9f24117c4b1fe649286651ece45b3dc2007cb8a163bf0598da48361c55d39a69163fa8fd24cf5f83655d23dca3ad961c62f356208552bb9ed529077096966d670c354e4abc9804f1746c08ca237327ffffffffffffffff", 16)
DHG = 2
DHAES_TOKEN = base64.b64decode("rCXGfd2POMGzeiNIgo4iLg==")

# ---- PyCryptodome AES-CBC ----
def _pkcs7_pad(data:bytes)->bytes:
    n=16-len(data)%16
    return data+bytes([n])*n

def _pkcs7_unpad(data:bytes)->bytes:
    n=data[-1]
    if n<1 or n>16 or data[-n:]!=bytes([n])*n:
        raise ValueError('bad pkcs7')
    return data[:-n]

def aes_cbc_encrypt(data:bytes,key:bytes,iv:bytes)->bytes:
    return PYCRYPTODOME_AES.new(key,PYCRYPTODOME_AES.MODE_CBC,iv).encrypt(_pkcs7_pad(data))

def aes_cbc_decrypt(data:bytes,key:bytes,iv:bytes)->bytes:
    return _pkcs7_unpad(PYCRYPTODOME_AES.new(key,PYCRYPTODOME_AES.MODE_CBC,iv).decrypt(data))

def aes_cbc_decrypt_fast(data:bytes,key:bytes,iv:bytes)->bytes:
    return aes_cbc_decrypt(data,key,iv)
# ---- common helpers ----
def java_bigint_bytes(n:int)->bytes:
    if n==0: return b'\x00'
    b=n.to_bytes((n.bit_length()+7)//8,'big')
    return b'\x00'+b if b[0]&0x80 else b

def strip_leading_zero_to_32(b:bytes)->bytes:
    i=0
    while i<len(b) and b[i]==0: i+=1
    if i+31>=len(b): i=max(0,len(b)-32)
    key=b[i:i+32]
    if len(key)!=32: raise ValueError('bad shared key length')
    return key

def _dh_private_bits_from_env()->int:
    """Keep client DH work small while preserving a valid random public key."""
    try:
        bits=int(os.environ.get('FANQIE_DH_PRIVATE_BITS','128'))
    except (TypeError,ValueError):
        bits=128
    return max(128,min(2048,bits))


DH_PRIVATE_BITS=_dh_private_bits_from_env()

def make_encrypt_context()->Tuple[int,str]:
    # The API only receives y=g^x mod p.  A 128-bit random x is valid for the
    # protocol and makes each client-side shared-secret pow far cheaper than a
    # full-width 2048-bit private exponent, especially on small servers.
    x=secrets.randbits(DH_PRIVATE_BITS)|(1<<(DH_PRIVATE_BITS-1))|1
    y=pow(DHG,x,DHP); yb=java_bigint_bytes(y)
    iv=os.urandom(16); enc=aes_cbc_encrypt(yb,DHAES_TOKEN,iv)
    return x, base64.b64encode(iv+enc).decode()

def decrypt_novelfm_content(content_b64:str, server_y_b64:str, client_x:int)->str:
    """Decrypt one chapter without an extra Python package or helper file."""
    iv=content_b64.encode('utf-8')[:16]
    ciphertext=base64.b64decode(content_b64)
    server_y=int.from_bytes(base64.b64decode(server_y_b64),'big')
    shared=pow(server_y,int(client_x),DHP)
    key=strip_leading_zero_to_32(java_bigint_bytes(shared))
    plaintext=aes_cbc_decrypt(ciphertext,key,iv)
    return plaintext[16:].decode('utf-8','replace')

# App requests use HTTP/2.  The standard-library downloader can speak the
# compact subset this endpoint needs without pulling in httpx/h2 or a binary.
FULL_MGET_TRANSPORT=os.environ.get('FANQIE_FULL_MGET_TRANSPORT','auto').lower()
if FULL_MGET_TRANSPORT not in {'auto','http1','http2'}:
    FULL_MGET_TRANSPORT='auto'
DEFAULT_FULL_MGET_MAX_ITEMS=1500
FULL_MGET_HARD_MAX_ITEMS=3000

def set_full_mget_transport(mode:str)->None:
    global FULL_MGET_TRANSPORT
    mode=(mode or 'auto').lower()
    if mode not in {'auto','http1','http2'}:
        raise ValueError('transport 必须是 auto/http1/http2')
    FULL_MGET_TRANSPORT=mode

class FullMgetHttp1Session:
    """Small pure-stdlib HTTP/1.1 keep-alive client for sequential full/mget.

    urllib opens/closes too eagerly for our batched full/mget loop.  This class
    keeps one HTTPSConnection per host and reuses it across batches.  It is not
    shared across threads.
    """
    def __init__(self, timeout:int=60):
        self.timeout=int(timeout)
        self._conns:Dict[Tuple[str,int],http.client.HTTPSConnection]={}

    def close(self)->None:
        for conn in list(self._conns.values()):
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()

    def _drop(self, key:Tuple[str,int])->None:
        conn=self._conns.pop(key,None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _conn(self, host:str, port:int, timeout:int)->http.client.HTTPSConnection:
        key=(host,port)
        conn=self._conns.get(key)
        if conn is None:
            context=ssl.create_default_context()
            try:
                context.set_alpn_protocols(['http/1.1'])
            except Exception:
                pass
            conn=http.client.HTTPSConnection(host,port,timeout=timeout,context=context)
            self._conns[key]=conn
        return conn

    def post_bytes(self, url:str, headers:Dict[str,str], data:bytes, *,
                   timeout:Optional[int]=None, retries:int=3)->bytes:
        split=urllib.parse.urlsplit(url)
        if split.scheme!='https' or not split.hostname:
            raise ValueError('HTTP/1.1 keep-alive transport only supports absolute HTTPS URLs')
        host=split.hostname
        port=split.port or 443
        key=(host,port)
        path=(split.path or '/')+(('?'+split.query) if split.query else '')
        timeout=int(timeout or self.timeout)
        last:Optional[BaseException]=None
        for attempt in range(1,max(1,retries)+1):
            try:
                conn=self._conn(host,port,timeout)
                req_headers=dict(headers)
                req_headers['Host']=split.netloc
                req_headers['Content-Length']=str(len(data))
                req_headers.setdefault('Connection','keep-alive')
                conn.request('POST',path,body=data,headers=req_headers)
                resp=conn.getresponse()
                try:
                    raw=resp.read()
                    enc=(resp.getheader('Content-Encoding') or '').lower()
                    if enc=='gzip' or raw[:2]==b'\x1f\x8b':
                        raw=gzip.decompress(raw)
                    return raw
                finally:
                    if getattr(resp,'will_close',False):
                        self._drop(key)
            except Exception as e:
                last=e
                self._drop(key)
                if attempt>=max(1,retries):
                    break
                time.sleep(0.25*attempt)
        raise last  # type: ignore[misc]

    def post_json(self, url:str, headers:Dict[str,str], data:bytes, *,
                  timeout:Optional[int]=None, retries:int=3)->Any:
        raw=self.post_bytes(url,headers,data,timeout=timeout,retries=retries)
        if not raw:
            return {}
        return orjson.loads(raw) if orjson is not None else json.loads(raw.decode('utf-8','replace'))

def _h2_frame(frame_type:int, flags:int, stream_id:int, payload:bytes=b'')->bytes:
    return len(payload).to_bytes(3,'big')+bytes([frame_type&0xff,flags&0xff])+struct.pack('>I',stream_id&0x7fffffff)+payload

def _h2_recv_exact(sock:ssl.SSLSocket, size:int)->bytes:
    parts=[]
    while size:
        chunk=sock.recv(size)
        if not chunk:
            raise EOFError('HTTP/2 connection closed before response completed')
        parts.append(chunk)
        size-=len(chunk)
    return b''.join(parts)

def _hpack_int(value:int, prefix_bits:int, prefix:int=0)->bytes:
    limit=(1<<prefix_bits)-1
    if value<limit:
        return bytes([prefix|value])
    out=bytearray([prefix|limit])
    value-=limit
    while value>=128:
        out.append((value&0x7f)|0x80)
        value>>=7
    out.append(value)
    return bytes(out)

def _hpack_string(value:str|bytes)->bytes:
    raw=value.encode('utf-8') if isinstance(value,str) else bytes(value)
    # High bit remains clear: sending literal strings avoids a Huffman table.
    return _hpack_int(len(raw),7)+raw

def _hpack_literal_header(name:str, value:str)->bytes:
    # Literal Header Field without Indexing, new-name representation.
    return _hpack_int(0,4)+_hpack_string(name.lower())+_hpack_string(value)

class FullMgetHttp2Session:
    """Sequential HTTP/2 keep-alive transport for repeated full/mget calls.

    One instance belongs to one caller thread.  It deliberately keeps requests
    sequential: parallel callers each create their own session, while a worker
    that receives several 3000-item batches avoids repeating TLS and HTTP/2
    setup for every batch.
    """
    def __init__(self, timeout:int=60):
        self.timeout=int(timeout)
        self._sock:Optional[ssl.SSLSocket]=None
        self._host=''
        self._port=0
        self._next_stream_id=1

    def close(self)->None:
        sock,self._sock=self._sock,None
        self._host=''
        self._port=0
        self._next_stream_id=1
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _connect(self, host:str, port:int, timeout:int)->None:
        if self._sock is not None and self._host==host and self._port==port:
            return
        self.close()
        tcp:Optional[socket.socket]=None
        try:
            context=ssl.create_default_context()
            context.set_alpn_protocols(['h2'])
            tcp=socket.create_connection((host,port),timeout=timeout)
            sock=context.wrap_socket(tcp,server_hostname=host)
            tcp=None
            sock.settimeout(timeout)
            if sock.selected_alpn_protocol()!='h2':
                sock.close()
                raise RuntimeError('server did not negotiate HTTP/2')
            sock.sendall(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
            h2_window=16*1024*1024
            sock.sendall(_h2_frame(4,0,0,struct.pack('>HI',4,h2_window)))
            sock.sendall(_h2_frame(8,0,0,struct.pack('>I',h2_window-65535)))
            self._sock=sock
            self._host=host
            self._port=port
        finally:
            if tcp is not None:
                tcp.close()

    def post_bytes(self, url:str, headers:Dict[str,str], data:bytes, *,
                   timeout:Optional[int]=None, retries:int=3)->bytes:
        split=urllib.parse.urlsplit(url)
        if split.scheme!='https' or not split.hostname:
            raise ValueError('HTTP/2 keep-alive transport only supports absolute HTTPS URLs')
        host=split.hostname
        port=split.port or 443
        path=(split.path or '/')+(('?'+split.query) if split.query else '')
        timeout=int(timeout or self.timeout)
        last:Optional[BaseException]=None
        for attempt in range(1,max(1,retries)+1):
            try:
                self._connect(host,port,timeout)
                sock=self._sock
                if sock is None:
                    raise RuntimeError('HTTP/2 connection was not created')
                stream_id=self._next_stream_id
                self._next_stream_id+=2
                pairs=[
                    (':method','POST'),
                    (':scheme','https'),
                    (':authority',split.netloc),
                    (':path',path),
                ]
                seen=set()
                forbidden={'connection','host','keep-alive','proxy-connection','transfer-encoding','upgrade'}
                for key,value in headers.items():
                    name=key.lower()
                    if name in forbidden or name.startswith(':'):
                        continue
                    seen.add(name)
                    pairs.append((name,str(value)))
                if 'content-length' not in seen:
                    pairs.append(('content-length',str(len(data))))
                block=b''.join(_hpack_literal_header(name,value) for name,value in pairs)
                if len(block)>16_384:
                    raise ValueError('HTTP/2 header block exceeds supported frame size')
                sock.sendall(_h2_frame(1,0x04|(0x01 if not data else 0),stream_id,block))
                for offset in range(0,len(data),16_384):
                    chunk=data[offset:offset+16_384]
                    flags=0x01 if offset+len(chunk)==len(data) else 0
                    sock.sendall(_h2_frame(0,flags,stream_id,chunk))

                response=bytearray()
                while True:
                    header=_h2_recv_exact(sock,9)
                    payload_size=int.from_bytes(header[:3],'big')
                    frame_type=header[3]
                    flags=header[4]
                    frame_stream=struct.unpack('>I',header[5:9])[0]&0x7fffffff
                    payload=_h2_recv_exact(sock,payload_size)
                    if frame_type==4:
                        if not (flags&0x01):
                            sock.sendall(_h2_frame(4,0x01,0))
                        continue
                    if frame_type==6:
                        if not (flags&0x01):
                            sock.sendall(_h2_frame(6,0x01,0,payload))
                        continue
                    if frame_type==0 and frame_stream==stream_id:
                        flow_bytes=len(payload)
                        if flags&0x08:
                            if not payload:
                                raise RuntimeError('invalid padded HTTP/2 DATA frame')
                            pad_len=payload[0]
                            if pad_len>=len(payload):
                                raise RuntimeError('invalid HTTP/2 DATA padding')
                            payload=payload[1:len(payload)-pad_len]
                        response.extend(payload)
                        if flow_bytes:
                            increment=struct.pack('>I',flow_bytes)
                            sock.sendall(_h2_frame(8,0,0,increment))
                            sock.sendall(_h2_frame(8,0,stream_id,increment))
                        if flags&0x01:
                            return bytes(response)
                        continue
                    if frame_type==1 and frame_stream==stream_id and (flags&0x01):
                        return bytes(response)
                    if frame_type==3 and frame_stream==stream_id:
                        code=int.from_bytes(payload[:4],'big') if len(payload)>=4 else -1
                        raise RuntimeError(f'HTTP/2 RST_STREAM code={code}')
                    if frame_type==7:
                        raise RuntimeError('HTTP/2 GOAWAY received')
            except Exception as e:
                last=e
                self.close()
                if attempt>=max(1,retries):
                    break
                time.sleep(0.25*attempt)
        raise last  # type: ignore[misc]

    def post_json(self, url:str, headers:Dict[str,str], data:bytes, *,
                  timeout:Optional[int]=None, retries:int=3)->Any:
        raw=self.post_bytes(url,headers,data,timeout=timeout,retries=retries)
        if raw[:2]==b'\x1f\x8b':
            raw=gzip.decompress(raw)
        if not raw:
            return {}
        return orjson.loads(raw) if orjson is not None else json.loads(raw.decode('utf-8','replace'))

def _http2_post_bytes(url:str, headers:Dict[str,str], data:bytes, timeout:int=60)->bytes:
    """Issue one HTTPS HTTP/2 POST and return its raw response body."""
    split=urllib.parse.urlsplit(url)
    if split.scheme!='https' or not split.hostname:
        raise ValueError('HTTP/2 transport only supports absolute HTTPS URLs')
    host=split.hostname
    port=split.port or 443
    path=(split.path or '/')+(('?'+split.query) if split.query else '')
    tcp:Optional[socket.socket]=None
    sock:Optional[ssl.SSLSocket]=None
    try:
        context=ssl.create_default_context()
        context.set_alpn_protocols(['h2'])
        tcp=socket.create_connection((host,port),timeout=timeout)
        sock=context.wrap_socket(tcp,server_hostname=host)
        tcp=None
        sock.settimeout(timeout)
        if sock.selected_alpn_protocol()!='h2':
            raise RuntimeError('server did not negotiate HTTP/2')

        sock.sendall(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n')
        # A larger per-stream receive window prevents a 64 KiB pause on
        # multi-chapter responses.  HTTP/2 also has a separate connection
        # window; if it stays at the default 65 KiB the server must wait for a
        # round-trip every few DATA frames, which makes 1000+ chapter mget
        # responses look much slower than the App/Cronet capture.  Open both
        # windows up-front, then still replenish credits as frames are read.
        h2_window=16*1024*1024
        sock.sendall(_h2_frame(4,0,0,struct.pack('>HI',4,h2_window)))
        sock.sendall(_h2_frame(8,0,0,struct.pack('>I',h2_window-65535)))

        pairs=[
            (':method','POST'),
            (':scheme','https'),
            (':authority',split.netloc),
            (':path',path),
        ]
        seen=set()
        forbidden={'connection','host','keep-alive','proxy-connection','transfer-encoding','upgrade'}
        for key,value in headers.items():
            name=key.lower()
            if name in forbidden or name.startswith(':'):
                continue
            seen.add(name)
            pairs.append((name,str(value)))
        if 'content-length' not in seen:
            pairs.append(('content-length',str(len(data))))
        block=b''.join(_hpack_literal_header(name,value) for name,value in pairs)
        if len(block)>16_384:
            raise ValueError('HTTP/2 header block exceeds supported frame size')
        sock.sendall(_h2_frame(1,0x04|(0x01 if not data else 0),1,block))
        for offset in range(0,len(data),16_384):
            chunk=data[offset:offset+16_384]
            flags=0x01 if offset+len(chunk)==len(data) else 0
            sock.sendall(_h2_frame(0,flags,1,chunk))

        response=bytearray()
        while True:
            header=_h2_recv_exact(sock,9)
            payload_size=int.from_bytes(header[:3],'big')
            frame_type=header[3]
            flags=header[4]
            stream_id=struct.unpack('>I',header[5:9])[0]&0x7fffffff
            payload=_h2_recv_exact(sock,payload_size)
            if frame_type==4:  # SETTINGS
                if not (flags&0x01):
                    sock.sendall(_h2_frame(4,0x01,0))
                continue
            if frame_type==6:  # PING
                if not (flags&0x01):
                    sock.sendall(_h2_frame(6,0x01,0,payload))
                continue
            if frame_type==0 and stream_id==1:  # DATA
                flow_bytes=len(payload)
                if flags&0x08:  # PADDED
                    if not payload:
                        raise RuntimeError('invalid padded HTTP/2 DATA frame')
                    pad_len=payload[0]
                    if pad_len>=len(payload):
                        raise RuntimeError('invalid HTTP/2 DATA padding')
                    payload=payload[1:len(payload)-pad_len]
                response.extend(payload)
                if flow_bytes:
                    increment=struct.pack('>I',flow_bytes)
                    sock.sendall(_h2_frame(8,0,0,increment))
                    sock.sendall(_h2_frame(8,0,1,increment))
                if flags&0x01:
                    return bytes(response)
                continue
            if frame_type==1 and stream_id==1 and (flags&0x01):
                return bytes(response)
            if frame_type==3 and stream_id==1:
                code=int.from_bytes(payload[:4],'big') if len(payload)>=4 else -1
                raise RuntimeError(f'HTTP/2 RST_STREAM code={code}')
            if frame_type==7:
                raise RuntimeError('HTTP/2 GOAWAY received')
    finally:
        if sock is not None:
            sock.close()
        elif tcp is not None:
            tcp.close()

def new_full_mget_session(timeout:int=60)->Any:
    """Return the reusable transport matching the selected full/mget mode."""
    if FULL_MGET_TRANSPORT=='http1':
        return FullMgetHttp1Session(timeout)
    return FullMgetHttp2Session(timeout)

def full_mget_http_json(url:str, headers:Dict[str,str], data:bytes, timeout:int=60,
                        retries:int=3, session:Optional[Any]=None)->Any:
    """Use HTTP/2 for full/mget when available, then fall back to urllib."""
    if session is not None:
        return session.post_json(url,headers,data,timeout=timeout,retries=retries)
    if FULL_MGET_TRANSPORT in {'auto','http2'}:
        try:
            raw=_http2_post_bytes(url,headers,data,timeout)
            if raw[:2]==b'\x1f\x8b':
                raw=gzip.decompress(raw)
            if not raw:
                return {}
            return orjson.loads(raw) if orjson is not None else json.loads(raw.decode('utf-8','replace'))
        except Exception:
            if FULL_MGET_TRANSPORT=='http2':
                raise
    return http_json_bytes(url,'POST',headers,data,timeout=timeout,retries=retries)

def _open_with_retries(req:urllib.request.Request, timeout:int=30, retries:int=3, backoff:float=0.8)->bytes:
    last:Optional[BaseException]=None
    for attempt in range(1, max(1, retries)+1):
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r:
                raw=r.read()
                if r.headers.get('Content-Encoding','').lower()=='gzip': raw=gzip.decompress(raw)
                return raw
        except Exception as e:
            last=e
            if attempt >= max(1, retries):
                break
            time.sleep(backoff * attempt)
    raise last  # type: ignore[misc]

def http_json(url:str, method='GET', headers:Optional[Dict[str,str]]=None, body:Any=None, timeout=30, retries:int=3)->Any:
    data=None
    h=dict(headers or {})
    if body is not None:
        data=orjson.dumps(body) if orjson is not None else json.dumps(body,separators=(',',':'),ensure_ascii=False).encode('utf-8')
        h.setdefault('Content-Type','application/json; charset=utf-8')
    req=urllib.request.Request(url,data=data,headers=h,method=method)
    raw=_open_with_retries(req,timeout=timeout,retries=retries)
    if not raw: return {}
    return orjson.loads(raw) if orjson is not None else json.loads(raw.decode('utf-8','replace'))

def json_body_bytes(body:Any)->bytes:
    return orjson.dumps(body) if orjson is not None else json.dumps(body,separators=(',',':'),ensure_ascii=False).encode('utf-8')

def http_json_bytes(url:str, method='POST', headers:Optional[Dict[str,str]]=None, data:bytes=b'', timeout=30, retries:int=3)->Any:
    h=dict(headers or {})
    h.setdefault('Content-Type','application/json; charset=utf-8')
    req=urllib.request.Request(url,data=data,headers=h,method=method)
    raw=_open_with_retries(req,timeout=timeout,retries=retries)
    if not raw: return {}
    return orjson.loads(raw) if orjson is not None else json.loads(raw.decode('utf-8','replace'))

def make_url(path:str, query:Dict[str,str], *, host:str=API_HOST)->str:
    q=dict(DEFAULT_QUERY); q['_rticket']=str(int(time.time()*1000)); q.update(query)
    if not path.startswith('/'):
        path='/'+path
    return host+path+'?'+urllib.parse.urlencode(q)

def make_app_headers(url:str, body_bytes:bytes=b'', sign_mode:str='auto')->Dict[str,str]:
    """Build pure-Python App JSON headers for novelfm RPC endpoints."""
    mode=(sign_mode or 'auto').lower()
    headers=dict(APP_COMMON_HEADERS)
    if mode in {'auto','pure3040'}:
        # accepted881 is the currently verified pure-Python profile.
        # Keep khronos fixed until latest dynamic fields are fully recovered.
        headers.update(build_pure3040_headers(url, body_bytes, khronos=1_783_204_357))
    elif mode=='legacy3040':
        headers.update(build_pure3040_legacy_headers(url, body_bytes))
    elif mode=='fixed':
        headers.update(FULL_MGET_SIGN_HEADERS)
    elif mode in {'captured','appcaptured'}:
        cap=load_captured_pool_request()
        if cap:
            headers.update(cap[1])
    else:
        raise ValueError('sign_mode must be auto/fixed/pure3040/legacy3040/captured/appcaptured')
    return headers

def signed_app_json(path:str, body:Any=None, query:Optional[Dict[str,str]]=None, *,
                    method:str='POST', sign_mode:str='auto', timeout:int=60,
                    host:str=API_HOST, unsigned_fallback:bool=True)->Any:
    """Request an App RPC JSON endpoint with pure-Python signing."""
    url=make_url(path, query or {}, host=host)
    method=method.upper()
    body_bytes=b'' if body is None else json_body_bytes(body)
    try:
        return http_json_bytes(url, method, make_app_headers(url, body_bytes, sign_mode), body_bytes, timeout=timeout)
    except Exception:
        if not unsigned_fallback:
            raise
        # Some business endpoints do not enforce metasec; keep an unsigned fallback.
        h={k:v for k,v in APP_COMMON_HEADERS.items() if method!='GET' or k.lower()!='content-type'}
        if body is None:
            return http_json(url, method, h, None, timeout=timeout)
        return http_json_bytes(url, method, h, body_bytes, timeout=timeout)

def resolve_book_id(value:str)->str:
    """从 book_id、番茄小说链接、App schema/share 文本中提取 book_id。

    现有正文接口本质只需要 book_id + item_id 列表；不同频道（看书/短篇/
    出版物/故事等）只要仍是书籍详情页或 schema 里的 book_id，都走同一套
    full/mget 下载链。
    """
    text=(value or '').strip()
    if not text:
        raise ValueError('empty book id/url')
    if re.fullmatch(r'\d{8,}', text):
        return text
    # 常见 App/schema/share 参数：book_id=、bookId=、bookid=
    m=re.search(r'(?i)(?:book[_-]?id|bookId)=([0-9]{8,})', text)
    if m:
        return m.group(1)
    # Shared links may contain a book ID in the URL path.
    m=re.search(r'fanqienovel\.com/(?:page|book)/([0-9]{8,})', text)
    if m:
        return m.group(1)
    # 有些分享文本里只暴露 /page/数字 或 aweme schema 内的 page%2F数字
    m=re.search(r'(?:/|%2[fF])(?:page|book)(?:/|%2[fF])([0-9]{8,})', text)
    if m:
        return m.group(1)
    # 最后兜底：如果整段文本只有一个长数字，认为是 book_id。
    ids=re.findall(r'\d{16,}', text)
    uniq=[]
    for x in ids:
        if x not in uniq:
            uniq.append(x)
    if len(uniq)==1:
        return uniq[0]
    raise ValueError(f'无法从输入提取 book_id: {value!r}')

def replace_query_param(url:str, key:str, value:str)->str:
    sp=urllib.parse.urlsplit(url)
    parts=sp.query.split('&') if sp.query else []
    out=[]; done=False
    for item in parts:
        k=item.split('=',1)[0]
        if k==key:
            out.append(f'{key}={value}'); done=True
        else:
            out.append(item)
    if not done:
        out.append(f'{key}={value}')
    return urllib.parse.urlunsplit((sp.scheme,sp.netloc,sp.path,'&'.join(out),sp.fragment))

def fresh_full_mget_signed_url(base_url:str=FULL_MGET_SIGNED_URL)->str:
    return replace_query_param(base_url,'_rticket',str(int(time.time()*1000)))

def _u32le(n:int)->bytes:
    return int(n & 0xFFFFFFFF).to_bytes(4,'little')

def _ror64(value:int,count:int)->int:
    value &= 0xFFFFFFFFFFFFFFFF; count &= 63
    return ((value>>count)|(value<<(64-count))) & 0xFFFFFFFFFFFFFFFF

def _helios3040_encrypt_block(hash_table:List[int], block16:bytes)->bytes:
    data0=int.from_bytes(block16[:8],'little')
    data1=int.from_bytes(block16[8:],'little')
    for i in range(0x22):
        data1=(hash_table[i]^((data0+_ror64(data1,8))&0xFFFFFFFFFFFFFFFF))&0xFFFFFFFFFFFFFFFF
        data0=(data1^_ror64(data0,61))&0xFFFFFFFFFFFFFFFF
    return data0.to_bytes(8,'little')+data1.to_bytes(8,'little')

def x_helios_3040(khronos:int, rand32:Optional[int]=None)->str:
    """纯 Python 生成 3040 / app 6.5.6.32 的 X-Helios。

    已用 out/medusa_oracle_batch.jsonl 八组样本校验：
      base64(uint32_le(rand) || enc(pkcs7(f"{khronos}-1532254240-3040")))
    """
    if rand32 is None:
        rand32=secrets.randbits(32)
    rand32 &= 0xFFFFFFFF
    md5=hashlib.md5(_u32le(rand32)+b'3040').digest()
    hex_table=b'0123456789abcdef'
    keybuf=bytearray(32)
    for i,v in enumerate(md5):
        keybuf[2*i]=hex_table[v>>4]; keybuf[2*i+1]=hex_table[v&15]
    words=[int.from_bytes(keybuf[i:i+8],'little') for i in range(0,32,8)]
    hash_table=[words[0]]
    b0,b8=words[0],words[1]
    q=words[2:]
    for i in range(0x22):
        x=((_ror64(b8,8)+b0)^i)&0xFFFFFFFFFFFFFFFF
        q.append(x)
        x=(x^_ror64(b0,61))&0xFFFFFFFFFFFFFFFF
        hash_table.append(x)
        b0=x; b8=q.pop(0)
    raw=f'{int(khronos)}-1532254240-3040'.encode('ascii')
    pad=16-len(raw)%16
    raw+=bytes([pad])*pad
    enc=b''.join(_helios3040_encrypt_block(hash_table,raw[i:i+16]) for i in range(0,len(raw),16))
    return base64.b64encode(_u32le(rand32)+enc).decode()

def _pure3040_rand31() -> int:
    return secrets.randbelow(0x80000000)

def build_pure3040_656_url(epoch_ms:int)->str:
    # Use the 6.5.6.32 api5-sinfonlinec.novelfm.com query profile that matches the
    # recovered accepted881 Medusa source family. The fully current 891-byte
    # family is body-bound; downloader defaults to this known code=0 profile.
    _ = epoch_ms
    return FULL_MGET_SIGNED_URL

def build_pure3040_headers(url:str, body_bytes:bytes, *, khronos:int|None=None)->Dict[str,str]:
    """纯 Python 生成 3040 full/mget metasec 头。

    使用本项目内的 stage336 -> stage33f -> stage34a/control32(3892) 管线；
    不再调用外部项目、App、adb、frida 或 so。
    """
    # 6.5.6.32 / aid=3040 的 accepted881 profile。实测 top_rand 只进入
    # X-Helios；X-Medusa 由 recovered source336 family 在 Python 内生成。
    if khronos is None:
        khronos=int(time.time())
    top_rand=_pure3040_rand31()
    # Server-accepted 6.5.6.32 / full/mget 881-byte Medusa profile.  These
    # values are not copied headers: X-Medusa is assembled in pure Python from
    # the recovered source336 family for the paired URL/khronos profile.
    ladon_raw=bytes.fromhex("d2da60f1")
    return {
        "X-Khronos": str(khronos),
        # App full/mget sends an empty X-SS-STUB for this request family.
        "X-SS-STUB": "",
        "X-Argus": x_argus(khronos),
        # 当前 App 返回的 X-Ladon 是 4 字节短串；同一 raw 值也进入
        # source336.metrics.fl 最后一项。这里先保持 Medusa 内外一致。
        "X-Ladon": base64.b64encode(ladon_raw).decode(),
        "X-Helios": x_helios_3040(khronos, top_rand),
        "X-Medusa": x_medusa_3040_full_mget_accepted881(
            url,
            khronos=khronos,
            ladon_raw=ladon_raw,
        ),
    }

def build_pure3040_legacy_headers(url:str, body_bytes:bytes)->Dict[str,str]:
    """旧 6.4.4.32 code=0 profile：保留为可用基线/自动兜底。"""
    return {
        "X-Khronos": str(LEGACY_CODE0_3040_KHRONOS),
        "X-Argus": x_argus(LEGACY_CODE0_3040_KHRONOS),
        "X-Helios": x_helios_3040(LEGACY_CODE0_3040_KHRONOS, 0x64C6140C),
        "X-Medusa": x_medusa_3040_full_mget_legacy_code0(),
    }

def load_captured_pool_requests(limit:int=8, *, include_legacy:bool=True)->List[Tuple[str,Dict[str,str]]]:
    """返回内置已复测 code=0 的 App full/mget 头池；不读取外部文件。"""
    out:List[Tuple[str,Dict[str,str]]]=[]
    if APP656_CAPTURED_SIGNED_URL and APP656_CAPTURED_SIGN_HEADERS:
        out.append((APP656_CAPTURED_SIGNED_URL, dict(APP656_CAPTURED_SIGN_HEADERS)))
    if CAPTURED_CODE0_SIGNED_URL and CAPTURED_CODE0_SIGN_HEADERS:
        if include_legacy:
            out.append((CAPTURED_CODE0_SIGNED_URL, dict(CAPTURED_CODE0_SIGN_HEADERS)))
    dedup=[]
    seen=set()
    for url,h in out:
        key=(url,h.get('X-Medusa') or h.get('x-medusa') or '')
        if key in seen:
            continue
        seen.add(key)
        dedup.append((url,h))
        if len(dedup) >= limit:
            break
    return dedup

def load_captured_pool_request()->Optional[Tuple[str,Dict[str,str]]]:
    pool=load_captured_pool_requests(limit=1, include_legacy=False)
    return pool[0] if pool else None

def full_mget_request_options(body_bytes:bytes, sign_mode:str='auto')->List[Tuple[str,Dict[str,str],str]]:
    mode=(sign_mode or 'auto').lower()
    if mode not in {'auto','fixed','pure3040','legacy3040','captured','appcaptured'}:
        raise ValueError('sign_mode 必须是 auto/fixed/pure3040/legacy3040/captured/appcaptured')
    out:List[Tuple[str,Dict[str,str],str]]=[]
    # auto 必须保持“纯 Python、零依赖”：只走本文件内生成的签名链路。
    # App/抓包头仅在显式指定 appcaptured/captured 时用于调试，不参与默认路径。
    if mode in {'auto','pure3040'}:
        epoch_ms=int(time.time()*1000)
        # Paired accepted881 timestamp.  Current891 can refresh khronos but is
        # body-bound; accepted881 remains the downloader-safe pure Python path.
        khronos=1_783_204_357
        url=build_pure3040_656_url(epoch_ms)
        headers={**APP_COMMON_HEADERS, **build_pure3040_headers(url,body_bytes,khronos=khronos)}
        out.append(('pure3040',headers,url))
    if mode in {'auto','legacy3040','pure3040'}:
        url=CAPTURED_CODE0_SIGNED_URL
        headers={**APP_COMMON_HEADERS, **build_pure3040_legacy_headers(url,body_bytes)}
        out.append(('pure3040-legacy',headers,url))
    if mode in {'appcaptured'}:
        cap=load_captured_pool_request()
        if cap:
            url,captured_headers=cap
            headers={**APP_COMMON_HEADERS, **captured_headers}
            out.append(('app-captured656',headers,url))
    if mode in {'captured'}:
        for idx,(url,captured_headers) in enumerate(load_captured_pool_requests(limit=8, include_legacy=True),1):
            headers={**APP_COMMON_HEADERS, **captured_headers}
            out.append((f'captured#{idx}',headers,url))
    if mode in {'fixed'}:
        headers={**APP_COMMON_HEADERS, **FULL_MGET_SIGN_HEADERS}
        out.append(('fixed',headers,FULL_MGET_SIGNED_URL))
    return out

def sanitize(name:str)->str:
    return (re.sub(r'[\\/:*?"<>|\r\n\t]+','_',name).strip(' .') or 'untitled')[:120]

def html_to_text(doc:str)->str:
    doc=re.sub(r'(?is)<(script|style).*?>.*?</\1>','',doc)
    paras=re.findall(r'(?is)<p\b[^>]*>(.*?)</p>',doc)
    if paras:
        lines=[]
        for p in paras:
            p=re.sub(r'(?is)<br\s*/?>','\n',p); p=re.sub(r'(?is)<[^>]+>','',p); p=html.unescape(p).strip()
            if p: lines.append(p)
        return '\n\n'.join(lines).strip()+'\n'
    text=re.sub(r'(?is)<br\s*/?>','\n',doc); text=re.sub(r'(?is)</p\s*>','\n\n',text); text=re.sub(r'(?is)<[^>]+>','',text)
    return re.sub(r'\n{3,}','\n\n',html.unescape(text)).strip()+'\n'

def batches(xs:List[str], n:int):
    for i in range(0,len(xs),n): yield xs[i:i+n]

def book_detail(book_id:str)->Dict[str,Any]:
    data=http_json(make_url('/novelfm/bookapi/detail/v1/',{'book_id':book_id}),headers={'User-Agent':DEFAULT_UA,'Accept-Encoding':'gzip'})
    return data.get('data') or {}

def unique_item_ids(ids:Iterable[Any], book_id:str='')->List[str]:
    out=[]; seen=set()
    for x in ids:
        s=str(x).strip()
        if not re.fullmatch(r'\d{8,}', s): continue
        if book_id and s==str(book_id): continue
        if s in seen: continue
        seen.add(s); out.append(s)
    return out

def read_items_file(path:Path, book_id:str='')->List[str]:
    """读取手动目录文件：支持一行一个 item_id、逗号/空白分隔、JSON list、chapters.json。"""
    raw=path.read_text('utf-8').strip()
    ids:List[Any]=[]
    if not raw:
        return []
    try:
        obj=json.loads(raw)
        if isinstance(obj, list):
            for x in obj:
                if isinstance(x, dict):
                    ids.append(x.get('item_id') or x.get('itemId') or x.get('id'))
                else:
                    ids.append(x)
        elif isinstance(obj, dict):
            arr=obj.get('item_ids') or obj.get('itemIds') or obj.get('chapters') or obj.get('data') or []
            if isinstance(arr, dict):
                arr=list(arr.values())
            if isinstance(arr, list):
                for x in arr:
                    if isinstance(x, dict):
                        ids.append(x.get('item_id') or x.get('itemId') or x.get('id'))
                    else:
                        ids.append(x)
        elif isinstance(obj, (str, int)):
            ids.append(obj)
    except Exception:
        ids=re.findall(r'\d{8,}', raw)
    if not ids:
        ids=re.findall(r'\d{8,}', raw)
    # items-file 是用户/抓包明确提供的 item_id 列表；部分非书籍内容
    # （例如头条/单条内容）会出现 item_id == book_id，不能在这里过滤掉。
    return unique_item_ids(ids, '')

def resolve_directory(book_id:str, source:str='app', items_file:Optional[Path]=None)->List[str]:
    """Resolve chapter item IDs from the App directory API or a supplied file."""
    if source in ('file','auto') and items_file:
        ids=read_items_file(items_file, book_id)
        if ids:
            return ids
        if source=='file':
            raise RuntimeError(f'items file has no usable item_id: {items_file}')
    if source in ('app','auto'):
        for ver in (2,1):
            try:
                ids,_raw=app_directory_items(book_id,version=ver,sign_mode='auto')
                if ids:
                    return ids
            except Exception:
                if source=='app' and ver==1:
                    raise
        if source=='app':
            raise RuntimeError('App directory endpoint returned no item_id')
    raise RuntimeError('no App directory available; use --directory-source app or provide --items-file')

def directory_infos(book_id:str,item_ids:List[str], sign_mode:str='auto')->Dict[str,Dict[str,Any]]:
    """App /directory/all_infos: batch chapter metadata."""
    body={'book_id':str(book_id),'item_ids':[str(i) for i in item_ids],'page_scene':6}
    data=signed_app_json('/novelfm/bookapi/directory/all_infos/v1/', body, sign_mode=sign_mode)
    rows=(data.get('data') if isinstance(data,dict) else None) or []
    return {str(x.get('item_id')):x for x in rows if isinstance(x,dict) and x.get('item_id')}

def app_directory_items(book_id:str, *, page_scene:int=6, version:int=2, sign_mode:str='auto')->Tuple[List[str],Dict[str,Any]]:
    """Try App directory item_id endpoints and return raw response for comparison."""
    path='/novelfm/bookapi/directory/all_items_v2/v1/' if version==2 else '/novelfm/bookapi/directory/all_items/v1/'
    query={'book_id':str(book_id),'page_scene':str(page_scene)}
    data=signed_app_json(path, None, query, method='GET', sign_mode=sign_mode)
    ids:List[Any]=[]
    if isinstance(data,dict):
        d=data.get('data')
        candidates=[d, data]
        for obj in candidates:
            if isinstance(obj,dict):
                for key in ('item_ids','itemIds','chapter_ids','chapterIds','ids'):
                    v=obj.get(key)
                    if isinstance(v,list):
                        ids.extend(v)
                for key in ('items','chapters','list','chapter_list','item_data_list','item_list','data_list'):
                    v=obj.get(key)
                    if isinstance(v,list):
                        for it in v:
                            if isinstance(it,dict):
                                ids.append(it.get('item_id') or it.get('itemId') or it.get('id'))
            elif isinstance(obj,list):
                for it in obj:
                    if isinstance(it,dict):
                        ids.append(it.get('item_id') or it.get('itemId') or it.get('id'))
                    else:
                        ids.append(it)
    return unique_item_ids(ids, book_id), data if isinstance(data,dict) else {'raw':data}

AUDIO_TYPE_VALUES={
    'book':0,'news':1,'xigua':2,'karaoke':3,'radio':4,'douyin':5,'douyinmusic':6,
    'douyin_for_recommend_book':7,'short_play':8,'shortplay':8,'podcast':9,
    'musicvideo':10,'toutiaomusicvideo':11,'toutiaostoryvideo':12,'toutiaougcvideo':13,
    'toutiaocollectionvideo':14,'short_story_tts':15,'short_story_audio':16,
}

def audio_type_value(value:Any)->int:
    if isinstance(value,int):
        return value
    s=str(value or 'book').strip().lower().replace('-','_')
    if s.isdigit():
        return int(s)
    if s not in AUDIO_TYPE_VALUES:
        raise ValueError(f'?? audio_type: {value!r}')
    return AUDIO_TYPE_VALUES[s]

def video_model_mget(book_id:str, item_ids:List[str], *, audio_type:Any='short_play', tone_id:int=91,
                     source:str='default', sign_mode:str='auto', device_score:float=0.0,
                     bgm_used:int=0)->Dict[str,Any]:
    """App /playerapi/video_model/mget for audio/video model metadata."""
    body={
        'item_ids':[str(i) for i in item_ids],
        'tone_id':int(tone_id),
        'audio_type':audio_type_value(audio_type),
        'book_id':str(book_id) if book_id else None,
        'source':source or 'default',
        'is_retry':False,
        'bgm_used':int(bgm_used),
        'user_select_start_para':0,
        'user_select_start_para_off':0,
        'multi_shift':False,
        'device_score':float(device_score),
    }
    return signed_app_json('/novelfm/playerapi/video_model/mget/v1/', body, sign_mode=sign_mode)

def news_mget(item_ids:List[str], *, sign_mode:str='auto')->Dict[str,Any]:
    """App /bookmall/news/mget for news metadata."""
    body={'news_ids':[str(i) for i in item_ids]}
    return signed_app_json('/novelfm/bookmall/news/mget/v1/', body, sign_mode=sign_mode)

def news_full_mget(item_ids:List[str], *, sign_mode:str='auto')->Dict[str,Any]:
    """App /playerapi/full/mget for Toutiao/news article HTML.

    News/Toutiao content does not use the novel DH ``key`` field.  The App sends
    audio_type=NEWS and scene=NEWS_CONTENT_WITH_TIMEPOINT, then uses the same
    full/mget signing profile.
    """
    body={
        'item_ids':[str(i) for i in item_ids],
        'audio_type':1,
        'need_stt':False,
        'scene':4,
        'tone_id':0,
    }
    body_bytes=json_body_bytes(body)
    last:Dict[str,Any]={}
    options=full_mget_request_options(body_bytes,sign_mode)
    for idx,(mode,headers,url) in enumerate(options):
        data=full_mget_http_json(url,headers,body_bytes,timeout=60)
        last=data if isinstance(data,dict) else {}
        if last.get('code')==6000 and idx < len(options)-1 and mode not in {'fixed','pure3040-legacy'}:
            print(f'  [sign] {mode} 返回 6000，继续尝试下一个签名头')
            continue
        return last
    return last

def news_list(channel_id:str='0', offset:int=0, limit:int=20, *, scene:int=1,
              sign_mode:str='auto')->Dict[str,Any]:
    """App /bookmall/news/list for discovering Toutiao/news item ids."""
    query={
        'news_channel_id':str(channel_id),
        'offset':str(int(offset)),
        'limit':str(int(limit)),
        'scene':str(int(scene)),
    }
    return signed_app_json('/novelfm/bookmall/news/list/v1/', None, query, method='GET', sign_mode=sign_mode)

def hot_search_rank(tab_type:Optional[int]=None, offset:int=0, limit:int=10, *,
                    only_need_tab:bool=False, sign_mode:str='auto')->Dict[str,Any]:
    """App /bookmall/search/hot-search-rank for discovering ids by tab.

    tab_type examples: 3=音乐, 4=节目, 9=新闻, 10=短剧, 13=漫剧.
    """
    query={
        'offset':str(int(offset)),
        'limit':str(int(limit)),
        'only_need_tab':'true' if only_need_tab else 'false',
    }
    if tab_type is not None:
        query['tab_type']=str(int(tab_type))
    return signed_app_json('/novelfm/bookmall/search/hot-search-rank/v1/', None, query, method='GET', sign_mode=sign_mode)

def search_page(query_word:str, tab_type:Optional[int]=None, offset:int=0, limit:int=10, *,
                sign_mode:str='auto')->Dict[str,Any]:
    """App /bookmall/search/page for discovering book/music/news/video ids."""
    body={'query':query_word,'offset':int(offset),'limit':int(limit)}
    if tab_type is not None:
        body['tab_type']=int(tab_type)
    return signed_app_json('/novelfm/bookmall/search/page/v1/', body, sign_mode=sign_mode)

def music_collection_item_infos(music_ids:List[str], *, sign_mode:str='auto')->Dict[str,Any]:
    """App /bookmall/music_collection/item_infos for music item metadata."""
    body={'music_ids':[str(i) for i in music_ids]}
    return signed_app_json('/novelfm/bookmall/music_collection/item_infos/v1/', body, sign_mode=sign_mode)

def radio_stream_mget(book_ids:List[str], *, sign_mode:str='auto')->Dict[str,Any]:
    """App /playerapi/radio_stream/mget for live/radio stream metadata."""
    body={'book_ids':[str(i) for i in book_ids]}
    return signed_app_json('/novelfm/playerapi/radio_stream/mget/v1/', body, sign_mode=sign_mode)

def print_probe_summary(label:str, data:Any)->None:
    if isinstance(data,dict):
        code=data.get('code')
        msg=data.get('message') or data.get('msg')
        d=data.get('data')
        if isinstance(d,dict):
            keys=','.join(list(d.keys())[:12])
        elif isinstance(d,list):
            keys=f'list[{len(d)}]'
        else:
            keys=type(d).__name__
        print(f'[{label}] code={code} msg={msg} data={keys}')
        print(json.dumps(data,ensure_ascii=False)[:2000])
    else:
        print(f'[{label}] {type(data).__name__}: {str(data)[:2000]}')

def _first_search_book(row:Dict[str,Any])->Dict[str,Any]:
    books=row.get('books')
    if isinstance(books,list) and books and isinstance(books[0],dict):
        return books[0]
    book=row.get('book') or row.get('book_info') or row.get('data')
    return book if isinstance(book,dict) else row

def print_search_results(query_word:str, *, tab_type:Optional[int]=None, offset:int=0,
                         limit:int=10, sign_mode:str='auto')->None:
    """Print compact App search results."""
    data=search_page(query_word,tab_type=tab_type,offset=offset,limit=limit,sign_mode=sign_mode)
    d=(data.get('data') or {}) if isinstance(data,dict) else {}
    rows=d.get('search_data') or []
    print(f'搜索: {query_word} code={data.get("code") if isinstance(data,dict) else None} rows={len(rows)} has_more={d.get("has_more")} next_offset={d.get("next_offset")}')
    for index,row in enumerate(rows,1):
        if not isinstance(row,dict):
            continue
        book=_first_search_book(row)
        book_id=book.get('book_id') or row.get('book_id') or row.get('id') or ''
        name=book.get('book_name') or book.get('title') or row.get('name') or row.get('title') or ''
        author=book.get('author') or row.get('author') or ''
        anchor=book.get('anchor') or row.get('anchor') or ''
        chapters=book.get('chapter_number') or book.get('serial_count') or ''
        show_type=row.get('show_type')
        abstract=book.get('abstract') or row.get('cell_abstract') or row.get('description') or ''
        abstract=re.sub(r'\s+',' ',str(abstract)).strip()
        if len(abstract)>80:
            abstract=abstract[:80]+'...'
        print(f'{index:02d}. book_id={book_id} 书名={name} 作者={author} 章节={chapters} 播音={anchor} show_type={show_type}')
        if abstract:
            print(f'    {abstract}')

def print_directory_items(book_id:str, *, source:str='auto', items_file:Optional[Path]=None,
                          output:Optional[Path]=None)->None:
    """Resolve and print/write chapter item IDs."""
    ids=resolve_directory(book_id,source,items_file)
    print(f'目录: book_id={book_id} source={source} count={len(ids)}')
    if output:
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text('\n'.join(ids)+'\n','utf-8')
        print(f'已写入: {output}')
    else:
        for item_id in ids:
            print(item_id)

def probe_app_content(book_id:str, item_ids:List[str], *, audio_type:Any='short_play', sign_mode:str='auto')->None:
    """Probe directory/full_mget/video/news endpoints without downloading files."""
    one=item_ids[:1] if item_ids else []
    has_book=bool(str(book_id or '').strip())
    print(f'probe book_id={book_id or "(empty)"} item_ids={one}')
    if one:
        if has_book:
            try:
                meta=directory_infos(book_id,one,sign_mode)
                print(f'[all_infos] rows={len(meta)} keys={list(next(iter(meta.values())).keys())[:10] if meta else []}')
            except Exception as e:
                print(f'[all_infos] error={e}')
            try:
                resp,_x=full_mget(book_id,one,sign_mode)
                infos=((resp.get('data') or {}).get('item_infos') or {}) if isinstance(resp,dict) else {}
                print_probe_summary('full_mget', resp)
                print(f'[full_mget] item_infos={len(infos)}/{len(one)}')
            except Exception as e:
                print(f'[full_mget] error={e}')
        else:
            print('[all_infos] skipped: no book_id')
            print('[full_mget] skipped: no book_id')
        probe_audio_types = list(AUDIO_TYPE_VALUES.keys()) if str(audio_type).lower() in {'all','*'} else [audio_type]
        for at in probe_audio_types:
            try:
                print_probe_summary(f'video_model:{at}', video_model_mget(book_id,one,audio_type=at,sign_mode=sign_mode))
            except Exception as e:
                print(f'[video_model:{at}] error={e}')
        try:
            print_probe_summary('news_mget', news_mget(one,sign_mode=sign_mode))
        except Exception as e:
            print(f'[news_mget] error={e}')
        try:
            print_probe_summary('news_full_mget', news_full_mget(one,sign_mode=sign_mode))
        except Exception as e:
            print(f'[news_full_mget] error={e}')
        try:
            print_probe_summary('music_collection_item_infos', music_collection_item_infos(one,sign_mode=sign_mode))
        except Exception as e:
            print(f'[music_collection_item_infos] error={e}')
        try:
            print_probe_summary('radio_stream_mget', radio_stream_mget(one,sign_mode=sign_mode))
        except Exception as e:
            print(f'[radio_stream_mget] error={e}')
    if not has_book:
        print('[all_items] skipped: no book_id')
        return
    for ver in (2,1):
        try:
            ids,raw=app_directory_items(book_id,version=ver,sign_mode=sign_mode)
            print(f'[all_items_v{ver}] ids={len(ids)} code={raw.get("code") if isinstance(raw,dict) else None}')
            print(json.dumps(raw,ensure_ascii=False)[:1200])
        except Exception as e:
            print(f'[all_items_v{ver}] error={e}')


# Some books remain readable in the Fanqie Novel App after their NovelFM/TTS
# record is retired.  The reader App has a separate signed API family (aid=1967)
# and returns AES-encrypted HTML through /reading/reader/batch_full/v.
NOVELAPP_API_HOST = 'https://api5-normal-sinfonlinec.fqnovel.com'
# The reader batch endpoint resolves content by item_ids.  Keep a known-live
# reader book context so books absent from the reader bookstore index can still
# request their chapter bodies.  It may be overridden without changing code.
NOVELAPP_READER_REQUEST_BOOK_ID = os.environ.get(
    'FANQIE_NOVELAPP_REQUEST_BOOK_ID', '7320841644486446142'
).strip() or '7320841644486446142'
NOVELAPP_DEVICE_ID = '375350790467434'
NOVELAPP_INSTALL_ID = '375350790471530'
NOVELAPP_CHANNEL = '43536163a'
NOVELAPP_VERSION_CODE = '70132'
NOVELAPP_VERSION_NAME = '7.0.1.32'
NOVELAPP_DEVICE_TYPE = 'P30'
NOVELAPP_DEVICE_BRAND = 'realme'
NOVELAPP_OS_API = '30'
NOVELAPP_OS_VERSION = '10'
NOVELAPP_RESOLUTION = '1280*720'
NOVELAPP_DPI = '240'
NOVELAPP_ROM_VERSION = 'JZUM.440813.61127057+release-keys'
NOVELAPP_CDID = 'f4a80914-8137-4604-9e95-18946143c295'
NOVELAPP_UA = (
    'com.dragon.read.oversea.gp/70132 (Linux; U; Android 10; zh_CN; P30; '
    'Build/JZUM.440813.61127057;tt-ok/3.12.13.4-tiktok)'
)
NOVELAPP_REGISTER_AES_KEY = bytes.fromhex('ac25c67ddd8f38c1b37a2348828e222e')
NOVELAPP_ARGUS_AES_KEY = bytes.fromhex('d31e3718288a1027baab59f146a09a9c')
NOVELAPP_ARGUS_AES_IV = bytes.fromhex('ea180a0336ed352fcd24e4d50018ae54')
NOVELAPP_ARGUS_MARKER = bytes.fromhex('f2f7fcfff2f7fcff')
NOVELAPP_ARGUS_MAGIC = bytes.fromhex('a66ead9f7701d00c18')
NOVELAPP_READER_MAX_ITEMS = 30
NOVELAPP_REGISTER_KEYS:Dict[int,bytes] = {}


def _novelapp_urlencode_value(key:str, value:Any)->str:
    text=str(value)
    if key in {'book_id', 'device_type', 'resolution', 'rom_version'}:
        return urllib.parse.quote_plus(text, safe='*')
    return text


def novelapp_query(extra:Iterable[Tuple[str,Any]]=(), *, epoch_ms:Optional[int]=None)->str:
    """Build the ordered query used by the Fanqie Novel Android reader API."""
    if epoch_ms is None:
        epoch_ms=int(time.time()*1000)
    pairs:List[Tuple[str,Any]]=[
        ('iid', NOVELAPP_INSTALL_ID),
        ('device_id', NOVELAPP_DEVICE_ID),
        ('ac', 'wifi'),
        ('channel', NOVELAPP_CHANNEL),
        ('aid', '1967'),
        ('app_name', 'novelapp'),
        ('version_code', NOVELAPP_VERSION_CODE),
        ('version_name', NOVELAPP_VERSION_NAME),
        ('device_platform', 'android'),
        ('os', 'android'),
        ('ssmix', 'a'),
        ('device_type', NOVELAPP_DEVICE_TYPE),
        ('device_brand', NOVELAPP_DEVICE_BRAND),
        ('language', 'zh'),
        ('os_api', NOVELAPP_OS_API),
        ('os_version', NOVELAPP_OS_VERSION),
        ('manifest_version_code', NOVELAPP_VERSION_CODE),
        ('resolution', NOVELAPP_RESOLUTION),
        ('dpi', NOVELAPP_DPI),
        ('update_version_code', NOVELAPP_VERSION_CODE),
        ('_rticket', str(int(epoch_ms))),
        ('host_abi', 'arm64-v8a'),
        ('dragon_device_type', 'pad'),
        ('pv_player', NOVELAPP_VERSION_CODE),
        ('compliance_status', '0'),
        ('need_personal_recommend', '1'),
        ('player_so_load', '1'),
        ('is_android_pad_screen', '1'),
        ('rom_version', NOVELAPP_ROM_VERSION),
        ('cdid', NOVELAPP_CDID),
    ]
    pairs.extend((str(key), value) for key,value in extra)
    return '&'.join(f'{key}={_novelapp_urlencode_value(key, value)}' for key,value in pairs)


def _novelapp_simon_keys()->List[int]:
    initial=bytes.fromhex(
        'fc78e0a9657a0c748ce51559903ccf03'
        '510e51d3cff232d71343e88a321c5304'
    )
    mask=0xFFFFFFFFFFFFFFFF
    keys=[int.from_bytes(initial[i:i+8], 'little') for i in range(0,32,8)]
    seed=0x03DC94C3A046D678B
    for index in range(4,72):
        tmp=_ror64(keys[index-1],3)^keys[index-3]
        bit_index=(((0xFC if index<0x42 else 0xBE)+index)&0xFF)&63
        z=((~2 if ((seed>>bit_index)&1) else ~3)&mask)^keys[index-4]
        keys.append((_ror64(tmp,1)^tmp^z)&mask)
    return keys


NOVELAPP_SIMON_KEYS=_novelapp_simon_keys()


def _novelapp_simon_encrypt(data:bytes)->bytes:
    if len(data)%16:
        raise ValueError('Simon input must be block aligned')
    mask=0xFFFFFFFFFFFFFFFF
    out=bytearray()
    for offset in range(0,len(data),16):
        left=int.from_bytes(data[offset:offset+8], 'little')
        right=int.from_bytes(data[offset+8:offset+16], 'little')
        for key in NOVELAPP_SIMON_KEYS:
            left,right=right,(left^_ror64(right,62)^(_ror64(right,56)&_ror64(right,63))^key)&mask
        out.extend(left.to_bytes(8,'little'))
        out.extend(right.to_bytes(8,'little'))
    return bytes(out)


def _novelapp_half_reverse_swap(data:bytes)->bytes:
    out=bytearray(data)
    half=len(out)//2
    second_start=len(out)-half
    for left in range(half):
        right=second_start+(half-1-left)
        out[left],out[right]=out[right],out[left]
    return bytes(out)


def novelapp_x_argus(raw_query:str, khronos:int, *, random_value:Optional[int]=None)->str:
    """Pure-Python 1967/70132 X-Argus used by the Fanqie Novel reader API."""
    if random_value is None:
        random_value=secrets.randbits(31)
    pv=lambda field,value: proto_key(field,0)+proto_varint(int(value))
    pb=lambda field,value: proto_field_bytes(field, value)
    nested=(
        pb(1,NOVELAPP_DEVICE_TYPE)+pb(2,NOVELAPP_OS_VERSION)+pb(3,b'googleplay')+
        pv(4,0x50000000)
    )
    payload=b''.join((
        pv(1,0x40401252), pv(2,2), pv(3,int(random_value)&0x7FFFFFFF),
        pb(4,b'1967'), pb(5,NOVELAPP_DEVICE_ID.encode()), pb(6,b'1611921764'),
        pb(7,NOVELAPP_VERSION_NAME.encode()), pb(8,b'v04.04.05-ov-android'),
        pv(9,0x08080A40), pb(10,b'\x00'*8), pv(11,0), pv(12,int(khronos)*2),
        pb(13,sm3(b'\x00'*16)[:6]), pb(14,sm3(raw_query.encode())[:6]),
        pv(20,738), pb(23,nested),
    ))
    transformed=_novelapp_simon_encrypt(_pkcs7_pad(payload))
    xored=bytes(value^NOVELAPP_ARGUS_MARKER[index&7] for index,value in enumerate(transformed))
    frame=NOVELAPP_ARGUS_MAGIC+_novelapp_half_reverse_swap(NOVELAPP_ARGUS_MARKER+xored)+b'ao'
    return base64.b64encode(b'\xf2\x81'+aes_cbc_encrypt(frame,NOVELAPP_ARGUS_AES_KEY,NOVELAPP_ARGUS_AES_IV)).decode()


def novelapp_x_ladon(khronos:int, *, random_prefix:Optional[bytes]=None)->str:
    """1967 X-Ladon/X-Helios generator.  The two headers use independent IVs."""
    if random_prefix is None:
        random_prefix=secrets.token_bytes(4)
    if len(random_prefix)!=4:
        raise ValueError('novelapp ladon random prefix must be 4 bytes')
    mask=0xFFFFFFFFFFFFFFFF
    md5_hex=hashlib.md5(random_prefix+b'1967').hexdigest().encode()
    table=bytearray(288)
    table[:32]=md5_hex
    queue=[int.from_bytes(table[index:index+8], 'little') for index in range(0,32,8)]
    first,second=queue[0],queue[1]
    queue=queue[2:]
    for index in range(0x22):
        value=((_ror64(second,8)+first)^index)&mask
        queue.append(value)
        value^=_ror64(first,0x3D)
        value&=mask
        table[(index+1)*8:(index+2)*8]=value.to_bytes(8,'little')
        first=value
        second=queue.pop(0)
    raw=_pkcs7_pad(f'{int(khronos)}-1611921764-3019'.encode())
    encrypted=bytearray()
    for offset in range(0,len(raw),16):
        left=int.from_bytes(raw[offset:offset+8], 'little')
        right=int.from_bytes(raw[offset+8:offset+16], 'little')
        for index in range(0x22):
            key=int.from_bytes(table[index*8:index*8+8], 'little')
            right=(key^(left+_ror64(right,8)))&mask
            left=(right^_ror64(left,0x3D))&mask
        encrypted.extend(left.to_bytes(8,'little'))
        encrypted.extend(right.to_bytes(8,'little'))
    return base64.b64encode(random_prefix+bytes(encrypted)).decode()


def novelapp_headers(raw_query:str, epoch_ms:int, *, content_type:Optional[str]=None)->Dict[str,str]:
    khronos=int(time.time())
    headers={
        'Accept-Encoding':'gzip',
        'Accept':'application/json; charset=utf-8,application/x-protobuf',
        'X-Xs-From-Web':'0',
        'X-SS-REQ-TICKET':str(int(epoch_ms)),
        'X-Reading-Request':f'{int(epoch_ms)}-{secrets.randbelow(2_000_000_000)}',
        'X-VC-BDTuring-SDK-Version':'3.7.2.cn',
        'LC':'101',
        'SDK-Version':'2',
        'Passport-SDK-Version':'50564',
        'X-TT-Store-Region':'cn-zj',
        'X-TT-Store-Region-Src':'did',
        'User-Agent':NOVELAPP_UA,
        'Cookie':f'store-region=cn-zj; store-region-src=did; install_id={NOVELAPP_INSTALL_ID};',
        'X-Khronos':str(khronos),
        'X-Argus':novelapp_x_argus(raw_query,khronos),
        'X-Ladon':novelapp_x_ladon(khronos),
        'X-Helios':novelapp_x_ladon(khronos),
    }
    if content_type:
        headers['Content-Type']=content_type
    return headers


def novelapp_json(path:str, extra:Iterable[Tuple[str,Any]]=(), *, method:str='GET', body:Any=None)->Dict[str,Any]:
    epoch_ms=int(time.time()*1000)
    raw_query=novelapp_query(extra,epoch_ms=epoch_ms)
    url=f'{NOVELAPP_API_HOST}{path}?{raw_query}'
    data=b'' if body is None else json_body_bytes(body)
    headers=novelapp_headers(raw_query,epoch_ms,content_type='application/json' if body is not None else None)
    response=http_json_bytes(url,method,headers,data,timeout=60,retries=2)
    return response if isinstance(response,dict) else {}


def novelapp_register_key(required_version:int=0)->bytes:
    if required_version and required_version in NOVELAPP_REGISTER_KEYS:
        return NOVELAPP_REGISTER_KEYS[required_version]
    iv=secrets.token_bytes(16)
    payload=struct.pack('<QQ',int(NOVELAPP_DEVICE_ID),0)
    content=base64.b64encode(iv+aes_cbc_encrypt(payload,NOVELAPP_REGISTER_AES_KEY,iv)).decode()
    response=novelapp_json('/reading/crypt/registerkey',method='POST',body={'content':content,'keyver':1})
    data=response.get('data') or {}
    if response.get('code')!=0 or not isinstance(data,dict) or not data.get('key'):
        raise RuntimeError(f'novelapp registerkey error: code={response.get("code")}, message={response.get("message") or response.get("msg")}')
    raw=base64.b64decode(str(data['key']))
    if len(raw)<32:
        raise RuntimeError('novelapp registerkey response is too short')
    plain=aes_cbc_decrypt(raw[16:],NOVELAPP_REGISTER_AES_KEY,raw[:16])
    if len(plain)<16:
        raise RuntimeError('novelapp registerkey plaintext is too short')
    version=int(data.get('keyver') or 0)
    key=plain[:16]
    if version:
        NOVELAPP_REGISTER_KEYS[version]=key
    if required_version:
        NOVELAPP_REGISTER_KEYS[required_version]=key
    return key


def novelapp_decrypt_content(content:str, key:bytes)->str:
    raw=base64.b64decode(content)
    if len(raw)<32:
        raise RuntimeError('novelapp chapter ciphertext is too short')
    plain=aes_cbc_decrypt(raw[16:],key[:16],raw[:16])
    if plain[:2]==b'\x1f\x8b':
        plain=gzip.decompress(plain)
    elif plain[:1]==b'\x78':
        try:
            plain=zlib.decompress(plain)
        except zlib.error:
            pass
    return plain.decode('utf-8','replace')


def novelapp_reader_full(book_id:str, item_ids:List[str])->Dict[str,Any]:
    """Return Fanqie Novel App chapters in the normal NovelFM full/mget shape."""
    ids=[str(item_id) for item_id in item_ids]
    # The reader App accepts a longer comma-separated list but only returns
    # the first 30 entries.  Split here so the normal downloader does not
    # need to repeatedly bisect a successful-but-partial response.
    if len(ids)>NOVELAPP_READER_MAX_ITEMS:
        merged:Dict[str,Dict[str,Any]]={}
        for part in batches(ids,NOVELAPP_READER_MAX_ITEMS):
            partial=novelapp_reader_full(book_id,part)
            if partial.get('code')!=0:
                return partial
            part_infos=(partial.get('data') or {}).get('item_infos') or {}
            if not isinstance(part_infos,dict):
                raise RuntimeError('novelapp batch_full partial data is not an object')
            merged.update(part_infos)
        return {
            'code':0,
            'message':'',
            'data':{'item_infos':merged},
            'source':'novelapp_reader_batch_full',
            'request_book_id':NOVELAPP_READER_REQUEST_BOOK_ID,
        }
    response=novelapp_json(
        '/reading/reader/batch_full/v',
        (
            ('item_ids',','.join(ids)),
            ('key_register_ts','0'),
            ('book_id',NOVELAPP_READER_REQUEST_BOOK_ID),
            ('req_type','0'),
        ),
    )
    if response.get('code')!=0:
        return response
    raw_items=response.get('data') or {}
    if not isinstance(raw_items,dict):
        raise RuntimeError('novelapp batch_full data is not an object')
    infos:Dict[str,Dict[str,Any]]={}
    for item_id in ids:
        item=raw_items.get(item_id)
        if not isinstance(item,dict):
            continue
        cipher=str(item.get('content') or '')
        if not cipher:
            continue
        version=int(item.get('key_version') or 0)
        plaintext=novelapp_decrypt_content(cipher,novelapp_register_key(version))
        info=dict(item)
        info['content']=plaintext
        info['crypt_status']=0
        info.setdefault('title',item.get('title') or '')
        infos[item_id]=info
    return {
        'code':0,
        'message':response.get('message') or '',
        'data':{'item_infos':infos},
        'source':'novelapp_reader_batch_full',
        'request_book_id':NOVELAPP_READER_REQUEST_BOOK_ID,
    }


def try_novelapp_reader_fallback(book_id:str, item_ids:List[str])->Optional[Dict[str,Any]]:
    try:
        response=novelapp_reader_full(book_id,item_ids)
        infos=(response.get('data') or {}).get('item_infos') or {}
        if response.get('code')==0 and infos:
            print(f'  [fallback] NovelFM 正文不可用，已切换番茄小说 App 正文接口: {len(infos)}/{len(item_ids)}')
            return response
        print(f'  [fallback] 番茄小说 App 正文接口未返回章节: code={response.get("code")} message={response.get("message") or response.get("msg") or ""}')
    except Exception as exc:
        print(f'  [fallback] 番茄小说 App 正文接口失败: {exc}')
    return None


def full_mget(book_id:str,item_ids:List[str],sign_mode:str='auto',
              session:Optional[Any]=None)->Tuple[Dict[str,Any],int]:
    x,req_key=make_encrypt_context()
    body={
        'item_ids':[str(i) for i in item_ids],
        'book_id':NOVELFM_REQUEST_BOOK_ID,
        'key':req_key,
        'need_stt':False,
        'scene':3,
        'tone_id':91,
    }
    body_bytes=json_body_bytes(body)
    last:Dict[str,Any]={}
    options=full_mget_request_options(body_bytes,sign_mode)
    for idx,(mode,headers,url) in enumerate(options):
        data=full_mget_http_json(url,headers,body_bytes,timeout=60,session=session)
        last=data if isinstance(data,dict) else {}
        # 某组内置头如果被服务端淘汰，则继续尝试后备头。
        if last.get('code')==6000 and idx < len(options)-1 and mode not in {'fixed','pure3040-legacy'}:
            print(f'  [sign] {mode} 返回 6000，继续尝试下一个签名头')
            continue
        return last,x
    return last,x

def _mode_is_pure_python_generated(mode:str)->bool:
    return mode in {'pure3040','pure3040-legacy'}

def verify_full_mget_sign(book_id:str, item_id:str, sign_mode:str='auto')->Dict[str,Any]:
    """直接请求一次 full/mget，用来确认当前内置纯 Python 签名是否仍是 code=0。

    这个检查不写文件、不解密正文；只看服务端业务 code 和返回的 item_infos 数。
    auto 会先试本机 App 实抓签名头，再回退本文件内的纯 Python 生成链路。
    """
    _x, req_key = make_encrypt_context()
    body = {
        'item_ids': [str(item_id)],
        'book_id': NOVELFM_REQUEST_BOOK_ID,
        'key': req_key,
        'need_stt': False,
        'scene': 3,
        'tone_id': 91,
    }
    body_bytes = json_body_bytes(body)
    rows:List[Dict[str,Any]] = []
    for mode, headers, url in full_mget_request_options(body_bytes, sign_mode):
        started = time.perf_counter()
        try:
            resp = full_mget_http_json(url,headers,body_bytes,timeout=30,retries=1)
            data = resp if isinstance(resp, dict) else {}
            infos = (data.get('data') or {}).get('item_infos') or {}
            row = {
                'mode': mode,
                'code': data.get('code'),
                'message': data.get('message') or data.get('msg') or '',
                'item_infos': len(infos),
                'elapsed_ms': int((time.perf_counter() - started) * 1000),
                'pure_python_generated': _mode_is_pure_python_generated(mode),
                'x_ss_stub': headers.get('X-SS-STUB', ''),
                'x_khronos': headers.get('X-Khronos', ''),
                'url_host': urllib.parse.urlsplit(url).netloc,
            }
        except Exception as e:
            row = {
                'mode': mode,
                'code': None,
                'message': repr(e),
                'item_infos': 0,
                'elapsed_ms': int((time.perf_counter() - started) * 1000),
                'pure_python_generated': _mode_is_pure_python_generated(mode),
            }
        rows.append(row)
    ok = next((r for r in rows if r.get('code') == 0 and r.get('item_infos', 0) > 0), None)
    return {'ok': bool(ok), 'book_id': str(book_id), 'item_id': str(item_id), 'results': rows}

def existing_chapter_file(cdir:Path, index:int)->Optional[Path]:
    hits=sorted(cdir.glob(f'{index:04d}-*.txt'))
    return hits[0] if hits else None

def download_batch(book_id:str, batch:List[str], allow_split:bool=True, sign_mode:str='auto',
                   session:Optional[Any]=None)->List[Tuple[str,Optional[Dict[str,Any]],Optional[int],Optional[BaseException]]]:
    try:
        resp,x=full_mget(book_id,batch,sign_mode,session)
        if resp.get('code')!=0:
            raise RuntimeError(f'full_mget 错误: {resp}')
        infos=(resp.get('data') or {}).get('item_infos') or {}
        if allow_split and len(batch)>1 and len(infos)<len(batch):
            mid=max(1,len(batch)//2)
            return download_batch(book_id,batch[:mid],True,sign_mode,session)+download_batch(book_id,batch[mid:],True,sign_mode,session)
        return [(item_id, infos.get(str(item_id)), x, None) for item_id in batch]
    except Exception as e:
        if allow_split and len(batch)>1:
            mid=max(1,len(batch)//2)
            return download_batch(book_id,batch[:mid],True,sign_mode,session)+download_batch(book_id,batch[mid:],True,sign_mode,session)
        return [(item_id, None, None, e) for item_id in batch]

def request_only_batch(book_id:str, batch:List[str], rawdir:Path, sign_mode:str='auto',
                       seq_counter:Optional[List[int]]=None, start_index:int=1,
                       allow_split:bool=True, file_prefix:Optional[str]=None,
                       session:Optional[Any]=None)->List[Dict[str,Any]]:
    """只请求 full/mget 保存原始 JSON；不解密、不解析正文。

    大批量 full/mget 偶尔会 code=0 但 item_infos 为空/缺失，这种情况下自动二分重试。
    注意：父批次如果被拆分，不保存空响应，只保存最终实际采用的响应。
    """
    if seq_counter is None:
        seq_counter=[0]
    end_index=start_index+len(batch)-1
    try:
        resp,client_x=full_mget(book_id,batch,sign_mode,session)
    except Exception as e:
        if allow_split and len(batch)>1:
            mid=max(1,len(batch)//2)
            print(f'  [请求异常拆分] {start_index}-{end_index}: {e}')
            return (
                request_only_batch(book_id,batch[:mid],rawdir,sign_mode,seq_counter,start_index,True,file_prefix,session) +
                request_only_batch(book_id,batch[mid:],rawdir,sign_mode,seq_counter,start_index+mid,True,file_prefix,session)
            )
        if file_prefix is None:
            seq_counter[0]+=1
            batch_label=f'{seq_counter[0]:04d}'
            batch_value:Any=seq_counter[0]
        else:
            batch_label=file_prefix
            batch_value=file_prefix
        raw_path=rawdir/f'full_mget_{batch_label}_{start_index:04d}-{end_index:04d}_error.json'
        payload={'error':str(e),'book_id':str(book_id),'item_ids':[str(i) for i in batch]}
        raw_path.write_text(json.dumps(payload,ensure_ascii=False,indent=2),'utf-8')
        print(f'  [请求失败] {start_index}-{end_index}: {e} raw={raw_path.name}')
        return [{'batch':batch_value,'range':[start_index,end_index],'count':len(batch),'error':str(e),'item_infos':0,'raw_path':str(raw_path)}]

    infos=(resp.get('data') or {}).get('item_infos') or {}
    code=resp.get('code')
    if allow_split and len(batch)>1 and code==0 and len(infos)<len(batch):
        mid=max(1,len(batch)//2)
        print(f'  [响应缺失拆分] {start_index}-{end_index}: item_infos={len(infos)}/{len(batch)}')
        return (
            request_only_batch(book_id,batch[:mid],rawdir,sign_mode,seq_counter,start_index,True,file_prefix,session) +
            request_only_batch(book_id,batch[mid:],rawdir,sign_mode,seq_counter,start_index+mid,True,file_prefix,session)
        )

    if file_prefix is None:
        seq_counter[0]+=1
        batch_label=f'{seq_counter[0]:04d}'
        batch_value:Any=seq_counter[0]
    else:
        batch_label=file_prefix
        batch_value=file_prefix
    raw_path=rawdir/f'full_mget_{batch_label}_{start_index:04d}-{end_index:04d}.json'
    meta_path=rawdir/f'full_mget_{batch_label}_{start_index:04d}-{end_index:04d}.meta.json'
    raw_path.write_text(json.dumps(resp,ensure_ascii=False,separators=(',',':')),'utf-8')
    meta_path.write_text(json.dumps({
        'book_id':str(book_id),
        'range':[start_index,end_index],
        'count':len(batch),
        'item_ids':[str(i) for i in batch],
        'sign_mode':sign_mode,
        # full/mget 的 content 是按本次请求的 DH client_x 加密返回的。
        # request-only 不解密，但保存它后，以后可离线解密 raw JSON，不必重新请求。
        'client_x':str(client_x),
    },ensure_ascii=False,indent=2),'utf-8')
    print(f'  [请求完成] code={code} item_infos={len(infos)}/{len(batch)} raw={raw_path.name}')
    rec={'batch':batch_value,'range':[start_index,end_index],'count':len(batch),'code':code,'item_infos':len(infos),'raw_path':str(raw_path),'meta_path':str(meta_path)}
    if code!=0:
        rec['error']=f'code={code}'
    return [rec]

def request_only_batch_worker(args:Tuple[int,int,str,List[str],Path,str],
                              session:Optional[Any]=None)->Dict[str,Any]:
    """Run one top-level request-only batch without sharing output names."""
    bi,start_index,book_id,batch,rawdir,sign_mode=args
    t0=time.perf_counter()
    records=request_only_batch(
        book_id,
        batch,
        rawdir,
        sign_mode,
        [0],
        start_index,
        True,
        f'batch{bi:04d}',
        session,
    )
    return {
        'batch':bi,
        'start':start_index,
        'end':start_index+len(batch)-1,
        'count':len(batch),
        'elapsed':time.perf_counter()-t0,
        'records':records,
    }

def request_only_batch_lane(jobs:List[Tuple[int,int,str,List[str],Path,str]])->List[Dict[str,Any]]:
    """Run several request-only batches on one reusable worker connection."""
    session=new_full_mget_session()
    try:
        return [request_only_batch_worker(job,session) for job in jobs]
    finally:
        session.close()

def decrypt_item_worker(args:Tuple[int,str,Dict[str,Any],int])->Dict[str,Any]:
    index,item_id,info,x=args
    try:
        title=info.get('title') or ((info.get('novel_data') or {}).get('title')) or f'第{index}章'
        content=info.get('content') or ''
        server_key=info.get('key') or ''
        chapter_html=decrypt_novelfm_content(content,server_key,x) if info.get('crypt_status')==1 and content and server_key and x is not None else content
        text=html_to_text(chapter_html)
        return {'index':index,'item_id':item_id,'title':title,'text':text,'error':None}
    except Exception as e:
        return {'index':index,'item_id':item_id,'title':f'第{index}章','text':'','error':str(e)}

def fetch_batch_worker(args:Tuple[int,int,str,List[str],str],
                       session:Optional[Any]=None)->Dict[str,Any]:
    bi,start_index,book_id,batch,sign_mode=args
    t0=time.perf_counter()
    results=download_batch(book_id,batch,allow_split=True,sign_mode=sign_mode,session=session)
    ok=sum(1 for _item_id,info,_x,err in results if info and not err)
    return {
        'batch':bi,
        'start':start_index,
        'end':start_index+len(batch)-1,
        'count':len(batch),
        'ok':ok,
        'elapsed':time.perf_counter()-t0,
        'results':results,
    }

def fetch_batch_lane(jobs:List[Tuple[int,int,str,List[str],str]])->List[Dict[str,Any]]:
    """Fetch a worker's sequential batches through one HTTP/2 keep-alive session."""
    session=new_full_mget_session()
    try:
        return [fetch_batch_worker(job,session) for job in jobs]
    finally:
        session.close()

def download_single_file_fast(book_id:str, item_ids:List[str], batch_size:int, sign_mode:str,
                              quiet:bool, request_workers:int, decrypt_workers:int)->Tuple[List[str],List[Dict[str,Any]]]:
    """single-file 专用：并发请求 + 并发解密，最后按章节顺序合并。"""
    req_jobs=[]
    start=1
    for bi,batch in enumerate(batches(item_ids,batch_size),1):
        req_jobs.append((bi,start,book_id,batch,sign_mode))
        start+=len(batch)
    total=len(item_ids)
    text_by_index:Dict[int,str]={}
    rec_by_index:Dict[int,Dict[str,Any]]={}
    decrypt_futs={}
    t0=time.perf_counter()
    proc_pool=None
    if decrypt_workers>1:
        proc_pool=concurrent.futures.ProcessPoolExecutor(max_workers=decrypt_workers)
    try:
        worker_count=min(5,max(1,request_workers),len(req_jobs))
        lanes=[[] for _ in range(worker_count)]
        for index,job in enumerate(req_jobs):
            lanes[index%worker_count].append(job)
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as req_pool:
            futs=[req_pool.submit(fetch_batch_lane,lane) for lane in lanes]
            for fut in concurrent.futures.as_completed(futs):
                for br in fut.result():
                    print(f"请求正文 batch {br['batch']}: {br['start']}-{br['end']} / {total}  [请求完成] ok={br['ok']}/{br['count']} {br['elapsed']:.2f}s")
                    for offset,(item_id,info,x,err) in enumerate(br['results']):
                        idx=int(br['start'])+offset
                        if err:
                            rec_by_index[idx]={'index':idx,'item_id':item_id,'error':str(err)}
                            continue
                        if not info:
                            rec_by_index[idx]={'index':idx,'item_id':item_id,'error':'no item_info'}
                            continue
                        job=(idx,item_id,info,x if x is not None else 0)
                        if proc_pool is not None:
                            decrypt_futs[proc_pool.submit(decrypt_item_worker,job)]=idx
                        else:
                            rec=decrypt_item_worker(job)
                            if rec.get('error'):
                                rec_by_index[idx]={'index':idx,'item_id':item_id,'error':rec.get('error')}
                            else:
                                title=rec.get('title') or f'第{idx}章'
                                text=rec.get('text') or ''
                                text_by_index[idx]=title+'\n\n'+text.strip()+'\n'
                                rec_by_index[idx]={'index':idx,'item_id':item_id,'title':title,'txt_path':None,'html_path':None}
        if decrypt_futs:
            done_dec=0
            for fut in concurrent.futures.as_completed(decrypt_futs):
                idx=decrypt_futs[fut]
                rec=fut.result()
                item_id=str(rec.get('item_id') or '')
                if rec.get('error'):
                    rec_by_index[idx]={'index':idx,'item_id':item_id,'error':rec.get('error')}
                else:
                    title=rec.get('title') or f'第{idx}章'
                    text=rec.get('text') or ''
                    text_by_index[idx]=title+'\n\n'+text.strip()+'\n'
                    rec_by_index[idx]={'index':idx,'item_id':item_id,'title':title,'txt_path':None,'html_path':None}
                done_dec+=1
                if not quiet and (done_dec%500==0 or done_dec==len(decrypt_futs)):
                    print(f'  [解密进度] {done_dec}/{len(decrypt_futs)}')
    finally:
        if proc_pool is not None:
            proc_pool.shutdown()

    merged=[]; records=[]
    for idx,item_id in enumerate(item_ids,1):
        rec=rec_by_index.get(idx) or {'index':idx,'item_id':item_id,'error':'missing result'}
        records.append(rec)
        if not rec.get('error'):
            merged.append(text_by_index.get(idx,''))
    ok=sum(1 for r in records if not r.get('error'))
    print(f'  [快速模式完成] ok={ok}/{total} elapsed={time.perf_counter()-t0:.2f}s')
    return merged,records

def extract_media_url_from_model(row:Dict[str,Any])->Tuple[str,str]:
    """Extract a direct downloadable media URL and extension from one video_model row."""
    vm_raw=row.get('video_model') or ''
    vm={}
    if isinstance(vm_raw,str) and vm_raw.strip():
        try:
            vm=json.loads(vm_raw)
        except Exception:
            vm={}
    urls=[]; ext=''
    media_type=str(vm.get('media_type') or '').lower() if isinstance(vm,dict) else ''
    for v in (vm.get('video_list') or []) if isinstance(vm,dict) else []:
        if isinstance(v,dict):
            meta=v.get('video_meta') or {}
            if not ext and isinstance(meta,dict):
                vt=str(meta.get('vtype') or meta.get('format') or '').lower()
                mime=str(meta.get('mime_type') or '').lower()
                if 'mp4' in vt or 'mp4' in mime:
                    # video_model 的 video_meta 经常只有 vtype=mp4、mime 为空；
                    # 这时要结合外层 media_type 或 URL 上的 mime_type 判断，
                    # 否则短剧/漫剧视频会被误保存成 .m4a。
                    ext='.mp4' if ('video' in mime or media_type == 'video') else '.m4a'
                elif vt:
                    ext='.'+re.sub(r'[^a-z0-9]+','',vt)[:8]
            for k in ('main_url','backup_url'):
                u=v.get(k)
                if isinstance(u,str) and u.startswith('http'):
                    urls.append(u)
    if isinstance(vm,dict):
        fb=(vm.get('fallback_api') or {}).get('fallback_api') if isinstance(vm.get('fallback_api'),dict) else None
        if isinstance(fb,str) and fb.startswith('http'):
            urls.append(fb)
        if not ext:
            ext='.mp4' if media_type=='video' else '.m4a'
    if not urls:
        # Some versions may expose URL strings in pb_video_model; scan as fallback.
        blob=json.dumps(row,ensure_ascii=False)
        urls=[u for u in re.findall(r'https?://[^"\\\s]+', blob) if 'http' in u]
    if not urls:
        raise RuntimeError('no downloadable URL in video_model')
    return urls[0].replace('\u0026','&'), ext or '.m4a'

def download_binary_url(url:str, out:Path, *, timeout:int=90)->None:
    tmp=out.with_suffix(out.suffix+'.part')
    req=urllib.request.Request(url,headers={'User-Agent':DEFAULT_UA,'Accept-Encoding':'identity'})
    with urllib.request.urlopen(req,timeout=timeout) as r, tmp.open('wb') as f:
        while True:
            chunk=r.read(1024*512)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(out)

def download_media(book_id:str, output:Path, limit:int=0, batch_size:int=20, sleep:float=0.1,
                   items_file:Optional[Path]=None, directory_source:str='app', overwrite:bool=False,
                   start:int=1, end:int=0, sign_mode:str='auto', audio_type:Any='book',
                   tone_id:int=91, workers:int=4, explicit_item_ids:Optional[List[str]]=None):
    """Download audio/video media returned by video_model. Pure Python, no App/so."""
    detail=book_detail(book_id) if str(book_id or '').strip() else {}
    first_explicit=(explicit_item_ids or [''])[0] if explicit_item_ids else ''
    name=detail.get('book_name') or detail.get('title') or (f'media_{first_explicit}' if first_explicit else f'book_{book_id}')
    if explicit_item_ids:
        item_ids=unique_item_ids(explicit_item_ids, '')
    else:
        item_ids=resolve_directory(book_id,directory_source,items_file)
    if start>1 or end:
        lo=max(1,start); hi=end if end and end>=lo else len(item_ids)
        item_ids=item_ids[lo-1:hi]
    if limit:
        item_ids=item_ids[:limit]
    root=output/sanitize(name)/'media'
    root.mkdir(parents=True,exist_ok=True)
    print(f'Media download: {name} count={len(item_ids)} audio_type={audio_type} out={root}')
    records=[]
    for base,batch in enumerate(batches(item_ids,batch_size),0):
        first=base*batch_size+1
        print(f'Media batch {base+1}: {first}-{first+len(batch)-1}/{len(item_ids)}')
        meta={}
        if str(book_id or '').strip():
            try:
                meta=directory_infos(book_id,batch,sign_mode)
            except Exception:
                meta={}
        if not meta:
            # Music/news single items often have no book directory.  Try cheap
            # metadata endpoints so output names are still readable.
            try:
                mdata=music_collection_item_infos(batch,sign_mode=sign_mode)
                for row in ((mdata.get('data') or {}).get('music_list') or []):
                    if isinstance(row,dict) and row.get('book_id'):
                        meta[str(row.get('book_id'))]={'title':row.get('book_name') or row.get('title') or row.get('name')}
            except Exception:
                pass
            try:
                ndata=news_mget(batch,sign_mode=sign_mode)
                nrows=((ndata.get('data') or {}).get('news_infos') or [])
                if isinstance(nrows,dict):
                    nrows=list(nrows.values())
                for row in nrows:
                    if isinstance(row,dict) and row.get('id'):
                        meta[str(row.get('id'))]={'title':row.get('title') or row.get('name')}
            except Exception:
                pass
        resp=video_model_mget(book_id,batch,audio_type=audio_type,tone_id=tone_id,sign_mode=sign_mode,source='default')
        if not isinstance(resp,dict) or resp.get('code')!=0:
            print(f'  [FAIL] video_model: {resp}')
            continue
        rows=((resp.get('data') or {}).get('video_model_datas') or [])
        byid={str(r.get('item_id')):r for r in rows if isinstance(r,dict)}
        tasks=[]
        for off,item_id in enumerate(batch,0):
            idx=first+off
            row=byid.get(str(item_id))
            title=(meta.get(str(item_id)) or {}).get('title') or f'{idx:04d}-{item_id}'
            if not row:
                records.append({'index':idx,'item_id':item_id,'title':title,'error':'no video_model_data'})
                continue
            try:
                url,ext=extract_media_url_from_model(row)
            except Exception as e:
                records.append({'index':idx,'item_id':item_id,'title':title,'error':str(e)})
                continue
            out=root/(sanitize(f'{idx:04d}-{title}')+ext)
            records.append({'index':idx,'item_id':item_id,'title':title,'url':url,'path':str(out)})
            if overwrite or not (out.exists() and out.stat().st_size>0):
                tasks.append((url,out,idx,title))
        def work(t):
            url,out,idx,title=t
            try:
                download_binary_url(url,out)
                return idx, None
            except Exception as e:
                return idx, str(e)
        if tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,workers)) as ex:
                for idx,err in ex.map(work,tasks):
                    if err:
                        print(f'  [download fail] {idx:04d}: {err}')
                        for r in records:
                            if r.get('index')==idx:
                                r['error']=err
                    else:
                        print(f'  [OK] {idx:04d}')
        if sleep:
            time.sleep(sleep)
    (root.parent/'media.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),'utf-8')
    ok=sum(1 for r in records if not r.get('error'))
    print(f'Media done: ok={ok}/{len(records)}')

def download(book_id:str, output:Path, limit:int=0, batch_size:int=1500, sleep:float=0.2, save_html:bool=False,
             items_file:Optional[Path]=None, directory_source:str='app', overwrite:bool=False,
             start:int=1, end:int=0, sign_mode:str='auto', single_file:bool=False, quiet:bool=False,
             request_only:bool=False, decrypt_workers:int=1, request_workers:int=1,
             max_request_items:int=DEFAULT_FULL_MGET_MAX_ITEMS):
    detail=book_detail(book_id)
    name=detail.get('book_name') or detail.get('title') or f'book_{book_id}'
    declared=int(detail.get('chapter_number') or detail.get('serial_count') or 0)
    print(f'书名: {name}')
    print('提取目录 ...')
    item_ids=resolve_directory(book_id,directory_source,items_file)
    if start>1 or end:
        lo=max(1,start); hi=end if end and end>=lo else len(item_ids)
        item_ids=item_ids[lo-1:hi]
    if limit: item_ids=item_ids[:limit]
    requested_batch_size=int(batch_size)
    requested_max_request_items=int(max_request_items)
    requested_request_workers=int(request_workers)
    max_request_items=requested_max_request_items
    if requested_batch_size<0:
        raise ValueError('batch_size 必须大于等于 0')
    if max_request_items<1:
        raise ValueError('max_request_items 必须大于 0')
    max_request_items=min(max_request_items,FULL_MGET_HARD_MAX_ITEMS)
    request_workers=max(1,min(requested_request_workers,5))
    dynamic_batch_size=requested_batch_size==0
    if dynamic_batch_size:
        if len(item_ids)>max_request_items:
            # Large multi-MiB full/mget responses contend heavily when split
            # into too many connections.  Keep each request at the proven API
            # maximum and cap the active long responses at three.
            batch_size=max_request_items
            request_workers=min(request_workers,3)
        else:
            # Small books benefit from parallel response setup and can be
            # evenly distributed across the requested small worker pool.
            batch_size=max(1,(len(item_ids)+request_workers-1)//request_workers)
    else:
        batch_size=min(requested_batch_size,max_request_items)
    root=output/sanitize(name); cdir=root/'chapters'; hdir=root/'html'; rawdir=root/'raw'
    root.mkdir(parents=True,exist_ok=True)
    (root/'book.json').write_text(json.dumps(detail,ensure_ascii=False),'utf-8')
    if not request_only and not single_file:
        cdir.mkdir(parents=True,exist_ok=True)
    if save_html and not request_only: hdir.mkdir(parents=True,exist_ok=True)
    if request_only:
        rawdir.mkdir(parents=True,exist_ok=True)
        if overwrite:
            for old_raw in rawdir.glob('full_mget_*.json'):
                old_raw.unlink()
            for old_meta in rawdir.glob('full_mget_*.meta.json'):
                old_meta.unlink()
    (root/'item_ids.txt').write_text('\n'.join(item_ids)+'\n','utf-8')
    print(f'章节数: {len(item_ids)}' + (f' / detail={declared}' if declared else ''))
    if requested_max_request_items>FULL_MGET_HARD_MAX_ITEMS:
        print(f'单请求硬上限: {FULL_MGET_HARD_MAX_ITEMS}（已从 {requested_max_request_items} 限制）')
    if dynamic_batch_size:
        print(f'动态正文批次: {batch_size} 章/请求，workers={request_workers}，单请求上限={max_request_items}')
    elif batch_size!=requested_batch_size:
        print(f'单请求章节数: {batch_size}（已按 --max-request-items 从 {requested_batch_size} 拆分）')
    else:
        print(f'单请求章节数: {batch_size}')
    if request_workers!=requested_request_workers:
        print(f'正文并发连接数: {request_workers}（已从 {requested_request_workers} 限制）')
    print(f'输出: {root}')
    records:List[Dict[str,Any]]=[]
    if request_only and request_workers>1:
        req_jobs=[]
        start_index=1
        for bi,batch in enumerate(batches(item_ids,batch_size),1):
            req_jobs.append((bi,start_index,book_id,batch,rawdir,sign_mode))
            start_index+=len(batch)
        worker_count=min(request_workers,len(req_jobs))
        lanes=[[] for _ in range(worker_count)]
        for index,job in enumerate(req_jobs):
            lanes[index%worker_count].append(job)
        print(f'并发原始请求: batches={len(req_jobs)} workers={worker_count} connection_reuse=on')
        started=time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as req_pool:
            futures=[req_pool.submit(request_only_batch_lane,lane) for lane in lanes]
            for fut in concurrent.futures.as_completed(futures):
                for result in fut.result():
                    records.extend(result['records'])
                    returned=sum(int(row.get('item_infos') or 0) for row in result['records'])
                    print(
                        f"请求正文 batch {result['batch']}: {result['start']}-{result['end']} / {len(item_ids)} "
                        f"[请求完成] item_infos={returned}/{result['count']} {result['elapsed']:.2f}s"
                    )
        records.sort(key=lambda row: (int((row.get('range') or [0])[0]), int((row.get('range') or [0,0])[1])))
        (root/'chapters.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),'utf-8')
        total_infos=sum(int(row.get('item_infos') or 0) for row in records)
        print(f'完成: {len(records)} 个原始响应，item_infos={total_infos}/{len(item_ids)} elapsed={time.perf_counter()-started:.2f}s')
        print(f'原始响应目录: {rawdir}')
        return
    if single_file and not request_only and not save_html and request_workers>1:
        merged,records=download_single_file_fast(book_id,item_ids,batch_size,sign_mode,quiet,request_workers,decrypt_workers)
        merged_path=root/(sanitize(name)+'.txt')
        merged_path.write_text('\n\n'.join(section.strip() for section in merged if section.strip())+'\n','utf-8')
        (root/'chapters.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),'utf-8')
        print(f'完成: {len(records)} 章')
        print(f'合并 TXT: {merged_path}')
        return
    merged=[]; records=[]; done=0; raw_seq=[0]
    decrypt_pool=None
    request_session=new_full_mget_session() if len(item_ids)>batch_size else None
    if request_session is not None:
        print(f'正文连接复用: {"HTTP/1.1" if FULL_MGET_TRANSPORT=="http1" else "HTTP/2"} keep-alive')
    if single_file and decrypt_workers>1 and not request_only and not save_html:
        decrypt_pool=concurrent.futures.ProcessPoolExecutor(max_workers=decrypt_workers)
    for bi,batch in enumerate(batches(item_ids,batch_size),1):
        print(f'请求正文 batch {bi}: {done+1}-{done+len(batch)} / {len(item_ids)}')
        if request_only:
            batch_records=request_only_batch(book_id,batch,rawdir,sign_mode,raw_seq,done+1,allow_split=True,session=request_session)
            records.extend(batch_records)
            done+=len(batch)
            if sleep: time.sleep(sleep)
            continue
        if not overwrite and not single_file:
            cached_files=[existing_chapter_file(cdir, done + offset) for offset in range(1, len(batch) + 1)]
            if cached_files and all(cached_files):
                for offset,(item_id,old_file) in enumerate(zip(batch,cached_files),1):
                    done+=1
                    text_old=old_file.read_text('utf-8',errors='replace').strip()
                    merged.append(text_old+'\n')
                    records.append({'index':done,'item_id':item_id,'title':old_file.stem.split('-',1)[-1],'txt_path':str(old_file),'html_path':None,'cached':True})
                print(f'  [缓存批次] {done-len(batch)+1:04d}-{done:04d}')
                if sleep: time.sleep(sleep)
                continue
        try:
            meta={} if single_file else directory_infos(book_id,batch,sign_mode)
        except Exception as e:
            print(f'  [WARN] all_infos 失败，标题将使用接口返回/序号: {e}')
            meta={}
        results=download_batch(book_id,batch,allow_split=True,sign_mode=sign_mode,session=request_session)
        if single_file and decrypt_workers>1 and not save_html:
            base_done=done
            jobs=[]
            fallback_records:Dict[int,Dict[str,Any]]={}
            for offset,(item_id,info,x,err) in enumerate(results,1):
                idx=base_done+offset
                if err:
                    fallback_records[idx]={'index':idx,'item_id':item_id,'title':f'第{idx}章','text':'','error':str(err)}
                elif not info:
                    fallback_records[idx]={'index':idx,'item_id':item_id,'title':f'第{idx}章','text':'','error':'no item_info'}
                else:
                    jobs.append((idx,item_id,info,x if x is not None else 0))
            worker_records={}
            if jobs:
                # Windows 下进程池启动有开销；每个任务是一章，chunksize 调大可减少 IPC。
                chunksize=max(1, len(jobs)//(decrypt_workers*8))
                ex=decrypt_pool
                if ex is None:
                    ex=concurrent.futures.ProcessPoolExecutor(max_workers=decrypt_workers)
                try:
                    for rec in ex.map(decrypt_item_worker,jobs,chunksize=chunksize):
                        worker_records[int(rec['index'])]=rec
                finally:
                    if decrypt_pool is None:
                        ex.shutdown()
            for offset,(item_id,_info,_x,_err) in enumerate(results,1):
                done=base_done+offset
                rec=worker_records.get(done) or fallback_records.get(done)
                if not rec:
                    rec={'index':done,'item_id':item_id,'title':f'第{done}章','text':'','error':'missing decrypt result'}
                if rec.get('error'):
                    if not quiet:
                        print(f"  [失败] {done:04d} {item_id}: {rec.get('error')}")
                    records.append({'index':done,'item_id':item_id,'error':rec.get('error')})
                    continue
                title=rec.get('title') or f'第{done}章'
                text=rec.get('text') or ''
                merged.append(title+'\n\n'+text.strip()+'\n')
                records.append({'index':done,'item_id':item_id,'title':title,'txt_path':None,'html_path':None})
                if not quiet:
                    print(f'  [OK] {done:04d} {title}')
            if quiet:
                ok_count=sum(1 for r in records[-len(batch):] if not r.get('error'))
                print(f'  [批次完成] ok={ok_count}/{len(batch)} total={done}/{len(item_ids)}')
            if sleep: time.sleep(sleep)
            continue
        for item_id,info,x,err in results:
            done+=1
            old_file=None if single_file else existing_chapter_file(cdir,done)
            if old_file and not overwrite:
                text_old=old_file.read_text('utf-8',errors='replace').strip()
                merged.append(text_old+'\n')
                records.append({'index':done,'item_id':item_id,'title':old_file.stem.split('-',1)[-1],'txt_path':str(old_file),'html_path':None,'cached':True})
                if not quiet:
                    print(f'  [缓存] {done:04d} {old_file.name}')
                continue
            if err:
                if not quiet:
                    print(f'  [失败] {done:04d} {item_id}: {err}')
                records.append({'index':done,'item_id':item_id,'error':str(err)})
                continue
            if not info:
                if not quiet:
                    print(f'  [跳过] {done:04d} {item_id}')
                records.append({'index':done,'item_id':item_id,'error':'no item_info'})
                continue
            title=(meta.get(str(item_id)) or {}).get('title') or info.get('title') or ((info.get('novel_data') or {}).get('title')) or f'第{done}章'
            content=info.get('content') or ''; server_key=info.get('key') or ''
            chapter_html=decrypt_novelfm_content(content,server_key,x) if info.get('crypt_status')==1 and content and server_key and x is not None else content
            text=html_to_text(chapter_html)
            fn=sanitize(f'{done:04d}-{title}')
            txt_path=None
            if not single_file:
                txt_path=cdir/(fn+'.txt')
                txt_path.write_text(title+'\n\n'+text,'utf-8')
            html_path=None
            if save_html:
                html_path=hdir/(fn+'.html'); html_path.write_text(chapter_html,'utf-8')
            merged.append(title+'\n\n'+text.strip()+'\n')
            records.append({'index':done,'item_id':item_id,'title':title,'txt_path':str(txt_path) if txt_path else None,'html_path':str(html_path) if html_path else None})
            if not quiet:
                print(f'  [OK] {done:04d} {title}')
        if quiet:
            ok_count=sum(1 for r in records[-len(batch):] if not r.get('error'))
            print(f'  [批次完成] ok={ok_count}/{len(batch)} total={done}/{len(item_ids)}')
        if sleep: time.sleep(sleep)
    if decrypt_pool is not None:
        decrypt_pool.shutdown()
    if request_session is not None:
        request_session.close()
    merged_path=root/(sanitize(name)+'.txt')
    if not request_only:
        merged_path.write_text('\n\n'.join(section.strip() for section in merged if section.strip())+'\n','utf-8')
    (root/'chapters.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),'utf-8')
    if request_only:
        total_infos=sum(int(r.get('item_infos') or 0) for r in records)
        print(f'完成: {len(records)} 个原始响应，item_infos={total_infos}/{len(item_ids)}')
        print(f'原始响应目录: {rawdir}')
    else:
        print(f'完成: {len(records)} 章')
        print(f'合并 TXT: {merged_path}')

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--book-id',help='书籍 book_id；也可以直接填番茄小说 page 链接/schema/share 文本')
    ap.add_argument('--url','--book-url',dest='book_url',help='番茄小说 page 链接、App schema 或分享文本，会自动提取 book_id')
    ap.add_argument('--output',type=Path,default=Path('downloads_pure'))
    ap.add_argument('--limit',type=int,default=0)
    ap.add_argument('--batch-size',type=int,default=1500,help='每个 full/mget 的目标章节数，默认 1500；受 --max-request-items 限制')
    ap.add_argument('--max-request-items',type=int,default=DEFAULT_FULL_MGET_MAX_ITEMS,help='单个 full/mget 的目标上限，默认 1500，服务端硬上限 3000；总章节数不受此限制')
    ap.add_argument('--sleep',type=float,default=0.2)
    ap.add_argument('--save-html',action='store_true')
    ap.add_argument('--items-file',type=Path,help='手动 item_id 列表：一行一个、JSON list、或 chapters.json')
    ap.add_argument('--directory-source',choices=['app','file'],default='app',help='目录来源；app 使用番茄畅听 App 接口，file 使用本地 item_id 文件')
    ap.add_argument('--overwrite',action='store_true',help='覆盖已存在章节；默认按 0001-*.txt 断点续传')
    ap.add_argument('--start',type=int,default=1,help='从第几章开始（1-based，对目录切片）')
    ap.add_argument('--end',type=int,default=0,help='下载到第几章（含，0 表示到末尾）')
    ap.add_argument('--single-file',action='store_true',help='快速模式：只生成合并 TXT，不写每章单独文件')
    ap.add_argument('--quiet',action='store_true',help='减少每章日志输出')
    ap.add_argument('--request-only',action='store_true',help='只请求 full/mget 并保存原始 JSON，不解密、不生成正文')
    ap.add_argument('--request-workers',type=int,default=1,help='single-file 或 --request-only 下的正文并发连接数，最多 5')
    ap.add_argument('--decrypt-workers',type=int,default=1,help='single-file 模式下并行解密进程数；要压速度可设为 CPU 核心数')
    ap.add_argument('--sign-mode',choices=['auto','pure3040','legacy3040','captured','appcaptured','fixed'],default='auto',help='full/mget 签名模式：auto/pure3040 都是本文件纯 Python 生成请求头；captured/appcaptured/fixed 仅保留为调试对照')
    ap.add_argument('--transport',choices=['auto','http1','http2'],default=FULL_MGET_TRANSPORT,help='full/mget 传输：auto 优先纯 Python HTTP/2，失败回退 HTTP/1.1')
    ap.add_argument('--search',help='使用番茄畅听 App 搜索接口搜索关键词，只打印结果，不下载')
    ap.add_argument('--search-tab',type=int,default=None,help='搜索 tab_type，可选；不填为综合搜索')
    ap.add_argument('--search-offset',type=int,default=0,help='搜索 offset')
    ap.add_argument('--search-limit',type=int,default=10,help='搜索 limit')
    ap.add_argument('--print-directory',action='store_true',help='只解析并打印/保存目录 item_id，不请求正文')
    ap.add_argument('--directory-output',type=Path,help='配合 --print-directory，把 item_id 写入文件')
    ap.add_argument('--verify-sign',action='store_true',help='只验证一次 full/mget 签名是否 code=0；不写文件、不解密')
    ap.add_argument('--probe',action='store_true',help='probe App directory/full/video/news endpoints only')
    ap.add_argument('--probe-item-id',action='append',default=[],help='item_id for --probe; can be repeated')
    ap.add_argument('--audio-type',default='short_play',help='video_model audio_type: book/news/short_play/podcast/radio/all or number')
    ap.add_argument('--download-media',action='store_true',help='download audio/video returned by video_model instead of text')
    ap.add_argument('--media-workers',type=int,default=4,help='parallel media downloads')
    ap.add_argument('--tone-id',type=int,default=91,help='video_model tone_id, default 91')
    ap.add_argument('--book-info-json',action='store_true',help='通过 App 详情接口获取书籍信息并输出 JSON')
    args=ap.parse_args()
    set_full_mget_transport(args.transport)
    raw_book = args.book_id or args.book_url
    if args.search:
        print_search_results(args.search,tab_type=args.search_tab,offset=args.search_offset,limit=args.search_limit,sign_mode=args.sign_mode)
        sys.exit(0)
    if not raw_book and not args.probe and not (args.download_media and args.probe_item_id):
        ap.error('必须提供 --book-id/--url；如果是新闻/单首音乐等无 book_id 媒体，可用 --download-media --probe-item-id <id>')
    book_id = resolve_book_id(raw_book) if raw_book else ''
    if args.book_info_json:
        print(json.dumps(book_detail(book_id), ensure_ascii=False))
    elif args.print_directory:
        print_directory_items(book_id,source=args.directory_source,items_file=args.items_file,output=args.directory_output)
    elif args.verify_sign:
        ids=unique_item_ids(args.probe_item_id, '')
        if not ids and args.items_file:
            ids=read_items_file(args.items_file, book_id)[:1]
        if not ids:
            ids=resolve_directory(book_id,args.directory_source,args.items_file)[:1]
        if not ids:
            raise SystemExit('无法取得 item_id，无法验证签名')
        result=verify_full_mget_sign(book_id, ids[0], args.sign_mode)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.probe:
        ids=unique_item_ids(args.probe_item_id, '')
        if not ids and args.items_file:
            ids=read_items_file(args.items_file, book_id)[:1]
        if not ids:
            try:
                ids=resolve_directory(book_id,args.directory_source,args.items_file)[:1]
            except Exception:
                ids=[]
        probe_app_content(book_id, ids, audio_type=args.audio_type, sign_mode=args.sign_mode)
    elif args.download_media:
        download_media(book_id,args.output,args.limit,args.batch_size,args.sleep,args.items_file,args.directory_source,args.overwrite,args.start,args.end,args.sign_mode,args.audio_type,args.tone_id,args.media_workers,args.probe_item_id)
    else:
        download(book_id,args.output,args.limit,args.batch_size,args.sleep,args.save_html,args.items_file,args.directory_source,args.overwrite,args.start,args.end,args.sign_mode,args.single_file,args.quiet,args.request_only,args.decrypt_workers,args.request_workers,args.max_request_items)

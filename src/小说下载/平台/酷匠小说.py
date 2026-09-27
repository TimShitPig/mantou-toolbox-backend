"""酷匠 App 详情、目录和正文；账号认证从部署环境读取。"""

import base64
import gzip
import os
import re
from urllib.parse import parse_qs, unquote, urlsplit

from .公共 import ProviderError
from .轻量请求 import clean, download_chapters, metadata, number, request_json, session

PLATFORM = {
    "id": "kujiang", "name": "酷匠小说",
    "hosts": ["kujiang.com", "www.kujiang.com", "app.kujiang.com"], "aliases": ["酷匠", "酷匠小说"],
    "coverHosts": ["bs.kjcdn.com"],
    "credentials": [
        {"env": "NOVEL_KUJIANG_CATALOG_AUTH_CODE", "label": "酷匠目录认证码", "defaultAvailable": True},
        {"env": "NOVEL_KUJIANG_READ_AUTH_CODE", "label": "酷匠正文认证码", "defaultAvailable": True},
        {"env": "NOVEL_KUJIANG_AUTH_CODE", "label": "酷匠旧版认证码", "required": False, "showInAdmin": False},
    ],
}
BASE = "https://app.kujiang.com/v1/book"
DEFAULT_CATALOG_AUTH_CODE = "dc67efdd82941586e69207b3374037b2"
DEFAULT_READ_AUTH_CODE = "440590fab1b828085ab67fdc9fc40cbb"


def identify(value: str) -> str:
    value = unquote(str(value or "").strip())
    if re.fullmatch(r"\d{1,30}", value):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in PLATFORM["hosts"]):
        return ""
    params = parse_qs(parsed.query)
    for key in ("book", "book_id", "bookId", "id"):
        candidate = str(params.get(key, [""])[0])
        if re.fullmatch(r"\d{1,30}", candidate):
            return candidate
    match = re.search(r"(?:book|novel|detail)[^0-9]{0,15}(\d{1,30})(?!\d)", parsed.path, re.I)
    return match.group(1) if match else ""


def headers():
    legacy = os.environ.get("NOVEL_KUJIANG_AUTH_CODE", "").strip()
    auth = os.environ.get("NOVEL_KUJIANG_READ_AUTH_CODE", "").strip() or legacy or DEFAULT_READ_AUTH_CODE
    return {"auth-code": auth, "app": "com.dpx.kujiang", "platform": "android", "device-uuid": "A589D18F6E1F84A2", "version": "3.9.14", "channel": "QQ", "User-Agent": "KuJiang/3.9.14(Android;P40;7.1.2)", "Accept": "application/json"}


def catalog_auth_code():
    legacy = os.environ.get("NOVEL_KUJIANG_AUTH_CODE", "").strip()
    return os.environ.get("NOVEL_KUJIANG_CATALOG_AUTH_CODE", "").strip() or legacy or DEFAULT_CATALOG_AUTH_CODE


def catalog_headers():
    return {"auth-code": catalog_auth_code(), "app": "com.dpx.kujiang", "platform": "android", "device-uuid": "5dd1f054b2f013b9", "version": "3.9.7", "channel": "XIAOMI", "User-Agent": "KuJiang/3.9.7", "Accept": "application/json"}


def success(data):
    if not isinstance(data, dict) or not isinstance(data.get("header"), dict) or str(data["header"].get("result")) != "0":
        raise ProviderError("provider_request_failed")
    return data.get("body")


async def load(client, book_id, auth):
    book_id = identify(book_id)
    if not book_id:
        raise ProviderError("invalid_book_id")
    detail = success(await request_json(client, "GET", f"{BASE}/get_book_infos", params={"book": book_id, "subsite": "m", "from": "search"}))
    raw = detail.get("bookinfo") if isinstance(detail, dict) else None
    if not isinstance(raw, dict):
        raise ProviderError("book_not_found")
    catalog = success(await request_json(client, "GET", f"{BASE}/catalog", params={"book": book_id, "auth_code": auth, "sort": "asc"}, headers=catalog_headers()))
    volumes = catalog.get("catalog") if isinstance(catalog, dict) else None
    if not isinstance(volumes, list):
        raise ProviderError("provider_invalid_response")
    chapters = []
    for volume in volumes:
        if not isinstance(volume, dict) or not isinstance(volume.get("chapters"), list):
            raise ProviderError("provider_invalid_response")
        for row in volume["chapters"]:
            if not isinstance(row, dict) or not str(row.get("chapter") or ""):
                raise ProviderError("provider_invalid_response")
            chapters.append({"id": str(row["chapter"]), "title": clean(row.get("v_chapter"))})
    if number(raw.get("chapter_count")) and len(chapters) != number(raw["chapter_count"]):
        raise ProviderError("chapter_catalog_incomplete")
    book = metadata(book_id, raw.get("v_book"), raw.get("penname"), raw.get("intro"), str(raw.get("fullflag", raw.get("status", ""))).lower() in {"1", "finish", "完结"}, raw.get("public_size") or raw.get("word_count"), chapters, raw)
    return book, chapters


def decrypt(value):
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import unpad
    except ImportError:
        raise ProviderError("provider_dependency_missing") from None
    try:
        if not isinstance(value, str) or len(value) <= 28:
            raise ValueError("empty")
        # App 的固定传输格式，密钥不是用户账号或认证码。
        encrypted = base64.b64decode(value[28:], validate=True)
        plain = unpad(AES.new(b"KujiangApp747605", AES.MODE_CBC, b"5efd3f6060e20330").decrypt(encrypted), AES.block_size)
        return clean(gzip.decompress(base64.b64decode(plain, validate=True)).decode("utf-8"))
    except (ValueError, UnicodeError, OSError, EOFError):
        raise ProviderError("chapter_decode_failed") from None


async def get_book(book_id):
    config = headers()
    async with session(config) as client:
        book, _ = await load(client, book_id, catalog_auth_code())
        return book


async def download_book(book_id, on_progress):
    config = headers()
    async with session(config) as client:
        book, chapters = await load(client, book_id, catalog_auth_code())

        async def fetch(chapter):
            raw = success(await request_json(client, "GET", f"{BASE}/read", params={"book": book["sourceBookId"], "chapter": chapter["id"]}))
            if not isinstance(raw, dict):
                raise ProviderError("chapter_content_empty")
            return decrypt(raw.get("content"))

        return {"book": book, "chapters": await download_chapters(chapters, fetch, on_progress)}

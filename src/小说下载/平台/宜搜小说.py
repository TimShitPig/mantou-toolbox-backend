"""宜搜 App API；只解析链接中的书籍编号，不请求分享网页。"""

import hashlib
import json
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .公共 import ProviderError
from .轻量请求 import clean, download_chapters, metadata, request, session

PLATFORM = {
    "id": "yisou", "name": "宜搜小说",
    "hosts": ["ieasou.com", "www.ieasou.com", "api.ieasou.com", "easou.com", "eayue.com", "www.eayue.com", "book.eayue.com"], "aliases": ["宜搜", "宜搜小说"],
    "coverHosts": [],
    "credentials": [],
}
BASE = "https://api.ieasou.com"
SIGNING_KEY = "EaSoU0517+PuBlIsHkEy-JRKKOWTUNZCNTWY-"
DEFAULT_ACCOUNT = {
    "session_id": "153F4EEE16F56A43FD63ZD21B866413ED9BE044EFB876A115C62DBED82EE4C824D",
    "udid": "3d3ec742930b635fc4c61f0575dbc4d2939edbe2",
    "birt": "1706674841000",
    "pushid": "7b4aaf1210a5bdbac3cea26d5030a419",
}


def identify(value: str) -> str:
    value = unquote(str(value or "").strip())
    if re.fullmatch(r"\d{1,30}_\d{1,30}", value):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in PLATFORM["hosts"]):
        return ""
    query = parse_qs(parsed.query)
    nid, gid = str(query.get("nid", [""])[0]), str(query.get("gid", [""])[0])
    if re.fullmatch(r"\d{1,30}", nid) and re.fullmatch(r"\d{1,30}", gid):
        return f"{nid}_{gid}"
    match = re.search(r"(?:book|novel|detail)[^0-9]{0,20}(\d{1,30})[_/-](\d{1,30})(?!\d)", parsed.path, re.I)
    return f"{match.group(1)}_{match.group(2)}" if match else ""


def account():
    return DEFAULT_ACCOUNT.copy()


def common(identity):
    timestamp = str(int(time.time() * 1000))
    return {"ac": "999", "appType": "0", "appid": "10001", "appverion": "508500", "bidType": "0", "ch": "blf1298_10928_001", "chType": "6", "cid": "eef_easou_book", "dzh": "1", "gender": "1", "instId": timestamp, "instime": timestamp, "os": "android", "pr": "-1.0", "ptype": "5", "recSw": "1", "rtype": "2", "scp": "0", "showj": "1", "tm": "0", "userInitPay": "3", "utype": "0", "vm": "5.8.5", **identity}


async def api(client, path, params):
    items = sorted((str(key), str(value)) for key, value in params.items() if key != "snk" and value not in (None, ""))
    signature = hashlib.md5(("&".join(f"{key}={value}" for key, value in items) + "&key=" + SIGNING_KEY).encode()).hexdigest().upper()
    body = await request(client, "GET", BASE + path, params={**params, "snk": signature})
    for candidate in (body, bytes(byte ^ 0xFF for byte in body)):
        try:
            data = json.loads(candidate.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            continue
        if isinstance(data, dict):
            if "success" in data and data.get("success") not in (True, 1, "1", "true"):
                raise ProviderError("provider_request_failed")
            return data
    raise ProviderError("provider_invalid_response")


def chapter_rows(data):
    pools = [data.get("chapters")]
    volumes = data.get("volumes", [])
    if isinstance(volumes, list):
        pools.extend(volume.get("chapters") for volume in volumes if isinstance(volume, dict))
    result = []
    for pool in pools:
        if pool is None:
            continue
        if not isinstance(pool, list):
            raise ProviderError("provider_invalid_response")
        for row in pool:
            if not isinstance(row, dict):
                raise ProviderError("provider_invalid_response")
            chapter_id = str(row.get("sort") or row.get("sequence") or "")
            if not chapter_id.isdigit():
                raise ProviderError("provider_invalid_response")
            result.append({"id": chapter_id, "title": clean(row.get("chapter_name") or row.get("name"))})
    return result


async def load(client, book_id, identity):
    book_id = identify(book_id)
    if not book_id:
        raise ProviderError("invalid_book_id")
    nid, gid = book_id.split("_", 1)
    params = {**common(identity), "ad": "0", "gid": gid, "nid": nid, "sort": "1", "size": "50", "returnType": "010", "gsort": "1"}
    detail = await api(client, "/api/bookapp/bookSummary.m", params)
    raw = detail.get("coverInfo")
    if not isinstance(raw, dict):
        raise ProviderError("book_not_found")
    chapters, seen = [], set()
    expected = 0
    try:
        expected = int(raw.get("chapterCount") or 0)
    except (ValueError, TypeError):
        pass
    for offset in range(1, 100001, 1000):
        data = await api(client, "/api/bookapp/bookSummary.m", {**params, **common(identity), "sort": str(offset), "size": "1000", "returnType": "100"})
        rows = chapter_rows(data)
        added = 0
        for chapter in rows:
            if chapter["id"] not in seen:
                seen.add(chapter["id"])
                chapters.append(chapter)
                added += 1
        if str(data.get("lastPage", "")).lower() in {"true", "1"} or len(rows) < 1000 or (expected and len(chapters) >= expected):
            break
        if not added:
            raise ProviderError("chapter_catalog_incomplete")
    else:
        raise ProviderError("chapter_catalog_incomplete")
    if expected and len(chapters) != expected:
        raise ProviderError("chapter_catalog_incomplete")
    chapters.sort(key=lambda chapter: int(chapter["id"]))
    book = metadata(book_id, raw.get("name"), raw.get("author"), raw.get("desc"), str(raw.get("status", "")).lower() in {"1", "2", "finish", "finished", "完本", "完结"}, raw.get("wordCount") or raw.get("words"), chapters, raw)
    return book, chapters


def decrypt(value):
    try:
        from Crypto.Cipher import DES
        from Crypto.Util.Padding import unpad
    except ImportError:
        raise ProviderError("provider_dependency_missing") from None
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]+", value) or len(value) % 2:
            raise ValueError("empty")
        # App 固定传输格式的协议常量。
        plain = unpad(DES.new(b"EaSoUcNt", DES.MODE_CBC, b"EaSoUcNt").decrypt(bytes.fromhex(value)), DES.block_size)
        return clean(plain.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ProviderError("chapter_decode_failed") from None


async def get_book(book_id):
    identity = account()
    async with session({"User-Agent": "esbook android 5.8.5"}) as client:
        book, _ = await load(client, book_id, identity)
        return book


async def download_book(book_id, on_progress):
    identity = account()
    async with session({"User-Agent": "esbook android 5.8.5"}) as client:
        book, chapters = await load(client, book_id, identity)
        nid, gid = book["sourceBookId"].split("_", 1)

        async def fetch(chapter):
            data = await api(client, "/api/bookapp/chargeChapter.m", {**common(identity), "a": "1", "autoBuy": "0", "gid": gid, "nid": nid, "sort": chapter["id"], "gsort": "0", "sgsort": "0", "sequence": "1"})
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            return decrypt(data.get("content") or nested.get("content"))

        return {"book": book, "chapters": await download_chapters(chapters, fetch, on_progress)}

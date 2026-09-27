"""米读 App API 与 App 目录、正文 CDN。"""

import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .公共 import ProviderError
from .轻量请求 import clean, download_chapters, metadata, request_json, session

PLATFORM = {
    "id": "midu", "name": "米读小说",
    "hosts": ["midureader.com", "www.midureader.com", "api.midureader.com", "book.midureader.com", "kuaittt.net", "www.kuaittt.net"], "aliases": ["米读", "米读小说"],
    "coverHosts": ["static.midureader.com"], "credentials": [],
}
BASE = "https://api.midureader.com"
CDN = "https://book.midureader.com"


def identify(value: str) -> str:
    value = unquote(str(value or "").strip())
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not any(host == domain or host.endswith("." + domain) for domain in PLATFORM["hosts"]):
        return ""
    params = parse_qs(parsed.query)
    for key in ("book_id", "bookId", "id"):
        candidate = str(params.get(key, [""])[0])
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", candidate):
            return candidate
    match = re.search(r"/(?:book|detail|fiction)/([A-Za-z0-9_-]{1,128})(?:/|$)", parsed.path, re.I)
    return match.group(1) if match else ""


def success(data):
    if not isinstance(data, dict) or str(data.get("code", "0")) not in {"0", "200"}:
        raise ProviderError("provider_request_failed")
    return data.get("data")


async def load(client, book_id):
    book_id = identify(book_id)
    if not book_id:
        raise ProviderError("invalid_book_id")
    raw = success(await request_json(client, "POST", f"{BASE}/fiction/book/getDetail", data={"app": "midu", "book_id": book_id, "source": "midu", "token": ""}))
    if not isinstance(raw, dict):
        raise ProviderError("book_not_found")
    rows = await request_json(client, "GET", f"{CDN}/book/chapter_list/100/{quote(book_id, safe='')}.txt")
    if not isinstance(rows, list):
        raise ProviderError("provider_invalid_response")
    chapters = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError("provider_invalid_response")
        chapter_id, digest = str(row.get("chapterId") or ""), str(row.get("content_md5") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chapter_id) or not re.fullmatch(r"[A-Fa-f0-9]{16,128}", digest):
            raise ProviderError("provider_invalid_response")
        chapters.append({"id": chapter_id, "md5": digest, "title": clean(row.get("title"))})
    book = metadata(book_id, raw.get("title") or raw.get("bookName"), raw.get("author"), raw.get("description"), str(raw.get("end_status", raw.get("status", ""))).lower() in {"1", "finish", "完结"}, raw.get("word_count") or raw.get("wordCount") or raw.get("words"), chapters, raw)
    return book, chapters


async def get_book(book_id):
    async with session({"User-Agent": "okhttp/3.12.1"}) as client:
        book, _ = await load(client, book_id)
        return book


async def download_book(book_id, on_progress):
    async with session({"User-Agent": "okhttp/3.12.1"}) as client:
        book, chapters = await load(client, book_id)

        async def fetch(chapter):
            data = await request_json(client, "GET", f"{CDN}/book/chapter/segment/master/{quote(book['sourceBookId'], safe='')}/{quote(chapter['id'], safe='')}/{chapter['md5']}.txt")
            rows = data.get("data") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ProviderError("provider_invalid_response")
            return "\n".join(clean(row.get("content")) for row in rows if isinstance(row, dict) and row.get("content")).strip()

        return {"book": book, "chapters": await download_chapters(chapters, fetch, on_progress)}

"""酷我小说 App JSON 接口。"""

import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .公共 import ProviderError
from .轻量请求 import clean, download_chapters, metadata, number, request_json, session

PLATFORM = {
    "id": "kuwo", "name": "酷我小说",
    "hosts": ["kuwo.cn", "www.kuwo.cn", "appi.kuwo.cn", "kuwo.com"], "aliases": ["酷我", "酷我小说"],
    "coverHosts": ["openbookcover.yuewen.com"], "credentials": [],
}
BASE = "https://appi.kuwo.cn/novels/api"


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
    for key in ("book_id", "bookId", "id"):
        candidate = str(params.get(key, [""])[0])
        if re.fullmatch(r"\d{1,30}", candidate):
            return candidate
    match = re.search(r"(?:book|novel|detail)[^0-9]{0,15}(\d{1,30})(?!\d)", parsed.path, re.I)
    return match.group(1) if match else ""


def success(data):
    if not isinstance(data, dict) or str(data.get("code")) != "200":
        raise ProviderError("provider_request_failed")
    return data.get("data")


async def load(client, book_id):
    book_id = identify(book_id)
    if not book_id:
        raise ProviderError("invalid_book_id")
    raw = success(await request_json(client, "GET", f"{BASE}/book/{quote(book_id)}"))
    if not isinstance(raw, dict):
        raise ProviderError("book_not_found")
    rows = success(await request_json(client, "GET", f"{BASE}/book/{quote(book_id)}/chapters", params={"paging": 0}))
    if not isinstance(rows, list):
        raise ProviderError("provider_invalid_response")
    chapters = []
    for row in rows:
        if not isinstance(row, dict) or not str(row.get("chapter_id") or ""):
            raise ProviderError("provider_invalid_response")
        chapters.append({"id": str(row["chapter_id"]), "title": clean(row.get("chapter_title"))})
    if number(raw.get("chapter_count")) and len(chapters) != number(raw["chapter_count"]):
        raise ProviderError("chapter_catalog_incomplete")
    book = metadata(book_id, raw.get("title"), raw.get("author_name"), raw.get("intro"), str(raw.get("status", "")).lower() in {"1", "50", "finish", "完结"}, raw.get("all_words") or raw.get("word_count"), chapters, raw)
    return book, chapters


async def get_book(book_id):
    async with session({"User-Agent": "okhttp/3.12.1"}) as client:
        book, _ = await load(client, book_id)
        return book


async def download_book(book_id, on_progress):
    async with session({"User-Agent": "okhttp/3.12.1"}) as client:
        book, chapters = await load(client, book_id)

        async def fetch(chapter):
            raw = success(await request_json(client, "GET", f"{BASE}/book/{quote(book['sourceBookId'])}/chapters/{quote(chapter['id'], safe='')}"))
            if not isinstance(raw, dict):
                raise ProviderError("chapter_content_empty")
            return clean(raw.get("content"))

        return {"book": book, "chapters": await download_chapters(chapters, fetch, on_progress)}

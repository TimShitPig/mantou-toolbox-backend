"""多个 App 小说适配器共用的有界请求和章节下载。"""

import asyncio
import html
import json
import re
from urllib.parse import urlsplit

import aiohttp

from .公共 import ProviderError


def clean(value):
    text = re.sub(r"<\s*br\s*/?>|</p\s*>", "\n", str(value or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()


def number(value):
    try:
        return max(0, int(str(value or "0").replace(",", "")))
    except (TypeError, ValueError):
        return 0


def cover(info):
    for field in ("coverUrl", "cover_url", "cover", "coverImage", "cover_img", "big_cover_img_url", "cover_img_url", "bookCover", "book_cover", "img", "img_url", "image", "pic", "pic_url", "big_cover", "imageUrl"):
        value = info.get(field)
        if isinstance(value, dict):
            value = value.get("url") or value.get("src")
        value = str(value or "").strip()
        if value.startswith("//"):
            value = "https:" + value
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
            return value
    return ""


def metadata(book_id, title, author, intro, status, words, chapters, raw):
    title = clean(title)
    if not title:
        raise ProviderError("book_not_found")
    return {
        "sourceBookId": str(book_id),
        "title": title,
        "author": clean(author) or "未知",
        "intro": clean(intro),
        "status": "已完结" if status else "连载中",
        "wordCount": number(words),
        "chapterCount": len(chapters),
        "coverUrl": cover(raw),
    }


def session(headers=None):
    return aiohttp.ClientSession(
        headers={"Accept": "application/json", **(headers or {})},
        timeout=aiohttp.ClientTimeout(total=30, sock_connect=10, sock_read=20),
        connector=aiohttp.TCPConnector(limit=4, limit_per_host=4, ttl_dns_cache=300),
    )


async def request(client, method, url, **kwargs):
    """只重试传输异常和临时 HTTP 错误；不把账号错误当成空内容。"""
    for attempt in range(3):
        try:
            async with client.request(method, url, allow_redirects=False, **kwargs) as response:
                if response.status in {401, 403}:
                    raise ProviderError("provider_auth_required")
                if response.status == 404:
                    raise ProviderError("book_not_found")
                if response.status == 429 or response.status >= 500:
                    if attempt < 2:
                        await asyncio.sleep(0.3 * (attempt + 1))
                        continue
                    raise ProviderError("provider_unavailable")
                if response.status != 200:
                    raise ProviderError("provider_request_failed")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 16 * 1024 * 1024:
                        raise ProviderError("provider_response_too_large")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == 2:
                raise ProviderError("provider_unavailable") from None
            await asyncio.sleep(0.3 * (attempt + 1))
    raise ProviderError("provider_unavailable")


async def request_json(client, method, url, **kwargs):
    body = await request(client, method, url, **kwargs)
    try:
        return json.loads(body.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise ProviderError("provider_invalid_response") from None


async def download_chapters(chapters, fetch, on_progress):
    if not chapters:
        raise ProviderError("chapters_not_found")
    results = [None] * len(chapters)
    position, completed = 0, 0
    on_progress(len(chapters), 0)

    async def worker():
        nonlocal position, completed
        while position < len(chapters):
            index = position
            position += 1
            chapter = chapters[index]
            content = await fetch(chapter)
            if not isinstance(content, str) or not content.strip():
                raise ProviderError("chapter_content_empty")
            results[index] = {"title": clean(chapter.get("title")) or f"第{index + 1}章", "content": content.strip()}
            completed += 1
            on_progress(len(chapters), completed)

    tasks = [asyncio.create_task(worker()) for _ in range(min(4, len(chapters)))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return results

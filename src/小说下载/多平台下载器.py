"""小说 App 适配器的命令行入口；stdout 只输出结构化进度与结果。"""
import asyncio
import contextlib
import importlib
import json
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

from 平台.公共 import ProviderError

ROOT = Path(__file__).resolve().parent
OUTPUT = sys.stdout


def emit(payload):
    OUTPUT.write(json.dumps(payload, ensure_ascii=False) + '\n')
    OUTPUT.flush()


def book_payload(book, book_id):
    if not isinstance(book, dict) or not isinstance(book.get('title'), str) or not book['title'].strip():
        raise ProviderError('book_not_found')
    result = {'sourceBookId': book_id}
    for name in ('title', 'author', 'status', 'intro', 'coverUrl'):
        value = book.get(name, '')
        if not isinstance(value, str):
            raise ProviderError('book_info_invalid')
        result[name] = value
    for name in ('wordCount', 'chapterCount'):
        value = book.get(name, '')
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ProviderError('book_info_invalid')
        result[name] = value
    return result


async def execute(request):
    source = str(request.get('source') or '')
    providers = json.loads((ROOT / '小说平台.json').read_text('utf-8'))
    provider = next((item for item in providers if item['id'] == source), None)
    if not provider or not provider.get('module'):
        raise ProviderError('unsupported_source')
    module = importlib.import_module('平台.' + provider['module'])
    action = request.get('action')
    if action == 'identify':
        value = str(request.get('link') or '').strip()
        prefixes = sorted([source, provider['name'], *provider.get('aliases', [])], key=len, reverse=True)
        for prefix in prefixes:
            match = re.match(re.escape(prefix) + r'\s*[:：]?\s*(.+)$', value, re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                break
        try:
            if source == 'qimao' and value.lower().startswith('freereader://'):
                params = parse_qs(urlsplit(value).query)
                value = str(json.loads(params.get('param', ['{}'])[0]).get('id') or '')
            else:
                addresses = re.findall(r'https?://[^\s<>"\']+', value)
                if addresses:
                    value = ''
                    for address in addresses:
                        address = address.rstrip('.,，。！!;；)）】')
                        hostname = (urlsplit(address).hostname or '').lower()
                        if any(hostname == host or hostname.endswith('.' + host) for host in provider['hosts']):
                            value = address
                            break
            book_id = str(module.identify(value) or '').strip()
        except (ValueError, TypeError, AttributeError):
            raise ProviderError('book_id_not_found')
        if not book_id or len(book_id) > 256 or any(ord(char) < 32 for char in book_id):
            raise ProviderError('book_id_not_found')
        emit({'type': 'result', 'bookId': book_id})
        return
    book_id = str(request.get('bookId') or '').strip()
    if not book_id or len(book_id) > 256:
        raise ProviderError('book_id_not_found')
    if action == 'detail':
        book = book_payload(await module.get_book(book_id), book_id)
        emit({'type': 'result', 'book': book})
        return
    if action != 'download':
        raise ProviderError('unsupported_action')
    last_progress = [0.0]

    def progress(total, completed):
        total = max(0, int(total))
        completed = max(0, min(total, int(completed)))
        now = time.monotonic()
        if now - last_progress[0] >= 0.25 or completed == total:
            last_progress[0] = now
            emit({'type': 'progress', 'total': total, 'completed': completed})

    result = await module.download_book(book_id, progress)
    book = result.get('book') if isinstance(result, dict) else None
    chapters = result.get('chapters') if isinstance(result, dict) else None
    book = book_payload(book, book_id)
    if not isinstance(chapters, list) or not chapters:
        raise ProviderError('catalog_empty')
    declared = str(book.get('chapterCount') or '').strip().removesuffix('章')
    if declared.isdigit() and int(declared) > 0 and int(declared) != len(chapters):
        raise ProviderError('download_incomplete')
    sections = []
    for index, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, dict) or not isinstance(chapter.get('content'), str) or not chapter['content'].strip():
            raise ProviderError('chapter_unavailable')
        if chapter.get('title') is not None and not isinstance(chapter['title'], str):
            raise ProviderError('chapter_unavailable')
        title = str(chapter.get('title') or f'第{index}章').strip()
        content = str(chapter['content']).replace('\r\n', '\n').replace('\r', '\n').strip()
        sections.append(title + '\n\n' + content)
    output_dir = Path(request['outputDir']).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / 'body.txt'
    output_path.write_text('\n\n'.join(sections) + '\n', encoding='utf-8')
    emit({'type': 'result', 'book': book, 'chapterCount': len(chapters), 'outputPath': str(output_path)})


def main():
    try:
        request = json.loads(sys.stdin.read(65537))
        if not isinstance(request, dict):
            raise ProviderError('invalid_request')
        with contextlib.redirect_stdout(sys.stderr):
            asyncio.run(execute(request))
    except ProviderError as error:
        code = error.code if re.fullmatch(r'[a-z0-9_:-]{1,100}', str(error.code)) else 'request_failed'
        emit({'type': 'error', 'code': code})
        return 1
    except ModuleNotFoundError:
        emit({'type': 'error', 'code': 'runtime_dependency_missing'})
        return 1
    except (asyncio.TimeoutError, TimeoutError):
        emit({'type': 'error', 'code': 'request_timeout'})
        return 1
    except Exception:
        emit({'type': 'error', 'code': 'request_failed'})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

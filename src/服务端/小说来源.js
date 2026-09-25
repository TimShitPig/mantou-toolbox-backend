const fs = require('node:fs/promises')
const { getFanqieBookDetails } = require('./番茄小说下载')

const MAX_EXPORT_BYTES = 10 * 1024 * 1024

function text(value, maximum = 4096) {
  return String(value ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim().slice(0, maximum)
}

function normalizeStatus(value) {
  const normalized = text(value, 64).toLowerCase()
  if (['0', '2', 'finished', 'completed', 'complete', 'ended', '完结', '已完结', '完本'].includes(normalized)) {
    return '已完结'
  }
  if (['1', '3', '4', 'ongoing', 'serial', 'serializing', '连载', '连载中'].includes(normalized)) {
    return '连载中'
  }
  return text(value, 64) || '待获取'
}

function formatWordCount(value) {
  const parsed = Number(value || 0)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return text(value, 64) || '待获取'
  }
  if (parsed >= 10000) {
    return `${Math.round(parsed / 1000) / 10}万字`.replace('.0万字', '万字')
  }
  return `${parsed}字`
}

function identifyNovelLink(link) {
  const input = text(link, 4096)
  if (!input) {
    throw Object.assign(new Error('novel_link_required'), { status: 400 })
  }

  const qimaoMatch = input.match(/(?:https?:\/\/)?(?:m\.|www\.)?qimao\.com\/(?:shuku|book)\/(\d+)/i)
    || input.match(/(?:https?:\/\/)?app-share\.wtzw\.com\/app-h5\/freebook\/(?:article|book)-detail\/(\d+)/i)
    || input.match(/freereader:\/\/reader_detail\?param=\{"id":"(\d+)"/i)
    || input.match(/wtzw\.com.*?(\d{6,})/i)
  const fanqieMatch = input.match(/(?:https?:\/\/)?(?:m\.|www\.)?fanqienovel\.com\/page\/(\d+)/i)
    || input.match(/(?:https?:\/\/)?(?:m\.|www\.)?changdunovel\.com\/t\/([a-zA-Z0-9_]+)\/?/i)
    || input.match(/(?:https?:\/\/)?(?:m\.|www\.)?changdunovel\.com.*?book_id=(\d+)/i)
    || input.match(/(?:https?:\/\/)?(?:m\.|www\.)?novelfm\.com\/s\/([a-zA-Z0-9_]+)\/?/i)
    || input.match(/fqnovel\.com.*?book_id=(\d+)/i)
    || input.match(/book_id=(\d+)/i)

  if (qimaoMatch) {
    return {
      source: 'qimao',
      sourceBookId: qimaoMatch[1],
      originalUrl: `https://m.qimao.com/shuku/${qimaoMatch[1]}/`,
    }
  }
  if (fanqieMatch) {
    const isNumeric = /^\d+$/.test(fanqieMatch[1])
    return {
      source: 'fanqie',
      sourceBookId: fanqieMatch[1],
      originalUrl: isNumeric
        ? `https://fanqienovel.com/page/${fanqieMatch[1]}`
        : `https://changdunovel.com/t/${fanqieMatch[1]}/`,
    }
  }

  const qimaoHint = /qimao\.com|wtzw\.com|freereader:\/\//i.test(input)
  const fanqieHint = /fanqienovel\.com|fqnovel\.com|changdunovel\.com|novelfm\.com|iesdouyin\.com|book_id=/i.test(input)
  if (qimaoHint) {
    throw Object.assign(new Error('qimao_book_id_not_found'), { status: 400 })
  }
  if (fanqieHint) {
    throw Object.assign(new Error('fanqie_book_id_not_found'), { status: 400 })
  }
  throw Object.assign(new Error('unsupported_link'), { status: 400 })
}

function fallbackBook(identity) {
  const platformName = identity.source === 'qimao' ? '七猫小说' : '番茄小说'
  return {
    coverUrl: '',
    title: `${platformName} ${identity.sourceBookId}`,
    author: '待获取',
    status: '待获取',
    wordCount: '待获取',
    chapterCount: '待获取',
    intro: '已识别小说链接，等待内容提供者返回书籍详情。',
    source: identity.source,
    sourceBookId: identity.sourceBookId,
    originalUrl: identity.originalUrl,
  }
}

function pickFirst(source, names) {
  for (const name of names) {
    const value = source && source[name]
    if (value !== undefined && value !== null && String(value).trim()) {
      return value
    }
  }
  return ''
}

function fanqieCoverUrl(source) {
  const direct = pickFirst(source, ['coverUrl', 'cover', 'cover_url', 'thumb_url', 'audio_thumb_uri', 'image'])
  if (direct) return direct
  try {
    const variants = JSON.parse(String(source && source.thumb_url_map_v2 || '{}'))
    return pickFirst(variants, ['large_square_thumb_url', 'small_square_thumb_url'])
  } catch {
    return ''
  }
}

function normalizeBook(candidate, identity) {
  const source = candidate && typeof candidate === 'object' ? candidate : {}
  return {
    coverUrl: text(fanqieCoverUrl(source), 2048),
    title: text(pickFirst(source, ['title', 'bookName', 'book_name', 'name']), 256)
      || fallbackBook(identity).title,
    author: text(pickFirst(source, ['author', 'authorName', 'author_name']), 256) || '待获取',
    status: normalizeStatus(pickFirst(source, ['status', 'bookStatus', 'book_status'])),
    wordCount: formatWordCount(pickFirst(source, ['wordCount', 'word_count', 'word_number', 'words', 'wordNum'])),
    chapterCount: text(pickFirst(source, ['chapterCount', 'chapter_count', 'chapter_number', 'serial_count', 'chapters']), 64) || '待获取',
    intro: text(pickFirst(source, ['intro', 'introduction', 'description', 'abstract']), 8000)
      || fallbackBook(identity).intro,
    source: identity.source,
    sourceBookId: identity.sourceBookId,
    originalUrl: identity.originalUrl,
  }
}

async function requestJson(url, config) {
  const response = await fetch(url, {
    headers: {
      Accept: 'application/json, text/plain, */*',
      'User-Agent': 'MantouToolboxBackend/1.0',
    },
    signal: AbortSignal.timeout(config.remoteRequestTimeoutMs),
  })
  if (!response.ok) {
    throw new Error(`metadata_status_${response.status}`)
  }
  return response.json()
}

async function fetchRemoteMetadata(identity, config) {
  const endpoint = new URL('https://api-bc.wtzw.com/api/v1/h5/adapt-reader')
  endpoint.searchParams.set('book_id', identity.sourceBookId)
  endpoint.searchParams.set('page', '1')
  const payload = await requestJson(endpoint, config)
  const candidate = payload && (payload.data || payload.book || payload)
  return normalizeBook(candidate, identity)
}

async function fetchFanqieAppMetadata(identity) {
  const detail = await getFanqieBookDetails(identity.sourceBookId)
  return normalizeBook(detail && (detail.data || detail.book || detail), identity)
}

async function parseNovel(link, config) {
  const identity = identifyNovelLink(link)
  let book = fallbackBook(identity)
  let warning = ''

  if (identity.source === 'fanqie') {
    try {
      book = await fetchFanqieAppMetadata(identity)
    } catch (error) {
      warning = `fanqie_app_metadata_failed: ${text(error && error.message, 160)}`
    }
  } else if (config.remoteMetadataEnabled) {
    try {
      book = await fetchRemoteMetadata(identity, config)
    } catch (error) {
      warning = `metadata_fetch_failed: ${text(error && error.message, 160)}`
    }
  } else {
    warning = 'remote_metadata_disabled'
  }

  return { identity, book, warning }
}

function normalizeRequestedBook(book, identity) {
  const normalized = normalizeBook(book, identity)
  if (normalized.title === fallbackBook(identity).title && text(book && book.title)) {
    normalized.title = text(book.title, 256)
  }
  return normalized
}

function contentFromChapters(chapters) {
  if (!Array.isArray(chapters)) {
    return ''
  }
  return chapters
    .slice(0, 5000)
    .map((chapter, index) => {
      const item = chapter && typeof chapter === 'object' ? chapter : {}
      const title = text(item.title, 512) || `Chapter ${index + 1}`
      const content = text(item.content, 1024 * 1024)
      return `${title}\n\n${content}`.trim()
    })
    .filter(Boolean)
    .join('\n\n')
}

async function readCatalogContent(config, book) {
  if (!config.contentCatalogFile) {
    return ''
  }

  let records
  try {
    records = JSON.parse(await fs.readFile(config.contentCatalogFile, 'utf8'))
  } catch {
    throw new Error('content_catalog_unavailable')
  }
  if (!Array.isArray(records)) {
    throw new Error('content_catalog_invalid')
  }

  const record = records.find((item) => item
    && String(item.source || '').trim().toLowerCase() === book.source
    && String(item.sourceBookId || '').trim() === book.sourceBookId)
  if (!record) {
    return ''
  }
  return text(record.content, MAX_EXPORT_BYTES) || contentFromChapters(record.chapters)
}

async function requestProviderContent(config, book, link) {
  if (!config.contentProviderUrl) {
    return ''
  }
  let response
  try {
    response = await fetch(config.contentProviderUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ book, link }),
      signal: AbortSignal.timeout(config.remoteRequestTimeoutMs * 3),
    })
  } catch {
    throw new Error('content_provider_unavailable')
  }
  if (!response.ok) {
    throw new Error(`content_provider_status_${response.status}`)
  }
  let payload
  try {
    payload = await response.json()
  } catch {
    throw new Error('content_provider_invalid_json')
  }
  return text(payload && payload.text, MAX_EXPORT_BYTES) || contentFromChapters(payload && payload.chapters)
}

function metadataExport(book) {
  return [
    book.title,
    `作者：${book.author}`,
    `状态：${book.status}`,
    `字数：${book.wordCount}`,
    `章节：${book.chapterCount}`,
    '',
    '---',
    '本文件由本地开发后端生成，当前只包含书籍资料和来源链接。',
    '配置 CONTENT_CATALOG_FILE 或 CONTENT_PROVIDER_URL 后可导出已接入的正文内容。',
    '',
    '简介：',
    book.intro,
    '',
    `来源：${book.originalUrl}`,
    '',
  ].join('\n')
}

async function buildDownloadText(config, book, link) {
  const catalogContent = await readCatalogContent(config, book)
  const providerContent = catalogContent || await requestProviderContent(config, book, link)
  return providerContent || metadataExport(book)
}

function safeFileStem(value) {
  return text(value, 72)
    .replace(/[\\/:*?"<>|]/g, '_')
    .replace(/\s+/g, ' ')
    .trim() || 'novel'
}

function buildFileName(book) {
  return `${safeFileStem(book.title)}.txt`
}

module.exports = {
  buildDownloadText,
  buildFileName,
  identifyNovelLink,
  normalizeRequestedBook,
  parseNovel,
}

const { spawn } = require('node:child_process')
const fs = require('node:fs/promises')
const path = require('node:path')
const readline = require('node:readline')

const DOWNLOADER_PATH = path.resolve(__dirname, '..', '小说下载', '番茄正文下载器.py')
const MAX_PROCESS_OUTPUT_BYTES = 1024 * 1024

function safeDiagnostic(lines) {
  return lines
    .filter((line) => line && !line.startsWith('Traceback'))
    .slice(-3)
    .join(' ')
    .replace(/https?:\/\/[^\s'"<>]+/g, '[url]')
    .slice(0, 400)
}

function assertBookId(bookId) {
  const normalized = String(bookId || '').trim()
  if (!/^\d{8,}$/.test(normalized)) {
    throw new Error('fanqie_book_id_invalid')
  }
  return normalized
}

function runDownloader(args, onStdoutLine, failureCode) {
  const python = process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3')
  const child = spawn(python, [DOWNLOADER_PATH, ...args], {
    cwd: path.dirname(DOWNLOADER_PATH),
    env: {
      ...process.env,
      PYTHONDONTWRITEBYTECODE: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
    },
    windowsHide: true,
  })

  const diagnostics = []
  const stdoutLines = []
  let stdoutBytes = 0
  let outputTooLarge = false
  const stdout = readline.createInterface({ input: child.stdout, crlfDelay: Infinity })
  const stderr = readline.createInterface({ input: child.stderr, crlfDelay: Infinity })
  stdout.on('line', (line) => {
    stdoutBytes += Buffer.byteLength(line, 'utf8') + 1
    if (stdoutBytes > MAX_PROCESS_OUTPUT_BYTES) {
      outputTooLarge = true
      child.kill()
      return
    }
    stdoutLines.push(line)
    if (typeof onStdoutLine === 'function') {
      try {
        onStdoutLine(line)
      } catch {}
    }
  })
  stderr.on('line', (line) => {
    diagnostics.push(line.slice(0, 500))
    if (diagnostics.length > 12) diagnostics.shift()
  })

  return new Promise((resolve, reject) => {
    child.once('error', (error) => {
      reject(new Error(error && error.code === 'ENOENT' ? 'python_runtime_unavailable' : 'fanqie_downloader_start_failed'))
    })
    child.once('close', (code, signal) => {
      if (outputTooLarge) {
        reject(new Error('fanqie_downloader_output_too_large'))
        return
      }
      if (code === 0) {
        resolve(stdoutLines.join('\n'))
        return
      }
      const detail = safeDiagnostic(diagnostics)
      reject(new Error(`${failureCode}${detail ? `: ${detail}` : `: exit=${code ?? signal ?? 'unknown'}`}`))
    })
  })
}

async function getFanqieBookDetails(bookId) {
  const normalizedBookId = assertBookId(bookId)
  const output = await runDownloader([
    '--book-id', normalizedBookId,
    '--book-info-json',
  ], null, 'fanqie_app_detail_failed')
  try {
    const detail = JSON.parse(output)
    if (!detail || typeof detail !== 'object' || Array.isArray(detail)) {
      throw new Error('invalid_detail')
    }
    return detail
  } catch {
    throw new Error('fanqie_app_detail_invalid_json')
  }
}

async function downloadFanqieNovel({ bookId, outputDir, onProgress }) {
  const normalizedBookId = assertBookId(bookId)
  await fs.mkdir(outputDir, { recursive: true })
  const temporaryDir = await fs.mkdtemp(path.join(outputDir, '.fanqie-'))
  let chapterCount = 0
  let completed = 0
  let mergedPath = ''

  function reportProgress(total, done) {
    chapterCount = Math.max(chapterCount, Number(total) || 0)
    completed = Math.max(completed, Math.min(chapterCount, Number(done) || 0))
    if (typeof onProgress === 'function' && chapterCount > 0) {
      try {
        onProgress({ total: chapterCount, completed })
      } catch {}
    }
  }

  try {
    await runDownloader([
      '--book-id', normalizedBookId,
      '--output', temporaryDir,
      '--directory-source', 'app',
      '--batch-size', '1500',
      '--max-request-items', '1500',
      '--single-file',
      '--quiet',
      '--request-workers', '1',
    ], (line) => {
      const count = line.match(/章节数\s*[:：]\s*(\d+)/)
      if (count) reportProgress(Number(count[1]), 0)

      const batch = line.match(/\[批次完成\].*?total=(\d+)\/(\d+)/)
      if (batch) reportProgress(Number(batch[2]), Number(batch[1]))

      const output = line.match(/^\s*合并 TXT:\s*(.+?)\s*$/)
      if (output) mergedPath = output[1]
    }, 'fanqie_download_failed')

    if (!mergedPath) {
      throw new Error('fanqie_download_output_missing')
    }
    const resolvedPath = path.resolve(mergedPath)
    const relativePath = path.relative(temporaryDir, resolvedPath)
    if (!relativePath || relativePath.startsWith('..') || path.isAbsolute(relativePath)) {
      throw new Error('fanqie_download_output_invalid')
    }

    const output = await fs.readFile(resolvedPath)
    const text = output.toString('utf8')
    if (!text.trim()) {
      throw new Error('fanqie_download_content_empty')
    }

    let records
    try {
      records = JSON.parse(await fs.readFile(path.join(path.dirname(resolvedPath), 'chapters.json'), 'utf8'))
    } catch {
      throw new Error('fanqie_download_chapter_index_invalid')
    }
    if (!Array.isArray(records) || !records.length) {
      throw new Error('fanqie_download_chapters_empty')
    }
    const successful = records.filter((record) => record && !record.error).length
    if (successful !== records.length) {
      throw new Error(`fanqie_download_incomplete:${successful}/${records.length}`)
    }

    reportProgress(records.length, records.length)
    return {
      text,
      fileName: path.basename(resolvedPath),
      chapterCount: records.length,
    }
  } finally {
    await fs.rm(temporaryDir, { recursive: true, force: true })
  }
}

module.exports = { downloadFanqieNovel, getFanqieBookDetails }

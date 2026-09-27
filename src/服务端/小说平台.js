const { spawn } = require('node:child_process')
const fs = require('node:fs/promises')
const path = require('node:path')
const readline = require('node:readline')
const providers = require('../小说下载/小说平台.json')

const RUNNER = path.resolve(__dirname, '../小说下载/多平台下载器.py')
const MAX_STDOUT_BYTES = 2 * 1024 * 1024

function getNovelProviders() {
  return providers
}

function getNovelProvider(source) {
  return providers.find((item) => item.id === source) || null
}

function detectNovelSource(input) {
  const value = String(input || '').trim()
  const matches = new Set()
  const addresses = value.match(/https?:\/\/[^\s<>"']+/gi) || []
  for (const address of addresses) {
    try {
      const hostname = new URL(address).hostname.toLowerCase()
      for (const provider of providers) {
        if (provider.hosts.some((host) => hostname === host || hostname.endsWith(`.${host}`))) matches.add(provider.id)
      }
    } catch {}
  }
  if (matches.size > 1) throw Object.assign(new Error('ambiguous_novel_link'), { status: 400 })
  if (matches.size === 1) return [...matches][0]
  if (addresses.length) return ''
  if (/^freereader:\/\//i.test(value)) return 'qimao'
  for (const provider of providers) {
    if (value.toLowerCase().startsWith(`${provider.id}:`)
      || [provider.name, ...(provider.aliases || [])].some((alias) => value.startsWith(alias))) return provider.id
  }
  if (/^book_id=\d+$/i.test(value)) return 'fanqie'
  try {
    const hostname = new URL(`https://${value}`).hostname.toLowerCase()
    const provider = providers.find((item) => item.hosts.some((host) => hostname === host || hostname.endsWith(`.${host}`)))
    if (provider) return provider.id
  } catch {}
  return ''
}

function providerEnvironment(config = {}) {
  return { ...process.env, ...(config.novelProviderEnv || {}) }
}

function pythonProviderEnvironment(provider, config) {
  const source = providerEnvironment(config)
  const sourceEntry = (name) => {
    const key = Object.keys(source).find((entry) => entry.toLowerCase() === name.toLowerCase())
    return key ? source[key] : ''
  }
  const env = {
    PATH: sourceEntry('PATH'),
    PYTHONDONTWRITEBYTECODE: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
  }
  for (const key of ['SYSTEMROOT', 'WINDIR', 'TMP', 'TEMP', 'HOME', 'LANG', 'LC_ALL', 'PYTHONPATH', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy']) {
    const value = sourceEntry(key)
    if (value) env[key] = value
  }
  for (const field of provider.credentials || []) {
    if (source[field.env]) env[field.env] = source[field.env]
  }
  return env
}

function novelProviderStates(config = {}, admin = false) {
  const env = providerEnvironment(config)
  return providers.map((provider) => ({
    id: provider.id,
    name: provider.name,
    hosts: provider.hosts,
    aliases: provider.aliases || [],
    enabled: config[`${provider.id}Enabled`] !== false,
    configured: (provider.credentials || []).filter((field) => field.required !== false).every((field) => field.defaultAvailable === true || Boolean(String(env[field.env] || '').trim())),
    ...(admin ? { credentials: (provider.credentials || []).filter((field) => field.showInAdmin !== false).map((field) => ({ ...field, configured: field.defaultAvailable === true || Boolean(String(env[field.env] || '').trim()) })) } : {}),
  }))
}

function providerError(source, code) {
  const normalized = /^[a-z0-9_:-]{1,100}$/.test(String(code)) ? code : 'request_failed'
  return Object.assign(new Error(`${source}_${normalized}`), { status: normalized === 'credentials_required' ? 503 : 502, expose: true })
}

function runProvider(source, action, input, config = {}, onProgress) {
  const provider = getNovelProvider(source)
  if (!provider || !provider.module) return Promise.reject(providerError(source, 'unsupported_source'))
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3'), [RUNNER], {
      cwd: path.dirname(RUNNER),
      env: pythonProviderEnvironment(provider, config),
      windowsHide: true,
    })
    const timeout = action === 'download' ? 30 * 60 * 1000 : (action === 'identify' ? 10000 : 30000)
    let settled = false
    let result = null
    let errorCode = ''
    let stdoutBytes = 0
    const finish = (error, value) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      if (error) reject(error)
      else resolve(value)
    }
    const timer = setTimeout(() => {
      child.kill()
      finish(providerError(source, 'request_timeout'))
    }, timeout)
    const stdout = readline.createInterface({ input: child.stdout, crlfDelay: Infinity })
    child.stderr.resume()
    child.stdout.on('data', (chunk) => {
      stdoutBytes += chunk.length
      if (stdoutBytes > MAX_STDOUT_BYTES) {
        child.kill()
        finish(providerError(source, 'output_too_large'))
      }
    })
    stdout.on('line', (line) => {
      let message
      try { message = JSON.parse(line) } catch { return }
      if (!message || typeof message !== 'object' || Array.isArray(message)) return
      if (message.type === 'result') result = message
      if (message.type === 'error') errorCode = message.code
      if (message.type === 'progress' && typeof onProgress === 'function') {
        const total = Math.max(0, Math.trunc(Number(message.total) || 0))
        const completed = Math.max(0, Math.min(total, Math.trunc(Number(message.completed) || 0)))
        try { onProgress({ total, completed }) } catch {}
      }
    })
    child.once('error', (error) => finish(providerError(source, error.code === 'ENOENT' ? 'python_runtime_unavailable' : 'runner_start_failed')))
    child.stdin.on('error', () => {})
    child.once('close', (code) => {
      if (code === 0 && result) finish(null, result)
      else finish(providerError(source, errorCode || 'request_failed'))
    })
    child.stdin.end(JSON.stringify({ source, action, ...input }))
  })
}

async function identifyProviderBook(source, link) {
  const result = await runProvider(source, 'identify', { link })
  const bookId = String(result.bookId || '').trim()
  if (!bookId || bookId.length > 256 || /[\r\n\0]/.test(bookId)) throw providerError(source, 'book_id_not_found')
  return bookId
}

async function getProviderBook(source, bookId, config) {
  const result = await runProvider(source, 'detail', { bookId }, config)
  if (!result.book || typeof result.book !== 'object' || Array.isArray(result.book)) throw providerError(source, 'book_not_found')
  return result.book
}

async function downloadProviderNovel({ source, bookId, outputDir, onProgress, config }) {
  await fs.mkdir(outputDir, { recursive: true })
  const temporaryDir = await fs.mkdtemp(path.join(outputDir, `.${source}-`))
  try {
    const result = await runProvider(source, 'download', { bookId, outputDir: temporaryDir }, config, onProgress)
    const outputPath = path.resolve(String(result.outputPath || ''))
    if (outputPath !== path.join(temporaryDir, 'body.txt')) throw providerError(source, 'output_invalid')
    const text = await fs.readFile(outputPath, 'utf8')
    if (!text.trim() || !Number.isInteger(result.chapterCount) || result.chapterCount < 1) throw providerError(source, 'chapter_unavailable')
    return { text, book: result.book, chapterCount: result.chapterCount }
  } finally {
    await fs.rm(temporaryDir, { recursive: true, force: true })
  }
}

module.exports = { getNovelProviders, getNovelProvider, detectNovelSource, novelProviderStates, identifyProviderBook, getProviderBook, downloadProviderNovel }

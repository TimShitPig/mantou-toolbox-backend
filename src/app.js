const crypto = require('node:crypto')
const fs = require('node:fs/promises')
const http = require('node:http')
const path = require('node:path')
const {
  clearSessionCookie,
  createSessionCookie,
  requireAdmin,
  revokeAdminSession,
  verifyAdminPassword,
} = require('./admin-auth')
const { createAuth, AuthError } = require('./auth')
const { createConfig } = require('./config')
const { createDatabase } = require('./database')
const {
  ApiError,
  corsHeaders,
  publicUrl,
  readJson,
  readMultipartForm,
  requestOrigin,
  sanitizeMeta,
  sendEmpty,
  sendError,
  sendJson,
} = require('./http')
const {
  buildDownloadText,
  buildFileName,
  identifyNovelLink,
  normalizeRequestedBook,
  parseNovel,
} = require('./providers')

const MAX_AVATAR_BYTES = 5 * 1024 * 1024
const MAX_PROXY_BYTES = 5 * 1024 * 1024

function assertMethod(req, expected) {
  if (req.method !== expected) {
    throw new ApiError(405, 'method_not_allowed')
  }
}

function normalizeShortText(value, maximum) {
  return String(value ?? '').trim().slice(0, maximum)
}

function startOfUtcDay() {
  const now = new Date()
  return Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())
}

function imageFileType(buffer) {
  if (buffer.length >= 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) {
    return { extension: 'jpg', contentType: 'image/jpeg' }
  }
  if (buffer.length >= 8 && buffer.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
    return { extension: 'png', contentType: 'image/png' }
  }
  if (buffer.length >= 6 && (buffer.subarray(0, 6).toString('ascii') === 'GIF87a' || buffer.subarray(0, 6).toString('ascii') === 'GIF89a')) {
    return { extension: 'gif', contentType: 'image/gif' }
  }
  if (buffer.length >= 12 && buffer.subarray(0, 4).toString('ascii') === 'RIFF' && buffer.subarray(8, 12).toString('ascii') === 'WEBP') {
    return { extension: 'webp', contentType: 'image/webp' }
  }
  return null
}

function isAllowedImageHost(hostname, allowlist) {
  const host = String(hostname || '').toLowerCase().replace(/\.$/, '')
  return allowlist.some((allowed) => host === allowed || host.endsWith(`.${allowed}`))
}

function assertOwnedJob(job, session) {
  if (!job) {
    throw new ApiError(404, 'download_not_found')
  }
  if (job.ownerUserId && (!session || job.ownerUserId !== session.user.id)) {
    throw new ApiError(403, 'download_forbidden')
  }
}

function statusManifest(config, store, session) {
  const used = store.countCompletedJobsSince(session && session.user.id, startOfUtcDay())
  const limitEnabled = config.downloadLimit > 0
  const remaining = limitEnabled ? Math.max(0, config.downloadLimit - used) : null
  return {
    enabled: config.downloadEnabled,
    parseEnabled: config.parseEnabled,
    downloadLimitEnabled: limitEnabled,
    downloadLimit: config.downloadLimit,
    downloadLimitPrompt: limitEnabled ? 'download_limit_reached' : '',
    downloadCountUsed: used,
    downloadCountRemaining: remaining,
    downloadCountReached: limitEnabled && used >= config.downloadLimit,
    qimaoEnabled: config.qimaoEnabled,
    fanqieEnabled: config.fanqieEnabled,
    disabledPrompt: config.downloadEnabled ? '' : 'download_disabled',
    parseDisabledPrompt: config.parseEnabled ? '' : 'parse_disabled',
    rewardedAdEnabled: config.rewardedAdEnabled,
    rewardedAdEveryDownloads: config.rewardedAdEveryDownloads,
    rewardedAdNotice: '',
    cloudDirectLinkEnabled: config.cloudDirectLinkEnabled,
    fanqieKeysConfigured: config.fanqieKeysConfigured,
  }
}

const ADMIN_SETTING_DEFAULTS = [
  'downloadEnabled',
  'parseEnabled',
  'qimaoEnabled',
  'fanqieEnabled',
  'downloadLimit',
  'rewardedAdEnabled',
  'rewardedAdEveryDownloads',
  'cloudDirectLinkEnabled',
]

function adminSettingsView(config) {
  return Object.fromEntries(ADMIN_SETTING_DEFAULTS.map((key) => [key, config[key]]))
}

function validateAdminSettings(patch) {
  const allowed = new Set(ADMIN_SETTING_DEFAULTS)
  const result = {}
  for (const [key, value] of Object.entries(patch || {})) {
    if (!allowed.has(key)) {
      throw new ApiError(400, `admin_setting_not_allowed:${key}`)
    }
    if (['downloadEnabled', 'parseEnabled', 'qimaoEnabled', 'fanqieEnabled', 'rewardedAdEnabled', 'cloudDirectLinkEnabled'].includes(key)) {
      if (typeof value !== 'boolean') {
        throw new ApiError(400, `admin_setting_invalid:${key}`)
      }
      result[key] = value
      continue
    }
    const numeric = Number(value)
    if (!Number.isInteger(numeric)) {
      throw new ApiError(400, `admin_setting_invalid:${key}`)
    }
    const maximum = key === 'downloadLimit' ? 1000000 : 1000
    const minimum = key === 'downloadLimit' ? 0 : 1
    if (numeric < minimum || numeric > maximum) {
      throw new ApiError(400, `admin_setting_out_of_range:${key}`)
    }
    result[key] = numeric
  }
  if (!Object.keys(result).length) {
    throw new ApiError(400, 'admin_settings_empty')
  }
  return result
}

function assertSourceEnabled(identity, config) {
  if (identity.source === 'qimao' && !config.qimaoEnabled) {
    throw new ApiError(503, 'qimao_disabled')
  }
  if (identity.source === 'fanqie' && !config.fanqieEnabled) {
    throw new ApiError(503, 'fanqie_disabled')
  }
}

async function fetchAllowedImage(url, config) {
  let target
  try {
    target = new URL(url)
  } catch {
    throw new ApiError(400, 'image_proxy_url_invalid')
  }
  for (let redirects = 0; redirects <= 3; redirects += 1) {
    if (!['http:', 'https:'].includes(target.protocol) || target.username || target.password || !isAllowedImageHost(target.hostname, config.imageProxyAllowedHosts)) {
      throw new ApiError(400, 'image_proxy_url_not_allowed')
    }

    let response
    try {
      response = await fetch(target, {
        redirect: 'manual',
        headers: { Accept: 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8' },
        signal: AbortSignal.timeout(config.remoteRequestTimeoutMs),
      })
    } catch {
      throw new ApiError(502, 'image_proxy_request_failed')
    }

    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get('location')
      if (!location) {
        throw new ApiError(502, 'image_proxy_redirect_invalid')
      }
      target = new URL(location, target)
      continue
    }
    if (!response.ok) {
      throw new ApiError(502, `image_proxy_status_${response.status}`)
    }

    const contentType = String(response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase()
    const contentLength = Number(response.headers.get('content-length') || 0)
    if (!contentType.startsWith('image/') || contentLength > MAX_PROXY_BYTES) {
      throw new ApiError(400, 'image_proxy_content_invalid')
    }
    const body = Buffer.from(await response.arrayBuffer())
    if (body.length > MAX_PROXY_BYTES) {
      throw new ApiError(413, 'image_proxy_too_large')
    }
    return { body, contentType }
  }
  throw new ApiError(502, 'image_proxy_redirect_limit')
}

function createApp(options = {}) {
  const config = options.config || createConfig(options.env)
  const store = options.store || createDatabase(config.databasePath)
  const auth = options.auth || createAuth(store, config, options.fetchImpl)
  let server = null
  let closed = false

  function getRuntimeConfig() {
    return { ...config, ...store.getAdminSettings() }
  }

  function requireAdminSession(req) {
    requireAdmin(req, config, store)
  }

  async function handleAdminAsset(req, res, pathname, origin) {
    assertMethod(req, 'GET')
    const assets = {
      '/admin': ['admin.html', 'text/html; charset=utf-8'],
      '/admin/': ['admin.html', 'text/html; charset=utf-8'],
      '/admin.css': ['admin.css', 'text/css; charset=utf-8'],
      '/admin.js': ['admin.js', 'text/javascript; charset=utf-8'],
    }
    const asset = assets[pathname]
    if (!asset) {
      throw new ApiError(404, 'not_found')
    }
    let body
    try {
      body = await fs.readFile(path.join(config.rootDir, 'public', asset[0]))
    } catch {
      throw new ApiError(404, 'admin_asset_not_found')
    }
    res.writeHead(200, {
      ...corsHeaders(config, origin),
      'Content-Type': asset[1],
      'Content-Length': body.length,
      'Cache-Control': 'no-cache',
      'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy': "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
    })
    res.end(body)
  }

  async function handleAdminLogin(req, res, origin) {
    assertMethod(req, 'POST')
    const body = await readJson(req, 16 * 1024)
    const valid = verifyAdminPassword(body.password, config)
    if (!valid) {
      throw new ApiError(401, 'admin_password_invalid')
    }
    res.setHeader('Set-Cookie', createSessionCookie(req, config, store))
    sendJson(res, config, 200, { data: { authenticated: true } }, origin)
  }

  async function handleAdminLogout(req, res, origin) {
    assertMethod(req, 'POST')
    revokeAdminSession(req, config, store)
    res.setHeader('Set-Cookie', clearSessionCookie(req))
    sendJson(res, config, 200, { data: { authenticated: false } }, origin)
  }

  async function handleAdminSummary(req, res, origin) {
    assertMethod(req, 'GET')
    requireAdminSession(req)
    const runtimeConfig = getRuntimeConfig()
    const summary = store.getAdminSummary(startOfUtcDay())
    sendJson(res, config, 200, {
      data: {
        metrics: summary.metrics,
        jobs: summary.jobs,
        logs: summary.logs,
        settings: adminSettingsView(runtimeConfig),
        generatedAt: Date.now(),
      },
    }, origin)
  }

  async function handleAdminSettings(req, res, origin) {
    requireAdminSession(req)
    if (req.method === 'GET') {
      sendJson(res, config, 200, { data: adminSettingsView(getRuntimeConfig()) }, origin)
      return
    }
    assertMethod(req, 'PATCH')
    const body = await readJson(req, 32 * 1024)
    const patch = validateAdminSettings(body)
    const current = getRuntimeConfig()
    const settings = store.setAdminSettings(patch)
    sendJson(res, config, 200, {
      data: adminSettingsView({ ...current, ...settings }),
    }, origin)
  }

  async function runDownloadJob(jobId) {
    const job = store.markJobRunning(jobId)
    if (!job) {
      return
    }

    try {
      const runtimeConfig = getRuntimeConfig()
      const text = await buildDownloadText(runtimeConfig, job.book, job.link)
      const output = Buffer.from(String(text || ''), 'utf8')
      if (!output.length) {
        throw new Error('download_content_empty')
      }
      const filePath = path.join(config.downloadDir, `${job.id}.txt`)
      await fs.writeFile(filePath, output, { mode: 0o600 })
      const manifest = {
        fileName: buildFileName(job.book),
        size: output.length,
        meta: {
          title: job.book.title,
          author: job.book.author,
          status: job.book.status,
        },
        panLinks: [],
        directLinkEnabled: runtimeConfig.cloudDirectLinkEnabled,
      }
      store.completeJob(job.id, manifest, filePath)
    } catch (error) {
      store.failJob(job.id, error && error.message ? error.message : 'download_generation_failed')
    }
  }

  function scheduleDownloadJob(jobId) {
    setImmediate(() => {
      runDownloadJob(jobId).catch(() => {})
    })
  }

  async function handleLogin(req, res, origin) {
    assertMethod(req, 'POST')
    const body = await readJson(req)
    const session = await auth.login(body.code, {
      nickName: normalizeShortText(body.nickName, 32),
      avatarUrl: normalizeShortText(body.avatarUrl, 2048),
    })
    sendJson(res, config, 200, { data: session }, origin)
  }

  async function handleMe(req, res, url, origin) {
    assertMethod(req, 'GET')
    const session = auth.requireSession(req.headers, url)
    sendJson(res, config, 200, {
      data: {
        token: session.token,
        expiresAt: new Date(session.expiresAt * 1000).toISOString(),
        expiresAtTs: session.expiresAt,
        user: session.user,
      },
    }, origin)
  }

  async function handleProfile(req, res, url, origin) {
    const session = auth.requireSession(req.headers, url)
    if (req.method === 'GET') {
      sendJson(res, config, 200, { data: { user: session.user } }, origin)
      return
    }
    assertMethod(req, 'POST')
    const body = await readJson(req)
    const nickName = normalizeShortText(body.nickName, 32)
    const avatarUrl = normalizeShortText(body.avatarUrl, 2048)
    if (!nickName) {
      throw new ApiError(400, 'profile_nickname_required')
    }
    const user = store.updateUser(session.user.id, { nickName, avatarUrl })
    sendJson(res, config, 200, { data: { user } }, origin)
  }

  async function handleAvatar(req, res, url, origin) {
    assertMethod(req, 'POST')
    const session = auth.requireSession(req.headers, url)
    const form = await readMultipartForm(req, requestOrigin(req, config), MAX_AVATAR_BYTES + 64 * 1024)
    const avatar = form.get('avatar')
    if (!avatar || typeof avatar.arrayBuffer !== 'function') {
      throw new ApiError(400, 'avatar_file_required')
    }
    if (Number(avatar.size || 0) > MAX_AVATAR_BYTES) {
      throw new ApiError(413, 'avatar_file_too_large')
    }
    const body = Buffer.from(await avatar.arrayBuffer())
    if (!body.length) {
      throw new ApiError(400, 'avatar_file_required')
    }
    if (body.length > MAX_AVATAR_BYTES) {
      throw new ApiError(413, 'avatar_file_too_large')
    }
    const type = imageFileType(body)
    if (!type) {
      throw new ApiError(400, 'avatar_file_type_invalid')
    }

    const fileName = `${crypto.randomUUID()}.${type.extension}`
    await fs.writeFile(path.join(config.avatarDir, fileName), body, { flag: 'wx', mode: 0o600 })
    const avatarUrl = publicUrl(config, `/uploads/avatars/${fileName}`)
    const user = store.updateUser(session.user.id, {
      nickName: session.user.nickName,
      avatarUrl,
    })
    sendJson(res, config, 200, { data: { avatarUrl, user } }, origin)
  }

  async function handleClientLog(req, res, url, origin) {
    assertMethod(req, 'POST')
    const session = auth.getOptionalSession(req.headers, url)
    const body = await readJson(req)
    const level = normalizeShortText(body.level, 16).toLowerCase()
    const allowedLevel = ['debug', 'info', 'warn', 'error'].includes(level) ? level : 'info'
    store.createClientLog({
      id: crypto.randomUUID(),
      userId: session && session.user.id,
      level: allowedLevel,
      scope: normalizeShortText(body.scope, 128) || 'client',
      message: normalizeShortText(body.message, 2048),
      requestId: normalizeShortText(body.requestId, 128),
      meta: sanitizeMeta(body.meta || {}),
      createdAt: Date.now(),
    })
    sendJson(res, config, 200, { data: { accepted: true } }, origin)
  }

  async function handleDownloadStatus(req, res, url, origin) {
    assertMethod(req, 'GET')
    const session = auth.getOptionalSession(req.headers, url)
    sendJson(res, config, 200, { data: statusManifest(getRuntimeConfig(), store, session) }, origin)
  }

  async function handleParse(req, res, url, origin) {
    assertMethod(req, 'POST')
    auth.getOptionalSession(req.headers, url)
    const runtimeConfig = getRuntimeConfig()
    if (!runtimeConfig.downloadEnabled) {
      throw new ApiError(503, 'download_disabled')
    }
    if (!runtimeConfig.parseEnabled) {
      throw new ApiError(503, 'parse_disabled')
    }
    const body = await readJson(req)
    let result
    try {
      const identity = identifyNovelLink(body.link)
      assertSourceEnabled(identity, runtimeConfig)
      result = await parseNovel(body.link, runtimeConfig)
    } catch (error) {
      if (error instanceof ApiError || error instanceof AuthError) {
        throw error
      }
      throw new ApiError(Number(error && error.status) || 400, String(error && error.message || 'parse_failed'))
    }
    sendJson(res, config, 200, { data: result.book, warning: result.warning }, origin)
  }

  async function handleGenerate(req, res, url, origin) {
    assertMethod(req, 'POST')
    const session = auth.getOptionalSession(req.headers, url)
    const runtimeConfig = getRuntimeConfig()
    if (!runtimeConfig.downloadEnabled) {
      throw new ApiError(503, 'download_disabled')
    }
    const body = await readJson(req)
    let identity
    try {
      identity = identifyNovelLink(body.link)
    } catch (error) {
      throw new ApiError(Number(error && error.status) || 400, String(error && error.message || 'unsupported_link'))
    }
    assertSourceEnabled(identity, runtimeConfig)
    const status = statusManifest(runtimeConfig, store, session)
    if (status.downloadCountReached) {
      throw new ApiError(429, 'download_limit_reached')
    }

    const requestedSource = normalizeShortText(body.source, 32).toLowerCase()
    const requestedBookId = normalizeShortText(body.sourceBookId, 128)
    if ((requestedSource && requestedSource !== identity.source) || (requestedBookId && requestedBookId !== identity.sourceBookId)) {
      throw new ApiError(400, 'download_identity_mismatch')
    }
    const book = normalizeRequestedBook(body.book, identity)
    const job = store.createJob({
      id: crypto.randomUUID(),
      ownerUserId: session ? session.user.id : null,
      source: identity.source,
      link: identity.originalUrl,
      book,
    })
    scheduleDownloadJob(job.id)
    sendJson(res, config, 200, {
      data: {
        status: 'processing',
        downloadId: job.id,
      },
    }, origin)
  }

  async function handleProgress(req, res, url, origin) {
    assertMethod(req, 'GET')
    const session = auth.getOptionalSession(req.headers, url)
    const jobId = normalizeShortText(url.searchParams.get('id'), 128)
    if (!jobId) {
      throw new ApiError(400, 'download_id_required')
    }
    const job = store.getJob(jobId)
    assertOwnedJob(job, session)
    const completed = job.status === 'completed' ? job.total : job.completed
    const payload = {
      total: job.total,
      completed,
      finished: job.status === 'completed' || job.status === 'failed',
      failed: job.status === 'failed',
      error: job.error || '',
    }
    if (job.status === 'completed') {
      const downloadUrl = publicUrl(config, `/api/download/file.php?id=${encodeURIComponent(job.id)}`)
      payload.downloadUrl = downloadUrl
      payload.manifest = job.manifest
    }
    sendJson(res, config, 200, { data: payload }, origin)
  }

  async function handleDownloadFile(req, res, url, origin) {
    assertMethod(req, 'GET')
    const session = auth.getOptionalSession(req.headers, url)
    const jobId = normalizeShortText(url.searchParams.get('id'), 128)
    const job = store.getJob(jobId)
    assertOwnedJob(job, session)
    if (job.status !== 'completed' || !job.downloadPath) {
      throw new ApiError(404, 'download_not_ready')
    }
    let output
    try {
      output = await fs.readFile(job.downloadPath)
    } catch {
      throw new ApiError(404, 'download_not_found')
    }
    const fileName = (job.manifest && job.manifest.fileName) || 'novel.txt'
    res.writeHead(200, {
      ...corsHeaders(config, origin),
      'Content-Type': 'text/plain; charset=utf-8',
      'Content-Length': output.length,
      'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(fileName)}`,
      'Cache-Control': 'private, max-age=300',
    })
    res.end(output)
  }

  async function handleImageProxy(req, res, url, origin) {
    assertMethod(req, 'GET')
    const target = normalizeShortText(url.searchParams.get('url'), 4096)
    if (!target) {
      throw new ApiError(400, 'image_proxy_url_required')
    }
    const image = await fetchAllowedImage(target, config)
    res.writeHead(200, {
      ...corsHeaders(config, origin),
      'Content-Type': image.contentType,
      'Content-Length': image.body.length,
      'Cache-Control': 'public, max-age=3600',
    })
    res.end(image.body)
  }

  async function handleAvatarFile(req, res, pathname, origin) {
    assertMethod(req, 'GET')
    const fileName = path.posix.basename(pathname)
    if (!/^[a-f0-9-]{36}\.(jpg|png|gif|webp)$/.test(fileName)) {
      throw new ApiError(404, 'avatar_not_found')
    }
    let output
    try {
      output = await fs.readFile(path.join(config.avatarDir, fileName))
    } catch {
      throw new ApiError(404, 'avatar_not_found')
    }
    const type = imageFileType(output)
    if (!type) {
      throw new ApiError(404, 'avatar_not_found')
    }
    res.writeHead(200, {
      ...corsHeaders(config, origin),
      'Content-Type': type.contentType,
      'Content-Length': output.length,
      'Cache-Control': 'public, max-age=86400',
    })
    res.end(output)
  }

  async function handler(req, res) {
    const origin = String(req.headers.origin || '')
    if (req.method === 'OPTIONS') {
      sendEmpty(res, config, 204, origin)
      return
    }

    let url
    try {
      url = new URL(requestOrigin(req, config))
      const pathname = url.pathname
      if (pathname === '/') {
        assertMethod(req, 'GET')
        res.writeHead(302, {
          ...corsHeaders(config, origin),
          Location: '/admin',
          'Cache-Control': 'no-store',
        })
        res.end()
        return
      }
      if (pathname === '/admin' || pathname === '/admin/' || pathname === '/admin.css' || pathname === '/admin.js') {
        return await handleAdminAsset(req, res, pathname, origin)
      }
      if (pathname === '/api/admin/login') return await handleAdminLogin(req, res, origin)
      if (pathname === '/api/admin/logout') return await handleAdminLogout(req, res, origin)
      if (pathname === '/api/admin/summary') return await handleAdminSummary(req, res, origin)
      if (pathname === '/api/admin/settings') return await handleAdminSettings(req, res, origin)
      if (pathname === '/healthz') {
        assertMethod(req, 'GET')
        sendJson(res, config, 200, { data: { status: 'ok' } }, origin)
        return
      }
      if (pathname === '/api/v1/auth/login.php') return await handleLogin(req, res, origin)
      if (pathname === '/api/v1/auth/me.php') return await handleMe(req, res, url, origin)
      if (pathname === '/api/v1/auth/profile.php') return await handleProfile(req, res, url, origin)
      if (pathname === '/api/v1/upload/avatar.php') return await handleAvatar(req, res, url, origin)
      if (pathname === '/api/v1/logs/client.php') return await handleClientLog(req, res, url, origin)
      if (pathname === '/api/download/status.php') return await handleDownloadStatus(req, res, url, origin)
      if (pathname === '/api/download/parse.php') return await handleParse(req, res, url, origin)
      if (pathname === '/api/download/generate.php') return await handleGenerate(req, res, url, origin)
      if (pathname === '/api/download/progress.php') return await handleProgress(req, res, url, origin)
      if (pathname === '/api/download/file.php') return await handleDownloadFile(req, res, url, origin)
      if (pathname === '/api/download/image_proxy.php') return await handleImageProxy(req, res, url, origin)
      if (pathname.startsWith('/uploads/avatars/')) return await handleAvatarFile(req, res, pathname, origin)
      throw new ApiError(404, 'not_found')
    } catch (error) {
      if (!error.expose && !(error instanceof ApiError) && !(error instanceof AuthError)) {
        console.error('Unhandled request error:', error)
      }
      sendError(res, config, error, origin)
    }
  }

  async function listen(port = config.port, host = config.host) {
    if (server) {
      return server
    }
    await fs.mkdir(config.avatarDir, { recursive: true })
    await fs.mkdir(config.downloadDir, { recursive: true })
    server = http.createServer(handler)
    await new Promise((resolve, reject) => {
      server.once('error', reject)
      server.listen(port, host, () => {
        server.off('error', reject)
        resolve()
      })
    })
    const address = server.address()
    if (address && typeof address === 'object' && /:0$/.test(config.appBaseUrl)) {
      config.appBaseUrl = `http://${host}:${address.port}`
    }
    return server
  }

  async function close() {
    if (closed) {
      return
    }
    closed = true
    if (server) {
      await new Promise((resolve) => server.close(resolve))
      server = null
    }
    store.close()
  }

  return {
    config,
    handler,
    listen,
    close,
    store,
  }
}

module.exports = {
  createApp,
}

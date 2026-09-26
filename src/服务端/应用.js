const { spawn } = require('node:child_process')
const crypto = require('node:crypto')
const fs = require('node:fs/promises')
const http = require('node:http')
const os = require('node:os')
const path = require('node:path')
const {
  clearSessionCookie,
  createSessionCookie,
  requireAdmin,
  revokeAdminSession,
  verifyAdminPassword,
} = require('./管理员认证')
const { createAuth, AuthError } = require('./用户认证')
const { createConfig } = require('./配置')
const { createDatabase } = require('./数据库')
const { createSelfUpdater } = require('./自更新')
const {
  QuarkError,
  deleteQuarkFile,
  deleteQuarkShare,
  decryptCookie,
  encryptCookie,
  normalizeCookie,
  testQuarkConnection,
  uploadNovelToQuark,
} = require('./夸克网盘')
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
} = require('./HTTP响应')
const {
  buildDownloadText,
  buildFileName,
  identifyNovelLink,
  normalizeRequestedBook,
  parseNovel,
} = require('./小说来源')
const { downloadFanqieNovel } = require('./番茄小说下载')

const MAX_AVATAR_BYTES = 5 * 1024 * 1024
const MAX_PROXY_BYTES = 5 * 1024 * 1024
const UPDATE_REPOSITORY = 'TimShitPig/mantou-toolbox-backend'
const UPDATE_CACHE_MS = 60 * 1000
const PUBLISH_WORKFLOW_NAMES = new Set(['Publish Docker image', '发布 Docker 镜像'])
const GITHUB_PROXIES = Object.freeze({
  github: null,
  edgeone: 'https://edgeone.gh-proxy.com',
  hk: 'https://hk.gh-proxy.com',
  'gh-proxy': 'https://gh-proxy.com',
  'gh-hik': 'https://gh.hik.top',
})

function proxyGithubUrl(url, proxyId) {
  const baseUrl = GITHUB_PROXIES[proxyId]
  return baseUrl ? `${baseUrl}/${url}` : url
}

function parseAppVersion(value) {
  const match = String(value || '').trim().match(/^v?(\d+)\.(\d+)\.(\d+)$/)
  if (!match) return null
  return {
    version: `v${match[1]}.${match[2]}.${match[3]}`,
    parts: match.slice(1).map(Number),
  }
}

function compareAppVersions(left, right) {
  const leftParts = parseAppVersion(left).parts
  const rightParts = parseAppVersion(right).parts
  for (let index = 0; index < 3; index += 1) {
    if (leftParts[index] !== rightParts[index]) return leftParts[index] - rightParts[index]
  }
  return 0
}

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

async function convertHeicForMiniProgram(image) {
  if (!/^image\/hei[cf](?:-sequence)?$/i.test(image.contentType)) {
    return image
  }

  const temporaryDir = await fs.mkdtemp(path.join(os.tmpdir(), 'mantou-cover-'))
  try {
    const sourcePath = path.join(temporaryDir, 'cover.heic')
    const outputPath = path.join(temporaryDir, 'cover.jpg')
    await fs.writeFile(sourcePath, image.body, { mode: 0o600 })
    await new Promise((resolve, reject) => {
      const child = spawn('heif-convert', [sourcePath, outputPath], {
        stdio: 'ignore',
        windowsHide: true,
      })
      child.once('error', reject)
      child.once('close', (code) => {
        if (code === 0) resolve()
        else reject(new Error('heic_conversion_failed'))
      })
    })

    const body = await fs.readFile(outputPath)
    if (!body.length || body.length > MAX_PROXY_BYTES) {
      throw new ApiError(502, 'image_conversion_output_invalid')
    }
    return { body, contentType: 'image/jpeg' }
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new ApiError(502, error && error.code === 'ENOENT' ? 'image_converter_unavailable' : 'image_conversion_failed')
  } finally {
    await fs.rm(temporaryDir, { recursive: true, force: true })
  }
}

function createApp(options = {}) {
  const config = options.config || createConfig(options.env)
  const store = options.store || createDatabase(config.databasePath)
  const auth = options.auth || createAuth(store, config, options.fetchImpl)
  const fanqieDownloader = options.fanqieDownloader || downloadFanqieNovel
  const updateFetch = options.updateFetchImpl || options.fetchImpl || globalThis.fetch
  const proxyFetch = options.proxyFetchImpl || options.fetchImpl || globalThis.fetch
  let server = null
  let closed = false
  let downloadCleanupTimer = null
  let downloadCleanupRunning = false
  const updateCache = new Map()

  function recordSystemLog(level, source, message, context = {}) {
    const entry = {
      id: crypto.randomUUID(),
      level,
      source: normalizeShortText(source, 64) || 'server',
      message: normalizeShortText(message, 1024) || 'system_event',
      requestId: normalizeShortText(context.requestId, 128),
      method: normalizeShortText(context.method, 16),
      path: normalizeShortText(context.path, 512),
      statusCode: Number.isInteger(context.statusCode) ? context.statusCode : null,
      meta: sanitizeMeta(context.meta || {}),
      createdAt: Date.now(),
    }

    try {
      store.createSystemLog(entry)
    } catch (error) {
      console.error('Failed to persist system log:', error)
    }

    const output = JSON.stringify(entry)
    if (level === 'error') console.error(output)
    else if (level === 'warn') console.warn(output)
    else console.info(output)
  }

  async function cleanupExpiredDownloads() {
    if (downloadCleanupRunning
      || typeof store.getExpiredDownloadJobs !== 'function'
      || typeof store.deleteExpiredDownloadJob !== 'function') return
    downloadCleanupRunning = true
    let removedFiles = 0
    let removedJobs = 0
    const retentionHours = Number(config.downloadRetentionHours) || 24
    const cutoff = Date.now() - retentionHours * 60 * 60 * 1000
    const downloadRoot = path.resolve(config.downloadDir)

    try {
      for (const job of store.getExpiredDownloadJobs(cutoff)) {
        if (job.downloadPath) {
          const filePath = path.resolve(downloadRoot, job.downloadPath)
          const relativePath = path.relative(downloadRoot, filePath)
          if (!relativePath || relativePath === '..' || relativePath.startsWith(`..${path.sep}`) || path.isAbsolute(relativePath)) {
            continue
          }
          try {
            const details = await fs.lstat(filePath)
            if (details.isSymbolicLink() || !details.isFile()) continue
            await fs.unlink(filePath)
            removedFiles += 1
          } catch (error) {
            if (error && error.code !== 'ENOENT') throw error
          }
        }
        removedJobs += store.deleteExpiredDownloadJob(job.id, cutoff)
      }
      if (removedFiles || removedJobs) {
        recordSystemLog('info', 'downloads', 'Expired download files and jobs removed', {
          method: 'SYSTEM',
          path: '/app/storage/downloads',
          meta: { removedFiles, removedJobs, retentionHours },
        })
      }
    } catch (error) {
      recordSystemLog('warn', 'downloads', 'Expired download cleanup failed', {
        method: 'SYSTEM',
        path: '/app/storage/downloads',
        meta: { error: String(error && error.message || error).slice(0, 512) },
      })
    } finally {
      downloadCleanupRunning = false
    }
  }

  const selfUpdater = options.updater || (config.selfUpdateEnabled
    ? createSelfUpdater({
      rootDir: config.rootDir,
      storageDir: config.storageDir,
      updateControlDir: config.updateControlDir,
      fetchImpl: updateFetch,
      onState: (operation) => {
        if (operation.state === 'failed') return
        const level = operation.state === 'rolled_back' ? 'warn' : 'info'
        recordSystemLog(level, 'updater', operation.message, {
          method: 'SYSTEM',
          path: '/api/admin/update',
          meta: {
            operationId: operation.operationId,
            version: operation.version,
            state: operation.state,
            progress: operation.progress,
          },
        })
      },
    })
    : null)

  async function recordTerminalUpdateLog() {
    if (!selfUpdater || typeof selfUpdater.claimTerminalLog !== 'function') return
    const operation = await selfUpdater.claimTerminalLog()
    if (!operation) return
    const level = operation.state === 'failed' ? 'error' : (operation.state === 'rolled_back' ? 'warn' : 'info')
    recordSystemLog(level, 'updater', operation.message, {
      method: 'SYSTEM',
      path: '/api/admin/update',
      meta: {
        operationId: operation.operationId,
        version: operation.version,
        state: operation.state,
        progress: operation.progress,
      },
    })
  }

  function getRuntimeConfig() {
    return { ...config, ...store.getAdminSettings() }
  }

  function requireAdminSession(req) {
    requireAdmin(req, config, store)
  }

  async function handleAdminAsset(req, res, pathname, origin) {
    assertMethod(req, 'GET')
    const assets = {
      '/admin': ['管理后台.html', 'text/html; charset=utf-8'],
      '/admin/': ['管理后台.html', 'text/html; charset=utf-8'],
      '/admin.css': ['管理后台.css', 'text/css; charset=utf-8'],
      '/admin.js': ['管理后台.js', 'text/javascript; charset=utf-8'],
    }
    const asset = assets[pathname]
    if (!asset) {
      throw new ApiError(404, 'not_found')
    }
    let body
    try {
      body = await fs.readFile(path.join(config.rootDir, 'public', '后台界面', asset[0]))
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
    await recordTerminalUpdateLog()
    const runtimeConfig = getRuntimeConfig()
    const summary = store.getAdminSummary(startOfUtcDay())
    sendJson(res, config, 200, {
      data: {
        metrics: summary.metrics,
        jobs: summary.jobs,
        systemLogs: summary.systemLogs,
        settings: adminSettingsView(runtimeConfig),
        generatedAt: Date.now(),
      },
    }, origin)
  }

  async function getAdminUpdateInfo(proxyId) {
    let cached = updateCache.get(proxyId)
    if (!cached || Date.now() - cached.checkedAt >= UPDATE_CACHE_MS) {
      const headers = {
        Accept: 'application/vnd.github+json',
        'User-Agent': 'mantou-toolbox-update-check',
      }
      const options = { headers, signal: AbortSignal.timeout(8000) }
      let tagsResponse
      let runsResponse
      let releasesResponse
      try {
        [tagsResponse, runsResponse, releasesResponse] = await Promise.all([
          updateFetch(proxyGithubUrl(`https://api.github.com/repos/${UPDATE_REPOSITORY}/tags?per_page=100`, proxyId), options),
          updateFetch(proxyGithubUrl(`https://api.github.com/repos/${UPDATE_REPOSITORY}/actions/runs?per_page=100`, proxyId), options),
          updateFetch(proxyGithubUrl(`https://api.github.com/repos/${UPDATE_REPOSITORY}/releases?per_page=100`, proxyId), options)
            .catch(() => null),
        ])
      } catch {
        throw new ApiError(502, 'update_check_failed')
      }
      if (!tagsResponse.ok || !runsResponse.ok) {
        throw new ApiError(502, 'update_check_failed')
      }

      let tags
      let runs
      let releases = []
      try {
        ;[tags, runs] = await Promise.all([tagsResponse.json(), runsResponse.json()])
      } catch {
        throw new ApiError(502, 'update_check_failed')
      }
      if (releasesResponse && releasesResponse.ok) {
        try {
          const releasePayload = await releasesResponse.json()
          if (Array.isArray(releasePayload)) releases = releasePayload
        } catch {}
      }
      if (!Array.isArray(tags) || !Array.isArray(runs.workflow_runs)) {
        throw new ApiError(502, 'update_check_failed')
      }

      const publishedRuns = new Map()
      for (const run of runs.workflow_runs) {
        if (!PUBLISH_WORKFLOW_NAMES.has(String(run.name || '')) || run.status !== 'completed' || run.conclusion !== 'success') continue
        const revision = String(run.head_sha || '').toLowerCase()
        if (!/^[a-f0-9]{40}$/.test(revision)) continue
        const existing = publishedRuns.get(revision)
        if (!existing || String(run.updated_at || run.created_at || '') > String(existing.updated_at || existing.created_at || '')) {
          publishedRuns.set(revision, run)
        }
      }
      const releasesByTag = new Map(releases
        .filter((release) => release && typeof release.tag_name === 'string')
        .map((release) => [release.tag_name, release]))
      const versions = tags
        .map((tag) => {
          const parsed = parseAppVersion(tag.name)
          const revision = String(tag.commit && tag.commit.sha || '').toLowerCase()
          const run = publishedRuns.get(revision)
          const release = releasesByTag.get(tag.name)
          if (!parsed || !/^[a-f0-9]{40}$/.test(revision) || !run) return null
          return {
            ...parsed,
            revision,
            publishedAt: String(release && release.published_at || run.updated_at || run.created_at || ''),
            content: String(release && release.body || run.head_commit && run.head_commit.message || run.display_title || '').trim(),
            prerelease: Boolean(release && release.prerelease),
            detailsUrl: String(release && release.html_url || `https://github.com/${UPDATE_REPOSITORY}/commit/${revision}`),
          }
        })
        .filter(Boolean)
        .sort((left, right) => compareAppVersions(right.version, left.version))
      const current = parseAppVersion(config.appBuildVersion)
      const latest = versions[0] || null
      const rollbackVersions = current
        ? versions.filter((version) => compareAppVersions(version.version, current.version) < 0).slice(0, 3)
        : []
      cached = {
        checkedAt: Date.now(),
        data: {
          proxyId,
          currentVersion: current && current.version,
          latestVersion: latest && latest.version,
          hasUpdate: Boolean(latest && (!current || compareAppVersions(latest.version, current.version) > 0)),
          versions: versions.slice(0, 30).map(({ version, publishedAt, content, prerelease, detailsUrl }) => ({
            version,
            publishedAt,
            content,
            prerelease,
            detailsUrl,
          })),
          rollbackVersions: rollbackVersions.map((version) => version.version),
        },
      }
      updateCache.set(proxyId, cached)
    }

    return cached.data
  }

  async function handleAdminUpdates(req, res, url, origin) {
    assertMethod(req, 'GET')
    requireAdminSession(req)
    const proxyId = url.searchParams.get('proxyId') || 'gh-proxy'
    if (!Object.hasOwn(GITHUB_PROXIES, proxyId)) throw new ApiError(400, 'admin_proxy_invalid')
    const updateInfo = await getAdminUpdateInfo(proxyId)
    sendJson(res, config, 200, { data: updateInfo }, origin)
  }

  async function handleAdminUpdate(req, res, origin) {
    assertMethod(req, 'POST')
    requireAdminSession(req)
    if (!selfUpdater || !config.selfUpdateEnabled) throw new ApiError(503, 'self_update_requires_supervisor')

    const body = await readJson(req, 16 * 1024)
    if (body.action !== 'update' && body.action !== 'rollback') throw new ApiError(400, 'admin_update_action_invalid')
    const action = body.action === 'rollback' ? 'rollback' : 'update'
    const version = String(body.version || '')
    const proxyId = String(body.proxyId || 'gh-proxy')
    if (!Object.hasOwn(GITHUB_PROXIES, proxyId)) throw new ApiError(400, 'admin_proxy_invalid')

    const updateInfo = await getAdminUpdateInfo(proxyId)
    const allowedVersion = action === 'update'
      ? Boolean(updateInfo.hasUpdate && version === updateInfo.latestVersion)
      : updateInfo.rollbackVersions.includes(version)
    if (!allowedVersion) throw new ApiError(400, 'admin_update_version_not_available')

    const operation = await selfUpdater.start({
      action,
      version,
      proxyId,
      currentVersion: updateInfo.currentVersion,
      archiveUrl: proxyGithubUrl(`https://github.com/${UPDATE_REPOSITORY}/archive/refs/tags/${version}.tar.gz`, proxyId),
    })
    sendJson(res, config, 202, { data: operation }, origin)
  }

  async function handleAdminUpdateOperation(req, res, origin) {
    assertMethod(req, 'GET')
    requireAdminSession(req)
    await recordTerminalUpdateLog()
    const operation = selfUpdater ? await selfUpdater.getOperation() : null
    sendJson(res, config, 200, {
      data: { enabled: Boolean(selfUpdater && config.selfUpdateEnabled), operation },
    }, origin)
  }

  async function handleAdminProxyTest(req, res, origin) {
    assertMethod(req, 'POST')
    requireAdminSession(req)
    const body = await readJson(req, 16 * 1024)
    const proxyId = String(body.proxyId || '')
    if (!Object.hasOwn(GITHUB_PROXIES, proxyId)) {
      throw new ApiError(400, 'admin_proxy_invalid')
    }

    const startedAt = Date.now()
    let statusCode = null
    let connected = false
    try {
      const response = await proxyFetch(
        proxyGithubUrl(`https://api.github.com/repos/${UPDATE_REPOSITORY}/tags?per_page=1`, proxyId),
        {
          headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'mantou-toolbox-proxy-test' },
          signal: AbortSignal.timeout(8000),
        }
      )
      statusCode = response.status
      const tags = response.ok ? await response.json() : null
      connected = response.ok && Array.isArray(tags)
    } catch {}

    sendJson(res, config, 200, {
      data: {
        proxyId,
        connected,
        statusCode,
        latencyMs: Date.now() - startedAt,
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

  function getQuarkSettings() {
    const settings = store.getAdminSettings()
    return {
      enabled: Boolean(settings.quarkEnabled),
      cookie: decryptCookie(settings.quarkCookieEncrypted, config.appSecret),
      folderName: String(settings.quarkFolderName || '馒头工具箱').trim() || '馒头工具箱',
    }
  }

  async function handleAdminQuark(req, res, origin) {
    requireAdminSession(req)
    if (req.method === 'GET') {
      const settings = getQuarkSettings()
      sendJson(res, config, 200, {
        data: {
          settings: {
            enabled: settings.enabled,
            hasCookie: Boolean(settings.cookie),
            folderName: settings.folderName,
          },
          files: typeof store.getAdminCloudJobs === 'function' ? store.getAdminCloudJobs(100) : [],
        },
      }, origin)
      return
    }
    assertMethod(req, 'PATCH')
    const body = await readJson(req, 16 * 1024)
    if (typeof body.enabled !== 'boolean') throw new ApiError(400, 'quark_enabled_invalid')
    if (body.clearCookie !== undefined && typeof body.clearCookie !== 'boolean') throw new ApiError(400, 'quark_clear_cookie_invalid')
    const folderName = String(body.folderName ?? getQuarkSettings().folderName).trim()
    if (!folderName || folderName.length > 64 || /[\\/\u0000-\u001f]/.test(folderName)) {
      throw new ApiError(400, 'quark_folder_name_invalid')
    }
    const current = getQuarkSettings()
    let cookieEncrypted = store.getAdminSettings().quarkCookieEncrypted || ''
    const submittedCookie = String(body.cookie || '').trim()
    if (submittedCookie) {
      try {
        normalizeCookie(submittedCookie)
      } catch {
        throw new ApiError(400, 'quark_cookie_invalid')
      }
      cookieEncrypted = encryptCookie(submittedCookie, config.appSecret)
    } else if (body.clearCookie) {
      cookieEncrypted = ''
    }
    store.setAdminSettings({
      quarkEnabled: body.enabled,
      quarkCookieEncrypted: cookieEncrypted,
      quarkFolderName: folderName,
    })
    sendJson(res, config, 200, {
      data: {
        enabled: body.enabled,
        hasCookie: Boolean(submittedCookie || (!body.clearCookie && current.cookie)),
        folderName,
      },
    }, origin)
  }

  async function handleAdminQuarkTest(req, res, origin) {
    assertMethod(req, 'POST')
    requireAdminSession(req)
    const body = await readJson(req, 16 * 1024)
    const cookie = String(body.cookie || '').trim() || getQuarkSettings().cookie
    try {
      await testQuarkConnection(cookie)
    } catch (error) {
      const code = error instanceof QuarkError ? error.code : 'quark_connection_failed'
      throw new ApiError(400, code)
    }
    sendJson(res, config, 200, { data: { connected: true } }, origin)
  }

  async function handleAdminQuarkFileDelete(req, res, jobId, origin) {
    assertMethod(req, 'DELETE')
    requireAdminSession(req)
    const savedFile = store.getQuarkShare(jobId)
    if (!savedFile) throw new ApiError(404, 'quark_file_not_found')
    if (!savedFile.fileId || !savedFile.shareId) throw new ApiError(409, 'quark_file_delete_unavailable')
    const { cookie } = getQuarkSettings()
    if (!cookie) throw new ApiError(503, 'quark_cookie_not_configured')
    if (!savedFile.shareDeleted) {
      try {
        await deleteQuarkShare(cookie, savedFile.shareId)
      } catch (error) {
        const code = error instanceof QuarkError ? error.code : 'quark_share_delete_failed'
        throw new ApiError(502, code)
      }
      store.markQuarkShareDeleted(jobId)
    }
    if (!savedFile.fileDeleted) {
      try {
        await deleteQuarkFile(cookie, savedFile.fileId)
      } catch (error) {
        const code = error instanceof QuarkError ? error.code : 'quark_file_delete_failed'
        throw new ApiError(502, code)
      }
      store.markQuarkFileDeleted(jobId)
    }
    store.deleteQuarkShare(jobId)
    recordSystemLog('info', 'quark', 'Deleted file and revoked share from Quark Drive', {
      meta: { jobId, title: String(savedFile.title || '').slice(0, 128) },
    })
    sendJson(res, config, 200, { data: { deleted: true } }, origin)
  }

  async function runDownloadJob(jobId) {
    const job = store.markJobRunning(jobId)
    if (!job) {
      return
    }

    try {
      const runtimeConfig = getRuntimeConfig()
      let result
      if (job.source === 'fanqie') {
        result = await fanqieDownloader({
          bookId: job.book.sourceBookId,
          outputDir: config.downloadDir,
          onProgress: ({ total, completed }) => {
            if (typeof store.updateJobProgress === 'function') {
              store.updateJobProgress(job.id, total, completed)
            }
          },
        })
      } else {
        result = { text: await buildDownloadText(runtimeConfig, job.book, job.link) }
      }
      const text = result.text
      const output = Buffer.from(String(text || ''), 'utf8')
      if (!output.length) {
        throw new Error('download_content_empty')
      }
      const filePath = path.join(config.downloadDir, `${job.id}.txt`)
      await fs.writeFile(filePath, output, { mode: 0o600 })
      const fileName = buildFileName(job.book)
      const panLinks = []
      const quarkSettings = getQuarkSettings()
      if (quarkSettings.enabled && quarkSettings.cookie) {
        try {
          const quarkLink = await uploadNovelToQuark({
            cookie: quarkSettings.cookie,
            folderName: quarkSettings.folderName,
            fileName,
            title: job.book.title,
            content: output,
          })
          panLinks.push(quarkLink)
          recordSystemLog('info', 'quark', 'Novel uploaded and shared on Quark Drive', {
            meta: { jobId: job.id, title: String(job.book.title || '').slice(0, 128) },
          })
        } catch (error) {
          const reason = error instanceof QuarkError ? error.code : 'quark_upload_failed'
          recordSystemLog('warn', 'quark', 'Cloud upload failed; local file was kept', {
            meta: { jobId: job.id, error: reason },
          })
        }
      }
      const manifest = {
        fileName,
        size: output.length,
        meta: {
          title: job.book.title,
          author: job.book.author,
          status: job.book.status,
          chapterCount: result.chapterCount || null,
        },
        panLinks,
        directLinkEnabled: panLinks.length ? false : runtimeConfig.cloudDirectLinkEnabled,
      }
      const quarkShare = panLinks[0]
        ? {
            title: String(job.book.title || '未命名书籍'),
            fileName: manifest.fileName,
            fileId: String(panLinks[0].fileId || ''),
            shareId: String(panLinks[0].shareId || ''),
            size: manifest.size,
            shareUrl: String(panLinks[0].shareUrl || panLinks[0].copyText || ''),
          }
        : null
      store.completeJob(job.id, manifest, filePath, quarkShare)
      if (panLinks.length) {
        let localFileRemoved = false
        try {
          await fs.unlink(filePath)
          localFileRemoved = true
        } catch (error) {
          recordSystemLog('warn', 'quark', 'Cloud copy completed but local file cleanup failed', {
            meta: { jobId: job.id, error: String(error && error.code || 'file_cleanup_failed') },
          })
        }
        if (localFileRemoved && typeof store.clearJobDownloadPath === 'function') {
          try {
            store.clearJobDownloadPath(job.id)
          } catch (error) {
            recordSystemLog('warn', 'quark', 'Local file was removed but its task path could not be cleared', {
              meta: { jobId: job.id, error: String(error && error.code || 'task_path_clear_failed') },
            })
          }
        }
      }
    } catch (error) {
      const message = error && error.message ? error.message : 'download_generation_failed'
      store.failJob(job.id, message)
      recordSystemLog('error', 'downloads', 'Download task failed', {
        meta: { jobId: job.id, source: job.source, error: String(message).slice(0, 512) },
      })
    }
  }

  function scheduleDownloadJob(jobId) {
    setImmediate(() => {
      runDownloadJob(jobId).catch((error) => {
        recordSystemLog('error', 'downloads', 'Download task crashed', {
          meta: { jobId, error: String(error && error.message || error).slice(0, 512) },
        })
      })
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
      payload.manifest = job.manifest
      const hasCloudLink = Array.isArray(job.manifest && job.manifest.panLinks) && job.manifest.panLinks.length > 0
      if (!hasCloudLink) {
        payload.downloadUrl = publicUrl(config, `/api/download/file.php?id=${encodeURIComponent(job.id)}`)
      }
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
    const image = await convertHeicForMiniProgram(await fetchAllowedImage(target, config))
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
    const requestedId = normalizeShortText(req.headers['x-request-id'], 128)
    const requestId = /^[A-Za-z0-9._:-]+$/.test(requestedId) ? requestedId : crypto.randomUUID()
    res.setHeader('X-Request-Id', requestId)
    if (req.method === 'OPTIONS') {
      sendEmpty(res, config, 204, origin)
      return
    }

    let url
    let pathname = '/'
    try {
      url = new URL(requestOrigin(req, config))
      pathname = url.pathname
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
      if (pathname === '/api/admin/updates') return await handleAdminUpdates(req, res, url, origin)
      if (pathname === '/api/admin/update') return await handleAdminUpdate(req, res, origin)
      if (pathname === '/api/admin/update-operation') return await handleAdminUpdateOperation(req, res, origin)
      if (pathname === '/api/admin/proxies/test') return await handleAdminProxyTest(req, res, origin)
      if (pathname === '/api/admin/settings') return await handleAdminSettings(req, res, origin)
      if (pathname === '/api/admin/quark') return await handleAdminQuark(req, res, origin)
      if (pathname === '/api/admin/quark/test') return await handleAdminQuarkTest(req, res, origin)
      const quarkFileDeleteMatch = pathname.match(/^\/api\/admin\/quark\/files\/([a-f0-9-]{36})$/i)
      if (quarkFileDeleteMatch) return await handleAdminQuarkFileDelete(req, res, quarkFileDeleteMatch[1], origin)
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
      const statusCode = Number(error && error.status) || 500
      const routineAuthMiss = statusCode === 401 && String(error && error.message) === 'admin_login_required'
      if (statusCode !== 404 && !routineAuthMiss) {
        const level = statusCode >= 500 ? 'error' : 'warn'
        recordSystemLog(level, 'http', `HTTP ${statusCode} ${req.method} ${pathname}: ${String(error && error.message || 'request_failed')}`, {
          requestId,
          method: req.method,
          path: pathname,
          statusCode,
          meta: { errorName: String(error && error.name || 'Error') },
        })
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
    await cleanupExpiredDownloads()
    downloadCleanupTimer = setInterval(() => {
      cleanupExpiredDownloads().catch(() => {})
    }, 60 * 60 * 1000)
    if (typeof downloadCleanupTimer.unref === 'function') downloadCleanupTimer.unref()
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
    recordSystemLog('info', 'server', 'Backend started', {
      method: 'SYSTEM',
      path: '/healthz',
      meta: { host, port: address && typeof address === 'object' ? address.port : port, version: config.appBuildVersion || 'unknown' },
    })
    return server
  }

  async function close() {
    if (closed) {
      return
    }
    closed = true
    if (downloadCleanupTimer) {
      clearInterval(downloadCleanupTimer)
      downloadCleanupTimer = null
    }
    if (server) {
      recordSystemLog('info', 'server', 'Backend stopping', { method: 'SYSTEM' })
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

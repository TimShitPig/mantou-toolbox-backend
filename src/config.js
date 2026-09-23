const path = require('node:path')

function bool(value, fallback) {
  if (typeof value === 'boolean') {
    return value
  }

  const normalized = String(value ?? '').trim().toLowerCase()
  if (['1', 'true', 'yes', 'on'].includes(normalized)) {
    return true
  }
  if (['0', 'false', 'no', 'off'].includes(normalized)) {
    return false
  }
  return fallback
}

function integer(value, fallback, minimum) {
  const parsed = Number.parseInt(String(value ?? ''), 10)
  if (!Number.isFinite(parsed) || parsed < minimum) {
    return fallback
  }
  return parsed
}

function list(value, fallback) {
  const source = String(value ?? '').trim()
  const values = (source ? source.split(',') : fallback)
    .map((item) => String(item).trim().toLowerCase())
    .filter(Boolean)
  return [...new Set(values)]
}

function trimTrailingSlash(value) {
  return String(value || '').trim().replace(/\/+$/, '')
}

function createConfig(env = process.env, overrides = {}) {
  const rootDir = path.resolve(__dirname, '..')
  const mode = String(env.NODE_ENV || 'development').trim().toLowerCase()
  const host = String(env.HOST || '127.0.0.1').trim()
  const port = integer(env.PORT, 8787, 1)
  const storageDir = path.resolve(env.STORAGE_DIR || path.join(rootDir, 'storage'))
  const appBaseUrl = trimTrailingSlash(env.APP_BASE_URL || `http://${host}:${port}`)

  const config = {
    rootDir,
    mode,
    host,
    port,
    storageDir,
    databasePath: path.resolve(env.DATABASE_PATH || path.join(storageDir, 'mantou.sqlite')),
    avatarDir: path.resolve(env.AVATAR_DIR || path.join(storageDir, 'avatars')),
    downloadDir: path.resolve(env.DOWNLOAD_DIR || path.join(storageDir, 'downloads')),
    appBaseUrl,
    appBuildRevision: String(env.APP_BUILD_REVISION || 'unknown').trim().toLowerCase(),
    appSecret: String(env.APP_SECRET || 'development-only-change-me').trim(),
    adminPassword: String(env.ADMIN_PASSWORD || '').trim(),
    corsAllowOrigin: String(env.CORS_ALLOW_ORIGIN || '*').trim(),
    allowDevelopmentLogin: bool(env.ALLOW_DEVELOPMENT_LOGIN, mode !== 'production'),
    sessionTtlSeconds: integer(env.SESSION_TTL_DAYS, 30, 1) * 24 * 60 * 60,
    wechatAppId: String(env.WECHAT_APP_ID || '').trim(),
    wechatAppSecret: String(env.WECHAT_APP_SECRET || '').trim(),
    downloadEnabled: bool(env.DOWNLOAD_ENABLED, true),
    parseEnabled: bool(env.PARSE_ENABLED, true),
    qimaoEnabled: bool(env.QIMAO_ENABLED, true),
    fanqieEnabled: bool(env.FANQIE_ENABLED, true),
    fanqieKeysConfigured: bool(env.FANQIE_KEYS_CONFIGURED, false),
    downloadLimit: integer(env.DOWNLOAD_LIMIT, 0, 0),
    rewardedAdEnabled: bool(env.REWARDED_AD_ENABLED, false),
    rewardedAdEveryDownloads: integer(env.REWARDED_AD_EVERY_DOWNLOADS, 3, 1),
    cloudDirectLinkEnabled: bool(env.CLOUD_DIRECT_LINK_ENABLED, true),
    remoteMetadataEnabled: bool(env.REMOTE_METADATA_ENABLED, false),
    remoteRequestTimeoutMs: integer(env.REMOTE_REQUEST_TIMEOUT_MS, 8000, 1000),
    contentCatalogFile: String(env.CONTENT_CATALOG_FILE || '').trim() || null,
    contentProviderUrl: String(env.CONTENT_PROVIDER_URL || '').trim() || null,
    imageProxyAllowedHosts: list(env.IMAGE_PROXY_ALLOWLIST, [
      'qimao.com',
      'wtzw.com',
      'fanqienovel.com',
      'fqnovel.com',
      'changdunovel.com',
    ]),
  }

  return { ...config, ...overrides }
}

module.exports = {
  createConfig,
}

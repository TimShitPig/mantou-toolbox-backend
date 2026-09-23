const crypto = require('node:crypto')

const COOKIE_NAME = 'mantou_admin'
const SESSION_TTL_SECONDS = 8 * 60 * 60

function createAdminAuthError(status, message) {
  const error = new Error(message)
  error.status = status
  error.expose = true
  return error
}

function hmac(value, config) {
  const key = `${config.appSecret}\0${config.adminPassword}`
  return crypto.createHmac('sha256', key).update(value).digest('base64url')
}

function safeEqual(left, right) {
  const leftBuffer = Buffer.from(left)
  const rightBuffer = Buffer.from(right)
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer)
}

function readCookie(headers) {
  const header = String(headers.cookie || '')
  for (const part of header.split(';')) {
    const separator = part.indexOf('=')
    if (separator < 0) continue
    if (part.slice(0, separator).trim() === COOKIE_NAME) {
      try {
        return decodeURIComponent(part.slice(separator + 1).trim())
      } catch {
        return ''
      }
    }
  }
  return ''
}

function isSecureRequest(req) {
  const forwarded = String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim().toLowerCase()
  return forwarded === 'https' || Boolean(req.socket && req.socket.encrypted)
}

function createSessionCookie(req, config, store) {
  const expiresAt = Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS
  const sessionId = crypto.randomUUID()
  const payload = `${expiresAt}.${sessionId}`
  store.createAdminSession({
    id: sessionId,
    expiresAt,
    createdAt: Math.floor(Date.now() / 1000),
  })
  const token = `${payload}.${hmac(payload, config)}`
  const secure = isSecureRequest(req) ? '; Secure' : ''
  return `${COOKIE_NAME}=${encodeURIComponent(token)}; Path=/api/admin; HttpOnly; SameSite=Strict; Max-Age=${SESSION_TTL_SECONDS}${secure}`
}

function clearSessionCookie(req) {
  const secure = isSecureRequest(req) ? '; Secure' : ''
  return `${COOKIE_NAME}=; Path=/api/admin; HttpOnly; SameSite=Strict; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT${secure}`
}

function verifySessionCookie(req, config, store) {
  if (!config.adminPassword) return false
  const token = readCookie(req.headers)
  const [expiry, sessionId, signature, ...extra] = token.split('.')
  if (!expiry || !sessionId || !signature || extra.length || !/^\d+$/.test(expiry)) return false
  if (Number(expiry) <= Math.floor(Date.now() / 1000)) return false
  const payload = `${expiry}.${sessionId}`
  if (!safeEqual(hmac(payload, config), signature)) return false
  const session = store.getAdminSession(sessionId)
  return Boolean(session && !session.revokedAt && session.expiresAt === Number(expiry) && session.expiresAt > Math.floor(Date.now() / 1000))
}

function verifyAdminPassword(password, config) {
  if (!config.adminPassword) {
    throw createAdminAuthError(503, 'admin_auth_not_configured')
  }
  const supplied = crypto.createHash('sha256').update(String(password || '')).digest()
  const expected = crypto.createHash('sha256').update(config.adminPassword).digest()
  return crypto.timingSafeEqual(supplied, expected)
}

function requireAdmin(req, config, store) {
  if (!verifySessionCookie(req, config, store)) {
    throw createAdminAuthError(401, 'admin_login_required')
  }
}

function revokeAdminSession(req, config, store) {
  const token = readCookie(req.headers)
  const [expiry, sessionId, signature, ...extra] = token.split('.')
  if (!expiry || !sessionId || !signature || extra.length || !/^\d+$/.test(expiry)) return
  const payload = `${expiry}.${sessionId}`
  if (safeEqual(hmac(payload, config), signature)) {
    store.revokeAdminSession(sessionId)
  }
}

module.exports = {
  clearSessionCookie,
  createAdminAuthError,
  createSessionCookie,
  requireAdmin,
  revokeAdminSession,
  verifyAdminPassword,
}

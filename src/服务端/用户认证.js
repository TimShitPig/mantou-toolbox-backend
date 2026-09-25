const crypto = require('node:crypto')

class AuthError extends Error {
  constructor(status, message) {
    super(message)
    this.name = 'AuthError'
    this.status = status
    this.expose = true
  }
}

function base64url(value) {
  return Buffer.from(value).toString('base64url')
}

function hmac(value, secret) {
  return crypto.createHmac('sha256', secret).update(value).digest('base64url')
}

function timingSafeEqual(left, right) {
  const leftBuffer = Buffer.from(left)
  const rightBuffer = Buffer.from(right)
  return leftBuffer.length === rightBuffer.length && crypto.timingSafeEqual(leftBuffer, rightBuffer)
}

function sha256(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex')
}

function createAuth(store, config, fetchImpl = globalThis.fetch) {
  function signSession(session) {
    const payload = base64url(JSON.stringify({ sid: session.id, exp: session.expiresAt }))
    return `${payload}.${hmac(payload, config.appSecret)}`
  }

  function verifyToken(token) {
    const parts = String(token || '').trim().split('.')
    if (parts.length !== 2 || !parts[0] || !parts[1] || !timingSafeEqual(hmac(parts[0], config.appSecret), parts[1])) {
      throw new AuthError(401, 'login_required')
    }

    let payload
    try {
      payload = JSON.parse(Buffer.from(parts[0], 'base64url').toString('utf8'))
    } catch {
      throw new AuthError(401, 'login_required')
    }

    if (!payload || typeof payload.sid !== 'string' || !Number.isFinite(payload.exp)) {
      throw new AuthError(401, 'login_required')
    }
    if (payload.exp <= Math.floor(Date.now() / 1000)) {
      throw new AuthError(401, 'login_expired')
    }

    const session = store.getSession(payload.sid)
    if (!session || session.revokedAt || session.expiresAt <= Math.floor(Date.now() / 1000)) {
      throw new AuthError(401, 'login_expired')
    }
    return session
  }

  function extractToken(headers, url) {
    const authorization = String(headers.authorization || '').trim()
    const bearer = authorization.match(/^Bearer\s+(.+)$/i)
    if (bearer && bearer[1]) {
      return bearer[1].trim()
    }
    return String(url.searchParams.get('token') || '').trim()
  }

  function getOptionalSession(headers, url) {
    const token = extractToken(headers, url)
    if (!token) {
      return null
    }
    return { ...verifyToken(token), token }
  }

  function requireSession(headers, url) {
    const token = extractToken(headers, url)
    if (!token) {
      throw new AuthError(401, 'login_required')
    }
    return { ...verifyToken(token), token }
  }

  function createSession(user) {
    const createdAt = Math.floor(Date.now() / 1000)
    const session = {
      id: crypto.randomUUID(),
      userId: user.id,
      createdAt,
      expiresAt: createdAt + config.sessionTtlSeconds,
    }
    store.createSession(session)
    const expiresAt = new Date(session.expiresAt * 1000).toISOString()
    return {
      token: signSession(session),
      expiresAt,
      expiresAtTs: session.expiresAt,
      user,
    }
  }

  async function resolveWechatSubject(code) {
    const normalizedCode = String(code || '').trim()
    if (!normalizedCode) {
      throw new AuthError(400, 'wechat_code_required')
    }
    if (normalizedCode.length > 1024) {
      throw new AuthError(401, 'wechat_invalid_code')
    }

    if (config.wechatAppId && config.wechatAppSecret) {
      const endpoint = new URL('https://api.weixin.qq.com/sns/jscode2session')
      endpoint.searchParams.set('appid', config.wechatAppId)
      endpoint.searchParams.set('secret', config.wechatAppSecret)
      endpoint.searchParams.set('js_code', normalizedCode)
      endpoint.searchParams.set('grant_type', 'authorization_code')

      let response
      let payload
      try {
        response = await fetchImpl(endpoint, { signal: AbortSignal.timeout(config.remoteRequestTimeoutMs) })
        payload = await response.json()
      } catch {
        throw new AuthError(503, 'wechat_request_failed')
      }

      if (!response.ok || !payload || !payload.openid) {
        if (payload && Number(payload.errcode) === 45011) {
          throw new AuthError(429, 'wechat_rate_limited')
        }
        throw new AuthError(401, 'wechat_invalid_code')
      }
      return `wechat:${payload.openid}`
    }

    if (!config.allowDevelopmentLogin) {
      throw new AuthError(503, 'wechat_login_not_configured')
    }
    return `development:${sha256(normalizedCode).slice(0, 48)}`
  }

  async function login(code, profile) {
    const subject = await resolveWechatSubject(code)
    const user = store.upsertUser('wechat', subject, profile)
    return createSession(user)
  }

  return {
    AuthError,
    createSession,
    getOptionalSession,
    login,
    requireSession,
  }
}

module.exports = {
  AuthError,
  createAuth,
}

const { Readable } = require('node:stream')

class ApiError extends Error {
  constructor(status, message, details) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.details = details
    this.expose = true
  }
}

function corsHeaders(config, origin) {
  const configured = config.corsAllowOrigin
  const allowOrigin = configured === '*' ? '*' : (origin === configured ? origin : configured)
  return {
    'Access-Control-Allow-Origin': allowOrigin,
    'Access-Control-Allow-Headers': 'Authorization, Content-Type',
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Max-Age': '600',
    Vary: 'Origin',
  }
}

function sendJson(res, config, status, payload, origin) {
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    ...corsHeaders(config, origin),
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  })
  res.end(body)
}

function sendError(res, config, error, origin) {
  const status = Number(error && error.status) || 500
  const message = status >= 500 && !error.expose ? 'internal_error' : String(error && error.message || 'bad_request')
  const payload = { message }
  if (error && error.details && status < 500) {
    payload.details = error.details
  }
  sendJson(res, config, status, payload, origin)
}

function sendEmpty(res, config, status, origin) {
  res.writeHead(status, corsHeaders(config, origin))
  res.end()
}

function readRequestBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = []
    let total = 0
    let settled = false

    req.on('data', (chunk) => {
      total += chunk.length
      if (total > limit) {
        settled = true
        reject(new ApiError(413, 'payload_too_large'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      if (!settled) {
        resolve(Buffer.concat(chunks))
      }
    })
    req.on('error', (error) => {
      if (!settled) {
        reject(error)
      }
    })
  })
}

async function readJson(req, limit = 256 * 1024) {
  const contentLength = Number(req.headers['content-length'] || 0)
  if (contentLength > limit) {
    throw new ApiError(413, 'payload_too_large')
  }
  const body = await readRequestBody(req, limit)
  if (!body.length) {
    return {}
  }
  try {
    const parsed = JSON.parse(body.toString('utf8'))
    if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
      throw new Error('not_an_object')
    }
    return parsed
  } catch {
    throw new ApiError(400, 'invalid_json')
  }
}

async function readMultipartForm(req, origin, limit = 5 * 1024 * 1024 + 64 * 1024) {
  const contentLength = Number(req.headers['content-length'] || 0)
  if (contentLength > limit) {
    throw new ApiError(413, 'avatar_file_too_large')
  }
  const contentType = String(req.headers['content-type'] || '')
  if (!contentType.toLowerCase().startsWith('multipart/form-data')) {
    throw new ApiError(400, 'avatar_file_required')
  }

  try {
    const request = new Request(origin, {
      method: req.method,
      headers: { 'Content-Type': contentType },
      body: Readable.toWeb(req),
      duplex: 'half',
    })
    return await request.formData()
  } catch {
    throw new ApiError(400, 'avatar_file_type_invalid')
  }
}

function publicUrl(config, pathname) {
  return `${config.appBaseUrl}${pathname.startsWith('/') ? pathname : `/${pathname}`}`
}

function requestOrigin(req, config) {
  const host = String(req.headers.host || `${config.host}:${config.port}`).trim()
  const protocol = String(req.headers['x-forwarded-proto'] || '').split(',')[0].trim() || 'http'
  return `${protocol}://${host}${req.url || '/'}`
}

function sanitizeMeta(value, depth = 0, key = '') {
  if (depth >= 4) {
    return '[depth-limited]'
  }
  if (/token|password|cookie|authorization|secret|refresh|api[_-]?key/i.test(key)) {
    return '[redacted]'
  }
  if (typeof value === 'string') {
    return value.slice(0, 512)
  }
  if (typeof value === 'number' || typeof value === 'boolean' || value === null) {
    return value
  }
  if (Array.isArray(value)) {
    return value.slice(0, 20).map((item) => sanitizeMeta(item, depth + 1, key))
  }
  if (value && typeof value === 'object') {
    const output = {}
    for (const [name, item] of Object.entries(value).slice(0, 30)) {
      output[name] = sanitizeMeta(item, depth + 1, name)
    }
    return output
  }
  return String(value ?? '').slice(0, 512)
}

module.exports = {
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
}

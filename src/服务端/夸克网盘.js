const crypto = require('node:crypto')

const BASE_URL = 'https://drive-pc.quark.cn'
const CHUNK_SIZE = 4 * 1024 * 1024
const API_TIMEOUT_MS = 20000
const UPLOAD_TIMEOUT_MS = 300000
const OSS_USER_AGENT = 'aliyun-sdk-js/1.0.0 Chrome Mobile 139.0.0.0 on Google Nexus 5 (Android 6.0)'
const COMPLETE_USER_AGENT = 'aliyun-sdk-js/1.0.0 Chrome 139.0.0.0 on OS X 10.15.7 64-bit'

class QuarkError extends Error {
  constructor(code) {
    super(code)
    this.name = 'QuarkError'
    this.code = code
  }
}

async function requestJson(cookie, pathname, { method = 'GET', params = {}, body, timeoutMs = API_TIMEOUT_MS } = {}) {
  const url = new URL(`${BASE_URL}${pathname}`)
  for (const [key, value] of Object.entries({ pr: 'ucpro', fr: 'pc', uc_param_str: '', ...params })) {
    url.searchParams.set(key, String(value))
  }
  let response
  try {
    response = await fetch(url, {
      method,
      headers: {
        Accept: 'application/json, text/plain, */*',
        'User-Agent': 'Mozilla/5.0',
        Cookie: cookie,
        ...(body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch {
    throw new QuarkError('quark_request_failed')
  }
  if (!response.ok) throw new QuarkError(`quark_http_${response.status}`)
  let payload
  try {
    payload = await response.json()
  } catch {
    throw new QuarkError('quark_response_invalid')
  }
  if (payload && payload.code !== undefined && Number(payload.code) !== 0) {
    throw new QuarkError(`quark_api_${String(payload.code).slice(0, 32)}`)
  }
  return payload || {}
}

function normalizeCookie(value) {
  const cookie = String(value || '').trim()
  if (!cookie || cookie.length > 8192 || /[\r\n]/.test(cookie)) {
    throw new QuarkError('quark_cookie_invalid')
  }
  return cookie
}

function deriveCookieKey(secret) {
  return Buffer.from(crypto.hkdfSync(
    'sha256',
    Buffer.from(String(secret || ''), 'utf8'),
    Buffer.from('mantou-toolbox-storage', 'utf8'),
    Buffer.from('quark-cookie-v1', 'utf8'),
    32
  ))
}

function encryptCookie(value, secret) {
  const cookie = String(value || '')
  if (!cookie) return ''
  const iv = crypto.randomBytes(12)
  const cipher = crypto.createCipheriv('aes-256-gcm', deriveCookieKey(secret), iv)
  cipher.setAAD(Buffer.from('quark-cookie-v1', 'utf8'))
  const ciphertext = Buffer.concat([cipher.update(cookie, 'utf8'), cipher.final()])
  return `v1.${iv.toString('base64url')}.${cipher.getAuthTag().toString('base64url')}.${ciphertext.toString('base64url')}`
}

function decryptCookie(value, secret) {
  const parts = String(value || '').split('.')
  if (parts.length !== 4 || parts[0] !== 'v1') return ''
  try {
    const decipher = crypto.createDecipheriv('aes-256-gcm', deriveCookieKey(secret), Buffer.from(parts[1], 'base64url'))
    decipher.setAAD(Buffer.from('quark-cookie-v1', 'utf8'))
    decipher.setAuthTag(Buffer.from(parts[2], 'base64url'))
    return Buffer.concat([
      decipher.update(Buffer.from(parts[3], 'base64url')),
      decipher.final(),
    ]).toString('utf8')
  } catch {
    return ''
  }
}

async function testQuarkConnection(cookieValue) {
  const cookie = normalizeCookie(cookieValue)
  const result = await requestJson(cookie, '/1/clouddrive/capacity/growth/info')
  if (!result.data || typeof result.data !== 'object') throw new QuarkError('quark_cookie_invalid')
  return { connected: true }
}

function ensureData(payload, code) {
  if (!payload || !payload.data || typeof payload.data !== 'object') throw new QuarkError(code)
  return payload.data
}

async function getOrCreateFolder(cookie, folderName) {
  const listing = await requestJson(cookie, '/1/clouddrive/file/sort', {
    params: {
      pdir_fid: '0',
      _page: 1,
      _size: 50,
      _fetch_total: 1,
      _fetch_sub_dirs: 1,
      _sort: 'file_type:asc,updated_at:desc',
    },
  })
  const items = Array.isArray(listing.data && listing.data.list) ? listing.data.list : []
  const existing = items.find((item) => (item.dir || item.dir_type) && String(item.file_name || item.title || '') === folderName)
  if (existing && (existing.fid || existing.file_id)) return String(existing.fid || existing.file_id)
  const created = await requestJson(cookie, '/1/clouddrive/file', {
    method: 'POST',
    body: { pdir_fid: '0', file_name: folderName, dir_init_lock: false, dir_path: '' },
  })
  const data = ensureData(created, 'quark_folder_create_failed')
  const folderId = data.fid || data.file_id
  if (!folderId) throw new QuarkError('quark_folder_id_missing')
  return String(folderId)
}

function rotateLeft(value, bits) {
  return ((value << bits) | (value >>> (32 - bits))) >>> 0
}

// Quark's multipart authorization needs the SHA-1 continuation state for later chunks.
function initialSha1State() {
  return [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0]
}

function applySha1Blocks(state, input) {
  let [h0, h1, h2, h3, h4] = state
  for (let offset = 0; offset + 64 <= input.length; offset += 64) {
    const words = new Array(80)
    for (let index = 0; index < 16; index += 1) words[index] = input.readUInt32BE(offset + index * 4)
    for (let index = 16; index < 80; index += 1) {
      words[index] = rotateLeft(words[index - 3] ^ words[index - 8] ^ words[index - 14] ^ words[index - 16], 1)
    }
    let [a, b, c, d, e] = [h0, h1, h2, h3, h4]
    for (let index = 0; index < 80; index += 1) {
      let f
      let k
      if (index < 20) {
        f = (b & c) | (~b & d)
        k = 0x5a827999
      } else if (index < 40) {
        f = b ^ c ^ d
        k = 0x6ed9eba1
      } else if (index < 60) {
        f = (b & c) | (b & d) | (c & d)
        k = 0x8f1bbcdc
      } else {
        f = b ^ c ^ d
        k = 0xca62c1d6
      }
      const temp = (rotateLeft(a, 5) + f + e + k + words[index]) >>> 0
      e = d
      d = c
      c = rotateLeft(b, 30)
      b = a
      a = temp
    }
    h0 = (h0 + a) >>> 0
    h1 = (h1 + b) >>> 0
    h2 = (h2 + c) >>> 0
    h3 = (h3 + d) >>> 0
    h4 = (h4 + e) >>> 0
  }
  return [h0, h1, h2, h3, h4]
}

function calculateFileHashes(buffer) {
  const md5 = crypto.createHash('md5').update(buffer).digest('hex')
  const sha1 = crypto.createHash('sha1').update(buffer).digest('hex')
  const state = initialSha1State()
  const contexts = []
  const partCount = Math.ceil(buffer.length / CHUNK_SIZE)
  for (let part = 0; part < partCount - 1; part += 1) {
    const end = (part + 1) * CHUNK_SIZE
    state.splice(0, state.length, ...applySha1Blocks(state, buffer.subarray(part * CHUNK_SIZE, end)))
    const hashState = Object.fromEntries(state.map((value, index) => [`h${index}`, String(value)]))
    contexts.push(Buffer.from(JSON.stringify({
      hash_type: 'sha1',
      ...hashState,
      Nl: String(end * 8),
      Nh: '0',
      data: '',
      num: '0',
    })).toString('base64'))
  }
  return { md5, sha1, contexts }
}

function buildPutAuthMeta({ mimeType, date, bucket, objectKey, uploadId, partNumber, hashContext = '' }) {
  const lines = ['PUT', '', mimeType, date, `x-oss-date:${date}`]
  if (hashContext) lines.push(`x-oss-hash-ctx:${hashContext}`)
  lines.push(`x-oss-user-agent:${OSS_USER_AGENT}`, `/${bucket}/${objectKey}?partNumber=${partNumber}&uploadId=${uploadId}`)
  return lines.join('\n')
}

function buildCompleteXml(etags) {
  const parts = etags.map((etag, index) => `<Part><PartNumber>${index + 1}</PartNumber><ETag>"${etag}"</ETag></Part>`)
  return `<?xml version="1.0" encoding="UTF-8"?>\n<CompleteMultipartUpload>\n${parts.join('\n')}\n</CompleteMultipartUpload>`
}

function buildCompleteAuthMeta({ date, bucket, objectKey, uploadId, xml, callback }) {
  const callbackBase64 = Buffer.from(JSON.stringify(callback)).toString('base64')
  const xmlMd5 = crypto.createHash('md5').update(xml).digest('base64')
  return [
    'POST',
    xmlMd5,
    'application/xml',
    date,
    `x-oss-callback:${callbackBase64}`,
    `x-oss-date:${date}`,
    `x-oss-user-agent:${COMPLETE_USER_AGENT}`,
    `/${bucket}/${objectKey}?uploadId=${uploadId}`,
  ].join('\n')
}

async function getUploadAuthorization(cookie, taskId, authInfo, authMeta) {
  const response = await requestJson(cookie, '/1/clouddrive/file/upload/auth', {
    method: 'POST',
    body: { task_id: taskId, auth_info: authInfo, auth_meta: authMeta },
  })
  const authKey = response.data && response.data.auth_key
  if (!authKey) throw new QuarkError('quark_upload_authorization_missing')
  return authKey
}

async function uploadPart({ bucket, objectKey, uploadId, partNumber, data, headers }) {
  const url = `https://${bucket}.pds.quark.cn/${objectKey}?partNumber=${partNumber}&uploadId=${uploadId}`
  let response
  try {
    response = await fetch(url, {
      method: 'PUT',
      headers,
      body: data,
      signal: AbortSignal.timeout(UPLOAD_TIMEOUT_MS),
    })
  } catch {
    throw new QuarkError('quark_upload_part_failed')
  }
  if (!response.ok) throw new QuarkError(`quark_upload_part_http_${response.status}`)
  const etag = String(response.headers.get('etag') || '').replace(/^"|"$/g, '')
  if (!etag) throw new QuarkError('quark_upload_etag_missing')
  return etag
}

async function completeMultipart({ cookie, taskId, authInfo, bucket, objectKey, uploadId, callback, etags }) {
  const xml = buildCompleteXml(etags)
  const date = new Date().toUTCString()
  const authKey = await getUploadAuthorization(cookie, taskId, authInfo, buildCompleteAuthMeta({
    date, bucket, objectKey, uploadId, xml, callback,
  }))
  const callbackBase64 = Buffer.from(JSON.stringify(callback)).toString('base64')
  const xmlMd5 = crypto.createHash('md5').update(xml).digest('base64')
  let response
  try {
    response = await fetch(`https://${bucket}.pds.quark.cn/${objectKey}?uploadId=${uploadId}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/xml',
        'x-oss-date': date,
        'x-oss-user-agent': COMPLETE_USER_AGENT,
        'x-oss-callback': callbackBase64,
        'Content-MD5': xmlMd5,
        authorization: authKey,
      },
      body: xml,
      signal: AbortSignal.timeout(UPLOAD_TIMEOUT_MS),
    })
  } catch {
    throw new QuarkError('quark_upload_complete_failed')
  }
  if (!response.ok) throw new QuarkError(`quark_upload_complete_http_${response.status}`)
}

async function uploadContent(cookie, folderId, fileName, content) {
  const fileBuffer = Buffer.isBuffer(content) ? content : Buffer.from(content)
  const now = Date.now()
  const mimeType = 'text/plain'
  const preResult = await requestJson(cookie, '/1/clouddrive/file/upload/pre', {
    method: 'POST',
    body: {
      ccp_hash_update: true,
      parallel_upload: true,
      pdir_fid: folderId,
      dir_name: '',
      size: fileBuffer.length,
      file_name: fileName,
      format_type: mimeType,
      l_updated_at: now,
      l_created_at: now,
    },
  })
  const data = ensureData(preResult, 'quark_upload_prepare_failed')
  const taskId = String(data.task_id || '')
  if (!taskId) throw new QuarkError('quark_upload_task_missing')
  const uploadId = String(data.upload_id || '')
  const objectKey = String(data.obj_key || '')
  const bucket = String(data.bucket || 'ul-zb')
  const authInfo = String(data.auth_info || '')
  const callback = data.callback || {}
  const hashes = calculateFileHashes(fileBuffer)
  await requestJson(cookie, '/1/clouddrive/file/update/hash', {
    method: 'POST',
    body: { task_id: taskId, md5: hashes.md5, sha1: hashes.sha1 },
  })

  if (authInfo && objectKey && uploadId) {
    const partCount = Math.ceil(fileBuffer.length / CHUNK_SIZE)
    if (partCount > 1 && !Object.keys(callback).length) throw new QuarkError('quark_multipart_callback_missing')
    const etags = []
    for (let index = 0; index < partCount; index += 1) {
      const partNumber = index + 1
      const start = index * CHUNK_SIZE
      const part = fileBuffer.subarray(start, Math.min(start + CHUNK_SIZE, fileBuffer.length))
      const date = new Date().toUTCString()
      const hashContext = index > 0 ? hashes.contexts[index - 1] : ''
      const authMeta = buildPutAuthMeta({
        mimeType,
        date,
        bucket,
        objectKey,
        uploadId,
        partNumber,
        hashContext,
      })
      const authKey = await getUploadAuthorization(cookie, taskId, authInfo, authMeta)
      const headers = {
        'Content-Type': mimeType,
        'x-oss-date': date,
        'x-oss-user-agent': OSS_USER_AGENT,
        authorization: authKey,
      }
      if (hashContext) headers['X-Oss-Hash-Ctx'] = hashContext
      etags.push(await uploadPart({ bucket, objectKey, uploadId, partNumber, data: part, headers }))
    }
    await completeMultipart({ cookie, taskId, authInfo, bucket, objectKey, uploadId, callback, etags })
  } else if (!data.rapid_upload) {
    throw new QuarkError('quark_upload_session_missing')
  }

  const finishBody = { task_id: taskId }
  if (objectKey) finishBody.obj_key = objectKey
  const finishResult = await requestJson(cookie, '/1/clouddrive/file/upload/finish', {
    method: 'POST',
    body: finishBody,
  })
  const finishData = ensureData(finishResult, 'quark_upload_finish_failed')
  const fid = finishData.fid || finishData.file_id || finishData.file && (finishData.file.fid || finishData.file.file_id)
  if (fid) return String(fid)
  const listing = await requestJson(cookie, '/1/clouddrive/file/sort', {
    params: {
      pdir_fid: folderId,
      _page: 1,
      _size: 50,
      _fetch_total: 1,
      _fetch_sub_dirs: 0,
      _sort: 'file_type:asc,updated_at:desc',
    },
  })
  const items = Array.isArray(listing.data && listing.data.list) ? listing.data.list : []
  const uploaded = items.find((item) => String(item.file_name || '') === fileName && Number(item.size) === fileBuffer.length)
  if (!uploaded || !(uploaded.fid || uploaded.file_id)) throw new QuarkError('quark_uploaded_file_id_missing')
  return String(uploaded.fid || uploaded.file_id)
}

async function createShare(cookie, fid, title) {
  const created = await requestJson(cookie, '/1/clouddrive/share', {
    method: 'POST',
    body: { fid_list: [fid], title, url_type: 1, expired_type: 1 },
  })
  const taskId = String(created.data && created.data.task_id || '')
  if (!taskId) throw new QuarkError('quark_share_task_missing')
  let shareId = ''
  for (let index = 0; index < 12; index += 1) {
    const task = await requestJson(cookie, '/1/clouddrive/task', {
      params: { task_id: taskId, retry_index: index },
    })
    const data = task.data || {}
    if (Number(data.status) === 2) {
      shareId = String(data.share_id || '')
      break
    }
    if (Number(data.status) === 3) throw new QuarkError('quark_share_creation_failed')
    if (index < 11) await new Promise((resolve) => setTimeout(resolve, 900))
  }
  if (!shareId) throw new QuarkError('quark_share_creation_timeout')
  const detail = await requestJson(cookie, '/1/clouddrive/share/password', {
    method: 'POST',
    body: { share_id: shareId },
  })
  const shareUrl = String(detail.data && (detail.data.share_url || detail.data.url) || '')
  if (!/^https:\/\/(?:pan\.)?quark\.cn\//i.test(shareUrl)) throw new QuarkError('quark_share_url_missing')
  return { shareId, shareUrl }
}

async function deleteQuarkShare(cookieValue, shareId) {
  const cookie = normalizeCookie(cookieValue)
  const id = String(shareId || '').trim()
  if (!id || id.length > 256 || /[\r\n]/.test(id)) throw new QuarkError('quark_share_id_invalid')
  const result = await requestJson(cookie, '/1/clouddrive/share/delete', {
    method: 'POST',
    body: { share_ids: [id] },
  })
  if (result.status !== undefined && Number(result.status) !== 200) {
    throw new QuarkError(`quark_share_delete_status_${String(result.status).slice(0, 16)}`)
  }
  return { deleted: true }
}

async function deleteQuarkFile(cookieValue, fileId) {
  const cookie = normalizeCookie(cookieValue)
  const fid = String(fileId || '').trim()
  if (!fid || fid.length > 256 || /[\r\n]/.test(fid)) throw new QuarkError('quark_file_id_invalid')
  await requestJson(cookie, '/1/clouddrive/file/delete', {
    method: 'POST',
    body: { action_type: 2, filelist: [fid], exclude_fids: [] },
  })
  return { deleted: true }
}

async function uploadNovelToQuark({ cookie: cookieValue, folderName, fileName, title, content }) {
  const cookie = normalizeCookie(cookieValue)
  const normalizedFolderName = String(folderName || '').trim()
  if (!normalizedFolderName) throw new QuarkError('quark_folder_name_invalid')
  const folderId = await getOrCreateFolder(cookie, normalizedFolderName)
  const fid = await uploadContent(cookie, folderId, fileName, content)
  const share = await createShare(cookie, fid, title || fileName)
  return {
    type: 'quark',
    label: '夸克网盘',
    fileId: fid,
    shareId: share.shareId,
    shareUrl: share.shareUrl,
    copyText: share.shareUrl,
  }
}

module.exports = {
  QuarkError,
  normalizeCookie,
  testQuarkConnection,
  uploadNovelToQuark,
  deleteQuarkShare,
  deleteQuarkFile,
  calculateFileHashes,
  encryptCookie,
  decryptCookie,
}

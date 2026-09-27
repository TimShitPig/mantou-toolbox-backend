const LOGIN_URL = 'https://passport.yuewen.com/userSdk/sendmsgnew'
const SMS_LOGIN_URL = 'https://passport.yuewen.com/userSdk/phonecodelogin'
const REFERER = 'https://passport.yuewen.com/yuewen.html?appid=1450000219&areaid=1'
const USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

class QQReaderLoginError extends Error {
  constructor(status, code) {
    super(code)
    this.name = 'QQReaderLoginError'
    this.status = status
    this.expose = true
  }
}

function assertPhone(value) {
  const phone = String(value || '').trim()
  if (!/^1\d{10}$/.test(phone)) throw new QQReaderLoginError(400, 'qqread_phone_invalid')
  return phone
}

function parseJsonp(text) {
  if (typeof text !== 'string' || text.length > 64 * 1024) {
    throw new QQReaderLoginError(502, 'qqread_auth_response_invalid')
  }
  const match = text.trim().match(/^[\w$]+\(([\s\S]*)\)\s*;?$/)
  if (!match) throw new QQReaderLoginError(502, 'qqread_auth_response_invalid')
  try {
    const data = JSON.parse(match[1])
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('invalid')
    return data
  } catch {
    throw new QQReaderLoginError(502, 'qqread_auth_response_invalid')
  }
}

async function requestJsonp(url, params, fetchImpl = globalThis.fetch) {
  const target = new URL(url)
  for (const [key, value] of Object.entries(params)) target.searchParams.set(key, value)
  let response
  try {
    response = await fetchImpl(target, {
      headers: {
        'User-Agent': USER_AGENT,
        Referer: REFERER,
        Origin: 'https://passport.yuewen.com',
      },
      signal: AbortSignal.timeout(10000),
    })
  } catch {
    throw new QQReaderLoginError(502, 'qqread_auth_upstream_unavailable')
  }
  if (!response.ok) throw new QQReaderLoginError(502, 'qqread_auth_upstream_unavailable')
  try {
    return parseJsonp(await response.text())
  } catch (error) {
    if (error instanceof QQReaderLoginError) throw error
    throw new QQReaderLoginError(502, 'qqread_auth_upstream_unavailable')
  }
}

async function requestSmsSession(phoneValue, fetchImpl) {
  const phone = assertPhone(phoneValue)
  const data = await requestJsonp(LOGIN_URL, {
    appId: '1450000219',
    areaId: '1',
    format: 'jsonp',
    method: 'callback',
    phoneIsAbroad: '0',
    inputUserId: `+86${phone}`,
    mobilePhone: phone,
    type: '1',
    needRegister: '0',
  }, fetchImpl)
  const sessionKey = String(data.data?.sessionKey || '').trim()
  if (!sessionKey || sessionKey.length > 512) throw new QQReaderLoginError(400, 'qqread_sms_session_failed')
  return { phone, sessionKey }
}

async function sendSmsCode(input, fetchImpl) {
  input = input && typeof input === 'object' && !Array.isArray(input) ? input : {}
  const phone = assertPhone(input.phone)
  const sessionKey = String(input.sessionKey || '').trim()
  const ticket = String(input.ticket || '').trim()
  const randstr = String(input.randstr || '').trim()
  if (!sessionKey || sessionKey.length > 512 || !ticket || ticket.length > 512 || !randstr || randstr.length > 512) {
    throw new QQReaderLoginError(400, 'qqread_captcha_required')
  }
  const data = await requestJsonp(LOGIN_URL, {
    appId: '1450000219',
    areaId: '1',
    format: 'jsonp',
    method: 'callback',
    phoneIsAbroad: '0',
    inputUserId: `+86${phone}`,
    mobilePhone: phone,
    sessionKey,
    validateCode: `${randstr};${ticket}`,
    type: '1',
    needRegister: '0',
  }, fetchImpl)
  if (Number(data.code) !== 0) throw new QQReaderLoginError(400, 'qqread_sms_send_failed')
  const nextSessionKey = String(data.data?.sessionKey || sessionKey).trim()
  return { phone, sessionKey: nextSessionKey }
}

async function loginBySms(input, fetchImpl) {
  input = input && typeof input === 'object' && !Array.isArray(input) ? input : {}
  const phone = assertPhone(input.phone)
  const sessionKey = String(input.sessionKey || '').trim()
  const code = String(input.code || '').trim()
  if (!sessionKey || sessionKey.length > 512 || !/^\d{4,8}$/.test(code)) {
    throw new QQReaderLoginError(400, 'qqread_sms_input_invalid')
  }
  const data = await requestJsonp(SMS_LOGIN_URL, {
    appId: '1450000219',
    areaId: '1',
    format: 'jsonp',
    method: 'callback',
    inputUserId: phone,
    sessionKey,
    validateCode: code,
    auto: '1',
  }, fetchImpl)
  if (Number(data.code) !== 0) throw new QQReaderLoginError(400, 'qqread_sms_login_failed')
  const result = data.data || {}
  const ywguid = String(result.ywGuid || '').trim()
  const ywkey = String(result.ywKey || '').trim()
  if (!/^\d{6,32}$/.test(ywguid) || !/^[^\s;,"']{6,256}$/.test(ywkey)) {
    throw new QQReaderLoginError(502, 'qqread_login_ticket_missing')
  }
  return {
    ywguid,
    ywkey,
    phone,
    nickname: `书友_${ywguid.slice(-6)}`,
    avatarUrl: `https://shp.qpic.cn/qqreader_f/0/${ywguid}/136`,
  }
}

module.exports = { QQReaderLoginError, assertPhone, parseJsonp, requestSmsSession, sendSmsCode, loginBySms }

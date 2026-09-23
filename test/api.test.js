const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { createApp } = require('../src/app')
const { createConfig } = require('../src/config')

const PNG_BYTES = Buffer.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
  0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
  0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
  0x08, 0x06, 0x00, 0x00, 0x00, 0x1f, 0x15, 0xc4,
  0x89, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41,
  0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0xf0,
  0x1f, 0x00, 0x05, 0x00, 0x01, 0xff, 0x89, 0x99,
  0x3d, 0x1d, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,
  0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
])

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

function jsonRequest(method, payload, token) {
  const headers = {
    'Content-Type': 'application/json',
  }
  if (token) {
    headers.Authorization = `Bearer ${token}`
  }
  return {
    method,
    headers,
    body: JSON.stringify(payload),
  }
}

function authorization(token) {
  return { Authorization: `Bearer ${token}` }
}

async function requestJson(baseUrl, pathname, options) {
  const response = await fetch(new URL(pathname, baseUrl), options)
  return {
    response,
    payload: await response.json(),
  }
}

async function waitForCompletedJob(baseUrl, downloadId, token) {
  const deadline = Date.now() + 5000
  let latest

  while (Date.now() < deadline) {
    const { response, payload } = await requestJson(
      baseUrl,
      `/api/download/progress.php?id=${encodeURIComponent(downloadId)}`,
      { headers: authorization(token) }
    )
    assert.equal(response.status, 200)
    latest = payload.data
    if (latest.finished) {
      return latest
    }
    await sleep(25)
  }

  assert.fail(`download job ${downloadId} did not finish: ${JSON.stringify(latest)}`)
}

test('HTTP API workflow starts a real server and persists local state', async () => {
  const temporaryRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'mantou-api-test-'))
  const storageDir = path.join(temporaryRoot, 'storage')
  const currentRevision = 'c'.repeat(40)
  const revisions = ['a', 'b', 'c', 'd', 'e', 'f', '1'].map((character) => character.repeat(40))
  const commits = revisions.map((sha, index) => ({
    sha,
    commit: {
      message: `Release ${index}`,
      author: { date: `2026-09-${String(23 - index).padStart(2, '0')}T00:00:00Z` },
    },
  }))
  const workflowRuns = revisions.map((sha, index) => ({
    name: 'Publish Docker image',
    head_branch: 'main',
    head_sha: sha,
    status: 'completed',
    conclusion: index === 3 ? 'failure' : 'success',
  }))
  const config = createConfig({}, {
    host: '127.0.0.1',
    port: 0,
    appBaseUrl: 'http://127.0.0.1:0',
    storageDir,
    databasePath: path.join(storageDir, 'mantou.sqlite'),
    avatarDir: path.join(storageDir, 'avatars'),
    downloadDir: path.join(storageDir, 'downloads'),
    appSecret: 'test',
    adminPassword: 'integration-admin-password',
    appBuildRevision: currentRevision,
    allowDevelopmentLogin: true,
  })
  const app = createApp({
    config,
    updateFetchImpl: async (url) => ({
      ok: true,
      json: async () => String(url).includes('/actions/runs?')
        ? { workflow_runs: workflowRuns }
        : commits,
    }),
  })

  try {
    await app.listen()
    const baseUrl = app.config.appBaseUrl

    const health = await requestJson(baseUrl, '/healthz')
    assert.equal(health.response.status, 200)
    assert.deepEqual(health.payload, { data: { status: 'ok' } })

    const adminPage = await fetch(new URL('/admin', baseUrl))
    assert.equal(adminPage.status, 200)
    const adminMarkup = await adminPage.text()
    assert.match(adminMarkup, /馒头工具箱/)
    assert.match(adminMarkup, /aria-label="后台导航"/)
    assert.match(adminMarkup, /id="update-button"/)
    assert.match(adminMarkup, /id="update-dialog"/)
    for (const page of ['novel', 'logs', 'data', 'ads', 'status']) {
      assert.match(adminMarkup, new RegExp(`data-page="${page}"`))
      assert.match(adminMarkup, new RegExp(`id="page-${page}"`))
    }
    const anonymousAdmin = await requestJson(baseUrl, '/api/admin/summary')
    assert.equal(anonymousAdmin.response.status, 401)
    const anonymousUpdateCheck = await requestJson(baseUrl, '/api/admin/updates')
    assert.equal(anonymousUpdateCheck.response.status, 401)

    const unauthenticatedProfile = await requestJson(baseUrl, '/api/v1/auth/profile.php')
    assert.equal(unauthenticatedProfile.response.status, 401)
    assert.equal(unauthenticatedProfile.payload.message, 'login_required')

    const login = await requestJson(
      baseUrl,
      '/api/v1/auth/login.php',
      jsonRequest('POST', {
        code: 'development-integration-code',
        nickName: 'Initial User',
        avatarUrl: '',
      })
    )
    assert.equal(login.response.status, 200)
    assert.equal(typeof login.payload.data.token, 'string')
    assert.ok(login.payload.data.token.length > 20)
    assert.equal(login.payload.data.user.nickName, 'Initial User')
    const token = login.payload.data.token

    const me = await requestJson(baseUrl, '/api/v1/auth/me.php', {
      headers: authorization(token),
    })
    assert.equal(me.response.status, 200)
    assert.equal(me.payload.data.token, token)
    assert.equal(me.payload.data.user.id, login.payload.data.user.id)

    const updateProfile = await requestJson(
      baseUrl,
      '/api/v1/auth/profile.php',
      jsonRequest('POST', {
        nickName: 'Updated User',
        avatarUrl: 'https://example.test/avatar.png',
      }, token)
    )
    assert.equal(updateProfile.response.status, 200)
    assert.equal(updateProfile.payload.data.user.nickName, 'Updated User')

    const profile = await requestJson(baseUrl, '/api/v1/auth/profile.php', {
      headers: authorization(token),
    })
    assert.equal(profile.response.status, 200)
    assert.equal(profile.payload.data.user.nickName, 'Updated User')
    assert.equal(profile.payload.data.user.avatarUrl, 'https://example.test/avatar.png')

    const avatarForm = new FormData()
    avatarForm.set('avatar', new Blob([PNG_BYTES], { type: 'image/png' }), 'avatar.png')
    const avatarUpload = await requestJson(baseUrl, '/api/v1/upload/avatar.php', {
      method: 'POST',
      headers: authorization(token),
      body: avatarForm,
    })
    assert.equal(avatarUpload.response.status, 200)
    assert.equal(avatarUpload.payload.data.user.avatarUrl, avatarUpload.payload.data.avatarUrl)
    assert.match(avatarUpload.payload.data.avatarUrl, /^http:\/\/127\.0\.0\.1:\d+\/uploads\/avatars\//)

    const avatarFile = await fetch(avatarUpload.payload.data.avatarUrl)
    assert.equal(avatarFile.status, 200)
    assert.equal(avatarFile.headers.get('content-type'), 'image/png')
    assert.deepEqual(Buffer.from(await avatarFile.arrayBuffer()), PNG_BYTES)

    const status = await requestJson(baseUrl, '/api/download/status.php', {
      headers: authorization(token),
    })
    assert.equal(status.response.status, 200)
    assert.equal(status.payload.data.enabled, true)
    assert.equal(status.payload.data.parseEnabled, true)
    assert.equal(status.payload.data.qimaoEnabled, true)
    assert.equal(status.payload.data.downloadCountUsed, 0)

    const qimaoLink = 'https://m.qimao.com/shuku/123456/'
    const parse = await requestJson(
      baseUrl,
      '/api/download/parse.php',
      jsonRequest('POST', { link: qimaoLink }, token)
    )
    assert.equal(parse.response.status, 200)
    assert.equal(parse.payload.data.source, 'qimao')
    assert.equal(parse.payload.data.sourceBookId, '123456')
    assert.equal(parse.payload.data.originalUrl, qimaoLink)

    const fanqieParse = await requestJson(
      baseUrl,
      '/api/download/parse.php',
      jsonRequest('POST', { link: 'https://changdunovel.com/t/book_slug_1/' }, token)
    )
    assert.equal(fanqieParse.response.status, 200)
    assert.equal(fanqieParse.payload.data.source, 'fanqie')
    assert.equal(fanqieParse.payload.data.sourceBookId, 'book_slug_1')

    const generate = await requestJson(
      baseUrl,
      '/api/download/generate.php',
      jsonRequest('POST', {
        link: qimaoLink,
        source: 'qimao',
        sourceBookId: '123456',
        book: {
          ...parse.payload.data,
          title: 'Integration Book',
          author: 'Integration Author',
          intro: 'Generated by the integration test.',
        },
      }, token)
    )
    assert.equal(generate.response.status, 200)
    assert.equal(generate.payload.data.status, 'processing')
    assert.match(generate.payload.data.downloadId, /^[a-f0-9-]{36}$/)

    const progress = await waitForCompletedJob(baseUrl, generate.payload.data.downloadId, token)
    assert.equal(progress.failed, false)
    assert.equal(progress.finished, true)
    assert.equal(progress.completed, progress.total)
    assert.equal(progress.manifest.fileName, 'Integration Book.txt')
    assert.equal(typeof progress.downloadUrl, 'string')

    const fileUrl = new URL(progress.downloadUrl)
    fileUrl.searchParams.set('token', token)
    const generatedFile = await fetch(fileUrl)
    assert.equal(generatedFile.status, 200)
    assert.match(generatedFile.headers.get('content-type'), /^text\/plain; charset=utf-8$/)
    const generatedText = await generatedFile.text()
    assert.match(generatedText, /Integration Book/)
    assert.match(generatedText, /Integration Author/)

    const statusAfterDownload = await requestJson(baseUrl, '/api/download/status.php', {
      headers: authorization(token),
    })
    assert.equal(statusAfterDownload.response.status, 200)
    assert.equal(statusAfterDownload.payload.data.downloadCountUsed, 1)

    const badAdminLogin = await requestJson(
      baseUrl,
      '/api/admin/login',
      jsonRequest('POST', { password: 'incorrect-password' })
    )
    assert.equal(badAdminLogin.response.status, 401)
    assert.equal(badAdminLogin.payload.message, 'admin_password_invalid')

    const adminLogin = await requestJson(
      baseUrl,
      '/api/admin/login',
      jsonRequest('POST', { password: 'integration-admin-password' })
    )
    assert.equal(adminLogin.response.status, 200)
    const setCookie = adminLogin.response.headers.get('set-cookie')
    assert.match(setCookie, /HttpOnly/)
    assert.match(setCookie, /SameSite=Strict/)
    const adminCookie = setCookie.split(';')[0]

    const adminSummary = await requestJson(baseUrl, '/api/admin/summary', {
      headers: { Cookie: adminCookie },
    })
    assert.equal(adminSummary.response.status, 200)
    assert.equal(adminSummary.payload.data.metrics.users, 1)
    assert.equal(adminSummary.payload.data.metrics.jobs_completed_today, 1)
    assert.equal(adminSummary.payload.data.settings.downloadEnabled, true)
    assert.equal(adminSummary.payload.data.jobs[0].title, 'Integration Book')

    const updateInfo = await requestJson(baseUrl, '/api/admin/updates', {
      headers: { Cookie: adminCookie },
    })
    assert.equal(updateInfo.response.status, 200)
    assert.equal(updateInfo.payload.data.currentRevision, currentRevision)
    assert.equal(updateInfo.payload.data.latest.revision, revisions[0])
    assert.equal(updateInfo.payload.data.hasUpdate, true)
    assert.deepEqual(
      updateInfo.payload.data.rollbackVersions.map((version) => version.revision),
      [revisions[4], revisions[5], revisions[6]]
    )

    const updateSettings = await requestJson(
      baseUrl,
      '/api/admin/settings',
      {
        method: 'PATCH',
        headers: { Cookie: adminCookie, 'Content-Type': 'application/json' },
        body: JSON.stringify({ qimaoEnabled: false, downloadLimit: 7 }),
      }
    )
    assert.equal(updateSettings.response.status, 200)
    assert.equal(updateSettings.payload.data.qimaoEnabled, false)
    assert.equal(updateSettings.payload.data.downloadLimit, 7)

    const updatedDownloadStatus = await requestJson(baseUrl, '/api/download/status.php')
    assert.equal(updatedDownloadStatus.payload.data.qimaoEnabled, false)
    assert.equal(updatedDownloadStatus.payload.data.downloadLimitEnabled, true)
    assert.equal(updatedDownloadStatus.payload.data.downloadLimit, 7)

    const disabledQimaoParse = await requestJson(
      baseUrl,
      '/api/download/parse.php',
      jsonRequest('POST', { link: qimaoLink })
    )
    assert.equal(disabledQimaoParse.response.status, 503)
    assert.equal(disabledQimaoParse.payload.message, 'qimao_disabled')

    const adminLogout = await fetch(new URL('/api/admin/logout', baseUrl), {
      method: 'POST',
      headers: { Cookie: adminCookie },
    })
    assert.equal(adminLogout.status, 200)
    assert.match(adminLogout.headers.get('set-cookie'), /Max-Age=0/)
    const loggedOutAdmin = await requestJson(baseUrl, '/api/admin/summary', {
      headers: { Cookie: adminCookie },
    })
    assert.equal(loggedOutAdmin.response.status, 401)
  } finally {
    await app.close()
    await fs.rm(temporaryRoot, { recursive: true, force: true })
  }
})

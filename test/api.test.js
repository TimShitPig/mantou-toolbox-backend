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
  const currentVersion = 'v0.0.4'
  const revisions = ['a', 'b', 'c', 'd', 'e', 'f', '1'].map((character) => character.repeat(40))
  const versionNames = ['v0.0.6', 'v0.0.5', 'v0.0.4', 'v0.0.1', 'v0.0.3', 'v0.0.2', 'v0.0.0']
  const tags = versionNames.map((name, index) => ({ name, commit: { sha: revisions[index] } }))
  const workflowRuns = revisions.map((sha, index) => ({
    name: 'Publish Docker image',
    head_branch: 'main',
    head_sha: sha,
    status: 'completed',
    conclusion: index === 3 ? 'failure' : 'success',
    created_at: `2026-09-${String(24 - index).padStart(2, '0')}T10:00:00Z`,
    updated_at: `2026-09-${String(24 - index).padStart(2, '0')}T10:05:00Z`,
    html_url: `https://github.com/TimShitPig/mantou-toolbox-backend/actions/runs/${index + 1}`,
    head_commit: { message: `Update ${versionNames[index]}\n\nPublished application changes.` },
  }))
  const releases = [{
    tag_name: 'v0.0.6',
    published_at: '2026-09-24T10:06:00Z',
    prerelease: false,
    body: 'Added version history and release notes.',
    html_url: 'https://github.com/TimShitPig/mantou-toolbox-backend/releases/tag/v0.0.6',
  }]
  const updateFetchUrls = []
  const testedProxyUrls = []
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
    appBuildVersion: currentVersion,
    deployDir: '/root/mantou-toolbox-deploy',
    allowDevelopmentLogin: true,
  })
  const app = createApp({
    config,
    updateFetchImpl: async (url) => {
      updateFetchUrls.push(String(url))
      return {
        ok: true,
        json: async () => {
          if (String(url).includes('/actions/runs?')) return { workflow_runs: workflowRuns }
          if (String(url).includes('/releases?')) return releases
          return tags
        },
      }
    },
    proxyFetchImpl: async (url) => {
      testedProxyUrls.push(String(url))
      return {
        ok: true,
        status: 200,
        json: async () => [{ name: 'v0.0.6' }],
      }
    },
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
    assert.match(adminMarkup, /RUNTIME LOGS/)
    assert.match(adminMarkup, /运行日志/)
    assert.doesNotMatch(adminMarkup, /CLIENT REPORTS/)
    assert.match(adminMarkup, /aria-label="后台导航"/)
    assert.match(adminMarkup, /id="update-button"/)
    assert.match(adminMarkup, /id="update-dialog"/)
    assert.match(adminMarkup, /id="apply-update-button"/)
    assert.match(adminMarkup, /id="apply-rollback-button"/)
    assert.match(adminMarkup, /复制更新命令/)
    assert.match(adminMarkup, /id="release-versions-body"/)
    assert.match(adminMarkup, /<th>版本<\/th>/)
    assert.match(adminMarkup, /id="release-notes-dialog"/)
    for (const proxy of ['github', 'edgeone', 'hk', 'gh-proxy', 'gh-hik']) {
      assert.match(adminMarkup, new RegExp(`value="${proxy}"`))
    }
    assert.match(adminMarkup, /测试代理连通性/)
    for (const proxy of ['github', 'edgeone', 'hk', 'gh-proxy', 'gh-hik']) {
      assert.match(adminMarkup, new RegExp(`data-proxy-result="${proxy}"`))
    }
    const adminScriptResponse = await fetch(new URL('/admin.js', baseUrl))
    assert.equal(adminScriptResponse.status, 200)
    const adminScript = await adminScriptResponse.text()
    assert.match(adminScript, /update-source\.sh/)
    assert.match(adminScript, /renderSystemLogs/)
    assert.match(adminScript, /renderVersionHistory/)
    assert.match(adminScript, /Promise\.all\(proxyRadios\.map/)
    for (const page of ['novel', 'logs', 'data', 'ads', 'status']) {
      assert.match(adminMarkup, new RegExp(`data-page="${page}"`))
      assert.match(adminMarkup, new RegExp(`id="page-${page}"`))
    }
    const anonymousAdmin = await requestJson(baseUrl, '/api/admin/summary')
    assert.equal(anonymousAdmin.response.status, 401)
    const anonymousUpdateCheck = await requestJson(baseUrl, '/api/admin/updates')
    assert.equal(anonymousUpdateCheck.response.status, 401)
    const anonymousProxyTest = await requestJson(baseUrl, '/api/admin/proxies/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxyId: 'gh-proxy' }),
    })
    assert.equal(anonymousProxyTest.response.status, 401)
    const removedUpdateRoute = await requestJson(baseUrl, '/api/admin/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'update', version: 'v2.2.0' }),
    })
    assert.equal(removedUpdateRoute.response.status, 404)

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

    const clientLog = await requestJson(baseUrl, '/api/v1/logs/client.php', jsonRequest('POST', {
      level: 'error',
      scope: 'frontend-test',
      message: 'client-only-marker',
      requestId: 'client-test-request',
    }))
    assert.equal(clientLog.response.status, 200)

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
    assert.equal(adminSummary.payload.data.metrics.errors_24h, 0)
    assert.equal(adminSummary.payload.data.settings.downloadEnabled, true)
    assert.equal(adminSummary.payload.data.jobs[0].title, 'Integration Book')
    assert.ok(adminSummary.payload.data.systemLogs.some((entry) => entry.source === 'server' && entry.message === 'Backend started'))
    assert.ok(adminSummary.payload.data.systemLogs.some((entry) => entry.source === 'http'
      && entry.method === 'POST'
      && entry.path === '/api/admin/login'
      && entry.statusCode === 401))
    assert.ok(adminSummary.payload.data.systemLogs.every((entry) => !entry.message.includes('client-only-marker')))

    const proxyTest = await requestJson(baseUrl, '/api/admin/proxies/test', {
      method: 'POST',
      headers: { Cookie: adminCookie, 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxyId: 'gh-proxy' }),
    })
    assert.equal(proxyTest.response.status, 200)
    assert.equal(proxyTest.payload.data.connected, true)
    assert.equal(proxyTest.payload.data.statusCode, 200)
    assert.match(testedProxyUrls[0], /^https:\/\/gh-proxy\.com\/https:\/\/api\.github\.com\/repos\//)

    for (const proxyId of ['github', 'edgeone', 'hk', 'gh-proxy', 'gh-hik']) {
      const result = await requestJson(baseUrl, '/api/admin/proxies/test', {
        method: 'POST',
        headers: { Cookie: adminCookie, 'Content-Type': 'application/json' },
        body: JSON.stringify({ proxyId }),
      })
      assert.equal(result.response.status, 200)
      assert.equal(result.payload.data.connected, true)
      assert.equal(result.payload.data.proxyId, proxyId)
    }
    assert.equal(testedProxyUrls.length, 6)

    const invalidProxyTest = await requestJson(baseUrl, '/api/admin/proxies/test', {
      method: 'POST',
      headers: { Cookie: adminCookie, 'Content-Type': 'application/json' },
      body: JSON.stringify({ proxyId: 'https://attacker.example' }),
    })
    assert.equal(invalidProxyTest.response.status, 400)

    const updateInfo = await requestJson(baseUrl, '/api/admin/updates?proxyId=gh-proxy', {
      headers: { Cookie: adminCookie },
    })
    assert.equal(updateInfo.response.status, 200)
    assert.equal(updateInfo.payload.data.proxyId, 'gh-proxy')
    assert.equal(updateInfo.payload.data.currentVersion, currentVersion)
    assert.equal(updateInfo.payload.data.latestVersion, 'v0.0.6')
    assert.equal(updateInfo.payload.data.hasUpdate, true)
    assert.equal(updateInfo.payload.data.deployDir, '/root/mantou-toolbox-deploy')
    assert.equal(updateInfo.payload.data.versions.length, 6)
    assert.deepEqual(updateInfo.payload.data.versions[0], {
      version: 'v0.0.6',
      publishedAt: '2026-09-24T10:06:00Z',
      content: 'Added version history and release notes.',
      prerelease: false,
      detailsUrl: 'https://github.com/TimShitPig/mantou-toolbox-backend/releases/tag/v0.0.6',
    })
    assert.equal(updateInfo.payload.data.versions[1].content, 'Update v0.0.5\n\nPublished application changes.')
    assert.equal(updateInfo.payload.data.versions[1].publishedAt, '2026-09-23T10:05:00Z')
    assert.deepEqual(
      updateInfo.payload.data.rollbackVersions,
      ['v0.0.3', 'v0.0.2', 'v0.0.0']
    )
    assert.equal(updateFetchUrls.length, 3)
    assert.ok(updateFetchUrls.every((url) => url.startsWith('https://gh-proxy.com/https://api.github.com/')))

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

    const runtimeLogSummary = await requestJson(baseUrl, '/api/admin/summary', {
      headers: { Cookie: adminCookie },
    })
    assert.equal(runtimeLogSummary.response.status, 200)
    assert.ok(runtimeLogSummary.payload.data.metrics.errors_24h >= 1)
    assert.ok(runtimeLogSummary.payload.data.systemLogs.some((entry) => entry.level === 'error'
      && entry.path === '/api/download/parse.php'
      && entry.statusCode === 503))
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

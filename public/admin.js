(() => {
  const pageNames = ['novel', 'logs', 'data', 'ads', 'status']
  const loginView = document.getElementById('login-view')
  const dashboardView = document.getElementById('dashboard-view')
  const loginForm = document.getElementById('login-form')
  const loginMessage = document.getElementById('login-message')
  const navItems = [...document.querySelectorAll('.nav-item')]
  const updateDialog = document.getElementById('update-dialog')
  const proxyHosts = {
    github: null,
    edgeone: 'https://edgeone.gh-proxy.com',
    hk: 'https://hk.gh-proxy.com',
    'gh-proxy': 'https://gh-proxy.com',
    'gh-hik': 'https://gh.hik.top',
  }
  const proxyRadios = [...document.querySelectorAll('input[name="github-proxy"]')]
  let updateProxyId = 'gh-proxy'
  try {
    const savedProxy = localStorage.getItem('mantou-admin-update-proxy')
    if (Object.hasOwn(proxyHosts, savedProxy)) updateProxyId = savedProxy
  } catch {}
  for (const radio of proxyRadios) radio.checked = radio.value === updateProxyId
  let updateInfo = null
  let latestData = null
  let updatePollTimer = null
  let activeOperationId = ''
  let lastKnownOperationState = ''

  const settingForms = {
    novel: {
      form: document.getElementById('novel-settings-form'),
      keys: ['qimaoEnabled', 'fanqieEnabled'],
      message: document.getElementById('novel-settings-message'),
      state: document.getElementById('novel-settings-state'),
    },
    ads: {
      form: document.getElementById('ads-settings-form'),
      keys: ['rewardedAdEnabled', 'rewardedAdEveryDownloads', 'cloudDirectLinkEnabled'],
      message: document.getElementById('ads-settings-message'),
      state: document.getElementById('ads-settings-state'),
    },
    status: {
      form: document.getElementById('status-settings-form'),
      keys: ['downloadEnabled', 'parseEnabled', 'downloadLimit'],
      message: document.getElementById('status-settings-message'),
      state: document.getElementById('status-settings-state'),
    },
  }

  function setMessage(element, message, type) {
    element.textContent = message || ''
    element.className = `form-message${type ? ` ${type}` : ''}`
  }

  function showLogin(message) {
    dashboardView.hidden = true
    loginView.hidden = false
    document.getElementById('admin-password').focus()
    if (message) setMessage(loginMessage, message, 'error')
  }

  function showDashboard() {
    loginView.hidden = true
    dashboardView.hidden = false
  }

  function setPage(page, updateHash = true) {
    const selectedPage = pageNames.includes(page) ? page : 'novel'
    for (const name of pageNames) {
      document.getElementById(`page-${name}`).hidden = name !== selectedPage
    }
    for (const item of navItems) {
      const active = item.dataset.page === selectedPage
      item.classList.toggle('is-active', active)
      if (active) item.setAttribute('aria-current', 'page')
      else item.removeAttribute('aria-current')
    }
    if (updateHash && location.hash !== `#${selectedPage}`) {
      history.replaceState(null, '', `#${selectedPage}`)
    }
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      credentials: 'same-origin',
      ...options,
      headers: {
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
        ...(options.headers || {}),
      },
    })
    let payload = {}
    try {
      payload = await response.json()
    } catch {}
    if (response.status === 401 && path !== '/api/admin/login') {
      showLogin('登录状态已过期，请重新登录。')
    }
    if (!response.ok) {
      throw new Error(payload.message || `request_status_${response.status}`)
    }
    return payload.data
  }

  function formatDate(timestamp) {
    if (!timestamp) return '—'
    return new Intl.DateTimeFormat('zh-CN', {
      month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
    }).format(new Date(timestamp))
  }

  function formatReleaseDate(value) {
    const date = new Date(value)
    if (!value || Number.isNaN(date.getTime())) return '—'
    return new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric', month: 'numeric', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).format(date)
  }

  function setText(id, value) {
    document.getElementById(id).textContent = String(value)
  }

  function statusLabel(value) {
    return ({ queued: '排队中', running: '生成中', completed: '已完成', failed: '失败' })[value] || value || '未知'
  }

  function sourceLabel(value) {
    return ({ qimao: '七猫', fanqie: '番茄' })[value] || value || '—'
  }

  function appendCell(row, value, className) {
    const cell = document.createElement('td')
    if (className) cell.className = className
    cell.textContent = String(value ?? '—')
    row.appendChild(cell)
    return cell
  }

  function renderJobs(items) {
    const body = document.getElementById('jobs-body')
    body.replaceChildren()
    if (!items.length) {
      const row = document.createElement('tr')
      appendCell(row, '暂无任务', 'empty-cell').colSpan = 5
      body.appendChild(row)
      return
    }
    for (const item of items) {
      const row = document.createElement('tr')
      appendCell(row, item.title || item.id, 'cell-title')
      appendCell(row, sourceLabel(item.source), 'cell-muted')
      const statusCell = appendCell(row, '')
      const pill = document.createElement('span')
      pill.className = `status-pill${item.status === 'completed' ? ' completed' : ''}${item.status === 'failed' ? ' failed' : ''}`
      pill.textContent = statusLabel(item.status)
      statusCell.appendChild(pill)
      appendCell(row, formatDate(item.createdAt), 'cell-muted')
      appendCell(row, item.error || '—', item.error ? 'cell-error' : 'cell-muted')
      body.appendChild(row)
    }
  }

  function renderSystemLogs(items) {
    const body = document.getElementById('logs-body')
    body.replaceChildren()
    document.getElementById('log-count').textContent = `最近 ${items.length} 条`
    if (!items.length) {
      const row = document.createElement('tr')
      appendCell(row, '暂无运行日志', 'empty-cell').colSpan = 6
      body.appendChild(row)
      return
    }
    for (const item of items) {
      const row = document.createElement('tr')
      const level = appendCell(row, String(item.level || 'info').toUpperCase())
      if (item.level === 'error') level.className = 'level-error'
      if (item.level === 'warn') level.className = 'level-warn'
      appendCell(row, item.source || 'server', 'cell-muted')
      appendCell(row, item.method ? `${item.method} ${item.path || ''}` : (item.path || '—'), 'cell-muted')
      const statusCode = appendCell(row, item.statusCode || '—', 'cell-muted')
      if (item.statusCode >= 500) statusCode.className = 'cell-error'
      else if (item.statusCode >= 400) statusCode.className = 'level-warn'
      appendCell(row, item.message || '—')
      appendCell(row, formatDate(item.createdAt), 'cell-muted')
      body.appendChild(row)
    }
  }

  function renderSettings(settings) {
    for (const { form, keys } of Object.values(settingForms)) {
      for (const key of keys) {
        const input = form.elements[key]
        if (input.type === 'checkbox') input.checked = Boolean(settings[key])
        else input.value = String(settings[key] ?? 0)
      }
    }
  }

  function renderMetrics(metrics) {
    setText('users-count', metrics.users)
    setText('jobs-today', metrics.jobs_today)
    setText('jobs-completed', metrics.jobs_completed_today)
    setText('jobs-active', metrics.jobs_active)
    setText('jobs-failed', metrics.jobs_failed_today)
  }

  async function refreshDashboard() {
    for (const group of Object.values(settingForms)) group.state.textContent = '同步中'
    try {
      latestData = await api('/api/admin/summary')
      showDashboard()
      renderMetrics(latestData.metrics || {})
      renderJobs(latestData.jobs || [])
      renderSystemLogs(latestData.systemLogs || [])
      renderSettings(latestData.settings || {})
      setText('status-updated-at', formatDate(latestData.generatedAt))
      for (const group of Object.values(settingForms)) {
        group.state.textContent = '已同步'
        setMessage(group.message, '', '')
      }
      setText('updated-at', `更新于 ${formatDate(latestData.generatedAt)}`)
    } catch (error) {
      if (error.message === 'admin_login_required') return showLogin()
      for (const group of Object.values(settingForms)) {
        group.state.textContent = '读取失败'
        setMessage(group.message, error.message, 'error')
      }
    }
  }

  async function saveSettings(name) {
    const group = settingForms[name]
    const payload = {}
    for (const key of group.keys) {
      const input = group.form.elements[key]
      payload[key] = input.type === 'checkbox' ? input.checked : Number(input.value)
    }
    const button = group.form.querySelector('button[type="submit"]')
    button.disabled = true
    group.state.textContent = '保存中'
    try {
      await api('/api/admin/settings', { method: 'PATCH', body: JSON.stringify(payload) })
      setMessage(group.message, '设置已保存并立即生效。', 'success')
      await refreshDashboard()
    } catch (error) {
      group.state.textContent = '保存失败'
      setMessage(group.message, error.message, 'error')
    } finally {
      button.disabled = false
    }
  }

  function renderUpdateInfo(info) {
    updateInfo = info
    setText('current-version', info.currentVersion || '未知')
    setText('latest-version', info.latestVersion || '暂无')
    setText('available-version', info.latestVersion || '')
    document.getElementById('update-available').hidden = !info.hasUpdate
    setText('update-status', info.hasUpdate ? '有新版本可用' : '当前已是最新版本')
    setMessage(document.getElementById('update-action-message'), '', '')
    document.getElementById('apply-update-button').disabled = !info.hasUpdate || !info.latestVersion
    renderVersionHistory(info.versions || [])

    const select = document.getElementById('rollback-version')
    select.replaceChildren()
    for (const version of info.rollbackVersions || []) {
      const option = document.createElement('option')
      option.value = version
      option.textContent = version
      select.appendChild(option)
    }
    if (!info.rollbackVersions.length) {
      const option = document.createElement('option')
      option.value = ''
      option.textContent = '暂无可回退版本'
      select.appendChild(option)
    }
    select.disabled = info.rollbackVersions.length === 0
    document.getElementById('apply-rollback-button').disabled = !info.rollbackVersions.length
  }

  function renderVersionHistory(versions, emptyText = '暂无版本记录') {
    const body = document.getElementById('release-versions-body')
    const count = document.getElementById('release-count')
    body.replaceChildren()
    count.textContent = versions.length ? `共 ${versions.length} 个版本` : emptyText
    if (!versions.length) {
      const row = document.createElement('tr')
      appendCell(row, emptyText, 'empty-cell').colSpan = 3
      body.appendChild(row)
      return
    }

    for (const version of versions) {
      const row = document.createElement('tr')
      const versionCell = appendCell(row, '')
      const versionLabel = document.createElement('span')
      versionLabel.className = 'release-version-label'
      const versionName = document.createElement('span')
      versionName.textContent = version.version
      versionLabel.appendChild(versionName)
      if (version.prerelease) {
        const badge = document.createElement('span')
        badge.className = 'release-prerelease'
        badge.textContent = '预发布'
        versionLabel.appendChild(badge)
      }
      versionCell.appendChild(versionLabel)

      appendCell(row, formatReleaseDate(version.publishedAt), 'cell-muted')
      const contentCell = appendCell(row, '')
      const viewButton = document.createElement('button')
      viewButton.className = 'button button-secondary release-view-button'
      viewButton.type = 'button'
      viewButton.textContent = '查看'
      viewButton.addEventListener('click', () => showReleaseNotes(version))
      contentCell.appendChild(viewButton)
      body.appendChild(row)
    }
  }

  function showReleaseNotes(version) {
    setText('release-notes-title', `版本 ${version.version}`)
    setText('release-notes-date', `发布时间：${formatReleaseDate(version.publishedAt)}`)
    setText('release-notes-body', version.content || '暂无更新说明。可打开该版本提交查看代码差异。')
    const link = document.getElementById('release-details-link')
    link.href = version.detailsUrl || `https://github.com/TimShitPig/mantou-toolbox-backend/releases/tag/${encodeURIComponent(version.version)}`
    link.hidden = !version.detailsUrl
    document.getElementById('release-notes-dialog').showModal()
  }

  async function loadUpdateInfo() {
    const requestedProxy = updateProxyId
    updateInfo = null
    setText('update-status', '正在检查版本…')
    setText('current-version', '--')
    setText('latest-version', '--')
    setText('available-version', '')
    document.getElementById('update-available').hidden = true
    setMessage(document.getElementById('update-action-message'), '', '')
    document.getElementById('apply-update-button').disabled = true
    document.getElementById('apply-rollback-button').disabled = true
    renderVersionHistory([], '读取中')
    const select = document.getElementById('rollback-version')
    select.replaceChildren(new Option('读取中…', ''))
    select.disabled = true
    try {
      const info = await api(`/api/admin/updates?proxyId=${encodeURIComponent(requestedProxy)}`)
      if (requestedProxy === updateProxyId) {
        renderUpdateInfo(info)
        await refreshUpdateOperation()
      }
    } catch (error) {
      if (requestedProxy !== updateProxyId) return
      setText('update-status', error.message === 'admin_login_required' ? '登录状态已过期' : '版本检查失败')
      const message = ({
        update_check_failed: '读取 GitHub 版本信息失败，请稍后重试。',
        admin_login_required: '登录状态已过期，请重新登录。',
      })[error.message] || error.message
      renderVersionHistory([], '版本记录读取失败')
      select.replaceChildren(new Option('暂无可回退版本', ''))
      setMessage(document.getElementById('update-action-message'), message, 'error')
    }
  }

  async function openUpdateDialog() {
    updateDialog.showModal()
    updateInfo = null
    setText('current-version', '--')
    setText('latest-version', '--')
    setText('available-version', '')
    document.getElementById('update-available').hidden = true
    setMessage(document.getElementById('update-action-message'), '', '')
    document.getElementById('apply-update-button').disabled = true
    document.getElementById('apply-rollback-button').disabled = true
    const select = document.getElementById('rollback-version')
    select.replaceChildren(new Option('读取中…', ''))
    select.disabled = true
    await loadUpdateInfo()
  }

  function operationMessage(operation) {
    if (operation.state === 'downloading') {
      return operation.progress === null
        ? `${operation.version} 正在下载源码…`
        : `${operation.version} 正在下载源码… ${operation.progress}%`
    }
    return operation.message || ({
      applying: '正在替换运行源码…',
      restarting: '正在重启并检查服务…',
      rolling_back: '新版本未通过检查，正在自动回退…',
      completed: `${operation.version} 更新完成。`,
      rolled_back: `已恢复到 ${operation.fallbackVersion}。`,
      failed: '更新失败，当前版本未改变。',
    })[operation.state] || '正在处理更新…'
  }

  function renderUpdateOperation(operation) {
    const updateButton = document.getElementById('apply-update-button')
    const rollbackButton = document.getElementById('apply-rollback-button')
    const active = operation && ['downloading', 'applying', 'restarting', 'rolling_back'].includes(operation.state)
    lastKnownOperationState = operation.state
    updateButton.disabled = Boolean(active) || !updateInfo || !updateInfo.hasUpdate || !updateInfo.latestVersion
    rollbackButton.disabled = Boolean(active) || !updateInfo || !updateInfo.rollbackVersions.length
    setMessage(
      document.getElementById('update-action-message'),
      operationMessage(operation),
      operation.state === 'failed' ? 'error' : (['completed', 'rolled_back'].includes(operation.state) ? 'success' : '')
    )
    if (active) {
      activeOperationId = operation.operationId
      scheduleUpdatePoll()
    } else if (activeOperationId === operation.operationId) {
      activeOperationId = ''
      if (updatePollTimer) clearTimeout(updatePollTimer)
      updatePollTimer = null
      if (['completed', 'rolled_back', 'failed'].includes(operation.state)) {
        window.setTimeout(() => {
          if (updateDialog.open) loadUpdateInfo()
          refreshDashboard()
        }, 1200)
      }
    }
  }

  async function refreshUpdateOperation() {
    try {
      const result = await api('/api/admin/update-operation')
      if (result.operation) renderUpdateOperation(result.operation)
    } catch (error) {
      if (['downloading', 'applying', 'restarting', 'rolling_back'].includes(lastKnownOperationState)) {
        setMessage(document.getElementById('update-action-message'), '后端正在重启，等待服务恢复…', '')
      } else if (error.message !== 'admin_login_required') {
        setMessage(document.getElementById('update-action-message'), error.message, 'error')
      }
    }
  }

  function scheduleUpdatePoll() {
    if (updatePollTimer) clearTimeout(updatePollTimer)
    updatePollTimer = window.setTimeout(async () => {
      updatePollTimer = null
      await refreshUpdateOperation()
      if (activeOperationId) scheduleUpdatePoll()
    }, 1000)
  }

  async function applyVersion(action, version) {
    if (!updateInfo || !version) return
    const updateButton = document.getElementById('apply-update-button')
    const rollbackButton = document.getElementById('apply-rollback-button')
    updateButton.disabled = true
    rollbackButton.disabled = true
    setMessage(document.getElementById('update-action-message'), action === 'rollback' ? `正在准备回退到 ${version}…` : `正在准备更新到 ${version}…`, '')
    try {
      const operation = await api('/api/admin/update', {
        method: 'POST',
        body: JSON.stringify({ action, version, proxyId: updateProxyId }),
      })
      activeOperationId = operation.operationId
      renderUpdateOperation(operation)
    } catch (error) {
      const message = ({
        self_update_requires_supervisor: '请先运行新版部署准备脚本，再启动服务以启用后台更新。',
        admin_update_version_not_available: '所选版本不再是可更新或可回退版本，请重新检查版本。',
        update_already_in_progress: '已有更新任务正在运行。',
      })[error.message] || error.message
      setMessage(document.getElementById('update-action-message'), message, 'error')
      updateButton.disabled = !updateInfo.hasUpdate
      rollbackButton.disabled = !updateInfo.rollbackVersions.length
    }
  }

  function renderProxyResult(proxyId, state, result = {}) {
    const output = [...document.querySelectorAll('.proxy-result')]
      .find((element) => element.dataset.proxyResult === proxyId)
    if (!output) return
    output.replaceChildren()

    const addBadge = (text, className) => {
      const badge = document.createElement('span')
      badge.className = `proxy-result-badge ${className}`
      badge.textContent = text
      output.appendChild(badge)
    }

    if (state === 'pending') {
      addBadge('测试中', 'proxy-result-pending')
      output.setAttribute('aria-label', '正在测试连通性')
      return
    }

    if (result.connected) {
      addBadge('可用', 'proxy-result-available')
      addBadge(`${result.latencyMs}ms`, 'proxy-result-latency')
      output.setAttribute('aria-label', `可用，延迟 ${result.latencyMs} 毫秒`)
      return
    }

    addBadge('不可用', 'proxy-result-unavailable')
    addBadge(result.statusCode ? `HTTP ${result.statusCode}` : '超时', 'proxy-result-latency')
    output.setAttribute('aria-label', `不可用${result.statusCode ? `，HTTP ${result.statusCode}` : ''}`)
  }

  async function testProxy(proxyId) {
    renderProxyResult(proxyId, 'pending')
    try {
      const result = await api('/api/admin/proxies/test', {
        method: 'POST',
        body: JSON.stringify({ proxyId }),
      })
      renderProxyResult(proxyId, 'complete', result)
    } catch {
      renderProxyResult(proxyId, 'complete', { connected: false })
    }
  }

  async function testAllProxies() {
    const button = document.getElementById('proxy-test-button')
    button.disabled = true
    try {
      await Promise.all(proxyRadios.map((radio) => testProxy(radio.value)))
    } finally {
      button.disabled = false
    }
  }

  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault()
    setMessage(loginMessage, '正在验证…', '')
    const password = String(loginForm.elements.password.value || '')
    try {
      await api('/api/admin/login', {
        method: 'POST',
        body: JSON.stringify({ password }),
      })
      loginForm.reset()
      setPage(location.hash.slice(1), false)
      await refreshDashboard()
    } catch (error) {
      const message = error.message === 'admin_auth_not_configured'
        ? '管理员口令未配置，请检查服务端 ADMIN_PASSWORD。'
        : (error.message === 'admin_password_invalid' ? '口令不正确。' : error.message)
      setMessage(loginMessage, message, 'error')
    }
  })

  for (const [name, group] of Object.entries(settingForms)) {
    group.form.addEventListener('submit', (event) => {
      event.preventDefault()
      saveSettings(name)
    })
  }

  for (const item of navItems) {
    item.addEventListener('click', () => setPage(item.dataset.page))
  }
  window.addEventListener('hashchange', () => setPage(location.hash.slice(1), false))
  for (const radio of proxyRadios) {
    radio.addEventListener('change', () => {
      if (!radio.checked) return
      updateProxyId = radio.value
      try { localStorage.setItem('mantou-admin-update-proxy', updateProxyId) } catch {}
      if (updateDialog.open) loadUpdateInfo()
    })
  }
  document.getElementById('proxy-test-button').addEventListener('click', testAllProxies)
  document.getElementById('update-button').addEventListener('click', openUpdateDialog)
  document.getElementById('update-close-button').addEventListener('click', () => updateDialog.close())
  document.getElementById('release-notes-close').addEventListener('click', () => {
    document.getElementById('release-notes-dialog').close()
  })
  document.getElementById('release-notes-dialog').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close()
  })
  updateDialog.addEventListener('click', (event) => {
    if (event.target === updateDialog) updateDialog.close()
  })
  document.getElementById('apply-update-button').addEventListener('click', () => {
    if (updateInfo && updateInfo.latestVersion && updateInfo.hasUpdate) {
      applyVersion('update', updateInfo.latestVersion)
    }
  })
  document.getElementById('apply-rollback-button').addEventListener('click', () => {
    const version = document.getElementById('rollback-version').value
    if (updateInfo && updateInfo.rollbackVersions.includes(version)) applyVersion('rollback', version)
  })
  document.getElementById('refresh-button').addEventListener('click', refreshDashboard)
  document.getElementById('logout-button').addEventListener('click', async () => {
    try { await api('/api/admin/logout', { method: 'POST' }) } catch {}
    showLogin('已退出登录。')
  })

  setPage(location.hash.slice(1), false)
  refreshDashboard()
})()

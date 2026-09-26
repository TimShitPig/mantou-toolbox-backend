(() => {
  const pageNames = ['novel', 'logs', 'data', 'ads', 'cloud', 'status']
  const loginView = document.getElementById('login-view')
  const dashboardView = document.getElementById('dashboard-view')
  const loginForm = document.getElementById('login-form')
  const loginMessage = document.getElementById('login-message')
  const navItems = [...document.querySelectorAll('.nav-item')]
  const updateDialog = document.getElementById('update-dialog')
  const logLevelFilters = [...document.querySelectorAll('.log-level-filter input')]
  const logAutoScroll = document.getElementById('log-auto-scroll')
  const quarkForm = document.getElementById('quark-settings-form')
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
  let activePage = 'novel'
  let logPollTimer = null
  let logRefreshPending = false
  let logRenderDeferred = false
  let systemLogItems = []

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

  function updateLogPolling() {
    const shouldPoll = !dashboardView.hidden && activePage === 'logs'
    if (!shouldPoll && logPollTimer) {
      clearInterval(logPollTimer)
      logPollTimer = null
    } else if (shouldPoll && !logPollTimer) {
      logPollTimer = window.setInterval(refreshLatestLogs, 2500)
    }
  }

  function showLogin(message) {
    dashboardView.hidden = true
    loginView.hidden = false
    updateLogPolling()
    document.getElementById('admin-password').focus()
    if (message) setMessage(loginMessage, message, 'error')
  }

  function showDashboard() {
    loginView.hidden = true
    dashboardView.hidden = false
    updateLogPolling()
  }

  function setPage(page, updateHash = true) {
    activePage = pageNames.includes(page) ? page : 'novel'
    for (const name of pageNames) {
      document.getElementById(`page-${name}`).hidden = name !== activePage
    }
    for (const item of navItems) {
      const active = item.dataset.page === activePage
      item.classList.toggle('is-active', active)
      if (active) item.setAttribute('aria-current', 'page')
      else item.removeAttribute('aria-current')
    }
    if (updateHash && location.hash !== `#${activePage}`) {
      history.replaceState(null, '', `#${activePage}`)
    }
    updateLogPolling()
    if (!dashboardView.hidden && activePage === 'cloud') refreshQuarkPage()
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
    const scrollTop = body.scrollTop
    systemLogItems = items
    const selection = window.getSelection()
    if (selection && !selection.isCollapsed
      && (body.contains(selection.anchorNode) || body.contains(selection.focusNode))) {
      logRenderDeferred = true
      return
    }
    logRenderDeferred = false
    const selectedLevels = new Set(logLevelFilters.filter((filter) => filter.checked).map((filter) => filter.value))
    const visibleItems = items
      .filter((item) => selectedLevels.has(String(item.level || 'info').toLowerCase()))
      .reverse()
    body.replaceChildren()
    document.getElementById('log-count').textContent = `${visibleItems.length} / ${items.length} 条`
    if (!visibleItems.length) {
      const empty = document.createElement('p')
      empty.className = 'log-empty'
      empty.textContent = items.length ? '当前筛选没有日志' : '暂无运行日志'
      body.appendChild(empty)
      return
    }
    for (const item of visibleItems) {
      const level = String(item.level || 'info').toLowerCase()
      const row = document.createElement('div')
      row.className = `runtime-log-entry log-${level}`

      const time = new Date(Number(item.createdAt) || 0)
      const pad = (value) => String(value).padStart(2, '0')
      const timeCell = document.createElement('span')
      timeCell.className = 'runtime-log-time'
      timeCell.textContent = Number.isNaN(time.getTime())
        ? '--/-- --:--:--'
        : `${pad(time.getMonth() + 1)}/${pad(time.getDate())} ${pad(time.getHours())}:${pad(time.getMinutes())}:${pad(time.getSeconds())}`

      const levelCell = document.createElement('span')
      levelCell.className = 'runtime-log-level'
      levelCell.textContent = level.toUpperCase()

      const sourceCell = document.createElement('span')
      sourceCell.className = 'runtime-log-source'
      sourceCell.textContent = item.source || 'server'

      const messageCell = document.createElement('span')
      messageCell.className = 'runtime-log-message'
      const request = [item.method, item.path].filter(Boolean).join(' ')
      const details = [request, item.statusCode ? String(item.statusCode) : '', item.message || ''].filter(Boolean)
      messageCell.textContent = details.join(' ') || '—'

      row.append(timeCell, levelCell, sourceCell, messageCell)
      body.appendChild(row)
    }
    if (logAutoScroll.checked) body.scrollTop = body.scrollHeight
    else body.scrollTop = scrollTop
  }

  async function refreshLatestLogs() {
    if (logRefreshPending || dashboardView.hidden || activePage !== 'logs') return
    logRefreshPending = true
    try {
      const summary = await api('/api/admin/summary')
      latestData = summary
      if (!dashboardView.hidden && activePage === 'logs') renderSystemLogs(summary.systemLogs || [])
    } catch {} finally {
      logRefreshPending = false
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

  function formatBytes(value) {
    const bytes = Number(value) || 0
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  function renderQuarkFiles(items) {
    const body = document.getElementById('quark-files-body')
    body.replaceChildren()
    const files = Array.isArray(items) ? items : []
    setText('quark-file-count', `${files.length} 条`)
    if (!files.length) {
      const row = document.createElement('tr')
      appendCell(row, '暂无夸克网盘文件', 'empty-cell').colSpan = 5
      body.appendChild(row)
      return
    }
    for (const file of files) {
      const row = document.createElement('tr')
      appendCell(row, file.title || '未命名书籍', 'cell-title')
      appendCell(row, file.fileName || 'novel.txt', 'cell-muted')
      appendCell(row, formatBytes(file.size), 'cell-muted')
      appendCell(row, formatDate(file.updatedAt), 'cell-muted')
      const linkCell = appendCell(row, '')
      let shareUrl = ''
      try {
        const parsed = new URL(String(file.shareUrl || ''))
        if (parsed.protocol === 'https:' && (parsed.hostname === 'pan.quark.cn' || parsed.hostname === 'quark.cn')) {
          shareUrl = parsed.href
        }
      } catch {}
      if (shareUrl) {
        const link = document.createElement('a')
        link.className = 'quark-share-link'
        link.href = shareUrl
        link.target = '_blank'
        link.rel = 'noopener noreferrer'
        link.textContent = shareUrl
        linkCell.append(link)
        const copy = document.createElement('button')
        copy.className = 'button button-quiet quark-copy-button'
        copy.type = 'button'
        copy.textContent = '复制'
        copy.addEventListener('click', async () => {
          try {
            await navigator.clipboard.writeText(shareUrl)
            setMessage(document.getElementById('quark-settings-message'), '分享链接已复制。', 'success')
          } catch {
            setMessage(document.getElementById('quark-settings-message'), '复制失败，请打开分享链接后复制。', 'error')
          }
        })
        linkCell.append(copy)
      } else {
        linkCell.textContent = '链接格式无效'
      }
      body.appendChild(row)
    }
  }

  async function refreshQuarkPage() {
    const state = document.getElementById('quark-settings-state')
    state.textContent = '同步中'
    try {
      const data = await api('/api/admin/quark')
      const settings = data.settings || {}
      quarkForm.elements.enabled.checked = Boolean(settings.enabled)
      quarkForm.elements.folderName.value = String(settings.folderName || '馒头工具箱')
      quarkForm.elements.clearCookie.checked = false
      document.getElementById('quark-cookie-state').textContent = settings.hasCookie ? '已加密保存，测试或更换时输入新值' : '尚未配置'
      state.textContent = settings.enabled
        ? (settings.hasCookie ? '自动保存已启用' : '已启用，需配置 Cookie')
        : '自动保存未启用'
      renderQuarkFiles(data.files || [])
    } catch (error) {
      state.textContent = '读取失败'
      setMessage(document.getElementById('quark-settings-message'), error.message, 'error')
    }
  }

  async function saveQuarkSettings(event) {
    event.preventDefault()
    const button = quarkForm.querySelector('button[type="submit"]')
    button.disabled = true
    document.getElementById('quark-settings-state').textContent = '保存中'
    try {
      await api('/api/admin/quark', {
        method: 'PATCH',
        body: JSON.stringify({
          enabled: quarkForm.elements.enabled.checked,
          folderName: quarkForm.elements.folderName.value,
          cookie: quarkForm.elements.cookie.value,
          clearCookie: quarkForm.elements.clearCookie.checked,
        }),
      })
      quarkForm.elements.cookie.value = ''
      quarkForm.elements.clearCookie.checked = false
      setMessage(document.getElementById('quark-settings-message'), '设置已保存。', 'success')
      await refreshQuarkPage()
    } catch (error) {
      document.getElementById('quark-settings-state').textContent = '保存失败'
      setMessage(document.getElementById('quark-settings-message'), error.message, 'error')
    } finally {
      button.disabled = false
    }
  }

  async function testQuarkConnection() {
    const button = document.getElementById('quark-test-button')
    button.disabled = true
    setMessage(document.getElementById('quark-settings-message'), '正在测试夸克网盘连接…', '')
    try {
      await api('/api/admin/quark/test', {
        method: 'POST',
        body: JSON.stringify({ cookie: quarkForm.elements.cookie.value }),
      })
      setMessage(document.getElementById('quark-settings-message'), '连接成功。', 'success')
    } catch (error) {
      const messages = {
        quark_cookie_invalid: 'Cookie 无效或已失效，请重新复制夸克网盘 Cookie。',
        quark_request_failed: '连接夸克网盘失败，请检查服务器网络。',
        quark_http_401: '登录状态无效，请重新复制夸克网盘 Cookie。',
        quark_api_401: '登录状态无效，请重新复制夸克网盘 Cookie。',
      }
      setMessage(document.getElementById('quark-settings-message'), messages[error.message] || `连接失败：${error.message}`, 'error')
    } finally {
      button.disabled = false
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
      if (activePage === 'cloud') await refreshQuarkPage()
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
    renderUpdateProgress(null)
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

  function formatBytes(value) {
    const bytes = Math.max(0, Number(value) || 0)
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  function renderUpdateProgress(operation) {
    const panel = document.getElementById('update-progress-panel')
    const track = document.getElementById('update-progress-track')
    const fill = document.getElementById('update-progress-fill')
    if (!operation || !['downloading', 'applying', 'restarting', 'rolling_back'].includes(operation.state)) {
      panel.hidden = true
      track.classList.remove('is-indeterminate')
      track.removeAttribute('aria-valuenow')
      fill.style.width = '0%'
      return
    }

    panel.hidden = false
    const stageLabels = {
      downloading: `正在下载 ${operation.version}`,
      applying: operation.action === 'rollback' ? '正在应用回退版本' : '正在替换源码',
      restarting: '正在重建容器并清理旧镜像',
      rolling_back: `启动检查未通过，正在恢复 ${operation.fallbackVersion}`,
    }
    document.getElementById('update-progress-stage').textContent = stageLabels[operation.state]

    const hasDownloadProgress = operation.state === 'downloading' && Number.isFinite(operation.progress)
    if (hasDownloadProgress) {
      const percent = Math.max(0, Math.min(100, Math.floor(operation.progress)))
      track.classList.remove('is-indeterminate')
      track.setAttribute('aria-valuenow', String(percent))
      fill.style.width = `${percent}%`
      document.getElementById('update-progress-value').textContent = `${percent}%`
      document.getElementById('update-progress-detail').textContent = `${formatBytes(operation.downloadedBytes)} / ${formatBytes(operation.totalBytes)}`
      return
    }

    track.classList.add('is-indeterminate')
    track.removeAttribute('aria-valuenow')
    fill.style.width = ''
    document.getElementById('update-progress-value').textContent = operation.state === 'downloading'
      ? `${formatBytes(operation.downloadedBytes)} 已下载`
      : '进行中'
    document.getElementById('update-progress-detail').textContent = ({
      downloading: '正在连接所选镜像并读取源码包',
      applying: '源码包已校验，正在替换运行文件',
      restarting: '正在自动构建镜像、重建容器并等待健康检查',
      rolling_back: '正在恢复备份并启动旧版本',
    })[operation.state]
  }

  function renderUpdateOperation(operation) {
    const updateButton = document.getElementById('apply-update-button')
    const rollbackButton = document.getElementById('apply-rollback-button')
    const active = operation && ['downloading', 'applying', 'restarting', 'rolling_back'].includes(operation.state)
    lastKnownOperationState = operation.state
    renderUpdateProgress(operation)
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
        docker_update_agent_unavailable: '宿主机更新代理未就绪。请运行一次部署升级脚本完成安装，之后可在此处一键更新。',
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

  quarkForm.addEventListener('submit', saveQuarkSettings)
  document.getElementById('quark-test-button').addEventListener('click', testQuarkConnection)

  for (const item of navItems) {
    item.addEventListener('click', () => setPage(item.dataset.page))
  }
  for (const filter of logLevelFilters) {
    filter.addEventListener('change', () => renderSystemLogs(systemLogItems))
  }
  document.addEventListener('selectionchange', () => {
    const selection = window.getSelection()
    if (!logRenderDeferred || (selection && !selection.isCollapsed)) return
    if (!dashboardView.hidden && activePage === 'logs') renderSystemLogs(systemLogItems)
  })
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

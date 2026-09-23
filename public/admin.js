(() => {
  const loginView = document.getElementById('login-view')
  const dashboardView = document.getElementById('dashboard-view')
  const loginForm = document.getElementById('login-form')
  const loginMessage = document.getElementById('login-message')
  const settingsForm = document.getElementById('settings-form')
  const settingsMessage = document.getElementById('settings-message')
  const settingsState = document.getElementById('settings-state')
  const saveButton = document.getElementById('save-settings')

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
      const status = appendCell(row, statusLabel(item.status))
      status.firstChild?.remove()
      const pill = document.createElement('span')
      pill.className = `status-pill${item.status === 'completed' ? ' completed' : ''}${item.status === 'failed' ? ' failed' : ''}`
      pill.textContent = statusLabel(item.status)
      status.appendChild(pill)
      appendCell(row, formatDate(item.createdAt), 'cell-muted')
      appendCell(row, item.error || '—', item.error ? 'cell-error' : 'cell-muted')
      body.appendChild(row)
    }
  }

  function renderLogs(items) {
    const body = document.getElementById('logs-body')
    body.replaceChildren()
    if (!items.length) {
      const row = document.createElement('tr')
      appendCell(row, '暂无日志', 'empty-cell').colSpan = 4
      body.appendChild(row)
      return
    }
    for (const item of items) {
      const row = document.createElement('tr')
      const level = appendCell(row, String(item.level || 'info').toUpperCase())
      if (item.level === 'error') level.className = 'level-error'
      if (item.level === 'warn') level.className = 'level-warn'
      appendCell(row, item.scope || 'client', 'cell-muted')
      appendCell(row, item.message || '—')
      appendCell(row, formatDate(item.createdAt), 'cell-muted')
      body.appendChild(row)
    }
  }

  function renderSettings(settings) {
    for (const key of ['downloadEnabled', 'parseEnabled', 'qimaoEnabled', 'fanqieEnabled', 'rewardedAdEnabled', 'cloudDirectLinkEnabled']) {
      settingsForm.elements[key].checked = Boolean(settings[key])
    }
    settingsForm.elements.downloadLimit.value = String(settings.downloadLimit ?? 0)
    settingsForm.elements.rewardedAdEveryDownloads.value = String(settings.rewardedAdEveryDownloads ?? 3)
  }

  async function refreshDashboard() {
    settingsState.textContent = '更新中'
    try {
      const data = await api('/api/admin/summary')
      showDashboard()
      setText('service-status', '运行中')
      setText('users-count', data.metrics.users)
      setText('jobs-today', data.metrics.jobs_today)
      setText('jobs-active', data.metrics.jobs_active)
      setText('jobs-failed', data.metrics.jobs_failed_today)
      setText('errors-24h', data.metrics.errors_24h)
      setText('updated-at', `更新于 ${formatDate(data.generatedAt)}`)
      renderJobs(data.jobs || [])
      renderLogs(data.logs || [])
      renderSettings(data.settings || {})
      settingsState.textContent = '配置已同步'
      setMessage(settingsMessage, '', '')
    } catch (error) {
      settingsState.textContent = '读取失败'
      if (error.message === 'admin_login_required') return showLogin()
      setMessage(settingsMessage, error.message, 'error')
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
      await refreshDashboard()
    } catch (error) {
      const message = error.message === 'admin_auth_not_configured'
        ? '管理员口令未配置，请检查服务端 ADMIN_PASSWORD。'
        : (error.message === 'admin_password_invalid' ? '口令不正确。' : error.message)
      setMessage(loginMessage, message, 'error')
    }
  })

  settingsForm.addEventListener('submit', async (event) => {
    event.preventDefault()
    saveButton.disabled = true
    settingsState.textContent = '保存中'
    const form = settingsForm.elements
    const payload = {
      downloadEnabled: form.downloadEnabled.checked,
      parseEnabled: form.parseEnabled.checked,
      qimaoEnabled: form.qimaoEnabled.checked,
      fanqieEnabled: form.fanqieEnabled.checked,
      rewardedAdEnabled: form.rewardedAdEnabled.checked,
      cloudDirectLinkEnabled: form.cloudDirectLinkEnabled.checked,
      downloadLimit: Number(form.downloadLimit.value),
      rewardedAdEveryDownloads: Number(form.rewardedAdEveryDownloads.value),
    }
    try {
      await api('/api/admin/settings', { method: 'PATCH', body: JSON.stringify(payload) })
      setMessage(settingsMessage, '配置已保存并立即生效。', 'success')
      await refreshDashboard()
    } catch (error) {
      setMessage(settingsMessage, error.message, 'error')
      settingsState.textContent = '保存失败'
    } finally {
      saveButton.disabled = false
    }
  })

  document.getElementById('refresh-button').addEventListener('click', refreshDashboard)
  document.getElementById('logout-button').addEventListener('click', async () => {
    try { await api('/api/admin/logout', { method: 'POST' }) } catch {}
    showLogin('已退出登录。')
  })

  refreshDashboard()
})()

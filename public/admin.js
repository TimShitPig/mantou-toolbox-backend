(() => {
  const pageNames = ['novel', 'logs', 'data', 'ads', 'status']
  const loginView = document.getElementById('login-view')
  const dashboardView = document.getElementById('dashboard-view')
  const loginForm = document.getElementById('login-form')
  const loginMessage = document.getElementById('login-message')
  const navItems = [...document.querySelectorAll('.nav-item')]
  let latestData = null

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

  function renderLogs(items) {
    const body = document.getElementById('logs-body')
    body.replaceChildren()
    document.getElementById('log-count').textContent = `最近 ${items.length} 条`
    if (!items.length) {
      const row = document.createElement('tr')
      appendCell(row, '暂无日志', 'empty-cell').colSpan = 5
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
      appendCell(row, item.userId || '访客', 'cell-muted')
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
      renderLogs(latestData.logs || [])
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
  document.getElementById('refresh-button').addEventListener('click', refreshDashboard)
  document.getElementById('logout-button').addEventListener('click', async () => {
    try { await api('/api/admin/logout', { method: 'POST' }) } catch {}
    showLogin('已退出登录。')
  })

  setPage(location.hash.slice(1), false)
  refreshDashboard()
})()

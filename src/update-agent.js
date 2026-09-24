const crypto = require('node:crypto')
const { spawn, execFile } = require('node:child_process')
const fs = require('node:fs/promises')
const http = require('node:http')
const path = require('node:path')
const { promisify } = require('node:util')

const execFileAsync = promisify(execFile)
const VERSION_PATTERN = /^v\d+\.\d+\.\d+$/
const PROXY_HOSTS = Object.freeze({
  github: '',
  edgeone: 'https://edgeone.gh-proxy.com',
  hk: 'https://hk.gh-proxy.com',
  'gh-proxy': 'https://gh-proxy.com',
  'gh-hik': 'https://gh.hik.top',
})
const DEFAULT_IMAGE_REPOSITORY = 'ghcr.nju.edu.cn/timshitpig/mantou-toolbox-backend'

function sendJson(res, statusCode, payload) {
  const body = Buffer.from(JSON.stringify(payload))
  res.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
  })
  res.end(body)
}

async function readJson(req) {
  const chunks = []
  let length = 0
  for await (const chunk of req) {
    length += chunk.length
    if (length > 8 * 1024) throw new Error('request_too_large')
    chunks.push(chunk)
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
}

function createUpdateAgent(options = {}) {
  const secret = String(options.secret ?? process.env.UPDATE_AGENT_SECRET ?? '').trim()
  const targetName = String(options.targetName ?? process.env.UPDATE_TARGET_NAME ?? 'mantou-toolbox').trim()
  const mode = String(options.mode ?? process.env.UPDATE_MODE ?? 'run').trim()
  const deployDir = path.resolve(options.deployDir ?? process.env.UPDATE_DEPLOY_DIR ?? '/opt/mantou-toolbox')
  const projectName = String(options.projectName ?? process.env.UPDATE_PROJECT_NAME ?? 'mantou-toolbox').trim()
  const imageRepository = String(options.imageRepository ?? process.env.UPDATE_IMAGE_REPOSITORY ?? DEFAULT_IMAGE_REPOSITORY).trim().replace(/\/+$/, '')
  const stateFile = path.resolve(options.stateFile ?? process.env.UPDATE_AGENT_STATE_FILE ?? '/app/storage/update-agent-state.json')
  const execute = options.execute || executeDeployment
  const runDockerCommand = options.runCommand || runCommand
  const checkHealth = options.waitForHealth || waitForHealth
  let latestOperation = null
  let operationActive = false
  let server = null

  function authorized(req) {
    const supplied = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '')
    if (!secret || supplied.length !== secret.length) return false
    return crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(secret))
  }

  async function persistOperation() {
    await fs.mkdir(path.dirname(stateFile), { recursive: true })
    const temporaryFile = `${stateFile}.${process.pid}.tmp`
    await fs.writeFile(temporaryFile, JSON.stringify(latestOperation), { mode: 0o600 })
    await fs.rename(temporaryFile, stateFile)
  }

  async function updateOperation(patch) {
    latestOperation = { ...latestOperation, ...patch, updatedAt: Date.now() }
    await persistOperation()
  }

  async function waitForHealth() {
    const deadline = Date.now() + (options.healthTimeoutMs || 120_000)
    while (Date.now() < deadline) {
      try {
        const { stdout } = await execFileAsync('docker', [
          'inspect',
          '--format',
          '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}',
          targetName,
        ])
        const state = stdout.trim()
        if (state === 'healthy' || state === 'running') return
        if (state === 'unhealthy' || state === 'exited' || state === 'dead') {
          throw new Error(`updated_service_${state}`)
        }
      } catch (error) {
        if (error.message.startsWith('updated_service_')) throw error
      }
      await new Promise((resolve) => setTimeout(resolve, 1000))
    }
    throw new Error('updated_service_health_timeout')
  }

  async function executeDeployment({ version, proxyId, progress }) {
    const image = `${imageRepository}:${version}`
    if (mode === 'compose') {
      const composeArgs = ['compose', '--project-directory', deployDir, '--project-name', projectName, '-f', path.join(deployDir, 'compose.yaml')]
      const env = { ...process.env, BACKEND_IMAGE: image }
      await runDockerCommand('docker', [...composeArgs, 'pull', 'backend'], { cwd: deployDir, env, progress })
      await progress('replacing')
      await runDockerCommand('docker', [...composeArgs, 'up', '-d', '--no-build', 'backend'], { cwd: deployDir, env, progress })
    } else {
      const proxyHost = PROXY_HOSTS[proxyId]
      const env = {
        ...process.env,
        NAME: targetName,
        IMAGE: image,
        FORCE_UPDATE: 'true',
        GITHUB_PROXY: proxyHost,
        UPDATE_AGENT_INTERNAL: 'true',
      }
      await progress('pulling')
      await runDockerCommand('/bin/sh', ['/app/docker-update.sh'], { cwd: deployDir, env, progress })
    }
    await progress('healthcheck')
    await checkHealth()
  }

  async function runCommand(command, args, { cwd, env, progress }) {
    await new Promise((resolve, reject) => {
      const child = spawn(command, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] })
      let output = ''
      const collect = (chunk) => {
        output = `${output}${chunk}`.slice(-4000)
      }
      child.stdout.on('data', collect)
      child.stderr.on('data', collect)
      child.once('error', reject)
      child.once('close', (code) => {
        if (code === 0) resolve()
        else reject(new Error(`update_command_failed:${code ?? 'signal'}:${output.trim()}`))
      })
    })
    if (progress) await progress('replacing')
  }

  async function runOperation(operation) {
    try {
      await updateOperation({ state: 'pulling', message: `正在更新到 ${operation.version}` })
      await execute({
        action: operation.action,
        version: operation.version,
        fallbackVersion: operation.fallbackVersion,
        proxyId: operation.proxyId,
        progress: async (state) => updateOperation({ state, message: state === 'healthcheck' ? '正在检查服务状态' : '正在替换服务' }),
      })
      const operationLabel = operation.action === 'rollback' ? '回退' : '更新'
      await updateOperation({ state: 'completed', message: `${operation.version} ${operationLabel}完成`, finishedAt: Date.now() })
    } catch (error) {
      const failure = String(error && error.message || 'update_failed').slice(0, 1000)
      if (operation.fallbackVersion && operation.fallbackVersion !== operation.version) {
        try {
          await updateOperation({ state: 'rolling_back', message: `更新失败，正在恢复 ${operation.fallbackVersion}`, error: failure })
          await execute({
            action: 'rollback',
            version: operation.fallbackVersion,
            fallbackVersion: operation.version,
            proxyId: operation.proxyId,
            progress: async (state) => updateOperation({ state, message: '正在恢复原版本' }),
          })
          await updateOperation({ state: 'rolled_back', message: `已恢复到 ${operation.fallbackVersion}`, error: failure, finishedAt: Date.now() })
          return
        } catch (rollbackError) {
          await updateOperation({
            state: 'failed',
            message: '更新失败，自动恢复也未完成',
            error: `${failure}; rollback: ${String(rollbackError && rollbackError.message || 'rollback_failed').slice(0, 1000)}`,
            finishedAt: Date.now(),
          })
          return
        }
      }
      await updateOperation({ state: 'failed', message: '更新失败', error: failure, finishedAt: Date.now() })
    } finally {
      operationActive = false
    }
  }

  async function handle(req, res) {
    const url = new URL(req.url, 'http://update-agent.local')
    if (req.method === 'GET' && url.pathname === '/healthz') {
      return sendJson(res, 200, { data: { status: 'ok' } })
    }
    if (!authorized(req)) return sendJson(res, 401, { error: 'update_agent_unauthorized' })

    if (req.method === 'POST' && url.pathname === '/api/update') {
      let body
      try { body = await readJson(req) } catch { return sendJson(res, 400, { error: 'invalid_request' }) }
      const version = String(body.version || '')
      const action = String(body.action || 'update')
      const proxyId = String(body.proxyId || 'github')
      const fallbackVersion = String(body.fallbackVersion || '')
      if (!['update', 'rollback'].includes(action)
        || !VERSION_PATTERN.test(version)
        || !Object.hasOwn(PROXY_HOSTS, proxyId)) {
        return sendJson(res, 400, { error: 'invalid_update_target' })
      }
      if (fallbackVersion && !VERSION_PATTERN.test(fallbackVersion)) {
        return sendJson(res, 400, { error: 'invalid_fallback_version' })
      }
      if (operationActive) return sendJson(res, 409, { error: 'update_in_progress' })

      operationActive = true
      latestOperation = {
        operationId: crypto.randomUUID(),
        action,
        version,
        fallbackVersion,
        proxyId,
        state: 'queued',
        message: '更新任务已排队',
        createdAt: Date.now(),
        updatedAt: Date.now(),
      }
      await persistOperation()
      const result = { operationId: latestOperation.operationId, state: latestOperation.state }
      setImmediate(() => runOperation({ ...latestOperation }).catch(async () => {
        operationActive = false
        await updateOperation({ state: 'failed', message: '更新任务异常退出', finishedAt: Date.now() })
      }))
      return sendJson(res, 202, { data: result })
    }

    const operationMatch = req.method === 'GET' && url.pathname.match(/^\/api\/operations\/([a-f0-9-]{36})$/)
    if (operationMatch) {
      if (!latestOperation || latestOperation.operationId !== operationMatch[1]) {
        return sendJson(res, 404, { error: 'update_operation_not_found' })
      }
      return sendJson(res, 200, { data: latestOperation })
    }
    return sendJson(res, 404, { error: 'not_found' })
  }

  async function listen(port = Number(process.env.UPDATE_AGENT_PORT || 8790), host = process.env.UPDATE_AGENT_HOST || '0.0.0.0') {
    if (!secret) throw new Error('UPDATE_AGENT_SECRET is required')
    if (!targetName) throw new Error('UPDATE_TARGET_NAME is required')
    try {
      latestOperation = JSON.parse(await fs.readFile(stateFile, 'utf8'))
      if (['queued', 'pulling', 'replacing', 'healthcheck', 'rolling_back'].includes(latestOperation.state)) {
        latestOperation = {
          ...latestOperation,
          state: 'failed',
          message: '更新助手重启，操作已中断',
          finishedAt: Date.now(),
          updatedAt: Date.now(),
        }
        await persistOperation()
      }
    } catch (error) {
      if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error
    }
    server = http.createServer((req, res) => {
      handle(req, res).catch(() => sendJson(res, 500, { error: 'update_agent_internal_error' }))
    })
    await new Promise((resolve, reject) => {
      server.once('error', reject)
      server.listen(port, host, () => {
        server.off('error', reject)
        resolve()
      })
    })
    return server
  }

  async function close() {
    if (server) await new Promise((resolve) => server.close(resolve))
  }

  return { listen, close }
}

module.exports = { createUpdateAgent }

if (require.main === module) {
  const agent = createUpdateAgent()
  agent.listen().then(() => {
    console.log(`Mantou Toolbox update agent listening on port ${process.env.UPDATE_AGENT_PORT || 8790}`)
  }).catch((error) => {
    console.error('Failed to start update agent:', error)
    process.exitCode = 1
  })
  const shutdown = () => agent.close().finally(() => process.exit())
  process.once('SIGINT', shutdown)
  process.once('SIGTERM', shutdown)
}

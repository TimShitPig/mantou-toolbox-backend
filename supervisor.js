const fs = require('node:fs/promises')
const path = require('node:path')
const { spawn } = require('node:child_process')

const ACTIVE_STATES = new Set(['downloading', 'applying', 'restarting', 'rolling_back'])
const SOURCE_ENTRIES = ['Dockerfile', 'package.json', 'server.js', 'supervisor.js', 'src', 'public']

function updateWorkDir(storageDir, operation) {
  if (!/^[a-f0-9-]{36}$/.test(String(operation.operationId || ''))) return null
  return path.join(storageDir, `self-update-${operation.operationId}`)
}

const APP_DIR = path.resolve(process.env.APP_DIR || '/app')
const STORAGE_DIR = path.resolve(process.env.STORAGE_DIR || path.join(APP_DIR, 'storage'))
const OPERATION_FILE = path.join(STORAGE_DIR, 'self-update-operation.json')

function pause(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

function createSupervisor(options = {}) {
  const appDir = path.resolve(options.appDir || APP_DIR)
  const storageDir = path.resolve(options.storageDir || STORAGE_DIR)
  const operationFile = options.operationFile || path.join(storageDir, 'self-update-operation.json')
  const spawnApplication = options.startApplication || (() => {
    console.info(JSON.stringify({ level: 'info', source: 'supervisor', message: 'Starting backend process' }))
    const application = spawn(process.execPath, ['--no-warnings', 'server.js'], {
      cwd: appDir,
      env: { ...process.env, MANTOU_SUPERVISED: 'true' },
      stdio: 'inherit',
    })
    application.once('error', (error) => {
      console.error(JSON.stringify({ level: 'error', source: 'supervisor', message: 'Backend process failed to start', error: error.message }))
    })
    return application
  })
  const pauseFor = options.pause || pause
  let child = null
  let stopping = false

  async function readOperation() {
    try {
      return JSON.parse(await fs.readFile(operationFile, 'utf8'))
    } catch {
      return null
    }
  }

  async function writeOperation(operation) {
    const temporaryFile = `${operationFile}.${process.pid}.tmp`
    await fs.mkdir(path.dirname(operationFile), { recursive: true })
    await fs.writeFile(temporaryFile, JSON.stringify({ ...operation, updatedAt: Date.now() }), { mode: 0o600 })
    await fs.rename(temporaryFile, operationFile)
  }

  async function readSourceVersion() {
    try {
      const packageInfo = JSON.parse(await fs.readFile(path.join(appDir, 'package.json'), 'utf8'))
      return `v${String(packageInfo.version || '')}`
    } catch {
      return ''
    }
  }

  async function waitForHealth(application, timeoutMs = 90000) {
    if (options.waitForHealth) return options.waitForHealth(application, timeoutMs)
    const deadline = Date.now() + timeoutMs
    const port = Number(process.env.PORT) || 8787
    while (Date.now() < deadline && !stopping) {
      if (application.exitCode !== null || application.signalCode !== null) return false
      try {
        const response = await fetch(`http://127.0.0.1:${port}/healthz`, { signal: AbortSignal.timeout(1500) })
        if (response.ok) return true
      } catch {}
      await pauseFor(500)
    }
    return false
  }

  function waitForExit(application) {
    if (options.waitForExit) return options.waitForExit(application)
    if (application.exitCode !== null || application.signalCode !== null) return Promise.resolve()
    return new Promise((resolve) => application.once('exit', resolve))
  }

  async function stopApplication(application) {
    if (options.stopApplication) return options.stopApplication(application)
    if (!application || application.exitCode !== null || application.signalCode !== null) return
    application.kill('SIGTERM')
    await Promise.race([waitForExit(application), pauseFor(3000)])
    if (application.exitCode === null && application.signalCode === null) application.kill('SIGKILL')
    await waitForExit(application)
  }

  async function restoreSource(operation) {
    const workDir = updateWorkDir(storageDir, operation)
    if (!workDir) throw new Error('invalid_update_backup')
    const backupDir = path.join(workDir, 'backup')
    for (const entry of SOURCE_ENTRIES) {
      const source = path.join(backupDir, entry)
      const details = await fs.lstat(source)
      if (details.isSymbolicLink()) throw new Error(`backup_entry_is_symlink:${entry}`)
      const target = path.join(appDir, entry)
      await fs.rm(target, { recursive: true, force: true })
      await fs.cp(source, target, { recursive: true, force: true })
    }
  }

  async function removeUpdateBackup(operation) {
    const workDir = updateWorkDir(storageDir, operation)
    if (workDir) await fs.rm(workDir, { recursive: true, force: true })
  }

  async function beginRollback(operation, message) {
    await writeOperation({ ...operation, state: 'rolling_back', message })
    try {
      await restoreSource(operation)
      console.warn(JSON.stringify({ level: 'warn', source: 'supervisor', message: 'Restored previous backend source' }))
      return true
    } catch (error) {
      await writeOperation({
        ...operation,
        state: 'failed',
        message: '更新失败且无法恢复旧源码',
        error: String(error && error.message || error).slice(0, 1000),
        finishedAt: Date.now(),
      })
      console.error(JSON.stringify({ level: 'error', source: 'supervisor', message: 'Failed to restore previous source', error: String(error && error.message || error) }))
      return false
    }
  }

  async function prepareOperation(operation) {
    if (!operation || !ACTIVE_STATES.has(operation.state)) return
    const sourceVersion = await readSourceVersion()
    if (operation.state === 'downloading') {
      await writeOperation({ ...operation, state: 'failed', message: '服务在下载更新期间重启，源码未修改', finishedAt: Date.now() })
      await removeUpdateBackup(operation)
      return
    }
    if (operation.state === 'applying' || operation.state === 'rolling_back') {
      await beginRollback(operation, '正在恢复旧版本')
      return
    }
    if (operation.state === 'restarting' && sourceVersion !== operation.version) {
      await beginRollback(operation, '新版本源码不完整，正在恢复旧版本')
    }
  }

  async function finishOperation(operation, state, message) {
    await writeOperation({ ...operation, state, message, finishedAt: Date.now() })
    await removeUpdateBackup(operation)
    console.info(JSON.stringify({ level: 'info', source: 'supervisor', message }))
  }

  async function run() {
    while (!stopping) {
      let operation = await readOperation()
      await prepareOperation(operation)
      operation = await readOperation()

      const launchedVersion = await readSourceVersion()
      const application = spawnApplication()
      child = application
      const healthy = await waitForHealth(application)
      if (stopping) break

      operation = await readOperation()
      const currentSourceVersion = await readSourceVersion()
      if (operation && operation.state === 'restarting') {
        if (launchedVersion !== operation.version && currentSourceVersion === operation.version) {
          await waitForExit(application)
          continue
        }
        if (healthy && launchedVersion === operation.version && currentSourceVersion === operation.version) {
          const message = operation.action === 'rollback'
            ? `已回退到 ${operation.version}`
            : `${operation.version} 已更新完成`
          await finishOperation(operation, 'completed', message)
          await waitForExit(application)
          continue
        }
        await stopApplication(application)
        await beginRollback(operation, '新版本启动检查失败，正在恢复旧版本')
        continue
      }

      if (operation && operation.state === 'rolling_back') {
        if (healthy && launchedVersion === operation.fallbackVersion && currentSourceVersion === operation.fallbackVersion) {
          await finishOperation(operation, 'rolled_back', `已恢复到 ${operation.fallbackVersion}`)
          await waitForExit(application)
          continue
        }
        await stopApplication(application)
        await writeOperation({
          ...operation,
          state: 'failed',
          message: '旧版本恢复后未通过健康检查',
          finishedAt: Date.now(),
        })
        continue
      }

      if (healthy) await waitForExit(application)
      else await stopApplication(application)
      if (!stopping) await pauseFor(500)
    }
  }

  async function stop(signal = 'SIGTERM') {
    if (stopping) return
    stopping = true
    console.info(JSON.stringify({ level: 'info', source: 'supervisor', message: `Stopping backend (${signal})` }))
    await stopApplication(child)
  }

  return { run, stop, readOperation, readSourceVersion }
}

if (require.main === module) {
  const supervisor = createSupervisor()
  process.once('SIGINT', () => supervisor.stop('SIGINT').then(() => process.exit(130)))
  process.once('SIGTERM', () => supervisor.stop('SIGTERM').then(() => process.exit(0)))
  supervisor.run().catch((error) => {
    console.error('Backend supervisor failed:', error)
    process.exit(1)
  })
}

module.exports = { createSupervisor }

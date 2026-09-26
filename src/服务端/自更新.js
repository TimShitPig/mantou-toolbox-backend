const crypto = require('node:crypto')
const fs = require('node:fs/promises')
const nativeFs = require('node:fs')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const { Readable, Transform } = require('node:stream')
const { pipeline } = require('node:stream/promises')

const execFileAsync = promisify(execFile)
const MAX_ARCHIVE_BYTES = 150 * 1024 * 1024
const SOURCE_ENTRIES = ['Dockerfile', '宿主机更新代理.sh', 'package.json', 'server.js', 'supervisor.js', 'src', 'public']
const ACTIVE_STATES = new Set(['downloading', 'applying', 'restarting', 'rolling_back'])

function updateWorkDir(storageDir, operation) {
  if (!/^[a-f0-9-]{36}$/.test(String(operation.operationId || ''))) return null
  return path.join(storageDir, `self-update-${operation.operationId}`)
}

function publicOperation(operation) {
  if (!operation) return null
  const totalBytes = Number(operation.totalBytes) || 0
  const downloadedBytes = Number(operation.downloadedBytes) || 0
  return {
    operationId: operation.operationId,
    action: operation.action,
    version: operation.version,
    fallbackVersion: operation.fallbackVersion,
    proxyId: operation.proxyId,
    state: operation.state,
    message: operation.message,
    downloadedBytes,
    totalBytes,
    progress: totalBytes ? Math.min(100, Math.floor(downloadedBytes / totalBytes * 100)) : null,
    startedAt: operation.startedAt,
    updatedAt: operation.updatedAt,
    finishedAt: operation.finishedAt || null,
  }
}

function createSelfUpdater(options = {}) {
  const rootDir = path.resolve(options.rootDir || '/app')
  const storageDir = path.resolve(options.storageDir || path.join(rootDir, 'storage'))
  const updateControlDir = path.resolve(options.updateControlDir || path.join(rootDir, 'update-control'))
  const operationFile = path.join(storageDir, 'self-update-operation.json')
  const updateRequestFile = path.join(updateControlDir, 'request')
  const updateStatusFile = path.join(updateControlDir, 'status')
  const fetchImpl = options.fetchImpl || globalThis.fetch
  const onState = options.onState || (() => {})
  const onRestart = options.onRestart || (() => process.kill(process.pid, 'SIGTERM'))
  let busy = false
  let claimingTerminalLog = false

  async function readOperation() {
    try {
      return JSON.parse(await fs.readFile(operationFile, 'utf8'))
    } catch {
      return null
    }
  }

  async function readHostStatus() {
    try {
      const [operationId = '', state = '', message = '', updatedAt = ''] = (await fs.readFile(updateStatusFile, 'utf8')).split(/\r?\n/)
      return { operationId, state, message, updatedAt: (Number(updatedAt) || 0) * 1000 }
    } catch {
      return null
    }
  }

  async function assertHostUpdaterReady() {
    const status = await readHostStatus()
    if (status
      && ['ready', 'complete', 'failed'].includes(status.state)
      && status.updatedAt
      && Date.now() - status.updatedAt < 15000) return
    const error = new Error('docker_update_agent_unavailable')
    error.status = 503
    throw error
  }

  async function requestHostRebuild(operation) {
    await assertHostUpdaterReady()
    const temporaryFile = `${updateRequestFile}.${process.pid}.tmp`
    await fs.writeFile(temporaryFile, `${operation.operationId}\n${operation.version}\n`, { mode: 0o600 })
    await fs.rename(temporaryFile, updateRequestFile)

    const startedAt = Date.now()
    while (true) {
      const status = await readHostStatus()
      if (status && status.operationId === operation.operationId) {
        operation.message = status.message || operation.message
        if (status.state === 'failed') throw new Error(status.message || 'docker_update_agent_failed')
        if (status.state === 'complete') {
          await onRestart()
          return
        }
        if (status.updatedAt && Date.now() - status.updatedAt > 120000) {
          throw new Error('docker_update_agent_heartbeat_lost')
        }
      } else if (Date.now() - startedAt > 30000) {
        throw new Error('docker_update_agent_request_timeout')
      }
      await new Promise((resolve) => setTimeout(resolve, 1000))
    }
  }

  async function removePendingRequest(operation) {
    try {
      const request = await fs.readFile(updateRequestFile, 'utf8')
      if (request.split(/\r?\n/, 1)[0] === operation.operationId) {
        await fs.rm(updateRequestFile, { force: true })
      }
    } catch {}
  }

  async function getPublicOperation() {
    const operation = await readOperation()
    if (!operation || operation.state !== 'restarting') return publicOperation(operation)
    const status = await readHostStatus()
    if (!status || status.operationId !== operation.operationId || !status.message) return publicOperation(operation)
    return publicOperation({ ...operation, message: status.message })
  }

  async function writeOperation(operation) {
    const updated = { ...operation, updatedAt: Date.now() }
    const temporaryFile = `${operationFile}.${process.pid}.tmp`
    await fs.mkdir(storageDir, { recursive: true })
    await fs.writeFile(temporaryFile, JSON.stringify(updated), { mode: 0o600 })
    await fs.rename(temporaryFile, operationFile)
    return updated
  }

  async function transition(operation, state, fields = {}) {
    const previousState = operation.state
    const updated = await writeOperation({ ...operation, ...fields, state })
    Object.assign(operation, updated)
    if (previousState !== state) await onState(publicOperation(operation))
    return operation
  }

  async function saveProgress(operation, downloadedBytes, totalBytes) {
    const updated = await writeOperation({ ...operation, downloadedBytes, totalBytes })
    Object.assign(operation, updated)
  }

  async function copySource(fromDir, toDir, replace = false) {
    await fs.mkdir(toDir, { recursive: true })
    for (const entry of SOURCE_ENTRIES) {
      const source = path.join(fromDir, entry)
      const details = await fs.lstat(source)
      if (details.isSymbolicLink()) throw new Error(`source_entry_is_symlink:${entry}`)
      if ((entry === 'src' || entry === 'public') ? !details.isDirectory() : !details.isFile()) {
        throw new Error(`source_entry_type_invalid:${entry}`)
      }
      if (details.isDirectory()) await assertPlainTree(source)
      const target = path.join(toDir, entry)
      if (replace) await fs.rm(target, { recursive: true, force: true })
      await fs.cp(source, target, { recursive: true, force: true })
    }
  }

  async function assertPlainTree(directory) {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) throw new Error(`source_entry_is_symlink:${entry.name}`)
      if (entry.isDirectory()) await assertPlainTree(path.join(directory, entry.name))
      else if (!entry.isFile()) throw new Error(`source_entry_type_invalid:${entry.name}`)
    }
  }

  async function downloadArchive(operation, archiveUrl, archivePath) {
    const response = await fetchImpl(archiveUrl, {
      headers: { Accept: 'application/vnd.github+json', 'User-Agent': 'mantou-toolbox-self-updater' },
      signal: AbortSignal.timeout(120000),
    })
    if (!response.ok) throw new Error(`update_download_http_${response.status}`)

    const contentLength = Number(response.headers.get('content-length') || 0)
    if (contentLength > MAX_ARCHIVE_BYTES) throw new Error('update_archive_too_large')
    let downloadedBytes = 0
    let lastSavedBytes = 0
    let progressQueue = Promise.resolve()
    const progressStream = new Transform({
      transform(chunk, encoding, callback) {
        downloadedBytes += chunk.length
        if (downloadedBytes > MAX_ARCHIVE_BYTES) {
          callback(new Error('update_archive_too_large'))
          return
        }
        if (downloadedBytes - lastSavedBytes >= 64 * 1024) {
          lastSavedBytes = downloadedBytes
          progressQueue = progressQueue.then(() => saveProgress(operation, downloadedBytes, contentLength))
        }
        progressQueue.then(() => callback(null, chunk), callback)
      },
    })

    if (response.body) {
      await pipeline(Readable.fromWeb(response.body), progressStream, nativeFs.createWriteStream(archivePath, { mode: 0o600 }))
    } else {
      const buffer = Buffer.from(await response.arrayBuffer())
      if (buffer.length > MAX_ARCHIVE_BYTES) throw new Error('update_archive_too_large')
      downloadedBytes = buffer.length
      await fs.writeFile(archivePath, buffer, { mode: 0o600 })
    }
    await progressQueue
    await saveProgress(operation, downloadedBytes, contentLength || downloadedBytes)
  }

  async function validateArchive(extractedDir, archivePath, requestedVersion) {
    await execFileAsync('tar', ['-xzf', archivePath, '--strip-components=1', '-C', extractedDir], {
      timeout: 120000,
      maxBuffer: 1024 * 1024,
    })
    const packageInfo = JSON.parse(await fs.readFile(path.join(extractedDir, 'package.json'), 'utf8'))
    if (`v${String(packageInfo.version || '')}` !== requestedVersion) throw new Error('update_version_mismatch')
    const stagedDir = path.join(path.dirname(extractedDir), 'staged')
    await copySource(extractedDir, stagedDir)
    return stagedDir
  }

  async function execute(operation, archiveUrl) {
    const workDir = updateWorkDir(storageDir, operation)
    const extractedDir = path.join(workDir, 'extracted')
    const backupDir = path.join(workDir, 'backup')
    let hasBackup = false
    try {
      await fs.mkdir(extractedDir, { recursive: true })
      await downloadArchive(operation, archiveUrl, path.join(workDir, 'source.tar.gz'))
      const stagedDir = await validateArchive(extractedDir, path.join(workDir, 'source.tar.gz'), operation.version)
      await copySource(rootDir, backupDir)
      hasBackup = true
      await transition(operation, 'applying', { message: `正在应用 ${operation.version} 源码` })
      await copySource(stagedDir, rootDir, true)
      await transition(operation, 'restarting', { message: '正在请求宿主机重建容器' })
      await requestHostRebuild(operation)
    } catch (error) {
      await removePendingRequest(operation)
      if (hasBackup) {
        try {
          await copySource(backupDir, rootDir, true)
        } catch (restoreError) {
          await transition(operation, 'failed', {
            message: '更新失败，自动还原源码也失败',
            error: String(restoreError && restoreError.message || restoreError).slice(0, 1000),
            finishedAt: Date.now(),
          })
          return
        }
      }
      await transition(operation, 'failed', {
        message: '更新失败，当前版本保持不变',
        error: String(error && error.message || error).slice(0, 1000),
        finishedAt: Date.now(),
      })
      await fs.rm(workDir, { recursive: true, force: true })
    }
  }

  async function start(input) {
    if (busy) {
      const error = new Error('update_already_in_progress')
      error.status = 409
      throw error
    }
    busy = true
    try {
      await assertHostUpdaterReady()
      const previous = await readOperation()
      if (previous && ACTIVE_STATES.has(previous.state)) {
        const error = new Error('update_already_in_progress')
        error.status = 409
        throw error
      }
      const version = String(input.version || '')
      const fallbackVersion = String(input.currentVersion || '')
      if (!/^v\d+\.\d+\.\d+$/.test(version) || !/^v\d+\.\d+\.\d+$/.test(fallbackVersion)) {
        const error = new Error('admin_update_version_invalid')
        error.status = 400
        throw error
      }
      const operation = {
        operationId: crypto.randomUUID(),
        action: input.action === 'rollback' ? 'rollback' : 'update',
        version,
        fallbackVersion,
        proxyId: String(input.proxyId || ''),
        state: 'downloading',
        message: `正在下载 ${version}`,
        downloadedBytes: 0,
        totalBytes: null,
        startedAt: Date.now(),
      }
      Object.assign(operation, await writeOperation(operation))
      await onState(publicOperation(operation))
      setImmediate(() => {
        execute(operation, input.archiveUrl)
          .finally(() => { busy = false })
          .catch(() => { busy = false })
      })
      return publicOperation(operation)
    } catch (error) {
      busy = false
      throw error
    }
  }

  async function claimTerminalLog() {
    if (claimingTerminalLog) return null
    claimingTerminalLog = true
    try {
      const operation = await readOperation()
      if (!operation || !['completed', 'rolled_back', 'failed'].includes(operation.state) || operation.terminalLogRecordedAt) return null
      operation.terminalLogRecordedAt = Date.now()
      await writeOperation(operation)
      return publicOperation(operation)
    } finally {
      claimingTerminalLog = false
    }
  }

  return {
    getOperation: getPublicOperation,
    claimTerminalLog,
    start,
    readOperation,
    operationFile,
    rootDir,
    storageDir,
  }
}

module.exports = {
  ACTIVE_STATES,
  SOURCE_ENTRIES,
  createSelfUpdater,
  publicOperation,
  updateWorkDir,
}

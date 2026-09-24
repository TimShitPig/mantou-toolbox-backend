const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const { execFile } = require('node:child_process')
const { promisify } = require('node:util')
const test = require('node:test')

const { createSelfUpdater, updateWorkDir } = require('../src/self-updater')
const { createSupervisor } = require('../supervisor')

const execFileAsync = promisify(execFile)

async function createSource(root, version, marker) {
  await fs.mkdir(path.join(root, 'src'), { recursive: true })
  await fs.mkdir(path.join(root, 'public'), { recursive: true })
  await Promise.all([
    fs.writeFile(path.join(root, 'Dockerfile'), `FROM node\n# ${marker}\n`),
    fs.writeFile(path.join(root, 'package.json'), JSON.stringify({ version })),
    fs.writeFile(path.join(root, 'server.js'), `// ${marker}\n`),
    fs.writeFile(path.join(root, 'supervisor.js'), `// ${marker}\n`),
    fs.writeFile(path.join(root, 'src', 'app.js'), `// ${marker}\n`),
    fs.writeFile(path.join(root, 'public', 'admin.html'), `<!-- ${marker} -->\n`),
  ])
}

async function createArchive(sourceDir, archivePath) {
  const stagingDir = `${archivePath}.staging`
  const releaseDir = path.join(stagingDir, 'mantou-toolbox-release')
  await fs.mkdir(stagingDir, { recursive: true })
  await fs.cp(sourceDir, releaseDir, { recursive: true })
  await execFileAsync('tar', ['-czf', archivePath, '-C', stagingDir, 'mantou-toolbox-release'])
}

async function waitForState(updater, state) {
  const deadline = Date.now() + 5000
  while (Date.now() < deadline) {
    const operation = await updater.getOperation()
    if (operation && operation.state === state) return operation
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
  assert.fail(`update did not reach ${state}`)
}

test('self-updater replaces only runtime source, persists progress, and restores on restart failure', async (t) => {
  const temporaryRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'mantou-self-update-'))
  t.after(() => fs.rm(temporaryRoot, { recursive: true, force: true }))
  const appDir = path.join(temporaryRoot, 'app')
  const storageDir = path.join(temporaryRoot, 'storage')
  const releaseDir = path.join(temporaryRoot, 'release')
  const archivePath = path.join(temporaryRoot, 'release.tar.gz')
  await createSource(appDir, '0.0.4', 'old-source')
  await createSource(releaseDir, '0.0.5', 'new-source')
  await fs.writeFile(path.join(releaseDir, 'src', 'new-file.js'), 'new release only')
  await fs.mkdir(storageDir, { recursive: true })
  await fs.writeFile(path.join(storageDir, 'mantou.sqlite'), 'keep database')
  await fs.writeFile(path.join(appDir, 'keep-local-file'), 'preserve untracked source file')
  await createArchive(releaseDir, archivePath)

  const states = []
  const updater = createSelfUpdater({
    rootDir: appDir,
    storageDir,
    fetchImpl: async () => new Response(await fs.readFile(archivePath), {
      headers: { 'content-length': String((await fs.stat(archivePath)).size) },
    }),
    onState: async (operation) => states.push(operation.state),
    onRestart: async () => {
      assert.equal(JSON.parse(await fs.readFile(path.join(appDir, 'package.json'), 'utf8')).version, '0.0.5')
      assert.match(await fs.readFile(path.join(appDir, 'src', 'app.js'), 'utf8'), /new-source/)
      assert.match(await fs.readFile(path.join(appDir, 'public', 'admin.html'), 'utf8'), /new-source/)
      assert.equal(await fs.readFile(path.join(appDir, 'src', 'new-file.js'), 'utf8'), 'new release only')
      throw new Error('simulated restart failure')
    },
  })
  const started = await updater.start({
    action: 'update',
    version: 'v0.0.5',
    currentVersion: 'v0.0.4',
    proxyId: 'gh-proxy',
    archiveUrl: 'https://example.test/release.tar.gz',
  })
  assert.equal(started.state, 'downloading')
  const failed = await waitForState(updater, 'failed')
  assert.equal(failed.message, '更新失败，当前版本保持不变')
  assert.deepEqual(states, ['downloading', 'applying', 'restarting', 'failed'])
  assert.equal(JSON.parse(await fs.readFile(path.join(appDir, 'package.json'), 'utf8')).version, '0.0.4')
  assert.match(await fs.readFile(path.join(appDir, 'src', 'app.js'), 'utf8'), /old-source/)
  assert.match(await fs.readFile(path.join(appDir, 'public', 'admin.html'), 'utf8'), /old-source/)
  assert.equal(await fs.access(path.join(appDir, 'src', 'new-file.js')).then(() => true, () => false), false)
  assert.equal(await fs.readFile(path.join(appDir, 'keep-local-file'), 'utf8'), 'preserve untracked source file')
  assert.equal(await fs.readFile(path.join(storageDir, 'mantou.sqlite'), 'utf8'), 'keep database')
  assert.match(await fs.readFile(path.join(appDir, 'Dockerfile'), 'utf8'), /old-source/)
  assert.match(await fs.readFile(path.join(appDir, 'server.js'), 'utf8'), /old-source/)
  assert.match(await fs.readFile(path.join(appDir, 'supervisor.js'), 'utf8'), /old-source/)
  assert.equal(await fs.access(path.join(storageDir, `self-update-${started.operationId}`)).then(() => true, () => false), false)
})

test('supervisor restores the prior source after the new version fails health checks', async (t) => {
  const temporaryRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'mantou-supervisor-test-'))
  t.after(() => fs.rm(temporaryRoot, { recursive: true, force: true }))
  const appDir = path.join(temporaryRoot, 'app')
  const storageDir = path.join(temporaryRoot, 'storage')
  const operation = {
    operationId: 'd6407823-6cc6-495c-b52f-23d9e5818640',
    action: 'update',
    version: 'v0.0.5',
    fallbackVersion: 'v0.0.4',
    state: 'restarting',
    message: '正在重启并检查 v0.0.5',
  }
  await createSource(appDir, '0.0.5', 'new-source')
  const backupDir = path.join(updateWorkDir(storageDir, operation), 'backup')
  await createSource(backupDir, '0.0.4', 'old-source')
  await fs.mkdir(storageDir, { recursive: true })
  await fs.writeFile(path.join(storageDir, 'self-update-operation.json'), JSON.stringify(operation))

  let supervisor
  let healthChecks = 0
  let activeProcesses = 0
  let maximumProcesses = 0
  const stopApplication = async (application) => {
    if (application.running) {
      application.running = false
      activeProcesses -= 1
    }
    application.exitCode = 1
  }
  supervisor = createSupervisor({
    appDir,
    storageDir,
    startApplication: () => {
      activeProcesses += 1
      maximumProcesses = Math.max(maximumProcesses, activeProcesses)
      return { running: true, exitCode: null, signalCode: null }
    },
    waitForHealth: async () => ++healthChecks > 1,
    stopApplication,
    waitForExit: async (application) => {
      await stopApplication(application)
      await supervisor.stop('test-finished')
    },
    pause: async () => {},
  })

  await supervisor.run()
  const result = await supervisor.readOperation()
  assert.equal(result.state, 'rolled_back')
  assert.equal(result.fallbackVersion, 'v0.0.4')
  assert.equal(JSON.parse(await fs.readFile(path.join(appDir, 'package.json'), 'utf8')).version, '0.0.4')
  assert.match(await fs.readFile(path.join(appDir, 'src', 'app.js'), 'utf8'), /old-source/)
  assert.equal(maximumProcesses, 1)
  assert.equal(activeProcesses, 0)
  assert.equal(await fs.access(updateWorkDir(storageDir, operation)).then(() => true, () => false), false)
})

test('supervisor records a successful user-requested rollback', async (t) => {
  const temporaryRoot = await fs.mkdtemp(path.join(os.tmpdir(), 'mantou-supervisor-rollback-'))
  t.after(() => fs.rm(temporaryRoot, { recursive: true, force: true }))
  const appDir = path.join(temporaryRoot, 'app')
  const storageDir = path.join(temporaryRoot, 'storage')
  const operation = {
    operationId: 'd6407823-6cc6-495c-b52f-23d9e5818640',
    action: 'rollback',
    version: 'v0.0.3',
    fallbackVersion: 'v0.0.4',
    state: 'restarting',
  }
  await createSource(appDir, '0.0.3', 'rollback-source')
  await createSource(path.join(updateWorkDir(storageDir, operation), 'backup'), '0.0.4', 'current-source')
  await fs.mkdir(storageDir, { recursive: true })
  await fs.writeFile(path.join(storageDir, 'self-update-operation.json'), JSON.stringify(operation))

  let supervisor
  const stopApplication = async (application) => {
    application.running = false
    application.exitCode = 1
  }
  supervisor = createSupervisor({
    appDir,
    storageDir,
    startApplication: () => ({ running: true, exitCode: null, signalCode: null }),
    waitForHealth: async () => true,
    stopApplication,
    waitForExit: async (application) => {
      await stopApplication(application)
      await supervisor.stop('test-finished')
    },
  })

  await supervisor.run()
  const result = await supervisor.readOperation()
  assert.equal(result.state, 'completed')
  assert.equal(result.message, '已回退到 v0.0.3')
  assert.equal(JSON.parse(await fs.readFile(path.join(appDir, 'package.json'), 'utf8')).version, '0.0.3')
  assert.equal(await fs.access(updateWorkDir(storageDir, operation)).then(() => true, () => false), false)
})

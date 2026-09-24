const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const os = require('node:os')
const path = require('node:path')
const test = require('node:test')

const { createUpdateAgent } = require('../src/update-agent')

async function withAgent(options, run) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'mantou-update-agent-test-'))
  const agent = createUpdateAgent({
    secret: 'test-update-agent-secret',
    targetName: 'mantou-test',
    deployDir: root,
    stateFile: path.join(root, 'state.json'),
    ...options,
  })
  try {
    const server = await agent.listen(0, '127.0.0.1')
    const baseUrl = `http://127.0.0.1:${server.address().port}`
    await run(baseUrl, agent)
  } finally {
    await agent.close()
    await fs.rm(root, { recursive: true, force: true })
  }
}

async function postUpdate(baseUrl, payload, secret = 'test-update-agent-secret') {
  const response = await fetch(`${baseUrl}/api/update`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${secret}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  })
  return { response, payload: await response.json() }
}

test('update agent authenticates requests and reports successful updates', async () => {
  const calls = []
  await withAgent({
    execute: async (operation) => {
      calls.push(operation)
      await operation.progress('healthcheck')
    },
  }, async (baseUrl) => {
    const unauthorized = await fetch(`${baseUrl}/api/update`, { method: 'POST' })
    assert.equal(unauthorized.status, 401)

    const invalid = await postUpdate(baseUrl, { version: 'latest', proxyId: 'github' })
    assert.equal(invalid.response.status, 400)

    const started = await postUpdate(baseUrl, {
      action: 'update',
      version: 'v0.0.1',
      fallbackVersion: 'v0.0.0',
      proxyId: 'gh-proxy',
    })
    assert.equal(started.response.status, 202)

    let operation
    const deadline = Date.now() + 1000
    while (Date.now() < deadline) {
      const response = await fetch(`${baseUrl}/api/operations/${started.payload.data.operationId}`, {
        headers: { Authorization: 'Bearer test-update-agent-secret' },
      })
      operation = (await response.json()).data
      if (operation.state === 'completed') break
      await new Promise((resolve) => setTimeout(resolve, 10))
    }
    assert.equal(operation.state, 'completed')
    assert.equal(operation.version, 'v0.0.1')
    assert.equal(calls.length, 1)
    assert.equal(calls[0].fallbackVersion, 'v0.0.0')
  })
})

test('update agent automatically restores the fallback version after failure', async () => {
  const executedVersions = []
  await withAgent({
    execute: async ({ version }) => {
      executedVersions.push(version)
      if (version === 'v0.0.2') throw new Error('healthcheck failed')
    },
  }, async (baseUrl) => {
    const started = await postUpdate(baseUrl, {
      action: 'update',
      version: 'v0.0.2',
      fallbackVersion: 'v0.0.1',
      proxyId: 'edgeone',
    })
    assert.equal(started.response.status, 202)

    let operation
    const deadline = Date.now() + 1000
    while (Date.now() < deadline) {
      const response = await fetch(`${baseUrl}/api/operations/${started.payload.data.operationId}`, {
        headers: { Authorization: 'Bearer test-update-agent-secret' },
      })
      operation = (await response.json()).data
      if (['rolled_back', 'failed'].includes(operation.state)) break
      await new Promise((resolve) => setTimeout(resolve, 10))
    }
    assert.equal(operation.state, 'rolled_back')
    assert.equal(operation.version, 'v0.0.2')
    assert.deepEqual(executedVersions, ['v0.0.2', 'v0.0.1'])
  })
})

test('compose updates target only the backend service and preserve the updater', async () => {
  const commands = []
  await withAgent({
    mode: 'compose',
    projectName: 'mantou-toolbox',
    updateDeployDir: '/srv/mantou-toolbox',
    runCommand: async (command, args, options) => commands.push({ command, args, options }),
    waitForHealth: async () => {},
  }, async (baseUrl) => {
    const started = await postUpdate(baseUrl, {
      action: 'update',
      version: 'v0.0.1',
      fallbackVersion: 'v0.0.0',
      proxyId: 'github',
    })
    assert.equal(started.response.status, 202)

    const deadline = Date.now() + 1000
    let operation
    while (Date.now() < deadline) {
      const response = await fetch(`${baseUrl}/api/operations/${started.payload.data.operationId}`, {
        headers: { Authorization: 'Bearer test-update-agent-secret' },
      })
      operation = (await response.json()).data
      if (operation.state === 'completed') break
      await new Promise((resolve) => setTimeout(resolve, 10))
    }
    assert.equal(operation.state, 'completed')
    assert.equal(commands.length, 2)
    assert.deepEqual(commands[0].args.slice(-2), ['pull', 'backend'])
    assert.deepEqual(commands[1].args.slice(-4), ['up', '-d', '--no-build', 'backend'])
    assert.equal(commands[0].options.env.BACKEND_IMAGE, 'ghcr.io/timshitpig/mantou-toolbox-backend:v0.0.1')
    assert.equal(commands[0].args.includes('updater'), false)
  })
})

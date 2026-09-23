const { createApp } = require('./src/app')
const { createConfig } = require('./src/config')

const config = createConfig()
const app = createApp({ config })

app.listen().then((server) => {
  const address = server.address()
  const host = address && typeof address === 'object' ? address.address : config.host
  const port = address && typeof address === 'object' ? address.port : config.port
  console.log(`Mantou Toolbox backend listening on http://${host}:${port}`)
}).catch((error) => {
  console.error('Failed to start backend:', error)
  process.exitCode = 1
})

async function shutdown(signal) {
  try {
    await app.close()
  } finally {
    process.exit(signal === 'SIGINT' ? 130 : 0)
  }
}

process.once('SIGINT', () => shutdown('SIGINT'))
process.once('SIGTERM', () => shutdown('SIGTERM'))

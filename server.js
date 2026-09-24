const { createApp } = require('./src/app')
const { createConfig } = require('./src/config')

const config = createConfig()
const app = createApp({ config })

app.listen().catch((error) => {
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

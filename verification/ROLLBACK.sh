#!/usr/bin/env node
const fs = require('node:fs')
const crypto = require('node:crypto')

const [, , source, target] = process.argv
if (!source || !target) {
  console.error('usage: node ROLLBACK.sh <baseline> <target>')
  process.exit(2)
}

fs.copyFileSync(source, target)
const hash = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex').toUpperCase()
console.log(`RESTORED_SHA256=${hash(target)}`)

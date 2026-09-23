$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot

if (-not (Test-Path -LiteralPath '.env')) {
  Copy-Item -LiteralPath '.env.docker.example' -Destination '.env'
  Write-Host 'Created .env from .env.docker.example. Review APP_SECRET before production use.'
}

if (Select-String -Path '.env' -Pattern '^\s*BACKEND_IMAGE\s*=' -Quiet) {
  docker compose pull
  docker compose up -d --no-build --remove-orphans
} else {
  docker compose up -d --build --remove-orphans
}
docker compose ps
Write-Host 'Backend URL: http://127.0.0.1:8787'

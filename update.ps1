$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot

git pull --ff-only

if (Select-String -Path '.env' -Pattern '^\s*BACKEND_IMAGE\s*=' -Quiet) {
  docker compose pull
  docker compose up -d --no-build --remove-orphans
} else {
  docker compose up -d --build --remove-orphans
}

docker compose ps
Write-Host 'Backend updated.'

$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot
$env:UPDATE_DEPLOY_DIR = $PSScriptRoot

$package = Get-Content -Raw -LiteralPath 'package.json' | ConvertFrom-Json
$env:APP_BUILD_VERSION = "v$($package.version)"

$gitCommand = Get-Command git -ErrorAction SilentlyContinue
if ($gitCommand) {
  $gitPath = $gitCommand.Source
  $gitStatus = & $gitPath status --porcelain 2>$null
  if ($LASTEXITCODE -eq 0 -and -not $gitStatus) {
    $buildRevision = & $gitPath rev-parse HEAD 2>$null
    if ($LASTEXITCODE -eq 0 -and $buildRevision) {
      $env:APP_BUILD_REVISION = $buildRevision.Trim()
    }
  }
}

if (-not (Test-Path -LiteralPath '.env')) {
  Copy-Item -LiteralPath '.env.docker.example' -Destination '.env'
  Write-Host 'Created .env from .env.docker.example. Review APP_SECRET before production use.'
}

$updateSecretLine = Select-String -Path '.env' -Pattern '^UPDATE_AGENT_SECRET=(.*)$' | Select-Object -Last 1
if (-not $updateSecretLine -or -not $updateSecretLine.Matches[0].Groups[1].Value) {
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  $secretBytes = New-Object byte[] 32
  $rng.GetBytes($secretBytes)
  $rng.Dispose()
  $updateSecret = ($secretBytes | ForEach-Object { $_.ToString('x2') }) -join ''
  if ($updateSecretLine) {
    $envText = Get-Content -Raw -LiteralPath '.env'
    $envText = [regex]::Replace($envText, '(?m)^UPDATE_AGENT_SECRET=.*$', "UPDATE_AGENT_SECRET=$updateSecret")
    Set-Content -LiteralPath '.env' -Value $envText -NoNewline
  } else {
    Add-Content -LiteralPath '.env' -Value "`nUPDATE_AGENT_SECRET=$updateSecret"
  }
}

if (Select-String -Path '.env' -Pattern '^\s*BACKEND_IMAGE\s*=' -Quiet) {
  docker compose pull
  docker compose up -d --no-build --remove-orphans
} else {
  docker compose up -d --build --remove-orphans
}
docker compose ps
Write-Host 'Backend URL: http://127.0.0.1:8787'

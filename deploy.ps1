$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot
$env:UPDATE_DEPLOY_DIR = $PSScriptRoot

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

docker compose pull
docker compose up -d --no-build --remove-orphans
docker compose ps
Write-Host 'Backend URL: http://127.0.0.1:8787'

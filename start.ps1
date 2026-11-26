param([int]$Port = 5173, [int]$ApiPort = 8421)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE) { throw 'Python 3.12 环境创建失败' }
    & .venv/Scripts/python.exe -m pip install -r backend/requirements.txt
    if ($LASTEXITCODE) { throw '后端依赖安装失败' }
}
if (-not (Test-Path -LiteralPath 'frontend/node_modules/vite/package.json')) {
    npm --prefix frontend ci
    if ($LASTEXITCODE) { throw '前端依赖安装失败' }
}

$env:PYTHONPATH = 'backend'
$env:API_PROXY = "http://127.0.0.1:$ApiPort"
$env:SECURE_COOKIE = 'false'
if (-not $env:DATABASE_URL) {
    $env:DATABASE_URL = 'sqlite:///./.local/pilot.db'
}

& .venv/Scripts/python.exe -c 'from maintenance.db import init_db; init_db()'
if ($LASTEXITCODE) { throw '数据库初始化失败' }

$env:API_INIT = 'false'
$env:WORKER_INIT = 'false'
$pythonPath = (Resolve-Path -LiteralPath '.venv/Scripts/python.exe').Path
$services = @()
try {
    $apiProcess = Start-Process -FilePath $pythonPath -ArgumentList '-m','uvicorn','maintenance.main:app','--host','127.0.0.1','--port',"$ApiPort" -WindowStyle Hidden -PassThru
    $services += $apiProcess
    $deadline = (Get-Date).AddSeconds(30)
    $apiReady = $false
    do {
        if ($apiProcess.HasExited) { throw '后端在启动时退出，请检查端口和配置' }
        try {
            $health = Invoke-RestMethod -Uri "$env:API_PROXY/api/v1/health" -TimeoutSec 2
            $apiReady = $health.status -eq 'ok'
        } catch {
            Start-Sleep -Milliseconds 300
        }
    } until ($apiReady -or (Get-Date) -ge $deadline)
    if (-not $apiReady) { throw '后端健康检查超时' }

    $services += Start-Process -FilePath $pythonPath -ArgumentList '-m','maintenance.worker' -WindowStyle Hidden -PassThru
    npm --prefix frontend run dev -- --port $Port --strictPort
} finally {
    $services | Where-Object { -not $_.HasExited } | Stop-Process
}

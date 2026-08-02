# mcp-hub 开机自启脚本（由计划任务在用户登录时调用）
# 启动两个进程：
#   - hub MCP 服务：  http://127.0.0.1:8765/sse
#   - dashboard：     http://127.0.0.1:8766
# 已在跑就跳过，避免重复启动。

$ErrorActionPreference = 'SilentlyContinue'

$wd   = 'C:\Users\Lenovo\.minimax\agents\mavis\workspace\mcp-hub'
$logs = Join-Path $wd 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-PortInUse([int]$port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', $port)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-PortInUse 8765)) {
    Start-Process `
        -FilePath 'python' `
        -ArgumentList '-m','mcp_hub','--transport','sse','--port','8765' `
        -WorkingDirectory $wd `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'hub.out.log') `
        -RedirectStandardError  (Join-Path $logs 'hub.err.log')
}

if (-not (Test-PortInUse 8766)) {
    Start-Process `
        -FilePath 'python' `
        -ArgumentList '-m','mcp_hub.dashboard','--port','8766' `
        -WorkingDirectory $wd `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'dashboard.out.log') `
        -RedirectStandardError  (Join-Path $logs 'dashboard.err.log')
}

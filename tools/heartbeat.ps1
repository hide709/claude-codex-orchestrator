<#
  heartbeat.ps1 — queue/<engine>.alive を一定間隔で更新し続ける(worker 生存信号)。

  **LLM のターン速度に依存させない**ため、heartbeat は worker セッション本体ではなく
  この独立プロセスが書く(start-worker.ps1 が背景ジョブとして起動する)。
  単独でも使える:  .\tools\heartbeat.ps1 codex
  Ctrl+C で停止。
#>
param([Parameter(Mandatory)][string]$Engine, [int]$IntervalSec = 10)
$repo = Split-Path $PSScriptRoot -Parent
$alive = Join-Path $repo "queue\$Engine.alive"
New-Item -ItemType Directory -Force (Split-Path $alive) | Out-Null
while ($true) {
    Set-Content -Path $alive -Value ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) -NoNewline
    Start-Sleep -Seconds $IntervalSec
}

<#
  wait-for-task.ps1 — 次の未処理タスクが来るまでブロックして待ち、来たらそのパスを1行で出力。

  worker(常駐セッション)がこれをループで実行することで、待機中もターンを無駄にせず
  「新しいタスクが来たら即処理」を実現する。最大待機を過ぎたら 'NO_TASK' を出して戻る
  (worker はまた呼べばよい)。
  使い方:  pwsh -NoProfile -File tools/wait-for-task.ps1 <engine> [maxWaitSec]
#>
param([Parameter(Mandatory)][string]$Engine, [int]$MaxWaitSec = 50)
$repo = Split-Path $PSScriptRoot -Parent
$inbox = Join-Path $repo "queue\$Engine\inbox"
$reports = Join-Path $repo "queue\$Engine\reports"
$deadline = (Get-Date).AddSeconds($MaxWaitSec)
while ((Get-Date) -lt $deadline) {
    if (Test-Path $inbox) {
        $pending = Get-ChildItem $inbox -Filter *.json -File -ErrorAction SilentlyContinue |
            Where-Object { -not (Test-Path (Join-Path $reports $_.Name)) } |
            Sort-Object Name | Select-Object -First 1
        if ($pending) { Write-Output $pending.FullName; return }
    }
    Start-Sleep -Milliseconds 1500
}
Write-Output "NO_TASK"

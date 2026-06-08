<#
  start-claude-worker.ps1 — Claude queue worker を起動(Windows / PowerShell)

  対話セッションの Claude Code を使う(subscription 内)。**claude -p(別料金)は使わない。**
  使い方: 新しい PowerShell ウィンドウで、リポジトリ直下から:
      .\tools\start-claude-worker.ps1
  Ctrl+C で停止(止めれば orchestrator は次の run から自動で codex のみに切替)。
#>
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Error "claude が PATH に見つかりません。claude を入れた後に開いた『新しい』ターミナルで実行してください。"
    exit 1
}

New-Item -ItemType Directory -Force "queue\inbox", "queue\reports" | Out-Null

$instr = @"
あなたはこのリポジトリの queue worker です。claude-worker/INSTRUCTIONS.md の手順に従って常駐してください。
次のループを続けます(説明や雑談は最小限に):
1. queue/claude.alive に現在時刻を Write して heartbeat を更新する。
2. queue/inbox/*.json のうち、対応する queue/reports/<同名>.json が無いものを探す。
3. 各タスク {label, kind, schema, prompt} について、prompt に従い schema に厳密準拠した JSON だけを
   queue/reports/<同名>.json に Write する(説明文・コードフェンス禁止)。
4. 新しいタスクが来るまで 1 に戻って待機する。
まず queue/claude.alive を更新し、『queue を見張っています』と一言だけ言ってから開始してください。
"@

Write-Host "[worker] Claude を対話モードで起動します(claude -p は使いません)。Ctrl+C で停止。" -ForegroundColor Cyan
claude --permission-mode acceptEdits $instr

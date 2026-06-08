<#
  start-worker.ps1 — 常駐 queue worker(codex / claude)を起動(Windows / PowerShell)

  headless(codex exec / claude -p)は使わない。対話セッションを常駐させ、
  queue/<engine>/inbox のタスクを処理して queue/<engine>/reports に返す(program_creater / Shogun 方式)。

  使い方(新しい PowerShell ウィンドウで、リポジトリ直下から):
      .\tools\start-worker.ps1 codex
      .\tools\start-worker.ps1 claude
  Ctrl+C で停止(止めれば orchestrator は次の run でその engine を自動的に外す)。
#>
param([Parameter(Mandatory)][ValidateSet("codex", "claude")][string]$Engine)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
Set-Location $repo

if (-not (Get-Command $Engine -ErrorAction SilentlyContinue)) {
    Write-Error "$Engine が PATH に見つかりません。$Engine を入れた後に開いた『新しい』ターミナルで実行してください。"
    exit 1
}

New-Item -ItemType Directory -Force "queue\$Engine\inbox", "queue\$Engine\reports" | Out-Null

$instr = @"
あなたはこのリポジトリの '$Engine' queue worker です。worker/INSTRUCTIONS.md の手順に従って常駐してください。
次のループを続けます(説明や雑談は最小限に):
1. queue/$Engine.alive に現在時刻を Write して heartbeat を更新する。
2. queue/$Engine/inbox/*.json のうち、対応する queue/$Engine/reports/<同名>.json が無いものを探す。
3. 各タスク {label, kind, schema, prompt} について、prompt に従い schema に厳密準拠した JSON だけを
   まず queue/$Engine/reports/<同名>.json.tmp に Write し、完成後に queue/$Engine/reports/<同名>.json へ Rename/Move する(説明文・コードフェンス禁止)。
4. 新しいタスクが来るまで 1 に戻って待機する。
まず queue/$Engine.alive を更新し、『$Engine queue を見張っています』と一言だけ言ってから開始してください。
"@

Write-Host "[worker:$Engine] 対話モードで起動します(headless は使いません)。Ctrl+C で停止。" -ForegroundColor Cyan
if ($Engine -eq "claude") {
    claude --permission-mode acceptEdits $instr
}
else {
    # codex 対話セッション: workspace 書込可 + 承認なし(低フリクション)。service_tier は config.toml の不正値を回避。
    codex -c service_tier=flex -s workspace-write -a never $instr
}

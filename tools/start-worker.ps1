<#
  start-worker.ps1 — 常駐 queue worker(codex / claude)を起動(Windows / PowerShell)

  headless(codex exec / claude -p)は使わない。対話セッションを常駐させ、
  queue/<engine>/inbox のタスクを処理して queue/<engine>/reports に返す(program_creater / Shogun 方式)。

  heartbeat(queue/<engine>.alive)は **worker 自身のループ(wait-for-task.ps1)が更新する**。
  起動直後だけここで一度 bootstrap し、以降は worker がループを回す限り更新され続ける
  （= heartbeat の新しさ = worker が実際に処理できている、という意味のある信号）。

  使い方(新しい PowerShell ウィンドウで、リポジトリ直下から):
      .\tools\start-worker.ps1 codex
      .\tools\start-worker.ps1 claude
  Ctrl+C で停止(heartbeat も止まり、orchestrator は次の run でその engine を自動的に外す)。
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
$alive = Join-Path $repo "queue\$Engine.alive"
# 起動 bootstrap(orchestrator が起動直後に生存判定できるよう一度だけ書く。以降は wait-for-task が更新)
Set-Content -Path $alive -Value ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) -NoNewline

$instr = @"
あなたはこのリポジトリの '$Engine' queue worker です。worker/INSTRUCTIONS.md に従い、止まらず常駐してください。
次のループをひたすら繰り返す(説明・雑談はしない。ループを止めると heartbeat も止まり engine が外れます):
1. シェルで `pwsh -NoProfile -File tools/wait-for-task.ps1 $Engine` を実行する(次タスクを待つ。heartbeat も更新される)。
2. 出力が 'NO_TASK' なら 1 に戻る。ファイルパスが出たら、その JSON {label, kind, schema, prompt} を読む。
3. prompt に従い schema に厳密準拠した JSON だけを作り、まず queue/$Engine/reports/<labelと同名>.json.tmp に書いてから
   queue/$Engine/reports/<labelと同名>.json へ rename/move する(説明文・コードフェンス禁止)。
4. 1 に戻る。新しいタスクが来る限り捌き続ける。
まず『$Engine queue を見張っています』と一言だけ言って開始してください。
"@

Write-Host "[worker:$Engine] 対話セッションを起動します(heartbeat は wait-for-task ループが更新)。Ctrl+C で停止。" -ForegroundColor Cyan
try {
    if ($Engine -eq "claude") {
        claude --permission-mode acceptEdits $instr
    }
    else {
        # codex 対話セッション: workspace 書込可 + 承認なし(低フリクション)。service_tier は config.toml の不正値を回避。
        codex -c service_tier=flex -s workspace-write -a never $instr
    }
}
finally {
    Remove-Item $alive -ErrorAction SilentlyContinue   # 終了時に engine を解放
    Write-Host "[worker:$Engine] 停止(heartbeat 削除)。" -ForegroundColor Yellow
}

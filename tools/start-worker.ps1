<#
  start-worker.ps1 — 常駐 queue worker(codex / claude)を起動(Windows / PowerShell)

  headless(codex exec / claude -p)は使わない。対話セッションを常駐させ、
  queue/<engine>/inbox のタスクを処理して queue/<engine>/reports に返す(program_creater / Shogun 方式)。

  heartbeat(queue/<engine>.alive)は **LLM ではなく背景ジョブが書き続ける**(LLM のターン速度に依存させない)。
  worker セッションは「タスクを待って捌く」だけに専念する。

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

# heartbeat を背景ジョブで維持(LLM が長考中でも止まらない)
$hbScript = Join-Path $PSScriptRoot "heartbeat.ps1"
$hb = Start-Job -ScriptBlock { param($s, $e) & $s $e } -ArgumentList $hbScript, $Engine
Write-Host "[worker:$Engine] heartbeat 背景ジョブ開始(job $($hb.Id))。対話セッションを起動します。Ctrl+C で停止。" -ForegroundColor Cyan

$instr = @"
あなたはこのリポジトリの '$Engine' queue worker です。worker/INSTRUCTIONS.md に従い、止まらず常駐してください。
heartbeat は別プロセスが書くので、あなたは触らなくてよい。次のループをひたすら繰り返す(説明・雑談はしない):
1. シェルで `pwsh -NoProfile -File tools/wait-for-task.ps1 $Engine` を実行する(次タスクが来るまでブロックする)。
2. 出力が 'NO_TASK' なら 1 に戻る。ファイルパスが出たら、その JSON {label, kind, schema, prompt} を読む。
3. prompt に従い schema に厳密準拠した JSON だけを作り、まず queue/$Engine/reports/<labelと同名>.json.tmp に書いてから
   queue/$Engine/reports/<labelと同名>.json へ rename/move する(説明文・コードフェンス禁止)。
4. 1 に戻る。新しいタスクが来る限り捌き続ける。
まず『$Engine queue を見張っています』と一言だけ言って開始してください。
"@

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
    Stop-Job $hb -ErrorAction SilentlyContinue
    Remove-Job $hb -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $repo "queue\$Engine.alive") -ErrorAction SilentlyContinue
    Write-Host "[worker:$Engine] heartbeat 停止(engine を解放)。" -ForegroundColor Yellow
}

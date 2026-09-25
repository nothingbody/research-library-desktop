param([switch]$InstallDependencies)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskPython = Join-Path $taskRoot '.venv-client\Scripts\python.exe'
$taskNpm = (Get-Command npm.cmd -ErrorAction Stop).Source
function Check-Exit { if ($LASTEXITCODE -ne 0) { throw "构建命令退出码：$LASTEXITCODE" } }
if ($InstallDependencies) {
    if (-not (Test-Path -LiteralPath $taskPython)) {
        & python -m venv .venv-client
        Check-Exit
    }
    & $taskPython -m pip install -r requirements-client.txt -r requirements-async.txt
    Check-Exit
    Push-Location (Join-Path $taskRoot 'desktop')
    try { & $taskNpm ci; Check-Exit } finally { Pop-Location }
}
if (-not (Test-Path -LiteralPath $taskPython)) { throw '先使用 -InstallDependencies 初始化构建环境（需要 Python 3.12 和 Node）。' }
& $taskPython -m PyInstaller --noconfirm --name research-backend --onedir --console --distpath dist/backend --workpath build/backend --collect-all bibtexparser --collect-all pypdf --collect-all aiohttp --hidden-import async_runtime backend_entry.py
Check-Exit
Push-Location (Join-Path $taskRoot 'desktop')
try {
    & $taskNpm run build
    Check-Exit
    & $taskNpm run package
    Check-Exit
} finally { Pop-Location }
Write-Output '安装包与便携版位于 dist\desktop。'

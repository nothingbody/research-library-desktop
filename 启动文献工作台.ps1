$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskExecutable = Join-Path $taskRoot 'app\ResearchLibrary.exe'
if (-not (Test-Path -LiteralPath $taskExecutable)) { $taskExecutable = Join-Path $taskRoot 'dist\desktop\win-unpacked\ResearchLibrary.exe' }
if (-not (Test-Path -LiteralPath $taskExecutable)) { throw '客户端尚未构建，请先运行 build-desktop.ps1。' }
$env:RESEARCH_LIBRARY = Join-Path $taskRoot 'library'
$env:RESEARCH_JOURNALS = Join-Path $taskRoot 'data\scholay'
# The user is launching the interactive desktop application.
Start-Process -FilePath $taskExecutable -WorkingDirectory $taskRoot

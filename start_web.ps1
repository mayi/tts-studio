# TTS Studio - PowerShell 启动脚本
$Host.UI.RawUI.WindowTitle = "TTS Studio - 现代化文章转语音工作台"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "  🎙️ 正在启动 TTS Studio Web 智能工作台..." -ForegroundColor Green
Write-Host "  访问地址: http://127.0.0.1:8000" -ForegroundColor Yellow
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

$VenvPython = Join-Path $ScriptDir "venv\Scripts\python.exe"

if (Test-Path $VenvPython) {
    Write-Host "检测到虚拟环境: venv" -ForegroundColor DarkGray
    & $VenvPython web_app.py
} else {
    Write-Host "未检测到 venv，使用系统 Python 启动..." -ForegroundColor Yellow
    python web_app.py
}


<#
  SVN Manager — Windows 启动器
  单窗口逻辑：已有窗口只激活并刷新，不重复打开。
  用法：双击同目录的 "SVN Manager.bat"
#>
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms

$Port   = 8765
$Url    = "http://127.0.0.1:$Port"
$Root   = Split-Path -Parent $PSScriptRoot
$PyFile = Join-Path $Root 'svnmanager.py'

function Test-Service {
    try {
        $r = Invoke-WebRequest -Uri "$Url/api/projects" -TimeoutSec 2 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Find-SvnWindow {
    @(Get-Process chrome -ErrorAction SilentlyContinue |
      Where-Object { $_.MainWindowTitle -eq 'SVN Manager' })
}

function Find-Browser {
    $c = @()
    if ($env:ProgramFiles)        { $c += "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" }
    if (${env:ProgramFiles(x86)}) { $c += "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe" }
    if ($env:LOCALAPPDATA)        { $c += "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe" }
    if ($env:ProgramFiles)        { $c += "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
    if (${env:ProgramFiles(x86)}) { $c += "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe" }
    foreach ($p in $c) { if (Test-Path $p) { return $p } }
    return $null
}

function Show-Error($msg) {
    [System.Windows.Forms.MessageBox]::Show($msg, 'SVN Manager',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Warning) | Out-Null
}

function Start-Backend {
    $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    $py  = (Get-Command python.exe  -ErrorAction SilentlyContinue).Source
    if (-not $pyw -and -not $py) {
        Show-Error("找不到 Python 3。`n`n请先安装 Python 3.8 或更高版本，`n安装时务必勾选 ""Add Python to PATH""。`n`n下载：https://www.python.org/downloads/windows/")
        return $false
    }
    $exe = if ($pyw) { $pyw } else { $py }
    Start-Process -FilePath $exe `
        -ArgumentList @("`"$PyFile`"", '--port', "$Port", '--no-browser') `
        -WindowStyle Hidden
    for ($i = 0; $i -lt 80; $i++) {
        Start-Sleep -Milliseconds 300
        if (Test-Service) { return $true }
    }
    Show-Error("服务启动超时。`n`n请确认已安装 Subversion 命令行工具：`n· TortoiseSVN 安装时勾选 ""command line client tools""，或`n· 单独安装 SlikSVN`n`n日志：$env:TEMP\svnmanager.log")
    return $false
}

function Open-Window {
    $b = Find-Browser
    if ($b) {
        Start-Process -FilePath $b -ArgumentList @("--app=$Url", '--window-size=1380,880')
    } else {
        Start-Process $Url
    }
}

# ── 已有窗口：激活 + 刷新，直接退出 ──
$win = Find-SvnWindow | Select-Object -First 1
if ($win) {
    $shell = New-Object -ComObject WScript.Shell
    if ($shell.AppActivate($win.Id)) {
        Start-Sleep -Milliseconds 250
        $shell.SendKeys('{F5}')
    }
    exit 0
}

# ── 没有窗口：先保证服务在跑，再开窗口 ──
if (-not (Test-Service)) {
    if (-not (Start-Backend)) { exit 1 }
}

Open-Window
exit 0

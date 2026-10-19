<#
  SVN Manager — Windows 一键安装
  检查依赖，并在桌面创建带图标的快捷方式。
  用法：右键本文件 → "使用 PowerShell 运行"
#>
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms

$Root    = Split-Path -Parent $PSScriptRoot
$Bat     = Join-Path $PSScriptRoot 'SVN Manager.bat'
$Ico     = Join-Path $Root 'assets\AppIcon.ico'
$Desktop = [Environment]::GetFolderPath('Desktop')
$Lnk     = Join-Path $Desktop 'SVN Manager.lnk'

function Show-Msg($msg, $icon = 'Information') {
    [System.Windows.Forms.MessageBox]::Show($msg, 'SVN Manager 安装',
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::$icon) | Out-Null
}

# 1) Python
$py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $py) {
    Show-Msg("没找到 Python 3。`n`n请先安装：https://www.python.org/downloads/windows/`n安装时务必勾选 ""Add Python to PATH""。", 'Warning')
    exit 1
}

# 2) svn
$svnOk = $false
if (Get-Command svn.exe -ErrorAction SilentlyContinue) { $svnOk = $true }
if (Test-Path "$env:ProgramFiles\TortoiseSVN\bin\svn.exe") { $svnOk = $true }
if (Test-Path "${env:ProgramFiles(x86)}\TortoiseSVN\bin\svn.exe") { $svnOk = $true }
if (Test-Path "$env:ProgramFiles\SlikSvn\bin\svn.exe") { $svnOk = $true }
if (-not $svnOk) {
    Show-Msg("没找到 svn 命令行工具。`n`n如果你已经装了 TortoiseSVN，请重新运行它的安装程序，`n勾选 ""command line client tools""。`n`n或者单独安装 SlikSVN：https://sliksvn.com/download/", 'Warning')
    exit 1
}

# 3) 桌面快捷方式
$ws = New-Object -ComObject WScript.Shell
$s  = $ws.CreateShortcut($Lnk)
$s.TargetPath       = $Bat
$s.WorkingDirectory = $PSScriptRoot
if (Test-Path $Ico) { $s.IconLocation = $Ico }
$s.Description      = 'SVN Manager - 多项目 SVN 客户端'
$s.Save()

Show-Msg("安装完成！`n`n桌面已创建「SVN Manager」快捷方式，双击即可使用。`n`n源码目录：$Root")
exit 0

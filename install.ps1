<#
    svpn 安装脚本（Windows）
      1) 检查项目文件是否齐全
      2) 检查 Python 3.10+（会跳过 Microsoft Store 的 python.exe 占位）
      3) python -m svpn init
      4) python -m svpn doctor（没有节点时返回 1，属于正常）
      5) 桌面建快捷方式 "svpn 启动.lnk" -> start-svpn.cmd
      6) 打印下一步

    边界：不写注册表、不设开机自启、不装服务、不需要管理员。
    卸载：powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
    用法（项目根目录）：
      powershell -ExecutionPolicy Bypass -File .\install.ps1

    注意：本文件必须保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会按
    ANSI(936) 读入源码，中文字符串变乱码甚至破坏行解析。
#>
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Section([string]$text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Write-Ok([string]$text)      { Write-Host "[+] $text" -ForegroundColor Green }
function Write-Note([string]$text)    { Write-Host "[i] $text" -ForegroundColor Gray }
function Write-Warn([string]$text)    { Write-Host "[!] $text" -ForegroundColor Yellow }

function Find-SvpnPython {
    # 返回 @{ Exe; Prefix; Version } 或 $null。
    # 只认 -c 能跑通的解释器：Store 别名会失败或弹商店，直接跳过。
    foreach ($cand in @(@{ Exe = "python"; Prefix = @() }, @{ Exe = "py"; Prefix = @("-3") })) {
        $cmd = Get-Command $cand.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $probe = $cand.Prefix + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")
        try {
            $raw = & $cmd.Source @probe 2>$null
        } catch {
            continue
        }
        if ($LASTEXITCODE -ne 0 -or -not $raw) { continue }
        try {
            $ver = [version](@($raw)[0].ToString().Trim())
        } catch {
            continue
        }
        if ($ver -lt [version]"3.10") {
            Write-Warn "$($cand.Exe) 是 $ver，低于 3.10，跳过"
            continue
        }
        return @{ Exe = $cmd.Source; Prefix = $cand.Prefix; Version = $ver }
    }
    return $null
}

Write-Host "svpn installer (Windows)" -ForegroundColor White
Write-Note "项目目录: $root"

Write-Section "1/5 检查项目文件"
$required = @(
    "svpn\cli.py",
    "svpn.cmd",
    "svpnctl.ps1",
    "start-svpn.cmd",
    "uninstall.ps1"
)
$missing = @()
foreach ($f in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $f))) { $missing += $f }
}
if ($missing.Count -gt 0) {
    Write-Warn ("缺少 " + ($missing -join " / ") + " —— 这个目录不像 svpn 项目根目录，先把完整项目放好再跑。")
    exit 2
}
Write-Ok ("全部齐了: " + ($required -join " / "))

Write-Section "2/5 找 Python（需要 3.10+）"
$python = Find-SvpnPython
if (-not $python) {
    Write-Warn "没找到可用的 Python 3.10+。"
    Write-Note "装一个 Python 3.10+，安装时勾选 'Add python.exe to PATH'，然后重跑本脚本。"
    exit 3
}
Write-Ok ("Python {0}  {1}" -f $python.Version, $python.Exe)

Write-Section "3/5 初始化配置（python -m svpn init）"
& $python.Exe @($python.Prefix + @("-m", "svpn", "init"))
if ($LASTEXITCODE -ne 0) {
    Write-Warn "init 失败，退出码 $LASTEXITCODE"
    exit 4
}
Write-Ok "配置就绪：config.json / runtime 备份"

Write-Section "4/5 体检（python -m svpn doctor）"
& $python.Exe @($python.Prefix + @("-m", "svpn", "doctor"))
$doctorRc = $LASTEXITCODE
if ($doctorRc -eq 1) {
    Write-Note "doctor 返回 1：通常是还没配节点、社区/创意工坊不可用。这是已知状态，不算安装失败。"
    Write-Note "有 Clash/v2rayN 之类的客户端就 python -m svpn node scan；没有节点也能只加速下载。"
} elseif ($doctorRc -ne 0) {
    Write-Warn "doctor 返回 $doctorRc，先看上面的输出（不影响后面创建快捷方式）"
} else {
    Write-Ok "体检通过"
}

Write-Section "5/5 创建桌面快捷方式"
$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop -or -not (Test-Path $desktop)) {
    Write-Warn "找不到桌面目录（可能被 OneDrive 重定向且未就绪），跳过快捷方式"
    Write-Note "可以手动把 start-svpn.cmd 的快捷方式放到桌面"
} else {
    $target = Join-Path $root "start-svpn.cmd"
    $lnkPath = Join-Path $desktop "svpn 启动.lnk"
    try {
        $wsh = New-Object -ComObject WScript.Shell
        $lnk = $wsh.CreateShortcut($lnkPath)
        $lnk.TargetPath = $target
        $lnk.WorkingDirectory = $root
        $lnk.Description = "svpn: local SOCKS5/HTTP split tunnel for Steam"
        $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,13"
        $lnk.Save()
        if (Test-Path -LiteralPath $lnkPath) {
            Write-Ok "已创建: $lnkPath"
        } else {
            Write-Warn "快捷方式没有落盘: $lnkPath"
        }
    } catch {
        Write-Warn "创建快捷方式失败: $($_.Exception.Message)"
        Write-Note "可以手动给 start-svpn.cmd 建快捷方式，效果一样"
    }
}

Write-Host ""
Write-Ok "安装完成。没有写注册表、没有设开机自启、没有装服务、没有提权。"
Write-Host @"

下一步：
  1. 双击桌面 "svpn 启动"（= 本目录 start-svpn.cmd）：
       起代理 + 接管系统代理 + 拉起 Steam
  2. 没有节点也能加速下载：
       python -m svpn cdn --refresh
       python -m svpn hosts apply
  3. 有客户端（Clash / v2rayN / sing-box / Watt Toolkit）：
       python -m svpn node scan
       python -m svpn node set socks5://host:port
  4. 收工：python -m svpn down --clear-hosts
     彻底卸载：powershell -ExecutionPolicy Bypass -File "$root\uninstall.ps1"
               加 -Purge 连 runtime 备份一起删（默认保留）

"@
Write-Note "脚本被 PowerShell 拦截时，用: powershell -ExecutionPolicy Bypass -File .\install.ps1"
exit 0

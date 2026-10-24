<#
    svpn 卸载脚本（Windows）—— 可安全重复执行（每次都会跳过已经做过的步骤）

    做的事：
      1) 按 runtime/svpn.pid 停掉后台进程（只看 pid，不按镜像名乱杀 python）
      2) 还原系统代理（读 runtime/wininet-backup.json，没有备份就跳过）
      3) 从 hosts 里删掉 svpn 托管块（只删标记之间那几行，其余字节原样保留）
         —— 这步要管理员，会自动弹一次 UAC 重入（-HostsOnly），不会重复弹
      4) 删桌面快捷方式 "svpn 启动.lnk"（存在才删）
      5) -Purge 时删 runtime\ 与 svpn.egg-info\（默认保留备份，删前要确认）

    参数：
      -KeepProxy   不还原系统代理（默认还原）
      -Purge       同时删除 runtime\（里面是系统代理/hosts 备份）与 svpn.egg-info\
      -Yes         配合 -Purge，跳过交互确认（给自动化用）
      -HostsOnly   内部用：只做第 3 步，提权重入时用，避免递归

    用法：
      powershell -ExecutionPolicy Bypass -File .\uninstall.ps1
      powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge

    注意：本文件必须保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会把中文
    按 ANSI(936) 读成乱码。
#>
[CmdletBinding()]
param(
    [switch]$Purge,
    [switch]$KeepProxy,
    [switch]$Yes,
    [switch]$HostsOnly
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$runtime = Join-Path $root "runtime"
$pidFile = Join-Path $runtime "svpn.pid"

# hosts 托管块标记必须与 svpn/system.py 的 MARK_BEGIN / MARK_END 完全一致
$markBegin = "# >>> steam-vpn managed block (do not edit) >>>"
$markEnd = "# <<< steam-vpn managed block <<<"
$hostsPath = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"

$done = @()
$skipped = @()

function Write-Step([string]$t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Write-Ok([string]$t)   { Write-Host "[+] $t" -ForegroundColor Green }
function Write-Note([string]$t) { Write-Host "[i] $t" -ForegroundColor Gray }
function Write-Warn([string]$t) { Write-Host "[!] $t" -ForegroundColor Yellow }

function Test-Admin {
    try {
        & net session *> $null
        if ($LASTEXITCODE -eq 0) { return $true }
        return $false
    } catch {
        # 没有 net.exe 时的兜底
        try {
            $id = [Security.Principal.WindowsIdentity]::GetCurrent()
            $pr = New-Object Security.Principal.WindowsPrincipal($id)
            return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        } catch {
            return $false
        }
    }
}

function Test-Interactive {
    # 双击/终端里跑才算交互；被计划任务或管道拉起时不弹确认
    try {
        if ([Console]::IsInputRedirected) { return $false }
    } catch {
        return $false
    }
    if ($Host.Name -ne "ConsoleHost") { return $false }
    return [Environment]::UserInteractive
}

# ---------------------------------------------------------------- hosts 工具
# hosts 是用户文件：可能是 UTF-8 BOM、可能是 GBK 中文注释。用 Latin-1 做 1:1
# 字节↔字符映射读写，只按「行」过滤标记块，其余字节完全不动。
# 注意必须走 ReadAllBytes + Encoding.GetString：ReadAllText(path, latin1) 会自动
# 探测 BOM 改成 UTF-8 解码，然后 GetBytes 把中文变成 '?'，等于毁掉用户注释。
function Get-HostsLatinText {
    $latin = [System.Text.Encoding]::GetEncoding(28591)
    return $latin.GetString([System.IO.File]::ReadAllBytes($hostsPath))
}

function Test-HostsBlock {
    if (-not (Test-Path -LiteralPath $hostsPath)) { return $false }
    try {
        foreach ($line in ((Get-HostsLatinText) -split "`n")) {
            if ($line.Trim() -eq $markBegin) { return $true }
        }
    } catch {
        return $false
    }
    return $false
}

function Remove-HostsBlock {
    # 返回 @{ ok; removed; reason }
    $latin = [System.Text.Encoding]::GetEncoding(28591)
    try {
        $text = Get-HostsLatinText
    } catch {
        return @{ ok = $false; removed = 0; reason = "读不到 hosts: $($_.Exception.Message)" }
    }
    # 先备份（备份是原始字节，出问题可以直接拷回去）
    try {
        if (-not (Test-Path -LiteralPath $runtime)) {
            New-Item -ItemType Directory -Path $runtime -Force | Out-Null
        }
        $bak = Join-Path $runtime "hosts.backup-uninstall"
        [System.IO.File]::WriteAllBytes($bak, $latin.GetBytes($text))
    } catch {
        return @{ ok = $false; removed = 0; reason = "备份 hosts 失败: $($_.Exception.Message)" }
    }

    $kept = New-Object System.Collections.Generic.List[string]
    $skip = $false
    $removed = 0
    foreach ($line in ($text -split "`n")) {
        $t = $line.Trim()
        if ($t -eq $markBegin) { $skip = $true;  $removed++; continue }
        if ($t -eq $markEnd)   { $skip = $false; $removed++; continue }
        if ($skip)             { $removed++; continue }
        $kept.Add($line)
    }
    if ($removed -eq 0) {
        return @{ ok = $true; removed = 0; reason = "没有托管块" }
    }
    try {
        [System.IO.File]::WriteAllBytes($hostsPath, $latin.GetBytes(($kept -join "`n")))
    } catch {
        return @{ ok = $false; removed = 0; reason = "写 hosts 失败（权限？）: $($_.Exception.Message)" }
    }
    return @{ ok = $true; removed = $removed; reason = "" }
}

# ================================================================ 主流程
Write-Host "svpn uninstaller (Windows)" -ForegroundColor White
Write-Note "项目目录: $root"
if ($KeepProxy) { Write-Note "-KeepProxy：不还原系统代理" }
if ($Purge)     { Write-Note "-Purge：会删除 runtime\ 与 svpn.egg-info\" }

# ---- 提权重入：只做 hosts 那一步 -------------------------------------------
if ($HostsOnly) {
    Write-Step "清理 hosts 托管块（提权会话）"
    if (-not (Test-HostsBlock)) {
        Write-Note "hosts 里没有 svpn 托管块，跳过"
        exit 0
    }
    $res = Remove-HostsBlock
    if (-not $res.ok) {
        Write-Warn $res.reason
        exit 1
    }
    & ipconfig /flushdns *> $null
    Write-Ok "hosts 已清理（删掉 $($res.removed) 行，DNS 缓存已刷新）"
    exit 0
}

# ---- 1) 停进程 -------------------------------------------------------------
Write-Step "1/5 停止后台进程"
$stopped = $false
if (Test-Path -LiteralPath $pidFile) {
    $rawPid = ""
    try { $rawPid = ([System.IO.File]::ReadAllText($pidFile)).Trim() } catch { $rawPid = "" }
    $procId = 0
    if ([int]::TryParse($rawPid, [ref]$procId) -and $procId -gt 0) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            try {
                Stop-Process -Id $procId -Force -ErrorAction Stop
                Write-Ok "已停止 pid=$procId（$($proc.ProcessName)）"
                $stopped = $true
            } catch {
                Write-Warn "停不掉 pid=$procId : $($_.Exception.Message)"
            }
        } else {
            Write-Note "runtime\svpn.pid 里的 pid=$procId 已经不在了，跳过"
            $skipped += "停进程（pid $procId 已退出）"
        }
    } else {
        Write-Warn "runtime\svpn.pid 内容不是合法 pid（'$rawPid'），跳过"
        $skipped += "停进程（pid 文件损坏）"
    }
} else {
    Write-Note "没有 runtime\svpn.pid，说明本来就没在跑，跳过"
    $skipped += "停进程（无 pid 文件）"
}
if ($stopped) {
    Start-Sleep -Milliseconds 400
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    $done += "停止后台进程"
}

# ---- 2) 还原系统代理 -------------------------------------------------------
Write-Step "2/5 还原系统代理"
$bakWininet = Join-Path $runtime "wininet-backup.json"
if ($KeepProxy) {
    Write-Note "-KeepProxy：保持当前系统代理设置不动"
    $skipped += "还原系统代理（-KeepProxy）"
} elseif (-not (Test-Path -LiteralPath $bakWininet)) {
    Write-Note "没有 runtime\wininet-backup.json，跳过。"
    Write-Note "（不敢猜默认值：你的原始代理设置可能是公司 PAC 或别的客户端，瞎写会更糟）"
    Write-Note "手动改的话：设置 → 网络和 Internet → 代理。"
    $skipped += "还原系统代理（无备份）"
} else {
    try {
        $data = [System.IO.File]::ReadAllText($bakWininet, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        $key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        if (-not (Test-Path $key)) { New-Item -Path $key -Force | Out-Null }

        $enable = 0
        if ($null -ne $data.ProxyEnable) { $enable = [int]$data.ProxyEnable }
        $server   = ""
        if ($null -ne $data.ProxyServer)   { $server = [string]$data.ProxyServer }
        $override = ""
        if ($null -ne $data.ProxyOverride) { $override = [string]$data.ProxyOverride }
        $pac      = ""
        if ($null -ne $data.AutoConfigURL) { $pac = [string]$data.AutoConfigURL }

        Set-ItemProperty -Path $key -Name "ProxyEnable"   -Type DWord -Value $enable
        Set-ItemProperty -Path $key -Name "ProxyServer"   -Type String -Value $server
        Set-ItemProperty -Path $key -Name "ProxyOverride" -Type String -Value $override
        Set-ItemProperty -Path $key -Name "AutoConfigURL" -Type String -Value $pac
        Write-Ok "系统代理已还原：ProxyEnable=$enable ProxyServer='$server' AutoConfigURL='$pac'"
        Write-Note "已经开着的浏览器/客户端要重启才认新设置。"
        $done += "还原系统代理"
    } catch {
        Write-Warn "还原系统代理失败: $($_.Exception.Message)"
        Write-Note "备份文件还在：$bakWininet —— 也可以手动在「设置 → 网络 → 代理」里关掉。"
        $skipped += "还原系统代理（写入失败）"
    }
}

# ---- 3) 清 hosts -----------------------------------------------------------
Write-Step "3/5 清理 hosts 托管块"
if (-not (Test-Path -LiteralPath $hostsPath)) {
    Write-Warn "找不到 hosts 文件: $hostsPath，跳过"
    $skipped += "清理 hosts（文件不存在）"
} elseif (-not (Test-HostsBlock)) {
    Write-Note "hosts 里没有 svpn 托管块，跳过（这步是幂等的）"
    $skipped += "清理 hosts（本来就没有）"
} elseif (Test-Admin) {
    $res = Remove-HostsBlock
    if ($res.ok) {
        & ipconfig /flushdns *> $null
        Write-Ok "hosts 已清理（删掉 $($res.removed) 行，其余内容原样保留，DNS 缓存已刷新）"
        $done += "清理 hosts"
    } else {
        Write-Warn $res.reason
        $skipped += "清理 hosts（失败）"
    }
} else {
    Write-Note "写 hosts 需要管理员权限，正在弹 UAC（只这一次）…"
    $psExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $self = $MyInvocation.MyCommand.Path
    if (-not $self) { $self = $PSCommandPath }
    $elevated = $false
    try {
        $p = Start-Process -FilePath $psExe `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$self`"", "-HostsOnly") `
            -Verb RunAs -Wait -PassThru -ErrorAction Stop
        $elevated = ($p.ExitCode -eq 0)
    } catch {
        Write-Warn "提权被取消或失败: $($_.Exception.Message)"
        $elevated = $false
    }
    if (Test-HostsBlock) {
        Write-Warn "hosts 托管块还在。手动清（管理员 PowerShell）："
        Write-Note "  notepad $hostsPath   删掉 '$markBegin' 到 '$markEnd' 之间的行"
        $skipped += "清理 hosts（提权未完成）"
    } else {
        & ipconfig /flushdns *> $null
        Write-Ok "hosts 已清理（提权会话写入）"
        $done += "清理 hosts"
    }
    if (-not $elevated) {
        Write-Note "提权会话没有正常返回，但已按 hosts 实际内容判定结果，不影响继续。"
    }
}

# ---- 4) 删快捷方式 ---------------------------------------------------------
Write-Step "4/5 删除桌面快捷方式"
$desktop = [Environment]::GetFolderPath("Desktop")
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) {
    Write-Warn "找不到桌面目录，跳过"
    $skipped += "删快捷方式（无桌面目录）"
} else {
    $lnk = Join-Path $desktop "svpn 启动.lnk"
    if (Test-Path -LiteralPath $lnk) {
        try {
            Remove-Item -LiteralPath $lnk -Force -ErrorAction Stop
            Write-Ok "已删除: $lnk"
            $done += "删除桌面快捷方式"
        } catch {
            Write-Warn "删除失败: $($_.Exception.Message)"
            $skipped += "删快捷方式（失败）"
        }
    } else {
        Write-Note "桌面上没有 'svpn 启动.lnk'，跳过"
        $skipped += "删快捷方式（本来就没有）"
    }
}

# ---- 5) Purge --------------------------------------------------------------
Write-Step "5/5 清理运行时目录"
if (-not $Purge) {
    Write-Note "保留 runtime\（里面有 hosts / 系统代理备份，留着可回滚）与 svpn.egg-info\。"
    Write-Note "要一起删就加 -Purge（例：.\uninstall.ps1 -Purge）。"
    $skipped += "删除 runtime/egg-info（未加 -Purge）"
} else {
    $targets = @(
        (Join-Path $root "runtime"),
        (Join-Path $root "svpn.egg-info")
    ) | Where-Object { Test-Path -LiteralPath $_ }

    if (-not $targets -or $targets.Count -eq 0) {
        Write-Note "runtime\ 和 svpn.egg-info\ 都不存在，跳过"
        $skipped += "删除 runtime/egg-info（都不存在）"
    } else {
        Write-Note "将要删除："
        foreach ($t in $targets) { Write-Host "      $t" }
        $go = $false
        if ($Yes) {
            $go = $true
            Write-Note "-Yes 已给出，不再询问"
        } elseif (Test-Interactive) {
            $ans = Read-Host "确认删除？(yes/no)"
            $go = ($ans -match '^(y|yes|是)$')
            if (-not $go) { Write-Note "已取消，一个都没删。" }
        } else {
            Write-Note "非交互环境且没给 -Yes，跳过删除（要用就跑 -Purge -Yes）。"
        }
        if ($go) {
            foreach ($t in $targets) {
                try {
                    Remove-Item -LiteralPath $t -Recurse -Force -ErrorAction Stop
                    Write-Ok "已删除: $t"
                    $done += "删除 $(Split-Path -Leaf $t)"
                } catch {
                    Write-Warn "删不掉 $t : $($_.Exception.Message)"
                    $skipped += "删除 $(Split-Path -Leaf $t)（失败）"
                }
            }
        } elseif (-not $Yes) {
            if (Test-Interactive) { $skipped += "删除 runtime/egg-info（用户取消）" }
            else { $skipped += "删除 runtime/egg-info（非交互且无 -Yes）" }
        }
    }
}

# ---- 总结 ------------------------------------------------------------------
Write-Host ""
Write-Host "===== 卸载完成 =====" -ForegroundColor White
if ($done.Count -gt 0) {
    Write-Ok "做了 $($done.Count) 件事："
    foreach ($d in $done) { Write-Host "      - $d" -ForegroundColor Green }
} else {
    Write-Note "这一轮没有实际改动（大概已经卸过了）。"
}
if ($skipped.Count -gt 0) {
    Write-Note "跳过 $($skipped.Count) 项："
    foreach ($s in $skipped) { Write-Host "      - $s" -ForegroundColor DarkGray }
}
Write-Host ""
Write-Note "本脚本没删的东西：config.json、svpn\ 源码、Python 包（pip 装的请自己 pip uninstall svpn）。"
Write-Note "彻底清干净：把整个项目目录删掉即可。"
exit 0

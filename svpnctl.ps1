<#
    svpn 控制脚本 —— 在 PowerShell 里直接用，不用记 python -m svpn
    工作目录会自动切到脚本所在目录（config.json / runtime/ 都在这儿）

    日常:
      .\svpnctl.ps1 init
      .\svpnctl.ps1 node scan
      .\svpnctl.ps1 up -Background -System
      .\svpnctl.ps1 probe
      .\svpnctl.ps1 cdn -Refresh
      .\svpnctl.ps1 hosts apply
      .\svpnctl.ps1 down -ClearHosts

    节点:
      .\svpnctl.ps1 node set socks5://127.0.0.1:7890
      .\svpnctl.ps1 node clear
      .\svpnctl.ps1 up -Background -Node http://127.0.0.1:10809
      .\svpnctl.ps1 up -Background -NoAuto          # 不自动扫描，只用 config.json 里的节点

    CDN 与 hosts:
      .\svpnctl.ps1 cdn                            # 只读缓存并打印
      .\svpnctl.ps1 cdn -Refresh                   # 重新解析 + 竞速 + 1MB 实测
      .\svpnctl.ps1 cdn -Refresh -NoBench -Domain steamcdn-a.akamaihd.net
      .\svpnctl.ps1 hosts apply -NoElevate         # 不提权（当前已是管理员时才写得进去）

    Steam 与系统代理:
      .\svpnctl.ps1 steam -System -Restart
      .\svpnctl.ps1 pac -Write -Apply
      .\svpnctl.ps1 system show

    其余参数原样透传给 python -m svpn（例：.\svpnctl.ps1 probe --timeout 6）
#>
param(
    [Parameter(Position = 0)][string]$Command = "status",

    # up
    [switch]$Background,
    [switch]$System,          # up: --system-proxy / steam: --system
    [switch]$SystemProxy,     # 同 -System，写全一点也可以
    [int]$Port = 0,
    [string]$Mode = "",
    [string]$Node = "",
    [switch]$NoAuto,

    # down
    [switch]$KeepSystem,
    [switch]$ClearHosts,

    # probe / cdn
    [switch]$Json,
    [switch]$Refresh,
    [switch]$NoBench,
    [string]$Domain = "",
    [double]$Timeout = 0,

    # hosts
    [switch]$NoElevate,

    # steam
    [string]$Exe = "",
    [switch]$Restart,
    [switch]$NoAutostart,

    # pac / init
    [switch]$Write,
    [switch]$Apply,
    [switch]$Force,

    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Resolve-Python {
    # 返回 @{ Exe = 可执行文件; Prefix = 额外参数（py 要 -3）}
    foreach ($cand in @(@{ Name = "python"; Prefix = @() }, @{ Name = "py"; Prefix = @("-3") })) {
        $cmd = Get-Command $cand.Name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            & $cmd.Source @($cand.Prefix + @("-c", "import sys")) *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{ Exe = $cmd.Source; Name = $cand.Name; Prefix = $cand.Prefix }
            }
        } catch {
            continue
        }
    }
    throw "找不到可用的 python，请先装 Python 3.10+ 并加进 PATH"
}

function Rest-Args {
    param([string[]]$Items, [int]$Skip = 0)
    if (-not $Items -or $Items.Count -le $Skip) { return @() }
    return @($Items[$Skip..($Items.Count - 1)])
}

$py = Resolve-Python
$argsList = @($py.Prefix) + @("-m", "svpn", $Command)

switch ($Command) {
    "init" {
        if ($Force) { $argsList += "--force" }
    }
    "up" {
        if ($Background) { $argsList += "--background" }
        if ($Port) { $argsList += @("--port", "$Port") }
        if ($Mode) { $argsList += @("--mode", $Mode) }
        if ($System -or $SystemProxy) { $argsList += "--system-proxy" }
        if ($Node) { $argsList += @("--node", $Node) }
        if ($NoAuto) { $argsList += "--no-auto" }
        if ($Rest) { $argsList += $Rest }
    }
    "down" {
        if ($KeepSystem) { $argsList += "--keep-system" }
        if ($ClearHosts) { $argsList += "--clear-hosts" }
        if ($Rest) { $argsList += $Rest }
    }
    "probe" {
        if ($Node) { $argsList += @("--node", $Node) }
        if ($Timeout) { $argsList += @("--timeout", "$Timeout") }
        if ($Json) { $argsList += "--json" }
        if ($Rest) { $argsList += $Rest }
    }
    "node" {
        # node scan | node set URL | node clear，缺省 scan
        $sub = "scan"
        if ($Rest -and $Rest.Count -gt 0) { $sub = $Rest[0] }
        $argsList += $sub
        $argsList += Rest-Args $Rest 1
        if ($Json) { $argsList += "--json" }
    }
    "cdn" {
        if ($Refresh) { $argsList += "--refresh" }
        if ($NoBench) { $argsList += "--no-bench" }
        if ($Domain) { $argsList += @("--domain", $Domain) }
        if ($Timeout) { $argsList += @("--timeout", "$Timeout") }
        if ($Json) { $argsList += "--json" }
        if ($Rest) { $argsList += $Rest }
    }
    "hosts" {
        # hosts apply | hosts clear，缺省 apply
        $sub = "apply"
        if ($Rest -and $Rest.Count -gt 0) { $sub = $Rest[0] }
        $argsList += $sub
        if ($NoElevate) { $argsList += "--no-elevate" }
        if ($Timeout) { $argsList += @("--timeout", "$Timeout") }
        $argsList += Rest-Args $Rest 1
    }
    "system" {
        # system on | off | show，缺省 show
        $sub = "show"
        if ($Rest -and $Rest.Count -gt 0) { $sub = $Rest[0] }
        $argsList += $sub
        $argsList += Rest-Args $Rest 1
    }
    "pac" {
        # 不带开关只打印 PAC；-Write 落盘；-Apply 应用到系统
        if ($Write -or $Apply) { $argsList += "--write" }
        if ($Apply) { $argsList += "--apply" }
        if ($Rest) { $argsList += $Rest }
    }
    "steam" {
        if ($Exe) { $argsList += @("--exe", $Exe) }
        if ($System -or $SystemProxy) { $argsList += "--system" }
        if ($Restart) { $argsList += "--restart" }
        if ($NoAutostart) { $argsList += "--no-autostart" }
        if ($Rest) { $argsList += @("--args") + $Rest }
    }
    default {
        # status / doctor / 以及以后新增的命令：Command 已经带上，剩下的原样透传
        if ($Rest) { $argsList += $Rest }
    }
}

$shown = @($argsList[($py.Prefix.Count)..($argsList.Count - 1)])
Write-Host ("→ python " + ($shown -join " ")) -ForegroundColor DarkGray
& $py.Exe @argsList
$rc = $LASTEXITCODE
if ($null -eq $rc) { $rc = 0 }
exit $rc

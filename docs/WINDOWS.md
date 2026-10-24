# Windows 踩坑与系统契约

面向「在 Windows 上跑这套东西、或者改这几个脚本」的人。README 讲怎么用，这里讲**为什么这么写、哪一步坏了会怎么表现**。

适用：Windows 10（1809+）/ Windows 11，Windows PowerShell 5.1（系统自带那版），Python 3.10+。
PowerShell 7（`pwsh.exe`）不是目标环境：脚本按 5.1 的语法写，7 上一般也能跑，但**没有验证过**。

---

## 1. 四条硬规则（改脚本前先背下来）

| # | 规则 | 违反后的表现 |
|---|---|---|
| 1 | 所有 `.ps1` 必须是 **UTF-8 with BOM + CRLF** | 中文乱码；严重时 `ParserError`，脚本直接跑不起来 |
| 2 | 所有 `.cmd` 必须是 **CRLF、无 BOM** | BOM 会被当成命令的一部分 → `'ï»¿@echo' is not recognized` |
| 3 | `.cmd` 的 `rem` 注释**只写 ASCII** | 从中文 Windows 双击时冒出 `'?' is not recognized as an internal or external command` |
| 4 | `.cmd` 里任何**含中文的 `echo` 行后面必须紧跟一个空行** | 同上：cmd.exe 吃掉下一行行首，后面的正常命令变成"未知命令" |

`config.json` / `config.example.json` / `*.py` 用 **UTF-8 无 BOM**（JSON 带 BOM 会让 `json.loads` 报 `Expecting value`；Python 3 源码带 BOM 虽然能跑但没必要）。

### 1.1 为什么 `.ps1` 非要 BOM

Windows PowerShell 5.1 **不按 UTF-8 读没有 BOM 的脚本**，它会用系统 ANSI 代码页（中文系统是 936/GBK）。于是：

- 中文字符串变成乱码（`Write-Host "安装完成"` 显示成 `瀹夎瀹屾垚` 之类）；
- 更糟的是 **GBK 双字节里可能凑出 `"`/`'`/`` ` `` 的尾字节**，直接把字符串截断，报
  `表达式或语句中包含意外的标记` / `字符串缺少终止符` —— 这不是"观感问题"，是**硬故障**，整个脚本不执行。

本项目实测过：把 `install.ps1` 的 BOM 去掉，PS 5.1 立刻 `ParserError`。所以 `install.ps1`、`uninstall.ps1`、`svpnctl.ps1` 三个文件都带 BOM。

一键核对（三行，粘进 Git Bash / PowerShell 都行）：

```bash
python -c "import pathlib;[print(p.name, p.read_bytes()[:3]==b'\xef\xbb\xbf') for p in map(pathlib.Path,['install.ps1','uninstall.ps1','svpnctl.ps1'])]"
python -c "import pathlib;[print(p.name, p.read_bytes()[:3]==b'\xef\xbb\xbf') for p in map(pathlib.Path,['svpn.cmd','start-svpn.cmd'])]"
```

期望：前一行三个 `True`；后一行两个 `False`。

改完用「只解析不执行」验语法（不会真跑，安全）：

```powershell
$errs = $null
[void][System.Management.Automation.Language.Parser]::ParseFile("D:\LE\AI\dshwork\steam-vpn\install.ps1", [ref]$null, [ref]$errs)
"errors=$($errs.Count)"      # 必须是 errors=0
```

### 1.2 `.cmd` 的 `call` 坑（最容易静默失败的一处）

`python` 在 PATH 上**不一定是个 `.exe`**。pyenv-win、conda、Microsoft Store 的别名等都可能把它实现成 `.bat` / `.cmd` 包装（shim）。cmd.exe 的规则是：

> 在**批处理文件内部**调用另一个 `.bat`/`.cmd` 而不加 `call`，控制权**不会返回**给调用者，脚本就此结束。

表现极其阴间：**没有任何报错，退出码 0**，后面的命令一条都没执行。你在双击 `start-svpn.cmd` 时只会看到窗口一闪（或停在前半段）然后一切正常退出。

**关键点：`call` 不是只加在探测上，`%PY% -m svpn ...` 这行本身也必须加。** 探测那两行漏了，会在检测阶段就静默退出；
真实调用漏了，表现是"脚本跑了一半、什么都没发生、退出码 0"——用户完全看不出错在哪。

所以 `svpn.cmd` / `start-svpn.cmd` 里**每一处** `python` / `py -3` 调用都带 `call`：

```bat
call python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
...
call %PY% -m svpn up -b --system-proxy
```

**实测**（P5-scripts 用一个假的 `python.bat` 垫片，把它放到 PATH 最前面，从无关目录调用）：

| 写法 | 现象 |
|---|---|
| 探测和真实调用都不加 `call` | 输出停在第 1 步那一行，**直接 `EXIT=0`**，后面 2/3、3/3 全没执行 |
| 全部加 `call` | 三步走完，`EXIT=0`，`ERRORLEVEL` 正常透传（`node set` 缺参数时能拿到 2） |

`call` 对真正的 `.exe` 没有副作用，`ERRORLEVEL` 照常透传，所以无脑加是对的。

两条副作用要知道：

1. **`call` 会对参数做二次展开**：`call %PY% -m svpn steam --args "%FOO%"` 里的 `%FOO%` 会被多展开一次。要传含 `%` 的字面参数，写成 `%%` 或干脆别走 `.cmd`。
2. **别用 `goto :eof` 之外的花活**：`setlocal EnableExtensions` 已经开了，但 `call :label` 与 `errorlevel` 的交互在 5.1 的 cmd 上容易踩坑，本项目只用最简单的顺序结构。

### 1.3 `.cmd` 里中文的"载重规则"（最容易踩的一条，rem 也算）

**规则**：UTF-8 无 BOM 的 `.cmd` 里，**只要某一行含中文（`rem` 注释也算）**，而控制台起始代码页是 936
（中文 Windows 上双击就是这个），那么 `chcp 65001` 之后 cmd.exe 会让**下一行的行首被吃掉**，
冒出：

```
'?' is not recognized as an internal or external command,
operable program or batch file.
```

不是乱码问题，是**行偏移问题**：多字节字符让 cmd.exe 记错了下一行的字节位置，于是把下一行开头当成一条新命令。

**解法两条，缺一不可**：

1. 注释（`rem`）**一律 ASCII**——中文 `rem` 一样会触发，不是"注释无所谓"；
2. **每一个含中文的行后面紧跟一个空行**（空行吸收掉被吃掉的那一下）。

外加两条文件级约束：**CRLF 换行、无 BOM**。

**实测对照**（P5-scripts 做的，三轮一轮不差）：

| 版本 | 从 CP936 起跑 | 结果 |
|---|---|---|
| 中文 `rem` 注释、无空行 | 3 轮 | 每轮都冒 `'?' is not recognized`，从第 2 行开始报错 |
| ASCII 注释 + 中文 `echo` + 每个中文行后留空行 | 3 轮 | 0 处报错，端到端正常 |

`start-svpn.cmd` / `svpn.cmd` 的文件头都写着这条"载重规则"（`ENCODING RULES ... they are load-bearing`），
改的时候别删。**你要在这两个脚本里加自己的启动步骤，也必须遵守**；否则用户双击时会看到一堆
`'?' is not recognized`，而脚本"看起来还在跑"，极难排查。

快速自查（应该 0 命中）：含中文的行后面不是空行的情况——

```bash
python -c "import pathlib,re;[print(p.name, i+1, repr(l)) for p in map(pathlib.Path,['svpn.cmd','start-svpn.cmd']) for i,l in enumerate(p.read_text(encoding='utf-8').splitlines()) if re.search(r'[^\x00-\x7f]', l) and (i+1>=len(p.read_text(encoding='utf-8').splitlines()) or p.read_text(encoding='utf-8').splitlines()[i+1].strip()!='')]"
```

（上面这条会顺带把 `rem` 里的中文也揪出来——按规则，那本身就该改成 ASCII。）

---

## 2. UAC 与 hosts

### 2.1 为什么非要提权

`C:\Windows\System32\drivers\etc\hosts` 默认只有 Administrators 可写。`svpn hosts apply` 非管理员时会：

1. `ctypes.windll.shell32.IsUserAnAdmin()` 判断 → 不是管理员；
2. 用 `powershell -NoProfile -Command "Start-Process -FilePath '<python>' -ArgumentList @('-m','svpn','hosts','apply','--no-elevate') -WorkingDirectory '<项目根>' -Verb RunAs -Wait -WindowStyle Hidden"` 重新拉起自己（弹一次 UAC）；
3. **不看子进程返回码**，而是回来**按 hosts 的实际内容**判定成败（`_hosts_entries()` 有记录 = 成功）。

第 3 点很重要：UAC 被用户点「否」时，`Start-Process` 抛异常、子进程根本没跑，但如果不按内容判定就会误报成功。

### 2.2 提权相关的常见现象

| 现象 | 原因 | 处置 |
|---|---|---|
| 弹了 UAC，点「否」后提示「提权未完成」 | 正常路径 | 用管理员 PowerShell 跑 `python -m svpn hosts apply --no-elevate` |
| 什么都没弹，直接说失败 | 系统策略禁了提权（域控/家长控制），或 `powershell` 不在 PATH | 同上，手动管理员执行 |
| `--no-elevate` 跑完提示「需要管理员权限写 hosts」 | 这就是它的语义：不提权，只在当前已是管理员时才写得进去 | 换管理员终端 |
| 提示「N 个条目未通过 hosts 校验」 | `sanitize_entries` 拦掉了脏域名（主机名位置出现 IP、带空格/`#`/换行、超过 253 字符） | 看 `runtime/cdn-cache.json` 是不是被改坏了；重新 `cdn --refresh` |

### 2.3 hosts 的编码契约（别把用户注释写坏）

`hosts` 是**用户文件**，可能是 UTF-8 BOM、纯 UTF-8、GBK，行尾可能是 CRLF 或 LF。`svpn/system.py` 的做法：

- 读：`read_bytes()` → 先看 BOM（`ef bb bf` → `utf-8-sig`），否则依次试 `utf-8` / `gbk` / `mbcs`，全失败退回 `latin-1`；
- 换行：原文有 `\r\n` 就用 `\r\n`，否则 `\n`；
- 写：**用探测到的同一编码** `encode(enc, errors="strict")` —— strict 是故意的，编码不了的字符宁可报错，也不要静默变成 `?`（那是不可逆的数据损失）；
- 只改自己标记块之间的行，块外一个字节不动；
- 首次写入前备份到 `runtime/hosts.backup`。

`uninstall.ps1` 里是另一套实现（PowerShell 侧），用 **Latin-1 做 1:1 字节↔字符映射**：

```powershell
$latin.GetString([System.IO.File]::ReadAllBytes($hostsPath))   # 读
[System.IO.File]::WriteAllBytes($hostsPath, $latin.GetBytes($text))  # 写
```

> 这里翻过一次车：`[IO.File]::ReadAllText($p, [Encoding]::GetEncoding(28591))` **会自动探测 BOM**，遇到 UTF-8 BOM 文件就改用 UTF-8 解码，随后 `GetBytes` 把中文全变成 `?`，用户的中文注释被毁。必须走 `ReadAllBytes` + `GetString` 才是真 1:1。

写入前还会备份到 `runtime/hosts.backup-uninstall`（原始字节，出问题直接拷回去）。

### 2.4 托管块标记是硬编码的，改一处要改两处

```
# >>> steam-vpn managed block (do not edit) >>>
# <<< steam-vpn managed block <<<
```

- `svpn/system.py`：`MARK_BEGIN` / `MARK_END`
- `uninstall.ps1`：`$markBegin` / `$markEnd`（文件顶部的主流程之前）

两边**必须逐字一致**，否则卸载脚本找不到块，会以为"本来就没有"。改文案请两边一起改。

---

## 3. WinINET 系统代理：位置、备份、回滚

### 3.1 注册表位置与四个值

```
HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Internet Settings
```

| 值名 | 类型 | 含义 |
|---|---|---|
| `ProxyEnable` | REG_DWORD | `1` = 启用固定代理 |
| `ProxyServer` | REG_SZ | `127.0.0.1:7900`（也可 `http=…;https=…`，本项目只写单值） |
| `ProxyOverride` | REG_SZ | 绕过表，分号分隔；`<local>` 是特殊标记 |
| `AutoConfigURL` | REG_SZ | PAC 的 `file:///…/runtime/svpn.pac` 或 `http://…`；非空时优先于 `ProxyServer` |

两种接管方式（`cli._apply_system()`）：

- **固定代理**（默认，`browser.use_pac: false`）：`ProxyServer = <listen_host>:<http_port>`、`ProxyEnable = 1`、`AutoConfigURL = ""`；
- **PAC**（`use_pac: true` 或 `pac --apply`）：`AutoConfigURL = file:///…/runtime/svpn.pac`、`ProxyEnable = 0`。

改完立刻调 `InternetSetOptionW(NULL, 39 /*SETTINGS_CHANGED*/, …)` 和 `37 /*REFRESH*/, …` 通知 WinINET——否则已经开着的程序还认旧值。

### 3.2 备份 / 回滚

| 动作 | 命令 | 备份文件 |
|---|---|---|
| 首次备份（`init` 或首次改代理前） | `python -m svpn init` / 任何 `apply` | `runtime/wininet-backup.json` |
| 还原 | `python -m svpn system off` | 读上面那份 |
| 还原（顺带停服务） | `python -m svpn down` | 同上 |
| 卸载时还原 | `uninstall.ps1`（`-KeepProxy` 可跳过） | 同上 |

关键语义：`backup_wininet()` **只保留第一次那份真·原始值**（文件存在就 return）。所以"先 apply 再 backup"不会把 svpn 自己的代理值当成原始值存下来。

手工核对/回滚（不用 svpn）：

```powershell
# 看
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" |
    Select-Object ProxyEnable, ProxyServer, ProxyOverride, AutoConfigURL
# 关掉（最常用的一步）
Set-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -Name ProxyEnable -Type DWord -Value 0
```

`uninstall.ps1` 的还原分支写的是**同键同类型**（`DWord` / `String`），与 `svpn/system.py:restore_wininet()` 一致。没备份时它**明确跳过并说明**，绝不猜默认值——用户的原始设置可能是公司 PAC，瞎写比不改更糟。

### 3.3 谁读 WinINET、谁不读

读：IE / 旧版 Edge / **Steam 的内置浏览器（CEF 跟随系统代理）** / 大多数 Win32/WinForms 程序 / `curl` 在部分构建下。

不读：Firefox（默认用自己的设置）、部分 Electron 应用、明确用 `HTTP_PROXY` 环境变量的程序。

所以"设了系统代理但某个程序不走"是正常的，跟 svpn 无关；要按域名精细控制就用 `pac --write --apply`。

---

## 4. Steam 客户端：改代理必须重启

`svpn steam --system` 只是把系统代理指到本机，**已经在跑的 Steam 不会重新读**。这就是 README 里那条「改代理必须重启客户端」的来历。

```powershell
python -m svpn steam --restart --system    # 结束旧进程 → 设系统代理 → 带参数重拉
```

- 已在运行且没加 `--restart`：只打印「Steam 已在运行；改代理要重启客户端（加 --restart）」并返回 0（**不会**替你杀进程）；
- 代理没跑且没加 `--no-autostart`：会先 `up -b` 把代理拉起来；
- 找不到 `steam.exe`：返回 **2**（`start-svpn.cmd` 里把 2 当作"没装 Steam，属正常"）。
- 启动参数固定追加 `-tcp -noreactlogin -console -nointro`：`-tcp` 在丢 UDP 的网络里能显著提高登录成功率。
- 退出 Steam 时它是整棵进程树（`steam.exe` / `steamwebhelper` / `steamservice` / `GameOverlayUI`）。

> Steam 的内置浏览器（社区、商店页面）走系统代理；游戏本体下载走裸 TCP + HTTP，代理管不到——所以下载加速只能靠 CDN 选边 / SNI 转发，不是代理的事。

---

## 5. Windows 10 与 Windows 11 的差异

| 项 | Win10 | Win11 |
|---|---|---|
| 代理设置界面 | 设置 → 网络和 Internet → 代理 | 同上（布局变了，项名一样） |
| 系统自带的 `curl.exe` | 1803 起自带 | 自带 |
| 默认终端 | conhost（控制台字体老，中文可能显示成方块） | Windows Terminal（UTF-8 显示正常） |
| `Hosts` 文件位置 | `%SystemRoot%\System32\drivers\etc\hosts` | 同 |
| 443 端口被谁占 | 取决于本机装了什么（Watt Toolkit / IIS / 某些 VPN 客户端都可能） | 同左，与系统版本无关；用 `netstat -ano -p TCP \| Select-String ":443"` 查 |
| 提权提示 | UAC 对话框 | 同（但 Win11 会加"应用需要你的许可"样式） |

实践建议：**Win10 上优先用 Windows Terminal 跑命令**，否则 `[+]`/`[i]` 那几行中文可能显示成方块（功能不受影响，只是难看）。命令本身已经 `SetConsoleOutputCP(65001)` + `sys.stdout.reconfigure(encoding="utf-8")`，输出流是 UTF-8 的，问题只在终端字体。

---

## 6. 常见 WinError 对照

| 错误 | 含义 | 在哪出现 | 处置 |
|---|---|---|---|
| **10048** `WSAEADDRINUSE` | 端口已被占用 | `up` 起服务时；`SniForwarder` 绑 443 | `svpn status` 看是否已有实例 → `down`；否则按提示看占用者 `pid=… 进程名`，或 `up --port 17899`。`cdn.enabled=true` 时 443 被占只警告、SNI 转发跳过 |
| **121** `ERROR_SEM_TIMEOUT` | 信号灯超时（Windows 上 `accept` 的经典抖动） | `asyncio.start_server` 偶发 | 代码里 `_bind()` 对 121 最多 bind 3 次（失败间隔 0.3/0.6/0.9s），仍失败才上抛；直接重跑 `up` 即可 |
| 10054 `WSAECONNRESET` | 对端重置 | 经代理访问被 RST 的域名 | 这是 SNI 阻断的正常表现；`probe` 会标 `sni_rst` |
| 10060 `WSAETIMEDOUT` | 连接超时 | 被丢弃的 SYN（防火墙/封锁） | 换节点或换 IP；`probe` 会标 `tcp` |
| 10013 `WSAEACCES` | 权限不足 | 绑特权端口 / 防火墙拦截 | 443 需要占用者退出；别用 <1024 的端口做实验 |
| 10049 `WSAEADDRNOTAVAIL` | 地址不可用 | 把 `listen_host` 写成本机没有的 IP | 改回 `127.0.0.1` |
| `UnicodeDecodeError`（不是 WinError 但同源） | 用 `text=True` 解 `tasklist`/`netstat` 的 OEM 输出 | `PYTHONUTF8=1` 环境下的命令行工具输出 | 代码已统一按字节收再 `mbcs` 解码；自己写脚本时别用 `text=True` 收这些命令 |

判断端口到底被谁占：

```powershell
netstat -ano -p TCP | Select-String ":7899|:7900|:443"
tasklist /FI "PID eq <上面最后一列>" /NH
```

---

## 7. 安装 / 卸载脚本的行为与退出码

### 7.1 `install.ps1`

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

| 退出码 | 含义 |
|---|---|
| 0 | 成功（`doctor` 返回 1 也算成功——没节点是已知状态） |
| 2 | 项目文件不全（会列出缺哪几个） |
| 3 | 没找到 Python 3.10+ |
| 4 | `svpn init` 失败 |

它**不写注册表、不设开机自启、不装服务、不提权**。桌面快捷方式指向 `<项目根>\start-svpn.cmd`，工作目录也设成项目根。

### 7.2 `uninstall.ps1`

```powershell
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1              # 常规卸载
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -KeepProxy   # 不动系统代理
powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge -Yes  # 连 runtime/ 一起删
```

| 参数 | 作用 |
|---|---|
| `-KeepProxy` | 不还原系统代理 |
| `-Purge` | 删 `runtime\` 与 `svpn.egg-info\` |
| `-Yes` | 配合 `-Purge`，跳过交互确认（给自动化/计划任务用） |
| `-HostsOnly` | 内部用：只做 hosts 那一步，提权重入时用 |

设计要点：

- **只按 pid 杀**（读 `runtime\svpn.pid`），不按镜像名 `python.exe`，避免误杀别人的 Python；
- 提权只弹**一次**：非管理员时 `Start-Process -Verb RunAs` 重入自身并带 `-HostsOnly`，子进程做完 hosts 就 `exit`；
- 全程幂等，重复执行只会说"跳过"；
- 不删 `config.json`、`svpn\` 源码、pip 包。

`-Purge` 在**非交互环境**（计划任务、管道、CI）里必须搭配 `-Yes`，否则直接跳过删除——这是刻意的，避免自动化里误删备份。

---

## 8. 排障口袋清单

```powershell
python -m svpn status            # 进程/端口/节点/系统代理/CDN/hosts 一屏
python -m svpn doctor            # 逐项体检，最后列出待处理项（有缺项返回 1）
python -m svpn probe --lenient   # 只卡真正的死端点
Get-Content .\runtime\svpn.log -Tail 40   # 后台服务的日志
```

| 症状 | 先看哪 |
|---|---|
| 双击 `start-svpn.cmd` 一闪而过 | 是不是 `python` 变成了 `.bat` shim 而某处漏了 `call`；先命令行跑一遍看报错 |
| 双击后中文变方块/乱码 | 终端字体（换 Windows Terminal）；别去改文件编码 |
| `svpn status` 说端口监听但网页打不开 | 系统代理是不是指向了 7900 但服务其实是前台跑另一个端口；`down` 后重来 |
| `hosts apply` 成功但速度没变 | `ipconfig /flushdns`；再 `nslookup steamcdn-a.akamaihd.net` 看解析到什么 |
| `cdn.enabled=true` 后下载全挂 | `svpn up` 是不是没跑（SNI 模式必须常驻）；443 有没有绑上（看启动日志那行「SNI 透明转发」） |
| 卸载后网页还是全挂 | 系统代理没还原：`python -m svpn system off` 或手动把 `ProxyEnable` 设 0 |
| 脚本报「禁止运行脚本」 | `-ExecutionPolicy Bypass`，或 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |

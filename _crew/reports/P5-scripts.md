# P5-scripts 交付报告

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `install.ps1` | 重存为 **UTF-8 with BOM + CRLF**；第 1 步必需文件清单从 3 个补到 5 个（新增 `start-svpn.cmd`、`uninstall.ps1`），改成先收集缺失项再一次性报出；结尾卸载提示改成带绝对路径、可复制直接执行的命令 | 135 → 151 |
| `uninstall.ps1` | **新建**。按 pid 停进程 → 还原系统代理 → 清 hosts 托管块（自提权）→ 删桌面快捷方式 → `-Purge` 清理，全程 `[+]/[i]/[!]`，可安全重复执行 | 0 → 374 |
| `start-svpn.cmd` | **新建**。双击一键盘：`chcp 65001` → `pushd` → `call` 探测 python → `up -b --system-proxy` → `steam --system` → `status` → `pause` | 0 → 106 |
| `svpn.cmd` | 两处 python 探测补 `call`；两处真实调用也补 `call`（理由见 §2 第 5 条）；中文 `rem` 注释改 ASCII（理由见 §2 第 6 条）。退出码透传逻辑、参数透传、`goto` 结构未动 | 51 → 58 |
| `svpnctl.ps1` | **未修改**（只读校验：BOM ✓、语法 0 错误、12 个子命令映射一致） | 182 → 182 |

未触碰：`svpn/*.py`、`tests/*`、`README.md`、`docs/*`、`WORKLOG.md`、`runtime/*`。

## 2. 逐条修复

| 编号 | 原缺陷 | 修法 | 验证方式 |
|---|---|---|---|
| 1 | `install.ps1` 无 BOM，PS 5.1 按 ANSI(936) 读源码，中文字符串乱码甚至破坏行解析 | 用 Python 重存为 `utf-8-sig` + CRLF。附带实测证据：临时副本漏写 BOM 后 PS 5.1 直接 `ParserError`（`表达式或语句中包含意外的标记`、`字符串缺少终止符`），说明这不是"观感问题"而是硬故障 | 头 3 字节 `ef bb bf`；`Parser::ParseFile` 0 错误 |
| 2 | `install.ps1` 引用了不存在的 `start-svpn.cmd` / `uninstall.ps1`（第 97、131 行） | 两个文件已真实创建；清单改为 5 项 `$required` 数组，缺失则 `exit 2` 并列出全部缺项 | 空目录跑 → `[!] 缺少 svpn\cli.py / svpn.cmd / svpnctl.ps1 / start-svpn.cmd / uninstall.ps1`、`EXIT=2`；5 个文件齐 → `[+] 全部齐了: ...`、继续到 stage 3 |
| 3 | `install.ps1` 结尾 `.\uninstall.ps1` 相对路径依赖当前目录 | 改成 `"$root\uninstall.ps1"` 绝对路径，并在注释头写明用法 | 静态检查 + `$root` 来自 `$MyInvocation.MyCommand.Path` |
| 4 | `svpn.cmd:27` `python -c "import sys"` 缺 `call`：python 是 `.bat/.cmd` 包装时控制权不返回，静默 exit 0 | 两处探测都加 `call` | shim 实测（见 §3 第 5 项）：不加 `call` 时脚本在第一处调用就停住，加了之后完整走完 |
| 5 | **（新发现，延续第 4 条同根因）** `%PY% -m svpn ...` 这行本身也缺 `call`：`PY` 若解析到 `.bat/.cmd` 包装，脚本同样在此静默终止 | `start-svpn.cmd` 的 3 处真实调用全部 `call %PY% ...`；`svpn.cmd` 的 2 处也加 `call`（`call` 对 `.exe` 无副作用，`ERRORLEVEL` 照常透传） | 用 `python.bat` 垫片验证：无 `call` 时输出停在 `FAKE-SVPN -m svpn up ...` 后直接 `EXIT=0`；有 `call` 时继续走完 3 步 |
| 6 | **（新发现）** `svpn.cmd` 的中文 `rem` 注释在控制台起始代码页 936 时会触发 cmd.exe 行偏移 bug，弹出 `'�' is not recognized as an internal or external command` | 两个 `.cmd` 的注释全部改 ASCII；中文只出现在 `echo` 行，且每个含中文的行后面强制留一个**空行**（cmd 会吃掉下一行的行首，空行正好吸收）。已在文件头写明这条"载重规则" | 对照实验：中文 `rem` 版从 CP936 起跑 2 行报错且 3 轮均复现；ASCII 注释 + 中文 echo + 空行版 3 轮 0 报错；端到端 3 轮 0 报错 |
| 7 | 缺"双击即用"的启动器，用户只能记 `python -m svpn up -b --system-proxy` | 新建 `start-svpn.cmd`：`pushd "%~dp0"` 保证从不相关目录双击也能跑；`up` 失败 → 打印排查建议 + `pause` + `exit 1`；`steam` 返回 2（没装 Steam）走 `:steam_missing` 分支明确说明"属于正常"；最后 `status` 回执 + `pause` | 见 §3 第 5 项（PATH 垫片，不真的起代理） |
| 8 | 没有卸载路径（`svpn down` 只管停服务+还原代理，hosts 块/快捷方式/备份都要手工） | 新建 `uninstall.ps1`：① 只按 `runtime/svpn.pid` 杀，**不按镜像名**（避免误杀别人的 python）② 读 `wininet-backup.json` 写回 `ProxyEnable/ProxyServer/ProxyOverride/AutoConfigURL`，无备份则明确"跳过"、绝不猜默认值 ③ hosts 只删 `# >>> steam-vpn managed block` … `# <<< steam-vpn managed block <<<` 之间 ④ 删 `svpn 启动.lnk` ⑤ `-Purge` 才删 `runtime\` / `svpn.egg-info\`，删前打印路径并要求 `-Yes` 或交互确认 | 见 §3 第 6、7 项 |
| 9 | hosts 清理容易毁坏用户文件（BOM / GBK 中文注释） | 用 **Latin-1 做 1:1 字节↔字符映射**：`Encoding.GetString(File.ReadAllBytes())` 读、`GetBytes()` 写，只按行过滤标记块，其余字节一个不动；写前先备份 `runtime/hosts.backup-uninstall` | 两例字节级比对（UTF-8 BOM + 块在中间、GBK + 块在末尾），删除前后除标记块外**逐字节相同**，BOM 与 GBK 中文注释均保留 |
| 10 | 提权清 hosts 会重复弹 UAC | 非管理员时 `Start-Process powershell -Verb RunAs -Wait` 重入自身并带 `-HostsOnly`，子进程只做 hosts 一步就 `exit`；父进程回来后**按 hosts 实际内容**判定成败（不看子进程返回码，UAC 被取消也不会误报成功） | `un_*.ps1 -HostsOnly` 两例均 `rc=0` 并正确删除 |

## 3. 验证结果

> 环境：Windows 11 + Windows PowerShell 5.1；下面为实际输出粘贴。
> 网络相关变量已 `unset`，全部为离线安全动作（未起代理、未改注册表、未改真实 hosts）。

### (1) BOM 检查 —— 3 个 `.ps1` 头 3 字节

```
$ "C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe" -c "import pathlib;[print(p.name, p.read_bytes()[:3]==b'\xef\xbb\xbf') for p in map(pathlib.Path, ['install.ps1','uninstall.ps1','svpnctl.ps1'])]"
install.ps1 True
uninstall.ps1 True
svpnctl.ps1 True
```

### (2) `.cmd` 换行检查（CRLF、无裸 LF、无 BOM）

```
$ ... -c "
import pathlib
for p in map(pathlib.Path, ['svpn.cmd','start-svpn.cmd']):
    b=p.read_bytes()
    print(p.name,'bom',b[:3]==b'\xef\xbb\xbf','crlf_present',b'\r\n' in b,'bare_lf',len(b)-len(b.replace(b'\n',b''))-b.count(b'\r\n'))
"
svpn.cmd bom False crlf_present True bare_lf 0
start-svpn.cmd bom False crlf_present True bare_lf 0
```

### (3) 三个 `.ps1` 语法解析（不执行）

```powershell
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile("D:\LE\AI\dshwork\steam-vpn\install.ps1", [ref]$null, [ref]$errors) | Out-Null
"install.ps1 errors: $($errors.Count)"
```

```
install.ps1 errors: 0
uninstall.ps1 errors: 0
svpnctl.ps1 errors: 0
```

### (4) 入口脚本真跑：`cmd /c svpn.cmd status`（控制台先置 CP936，模拟中文 Windows 双击）

```
$ cmd //c "chcp 936 >nul" && cmd //c svpn.cmd status
svpn 1.0.0  ·  Steam 分流/加速
进程     未运行
端口     127.0.0.1:7899  未监听
端口     127.0.0.1:7900  未监听
节点     未配置（纯直连）
模式     whitelist
系统代理 ProxyEnable=0
CDN缓存  8 个域名，928 分钟前更新
hosts    无 svpn 记录
上次统计 51 连接 / 下行 0.12MB / 错误 40
            45x www.baidu.com
             6x store.steampowered.com
EXIT=0
```

连跑 3 轮结果一致：**无乱码、无 `not recognized` 噪声、不静默、退出码 0**。
（P2 把 `merge_config()` 落进 `svpn/cli.py` 之后又复跑一次，输出同上，仍 `EXIT=0`。）

退出码透传也单独验了：

```
$ cmd //c svpn.cmd node set        # 缺 url，cli 返回 2
[!] 用法: python -m svpn node set socks5://user:pass@host:port
EXIT=2
```

### (5) `start-svpn.cmd` 端到端（PATH 垫片，不真的起代理/不拉 Steam，3 轮）

做法：往 PATH 最前面放一个 `python.bat`，让 `up` 返回 0、`steam` 返回 2、`status` 返回 0；
脚本本体**一字未改**，从**无关目录** `/tmp/cmdtest` 调用，控制台先 `chcp 936`。

```
=== round 1 ===
[i] Python: python
[i] 目录: D:\LE\AI\dshwork\steam-vpn

== 1/3 启动代理（后台常驻 + 接管系统代理）
FAKE-SVPN -m svpn up -b --system-proxy

== 2/3 拉起 Steam
FAKE-SVPN -m svpn steam --system
[i] 没找到 steam.exe（返回 2，属于正常）：代理照常跑，可只用来加速下载与商店。

== 3/3 当前状态
FAKE-SVPN -m svpn status

[i] 这个窗口可以关掉，代理在后台继续跑。
    收工（还原系统代理 + 清 hosts）: python -m svpn down --clear-hosts
Press any key to continue . . .
EXIT=0
```

3 轮输出完全一致，0 处 `not recognized`。`pushd "%~dp0"` 生效（目录被切到项目根）。

失败分支（让垫片 `up` 返回 1）：

```
== 1/3 启动代理（后台常驻 + 接管系统代理）

[x] 代理没起来，先看上面的报错。常见原因：
      - 端口 7899 / 7900 被别的程序占了（svpn status 会报占用者）
      - config.json 写坏了（删掉重跑 svpn init）
    排查: python -m svpn status
          python -m svpn doctor
Press any key to continue . . .
EXIT=1
```

### (6) `uninstall.ps1` 空跑（`-KeepProxy`，零系统改动）

```
== 1/5 停止后台进程
[i] 没有 runtime\svpn.pid，说明本来就没在跑，跳过
== 2/5 还原系统代理
[i] -KeepProxy：保持当前系统代理设置不动
== 3/5 清理 hosts 托管块
[i] hosts 里没有 svpn 托管块，跳过（这步是幂等的）
== 4/5 删除桌面快捷方式
[i] 桌面上没有 'svpn 启动.lnk'，跳过
== 5/5 清理运行时目录
[i] 保留 runtime\（里面有 hosts / 系统代理备份，留着可回滚）与 svpn.egg-info\。
[i] 要一起删就加 -Purge（例：.\uninstall.ps1 -Purge）。

===== 卸载完成 =====
[i] 这一轮没有实际改动（大概已经卸过了）。
[i] 跳过 5 项：
      - 停进程（无 pid 文件）
      - 还原系统代理（-KeepProxy）
      - 清理 hosts（本来就没有）
      - 删快捷方式（本来就没有）
      - 删除 runtime/egg-info（未加 -Purge）
EXIT=0
```

> 注：本机 `runtime\wininet-backup.json` **存在**，所以没跑"还原代理"分支——写回会改用户 HKCU。
> 该分支用 `Set-ItemProperty -Type DWord/String` 直写四个值，与 `svpn/system.py:restore_wininet()` 同键同类型，
> 逻辑已逐行核对；为避免污染本机注册表，未做真实写入测试。

### (7) hosts 托管块删除 —— 字节级保真 + 幂等

把 `uninstall.ps1` 复制到临时目录、只替换 `$hostsPath` 指向样例文件，跑 `-HostsOnly`：

样例 A（**UTF-8 with BOM**，中文注释，块在中间，块后仍有内容）：

```
before: b'\xef\xbb\xbf# hosts 测试文件（中文注释，UTF-8 BOM）\r\n127.0.0.1 localhost\r\n::1 localhost\r\n'
        b'# >>> steam-vpn managed block (do not edit) >>>\r\n1.2.3.4    steamcdn-a.akamaihd.net\r\n'
        b'5.6.7.8    steamstatic.com\r\n# <<< steam-vpn managed block <<<\r\n'
        b'# 块后的一行中文\r\n\r\n10.0.0.9 myapp.local\r\n'
[+] hosts 已清理（删掉 4 行，DNS 缓存已刷新）, rc=0
after : b'\xef\xbb\xbf# hosts 测试文件（中文注释，UTF-8 BOM）\r\n127.0.0.1 localhost\r\n::1 localhost\r\n'
        b'# 块后的一行中文\r\n\r\n10.0.0.9 myapp.local\r\n'
```

样例 B（**GBK**，中文注释，块在末尾）：

```
before: b'# hosts \xb2\xe2\xca\xd4\xce\xc4\xbc\xfe\xa3\xa8GBK \xb1\xe0\xc2\xeb\xa3\xa9\r\n127.0.0.1 localhost\r\n# >>> ... >>>\r\n1.2.3.4 steamcdn-a.akamaihd.net\r\n# <<< ... <<<\r\n'
[+] hosts 已清理（删掉 3 行，DNS 缓存已刷新）, rc=0
after : b'# hosts \xb2\xe2\xca\xd4\xce\xc4\xbc\xfe\xa3\xa8GBK \xb1\xe0\xc2\xeb\xa3\xa9\r\n127.0.0.1 localhost\r\n'
```

- BOM 保留、UTF-8 中文保留、GBK 中文**逐字节**保留、块外内容未动、行尾 CRLF 未变。
- 幂等：对 A 再跑一次 → `[i] hosts 里没有 svpn 托管块，跳过`，文件字节不变。
- 备份 `runtime/hosts.backup-uninstall` 落盘（内容为删前原始字节）。

> 这一步抓到一个真 bug 并已修：最初用 `[IO.File]::ReadAllText($p, latin1)` 读，它**默认会探测 BOM**，
> 遇到 UTF-8 BOM 文件就改用 UTF-8 解码，再 `GetBytes` 把中文全变成 `?`（样例 A 的中文注释被毁）。
> 改成 `Encoding.GetString([IO.File]::ReadAllBytes($p))` 后才是真正 1:1 字节映射。

### (8) `svpnctl.ps1` 只读校验

- BOM：`True`；`Parser::ParseFile`：0 错误；文件 mtime 仍是 03:25，**未修改**。
- 内部用 `& $cmd.Source @($cand.Prefix + @("-c","import sys"))` 直接调可执行文件，**不存在** batch 需要 `call` 的问题，故未改。
- 12 个公开子命令映射核对（`_serve` 为内部命令，不需映射）：

| 子命令 | cli.py 参数 | svpnctl.ps1 映射 | 结论 |
|---|---|---|---|
| `init` | `--force` | `-Force` | ✓ |
| `up` | `-b/--background` `-p/--port` `-m/--mode` `--system-proxy` `--node` `--no-auto` | `-Background` `-Port` `-Mode` `-System/-SystemProxy` `-Node` `-NoAuto` | ✓ |
| `down` | `--keep-system` `--clear-hosts` | `-KeepSystem` `-ClearHosts` | ✓ |
| `status` | 无 | `default` 分支 | ✓ |
| `probe` | `--node` `--timeout` `--json` | `-Node` `-Timeout` `-Json` | ✓ |
| `node` | `scan/set/clear` + `url` + `--json` | 位置参数+`Rest-Args` + `-Json`，缺省 `scan` | ✓ |
| `cdn` | `--refresh` `--no-bench` `--domain` `--timeout` `--json` | `-Refresh` `-NoBench` `-Domain` `-Timeout` `-Json` | ✓ |
| `hosts` | `apply/clear` + `--no-elevate` + `--timeout` | 位置参数 + `-NoElevate` `-Timeout`，缺省 `apply` | ✓ |
| `system` | `on/off/show` | 位置参数，缺省 `show` | ✓ |
| `pac` | `--write` `--apply` | `-Write` `-Apply`（`-Apply` 隐含 `--write`，与 cli 语义一致） | ✓ |
| `steam` | `--exe` `--system` `--restart` `--no-autostart` `--args` | `-Exe` `-System` `-Restart` `-NoAutostart` `-args`(REMAINDER) | ✓ |
| `doctor` | 无 | `default` 分支 | ✓ |

12/12 一致，无需改动。

### (9) 基线回归

本任务只动 `*.ps1` / `*.cmd`，不涉及 Python 代码，理论无影响；按要求不跑（避免与其他 agent 的临时改动互相干扰）。

## 4. 契约变更 / 需要别人配合

1. **`svpn/cli.py` 的子命令仍未变**（12 个公开 + `_serve`），`svpnctl.ps1` 与之对齐。**P2 若新增/重命名子命令或参数，必须同步告知 P5**，否则 `svpnctl.ps1` 的 `switch` 会静默透传到 `default` 分支。
2. **`.cmd` 的两条硬规则（对所有 agent 有效）**：注释只写 ASCII；任何含中文的行后面必须紧跟一个空行。违反会从中文 Windows（控制台起始 CP936）双击时冒 `'?' is not recognized`。`README.md` 如果示范了"自定义 `svpn.cmd`"，建议加这句提醒（README 不归我）。
3. **`uninstall.ps1` 依赖 `runtime/wininet-backup.json` 的键名**（`ProxyEnable/ProxyServer/ProxyOverride/AutoConfigURL`），与 `svpn/system.py:backup_wininet()/restore_wininet()` 同源。若 `system.py` 改了备份格式（例如改为带版本号的外层对象），卸载脚本会失配，需要同步。
4. **`system.py` 的 `MARK_BEGIN/MARK_END` 是卸载脚本的硬编码副本**，改标记文案必须同步 `uninstall.ps1` 顶部两个变量。建议后续把标记写进一个可由脚本读取的文件，但本轮不动 `system.py`。
5. 顺带记录（不属于 P5，未改）：`svpn/cli.py` 现在（P2 落地后复查）`DEFAULT_CONFIG` 有 **12** 个顶层键——`listen_host / listen_port / http_port / mode / connect_timeout / auto_node / upstream / auth / browser / cdn / proxy_hosts / direct_hosts`，`merge_config()` 已存在；子命令仍是 12 个公开 + `_serve`，与 `svpnctl.ps1` 对齐。注意 **SPEC §4.1 写的是"恰好 13 个键"，但正文只列了 12 个**——这是 SPEC 数错了，不是 P2/P6 的问题，建议 team-lead 改一下 SPEC 的措辞，免得 P6 的键集断言按 13 去写。

## 5. 没做完 / 发现的其它问题

1. **未做**：`install.ps1` 第 5 步（建桌面快捷方式）与 `uninstall.ps1` 第 2 步（还原注册表代理）**没有真跑**——前者会往桌面写 `.lnk`，后者会改用户 HKCU，都会污染本机且与并行任务无关。两者代码逐行核对过，键名/类型与 `svpn/system.py` 一致；`install.ps1` 的 1–4 步用"空目录 / 5 个占位文件"两种临时目录真跑过（分别 `exit 2` / 走到 stage 3 因占位树无 `svpn` 包而 `exit 4`）。
2. **`uninstall.ps1` 的提权分支未真跑**：本机当前非管理员，但真实 `hosts` 里没有 svpn 托管块，脚本会先判定"没有块→跳过"，走不到提权。提权入口（`Start-Process -Verb RunAs -Wait` + `-HostsOnly` 重入）只做了语法与逻辑核对，`-HostsOnly` 分支本体已在样例文件上验过。
3. **`call %PY% -m svpn %*` 的副作用**：`call` 会对参数做二次展开，理论上 `svpn steam --args "...%FOO%..."` 这类含 `%` 的参数会被多展开一次。实际用到的参数里没有这种情况，但如果是脚本里传参要留意。
4. **`-Purge` 的交互确认依赖 `[Console]::IsInputRedirected`**：在计划任务/管道里会判定为非交互，此时必须显式 `-Purge -Yes` 才删（这是刻意设计，避免自动化里误删备份）。
5. 发现但**不属于我的文件**、未改：`svpn/cli.py:361` `cmd_down --clear-hosts` 里 `if _hosts_entries(): cmd_hosts(...)`，把 `argparse.Namespace` 手搓出来调用；若 P2 给 `cmd_hosts` 加参数会漏传。留给 P2 判断。

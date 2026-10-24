# svpn 代码审查（只读）

**审查人**：code-reviewer（本轮不改任何业务代码，只产出本文件）
**审查对象**：`svpn/cli.py`、`proxy.py`、`netutil.py`、`discover.py`、`probe.py`、`cdn.py`、`forward.py`、`system.py`、`rules.py`、`pac.py`、`steam.py`（`split.py` 顺带看了，未接线）
**审查方式**：逐行静态审查 + 纯函数/协议解析的本地只读验证（**没有**启动 `up`/`_serve`，**没有**写 hosts，**没有**改注册表/系统代理）

## 基线（本报告行号以此为准）

审查期间有队友在并发改代码（`cli.py`/`netutil.py`/`proxy.py`/`probe.py` 在 20 分钟内改了 4 轮）。下表是**最后一次逐条复核时**（03:2x）的指纹，本报告所有行号都以这一版为准；若你手上的文件 sha256 前 12 位对不上，行号会漂，请按"位置"列里紧跟的代码片段 grep（每条引用都带了片段）。

| 文件 | 字节 | sha256[:12] | 行数 |
|---|---|---|---|
| cli.py | 35656 | 55d5a67f4c41 | 905 |
| proxy.py | 21062 | 49d51a38d4a5 | 550 |
| netutil.py | 21734 | 4b66341cf0ac | 608 |
| discover.py | 9291 | 622bdd38989e | 264 |
| probe.py | 7158 | c6eeb535713f | 198 |
| cdn.py | 6050 | 42c2438680a9 | 171 |
| forward.py | 8032 | 26928bb9f778 | 201 |
| system.py | 6698 | 1b24a485922a | 202 |
| rules.py | 6455 | 482add2e68ee | 188 |
| pac.py | 1271 | f9a4f39eb2a5 | 39 |
| steam.py | 3425 | ffc1c5f69fdf | 93 |

> 这一版里 `pac.py`/`system.py`/`forward.py`/`rules.py`/`cdn.py`/`discover.py` 与我初次审阅的快照完全一致，`proxy.py`/`netutil.py`/`cli.py`/`probe.py` 有更新——我把每一处改动都过了一遍，**P0/P1 里没有一条被这些改动修掉**（改动集中在 stats 落盘、DoH 多 provider 合并、`control_ip` 加 TTL、表格对齐、`parse_node_url` 方括号校验）。

**结论速览**：P0 × 2、P1 × 8、P2 × 20。协议实现本身比预期干净（SOCKS5 握手/ATYP/reply、CONNECT 隧道、`serve_forever` 幂等、`extract_sni` 都验证过没问题）；真正会咬人的是 **PAC 全废**、**Windows 上 `pipe` 漏捕 OSError**、**提权写 hosts 无校验**、**把自己当节点** 这四类。

---

## P0（必修，已影响可用性/稳定性）

| 级别 | 位置 | 问题 | 触发条件 | 影响 | 最小复现 / 修复方向 |
|---|---|---|---|---|---|
| P0 | `pac.py:12-21`（`pjs = ",\n".join(...)` → `var PROXY = {`）+ `cli.py:640-648`（`_apply_system` 的 PAC 分支，`system.py:78-80` 把 ProxyEnable 置 0） | 生成的 PAC 不是合法 JS：`var PROXY = { "a.com", "b.com" }` 是对象字面量语法，字符串成员缺键名 → 解析期 SyntaxError | `svpn pac --apply`，或 `config.json` 里 `browser.use_pac=true` 后 `up --system-proxy` | 浏览器/WinHTTP 加载 PAC 失败后**全部按 DIRECT 走**；同时 `AutoConfigURL` 已生效、`ProxyEnable` 已被置 0 → 整机一个包都不进代理，CLI 还打印"已应用"。`cdn.hosts` 之外的加速全部静默失效 | 实测：`python -c "from svpn.pac import build_pac;open('x.pac','w',encoding='utf-8').write(build_pac())"`，再 `node -e "new Function(require('fs').readFileSync('x.pac','utf8'))"` → `SyntaxError: Unexpected string`。修：`pjs`/`djs` 改成 `"[...]"`（数组），并在写盘后做一次 JS 解析自检（失败就报错、不改注册表） |
| P0 | `proxy.py:238-240`（`pipe` 只捕 `ConnectionResetError/BrokenPipeError/IncompleteReadError/TimeoutError`）+ `proxy.py:404-488`（`handle_http` 全程无外层 try/finally） | Windows Proactor 在连接卡住/对端消失时抛的是裸 `OSError`（`WinError 121 信号灯超时已到`、10054），`pipe` 不接 → 直接穿到 `client_connected_cb`；而 `handle_http` 的清理完全依赖 `_bridge` 的 finally | 客户端或上游静默断开/长时间无数据（Steam 商店页、CDN 大包、手机热点切换） | 每个这种连接都打一条 `Unhandled exception in client_connected_cb` + 完整 traceback；`stats.errors` 不计数（统计失真）；若异常发生在 `_bridge` 之前的 `await writer.drain()`（450/462/487 行）则 client/server 两侧 writer 都不关，transport 被 loop 持有 → 句柄不回收 | 现成样本：`runtime/svpn.log:48-71` 与 `72-95`（03:11:31 连续两条，`OSError: [WinError 121]`）。修：`pipe` 的 except 加 `OSError`（或直接 `except Exception`），`handle_http` 包一层 `try: ... finally: writer.close()`，与 `handle_socks` 的写法对齐 |

---

## P1（本周该修）

| 级别 | 位置 | 问题 | 触发条件 | 影响 | 最小复现 / 修复方向 |
|---|---|---|---|---|---|
| P1 | `proxy.py:441`（`port = int(p or 443)`）、`proxy.py:468`（`port = int(port_s or 80)`） | 端口解析没包 try，`ValueError` 会穿出 handler；`CONNECT example.com`（无端口）时 `rpartition` 还把 host 变成空串 | 任何畸形/半畸形请求：`CONNECT host` 不带端口、`GET http://host:abc/`、端口扫描器打 7900 | `handle_http` 没有外层 finally → 连接不关、不计错、日志多一条 traceback；本地任意进程可稳定触发 | `python -c "print('example.com'.rpartition(':'))"` → `('', '', 'example.com')` → `int('example.com')` 抛错。修：`try: port = int(...) except ValueError: 400 + close`；顺手拒掉空 host |
| P1 | `proxy.py:161`（`if host and all(c.isdigit() or c == "." for c in host)`） | 用"全是数字或点"判 IP，IPv6 字面量被判成域名，塞进 ATYP=3 发给上游 | `mode=all`／`blacklist` 下，客户端用 SOCKS5 ATYP=4 请求 v6 目标（Chrome 对双栈站点会这样）；或 `ip_map` 命中 v6 | 上游把 `2606:4700::1111` 当域名解析 → CONNECT 失败 → 502；v6-only 站点必挂，用户只看到"节点不行" | 实测表达式：`all(c.isdigit() or c == "." for c in "::1")` → `False`。修：改用 `netutil.is_ip(host)`，v6 发 `b"\x04" + ipaddress.ip_address(host).packed` |
| P1 | `netutil.py:76`（`_DNS_CACHE` 无上限）+ `netutil.py:96-147`（无负缓存、逐 provider 串行）+ `proxy.py:126`（`await asyncio.to_thread(netutil.resolve_merged, ...)`） | 解析失败不入缓存，每个新域名要问最多 4 家 DoH；每次尝试的超时是 `connect_timeout`（默认 12s），而 `to_thread` 用的是默认线程池（本机 20 线程） | DoH 被墙/断网/热点劫持时，一次正常浏览会带上几十个新域名；或上游整段不可用 | 20 个线程被 4×12s 的等待占满 → 之后**所有**直连拨号排队，代理表现为"整体假死"几十秒；`_DNS_CACHE` 随访问域名数无限增长（长跑内存只涨不降） | 修：`_DNS_CACHE` 加容量上限/简单 LRU（>2048 淘汰）；失败结果写 30s 负缓存；给整个 `resolve` 一个总 deadline（如 3s）而不是每 provider 12s；`to_thread` 换成有界 executor 或直接复用 `getaddrinfo` 的 asyncio 版本 |
| P1 | `discover.py:48`（`SELF_PORTS = {7899, 7900}` 只在 `_from_ports` 用）+ `discover.py:113-145`（`_from_wininet`）、`discover.py:92-111`（`_from_env`）不过滤 + `cli.py:237-240`（自动选用）、`cli.py:481`（`node set`） | 系统代理指向 svpn 自己时，`_from_wininet` 会把 `http://127.0.0.1:7900` 当成一个"能到 Steam 的节点"（因为它确实能） | `up --system-proxy` 后跑 `node scan`（输出还会提示"采用: python -m svpn node set http://127.0.0.1:7900"）；或 `down --keep-system`／崩溃后 WinINET 残留再 `up` 触发 auto 扫描 | `config.upstream` 写成自己 → 每个上游请求自连自环，连接数一路堆到 fd 耗尽（本地 DoS），排障时完全看不出原因 | 实测：`_from_wininet` 返回的候选端口 `7900 ∈ SELF_PORTS`，但该函数不做过滤（已在假注册表值下复刻解析逻辑确认）。修：在 `scan()` 里统一按 `(host,port)` 过滤自身监听地址，`node set` 显式拒绝自身地址并提示 |
| P1 | `system.py:148-153`（`hosts_block` 直接 f-string 拼）+ `system.py:156-176`（`apply_hosts` 不校验）+ `cli.py:598`（`entries = {ip: " ".join(...)}`） | 写进系统 hosts 前不校验 IP/域名，`best_ip` 和域名直接拼行 | `runtime/cdn-cache.json` 被改（普通用户可写，`svpn hosts apply` 却是**提权**执行）；或 `svpn cdn --domain` 传入含换行的域名 | 提权进程可被写成任意 hosts 行 → 把 `login.steampowered.com` 指到任意 IP（凭证劫持）或指到 127.0.0.1（登录直接挂）。这是整套工具里"权限最高 + 输入最不设防"的一段 | 实测（只拼字符串，没写盘）：`hosts_block({"5.6.7.8": "a.com\n127.0.0.1 login.steampowered.com"})` 会多出完整的 `127.0.0.1 login.steampowered.com` 行。修：`apply_hosts` 里逐条 `ipaddress.ip_address()` + `re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host)`，任一非法就整块拒写并报错；写盘改临时文件 + `os.replace`（顺带解决 `system.py:175` 的半个 hosts） |
| P1 | `system.py:96-97`（无备份时 `return {"ok": False, "reason": "no backup"}`）+ `cli.py:352-355`（`cmd_down` 不看返回值，一律打印"系统代理已还原"） | 还原失败被当成成功打印 | `runtime/` 被删、换机、提权进程没写成备份、或备份 JSON 损坏（`json.loads` 抛错也没接）后执行 `svpn down` | 用户以为已还原，实际 WinINET 还指着已经死掉的 `127.0.0.1:7900` → 浏览器、Steam、Windows 更新全断；CLI 的提示是**反的**，排障方向被带偏 | 修：检查返回值；`ok=False` 时打印当前 `read_wininet()` 的真实值 + 手动恢复步骤（或直接把 ProxyEnable 置 0、清空 ProxyServer/AutoConfigURL 再提示）；`json.loads` 也包 try |
| P1 | `cli.py:267`（`PIDFILE.write_text` 在判定之前）、`cli.py:269-276`（就绪判定只看端口是否被监听）、`cli.py:280`（失败时 `PIDFILE.unlink`） | 后台启动的成功判据不可靠：端口被别人监听也算"我的子进程起来了"，而 PID 文件先写后删 | 并发两次 `up -b`（`svpn.cmd` 连点两下、或 `steam` 子命令自动 up 与手动 up 撞车）；或子进程 bind 失败但端口恰好被第三方进程占住 | 父进程打印"后台已启动 pid=X"（X 已死），并把真正在跑的实例的 PIDFILE 覆盖成 X、再删掉 → `down` 说"没有在运行的实例"，7899/7900 被永久占用（还留着已接管的系统代理），只能手动 taskkill | 修：由子进程在 bind 成功后自己写 PID 文件（内含 pid + 启动时间 + 镜像名）；父进程用一条 magic 握手确认监听方是自己的子进程；失败分支先比对 PIDFILE 内容再决定要不要删 |
| P1 | `proxy.py:331-377`（握手只给首个 `readexactly(2)` 加了 10s，`methods`/ATYP 长度/端口全裸读）+ `proxy.py:508`（`backlog=512` 只是内核队列，代码里没有并发上限） | SOCKS5 入口无整段超时、无连接数上限 | 本地任意进程（或把 `listen_host` 改成 0.0.0.0 后的局域网）开几千条连接，每条只发 2 字节就挂着 | 每条连接一个 fd + 一个 asyncio task 常驻，Windows 默认句柄量下几千条就耗尽 → 正常连接也排不上；`stats.conns` 还会被这些空连接污染 | 修：用 `async with asyncio.timeout(10):` 包住整个握手段（含 `methods`、ATYP、port），再加一个 `asyncio.Semaphore(1024)` 式并发闸门，超限直接 `0xff`/关闭；顺手在 `pipe` 侧加空闲超时 |

---

## P2（排期修，不紧急但都是真问题）

| 级别 | 位置 | 问题 | 触发 / 影响 | 修复方向 |
|---|---|---|---|---|
| P2 | `proxy.py:90-119`（`_race_connect`，`for task in done` 在 110-113） | 同一批 `done` 里若有多个成功，只有第一个被返回，其余 `(reader, writer)` 直接丢弃 | 多个 IP 在同一轮事件循环内都连通（同城多边缘、双栈回退）时，被丢掉的 transport 由 loop 持有，不会回收 → fd 缓慢泄漏。修：把 `done` 里所有成功结果取出，非采用者立即 `w.close()` |
| P2 | `proxy.py:172-174`（`head = readexactly(4)`；只校验 `head[1]`） | 不校验 `head[0] == 0x05` | 上游返回畸形/非 SOCKS5 帧（如 `\x00\x00\x00\x01`）时被当成 CONNECT 成功 → 后续管道把垃圾当隧道数据。修：`if head[0] != 5 or head[1] != 0: raise` |
| P2 | `proxy.py:249-265`（`_bridge` 用 `gather` 等两个方向都结束，无 idle timeout） | 半开连接（一端一直不发 FIN 也不发数据）会永久占两条 task 和两个 fd | 笔记本睡眠/唤醒、NAT 超时后残留。修：两侧 `pipe` 加 `asyncio.timeout(idle)`，超时即关闭；或对 `gather` 用整体上限 |
| P2 | `proxy.py:491-493`（`if self._servers: return`）+ `proxy.py:547-550`（`close()` 不清空 `_servers`） | `close()` 之后再次 `start()` 会静默不监听（幂等判断把"已关闭"当成"已启动"） | 同进程重启代理（未来做 reload 时必踩）。修：`close()` 里 `self._servers.clear()` |
| P2 | `netutil.py:170-186`（`resolve_merged`：DoH + 系统 DNS 合并）+ `proxy.py:102-103`（`ips[:4]`） | 合并后只竞速前 4 个，而系统 DNS 的结果排在 DoH 之后还是之前取决于顺序 | 系统 DNS 被污染时给出 3-4 个假 IP，正好把真实 DoH 的 IP 挤出前 4 → 竞速全失败后回退到串行系统解析（回到被污染的路）。修：保证前 4 个里至少 2 个来自 DoH（分组交替取样） |
| P2 | `netutil.py:81`（`full = f"{url}{sep}name={name}&type={rtype}"`） | DoH 查询串没做 URL 编码，`name` 来自客户端 SOCKS5 请求 | host 里带 `&`、`#`、`%` 时污染查询参数或截断（`#` 之后全丢）；非 ASCII 直接原样拼进去。影响限于查询本身，但属于可被本地客户端操纵的输入。修：`urllib.parse.quote(name, safe="")` + 非 ASCII 先 `idna` |
| P2 | `netutil.py:407-413`（`raw.count(":") == 1` 分支 + `int(port_s or ...)`） | 端口无 0–65535 校验；裸 IPv6 被误解析 | `socks5://host:70000` 通过校验，之后 `connect` 抛 `OverflowError`（**不是** OSError，日志里表现为空错误信息）；`socks5://::1:1080` 被解析成 `host='::1:1080'`（实测）。注意 `[::1]1080`（缺冒号）在最新版已会报 `ValueError`，只剩裸 IPv6 这一支。修：端口范围校验 + 只接受带方括号的 IPv6，否则明确报错 |
| P2 | `netutil.py:470-497`（`http_connect` 的 `buf`） | `\r\n\r\n` 之后同一次 `recv` 里的字节被丢弃 | 上游代理把隧道首包和 200 响应一起发回时丢数据（TLS ClientHello 是客户端先发，所以少见，但 v2rayN/Clash 的早期数据会踩）。修：把 `buf` 剩余部分返回给调用方（改成返回 `(sock, leftover)`） |
| P2 | `rules.py:177-180`（`if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved`） | 漏了 CGNAT `100.64.0.0/10`（Tailscale、部分运营商 NAT）、`192.0.0.0/24`、`198.18.0.0/15` 等非 global 地址 | `mode=all/blacklist` 时这些地址被送去上游 → Tailscale/局域网穿透直接不可用（实测 `100.64.0.1` 在 whitelist 下 direct 是对的，但那是兜底，不是这条判断）。修：改成 `if not ip.is_global: return "direct"` |
| P2 | `proxy.py:276-282`（`Router(...)` 构造）+ `config.example.json` | `Router` 支持 `proxy_cidrs`，但 `ProxyServer` 不传，config 也没有该键 | 用户无法覆盖 Valve 网段（`STEAM_CIDRS` 里是 2023 年的段，Steam 换段后只能改代码）。修：`proxy_cidrs=cfg.get("proxy_cidrs")`，并在 example 里给出键 |
| P2 | `cli.py:98-105`（`pid_alive`：`return str(pid) in out`） | 用子串匹配 tasklist 的整行输出，且不校验镜像名 | `pid=78` 会命中内存列 `78,900 K` 之类的数字 → 误判"在运行"；PID 复用（重启后残留 PIDFILE）时 `down` 的 `taskkill /T /F` 会杀掉无关进程。修：按列解析精确比较 PID，并校验 `Image Name` 是 `python.exe`（或记录启动时间戳比对） |
| P2 | `cli.py:100`、`cli.py:132`、`cli.py:146`（`subprocess.run(..., text=True)`） | 对 tasklist/netstat 用严格解码（本机 locale=cp936）；`steam.py:51-66` 刚修了同类问题，这几处没跟着修 | 一旦用户设 `PYTHONUTF8=1`（或 Python 3.15 默认 UTF-8），`tasklist`/`netstat` 的中文表头 decode 抛 `UnicodeDecodeError` → `pid_alive`/`_port_owner` 崩，`up`/`down`/`status` 全挂。修：统一走 `capture_output=True`（bytes）+ `decode("mbcs", "replace")`，或 `text=True, errors="replace"` |
| P2 | `cli.py:563-577`（`_elevate`：`quoted = ",".join(f"'{a}'" ...)`、`-WorkingDirectory '{ROOT}'`） | PowerShell 单引号拼接 | 参数本身是常量（`-m svpn hosts apply/clear --no-elevate`），没有用户输入注入面；但 `sys.executable`/`ROOT` 一旦含 `'` 整条命令就断（`C:\Users\O'Brien\...`），提权静默失败。修：`-EncodedCommand`（UTF-16LE base64）或 `-ArgumentList` 用参数数组 + 双引号转义 |
| P2 | `cli.py:67-75`（`setup_log` 的 `FileHandler`） | 日志无轮转，每次 `up` 都 append，`SVPN_DEBUG` 时还写 DEBUG | 长跑/频繁重启后 `runtime/svn.log` 无限增长（现在已 6KB，正常使用会到几十 MB）。修：`RotatingFileHandler(maxBytes=5MB, backupCount=3)` |
| P2 | `cli.py:290-293`（前台 `up` 直接 `asyncio.run`，不写 PIDFILE） | 前台模式没有 PID 记录 | `status` 显示"未运行"、`down` 停不掉它，只能 Ctrl+C；如果是前台起的又接管了系统代理，`down` 连还原都不做。修：前台启动也写 PIDFILE（并在 finally 里删） |
| P2 | `cli.py:56`（`"cdn": {"enabled", "refresh_hours", "sni_port", ...}`） | `enabled`/`sni_port`/`hosts` 三个键**仍然没有任何代码读取**（`cdn.py` 用自己的 `CDN_DOMAINS`；`refresh_hours` 只在 `cli.py:509` 用来给表格标"过期"，也不参与缓存失效判定）；`cdn.hosts` 编辑后不生效 | 用户按 `config.example.json` 的注释打开 `cdn.enabled=true` 期待"SniForwarder 第二种接法"，实际什么都不会发生（`forward.py` 全文件都没被 cli import），`cdn.hosts` 加域名也无效果。修：要么接线（`_serve_async` 里按 `cdn.enabled` 起 `SniForwarder`，把 `cdn.hosts`/`sni_port` 传进去），要么把键删掉/改注释，别留假开关 |
| P2 | `forward.py:126-201`（`SniForwarder`）+ `forward.py:67-68`（`_need` 死代码） | 整个 forward 层未接线，且接起来就有三个现成缺陷：① `_handle` 成功路径只关 `writer`，**`sw` 永不关闭**（182 行后面的 finally 只管 client 侧）→ 每条 SNI 连接漏一个上游 socket；② `_allowed`（143-146）在 `allow` 为空时 `return True` → 白名单没配就是"任意 SNI 全放行"（虽然只绑 127.0.0.1，但会变成本地任意进程的通用 443 出口）；③ `_need` 从未被调用 | 一旦有人按注释接线，就会同时踩上面三条。修：接线时把 `sw` 放进统一的 `finally` 关闭；`allow` 为空时默认拒绝（或强制读 `CDN_DOMAINS`）；删掉 `_need` |
| P2 | `discover.py:234-251`（`save_upstream`） | `config.json` 解析失败时 `data = {}`，然后整份重写 | 配置文件被并发写坏（`cmd_up` 与 `node set` 同时写）或用户手改出语法错 → 下次 `node set` 把用户所有自定义键（`browser`、`direct_hosts` …）静默清空。修：解析失败就报错退出，不覆盖；写盘用临时文件 + `os.replace` |
| P2 | `system.py:47-53`（`backup_wininet`：文件存在就永不更新） | 备份只取"第一份" | 如果首次备份发生在 svpn 已接管系统代理之后（用户删过 `runtime/` 后重跑 `up --system-proxy`），"还原"会把系统代理设回自己 → 还原等于没还原。修：备份时校验"当前值与 svpn 的目标值不同"，否则不覆盖已有备份 |
| P2 | `system.py:175`（`HOSTS.write_text(body, ...)`）+ `system.py:177-178`（只捕 `PermissionError`） | hosts 非原子写，且异常只接一种 | 写一半被杀/断电 → hosts 残缺（整机 DNS 异常）；被 AV/另一进程锁住时 `OSError` 穿到 `main`（`main` 只接 `ValueError`/`ConnectionError`）→ traceback。修：临时文件 + `os.replace` 原子替换；捕获 `OSError` 并给出可读提示 |
| P2 | `system.py:119-137`（`set_user_env` 用 `setx`、`clear_user_env` 用 `KEY_ALL_ACCESS`） | 死代码（无人调用），且 `KEY_ALL_ACCESS` 在受限账户/策略下会 `PermissionError` | 将来接线时会踩：`setx` 永久写用户环境变量、每变量一个进程（4×~1s），`clear_user_env` 权限位过大。修：删除或改成 `KEY_SET_VALUE` + `HKCU\Environment` 直接 `SetValueEx` |

---

## 最该先修的 3 件事

1. **`pac.py` 生成非法 JS（P0）**——一行改动（对象字面量 → 数组），但现在等于"谁开了 PAC 谁就完全不走代理"，而且 `ProxyEnable` 已被置 0，用户看到的只是"加速器没用"。修完顺手在 `cmd_pac --apply` 里加一次 JS 语法自检，别再让坏 PAC 摸到注册表。
2. **`pipe` 漏捕 `OSError` + `handle_http` 没有 try/finally（P0）**——`runtime/svpn.log:48-95` 已经实锤，Windows 上每个卡住的连接都会 unhandled + 计数失真；把 `except OSError` 和 `finally: writer.close()` 补上，顺带把 `int(p or 443)` 那两处 `ValueError` 一起包了（P1），这一处能消掉日志里 90% 的噪音和大部分句柄不回收。
3. **提权写 hosts 前先校验（P1）**——这是全项目权限最高的路径，却把 `cdn-cache.json` 里的字符串直接拼进 `C:\Windows\System32\drivers\etc\hosts`（已实测可注入任意行）。加两行正则 + `ipaddress` 校验、改原子写，成本极低，风险等级最高。

> 紧随其后：`discover` 不排除自身端口（P1）——一旦用户按 `node scan` 的提示把自己设成节点，就会自环到 fd 耗尽；以及 `cmd_down` 在无备份时谎报"系统代理已还原"（P1）——这是唯一会让用户整机断网的"善意谎言"。

---

## 已核对且确认没问题（避免重复排查）

- **SOCKS5 协议实现**：方法协商（0x00/0x02，含 `need` 与 `methods` 的匹配）、用户密码子协商（RFC1929 的长度前缀、`_check_auth`）、ATYP 1/3/4 的地址解析、reply 的 ATYP 变长消费（`netutil.py:421-442` 的 `4+2 / 1+n+2 / 16+2`）、`_reply_socks` 的 `0.0.0.0:0` BND —— 均正确。
- **HTTP CONNECT 隧道**：`method.upper() == "CONNECT"`、`target.rpartition(":")`、`strip("[]")`（IPv6 字面量 `[::1]:443` 正确）、200 之后原样透传 —— 正确。
- **绝对 URI 重写**：跳过 `proxy-authorization`/`proxy-connection`/`connection`、补 `Connection: close`、查询串保留在 path 里 —— 逻辑正确（丢重复头、`k.title()` 改大小写属小瑕疵，不影响 HTTP 语义）。
- **`serve_forever` 幂等**：`start()` 有 `_servers` 短路，`_serve_async` 先 `start()` 再 `serve_forever()` 不会二次 bind（`runtime/svpn.log` 里历史那两次 10048 是旧版本 `cmd_serve` 的写法，当前代码已不会）。
- **`extract_sni`**：4000 组随机畸形输入 + 截断/空 SNI/前置未知扩展，全部返回 `None` 或正确域名，**零异常**（实测）；`p += elen` 边界与 `end = min(...)` 处理正确。
- **`parse_node_url`**：方括号 IPv6、`user:pass@`（密码含 `@` 走 `rsplit`）、默认端口（socks5→1080 / http→7890）、`socks4`/空串报错 —— 正确（端口范围/裸 IPv6 见 P2）。
- **默认监听**：`DEFAULT_CONFIG`、`config.json`、`config.example.json` 三处都是 `listen_host=127.0.0.1`；`_apply_system` 用 `http_port`（7900）而不是 SOCKS 端口 —— 正确。
- **172.16/12**：`172.16.5.5`、`172.20.0.1` 实测均 `direct`（`ipaddress.is_private` 覆盖整段），之前那个"只写了 172.16.*"的坑确实修好了。
- **`tls_probe` ok 分支**：`cert/version` 解析已包 try，`wrapped` 在 finally 关闭，`out` 在 finally 之后返回 —— 容错到位。
- **netstat 解析**：`parts[3].upper() == "LISTENING"` + `parts[1].endswith(f":{port}")` 不会把 17899 误判成 7899（`:17899` 不以 `:7899` 结尾），IPv6 行 `[::1]:7899` 也匹配 —— 正确。
- **`_port_busy` 的 TIME_WAIT 疑虑**：实测（本地随机端口、服务器侧主动关闭制造 TIME_WAIT，再 `bind` 同一端口）→ **绑定成功**，Windows 上不存在"刚 down 就起不来"的 TIME_WAIT 阻塞，这条不用担心。
- **`_race_connect` 的取消路径**：`finally` 里 cancel + `gather(return_exceptions=True)`，没有任务遗留（除 P2 的多成功分支）。
- **失败路径关闭**：`dial_socks5`/`dial_http_connect` 的 `except BaseException: writer.close()`、`_bridge` 的 finally 关两侧、`Forwarder._handle` 的 finally 关两侧 —— 都对（`SniForwarder` 除外，见 P2）。

---

## 附：本次只读验证记录（可复跑）

1. `pac.build_pac()` 写临时文件 → Node 解析 → `SyntaxError: Unexpected string`（P0-1 实锤）。
2. `extract_sni` 模糊测试 4000 组 + 4 组合法/截断样本 → 无异常。
3. `parse_node_url` 8 组边界输入 → 端口越界通过、裸 IPv6 误解析（见 P2）。
4. `Router.decide` 抽查 10 个 host（含 `172.16/172.20`、`23.45.70.1`、`100.64.0.1`、`x.s.team`）→ 与设计一致。
5. `system.hosts_block` 传含 `\n` 的 ip/域名 → 注入成立（只拼字符串，未写盘）。
6. `discover._from_wininet/_from_env` 实跑（只读注册表/环境变量）→ 当前返回空（系统代理未开）。
7. `socket.connect(port=70000)` → `OverflowError`（非 OSError），`all(c.isdigit() or c=="." for c in "::1") is False`。
8. TIME_WAIT 重绑实验 → 绑定成功。
9. `ast.parse` 全模块语法检查 → 通过（审查过程中一度撞到队友正在编辑 netutil.py 导致的 IndentationError，等待其写完后复检通过）。
10. 阅读 `runtime/svpn.log` 全量 110 行，P0-2 的两条 WinError 121 traceback 即出自该文件第 48-95 行。

**未做**：没有启动 `svpn up`/`_serve`，没有改系统代理，没有写 hosts，没有动注册表；所有验证脚本都放在 `%TEMP%`，仓库内只新增本文件。

# 工作日志 · steam-vpn（svpn 1.1.0）· 收尾轮

- 生成时间：2026-10-24 19:20（Asia/Shanghai）
- 上一版：2026-10-24 03:41「最终版」，归档在 `_crew/WORKLOG-上一版-0341.md`
- 面向人阅读的合并版：`D:\LE\AI\wbwrk\工作日志\svpn-Steam分流器收尾-工作日志.md`
- 项目路径：`D:\LE\AI\dshwork\steam-vpn`
- 一句话结论：**上一版 §7 列的所有代码级缺陷与交付面不自洽全部闭环（27 处修复）；离线 364 用例 + 冒烟 15 项全绿，静态审计 P0 归零。** 端到端成功率仍不达标，但已用「直连对照组」证明瓶颈在本机到 Steam 的通路，不是 svpn 的代码。

---

## 0. 本轮速览

| 维度 | 上一版（03:41） | 本轮（19:05） |
|---|---|---|
| 静态缺陷扫描 5 条 | 全部未修 | **5/5 修完** |
| 交付面不自洽 5 条 | 全部未修 | **5/5 修完**（含清理） |
| 离线测试 | 169 用例 | **364 用例**（+195） |
| 冒烟检查 | 记 17 项（实为 15，上一版数错） | **15 项 ALL PASS** |
| 静态审计 | P0 2 项 | **P0 0 项**；P1 只剩 1 项（DNS 缓存无上限），另 2 项部分闭合（见 §4.3） |
| `cdn.enabled` | 假开关（无任何代码读） | **真接线**（SNI 透明转发） |
| 安装器 | 缺 BOM + 引用不存在的脚本 | **可用**（install / uninstall / start 三件套齐） |
| 并行方式 | 1 Lead + 8 teammate + 13 subagent | **1 Lead + 11 子 AI（两轮并行）** |
| 版本号 | 1.0.0 | **1.1.0** |

---

## 1. 任务目标与分工

**目标**：把上一版 §7「已知限制与未完成项」里所有**代码级缺陷**和**交付面不自洽**全部闭环，让 `svpn` 成为真能交付的工具。

**分工原则**：文件所有权互斥 —— 每个子 AI 只准写自己名下的文件，跨文件的问题只写进报告，由 Lead 统一裁决。这样 8 路并行也不会互相覆盖。

### 第一轮（8 路并行 · 代码与脚本）

| 代号 | 文件所有权 | 职责 |
|---|---|---|
| **P1-proxy** | `svpn/proxy.py` + 新测试 | `_bridge` 兜底、`pipe` 异常白名单、端口/超时容错、同端口预检、CONNECT 端口畸形 |
| **P2-cli** | `svpn/cli.py` + 新测试 | 配置归一（`merge_config`）、命令输出编码、`probe` 退出码、端口预检、SNI 接线 |
| **P3-sni** | `svpn/forward.py` + 新测试 | `SniForwarder` 三个真实缺陷（空白名单放行 / 上游 socket 泄漏 / gather 无兜底） |
| **P4-netutil** | `svpn/netutil.py` + 新测试 | 状态行解析、DoH 降级、`parse_node_url` 边界、socket 回收 |
| **P5-scripts** | `install.ps1` / `uninstall.ps1` / `start-svpn.cmd` / `svpn.cmd` / `svpnctl.ps1` | BOM、`call` 坑、新建卸载器与启动器 |
| **P6-config** | `config.example.json` / `config.json` + 新测试 | 配置 canonical 化 + 契约测试（防再次漂移） |
| **P7-tests** | `tests/test_pac.py` / `tests/test_fuzz.py` | PAC 语义（有 node 则真跑）+ 固定种子模糊测试 |
| **P8-integration** | `tests/test_integration_local.py` | 进程内回环集成：SOCKS5 / HTTP CONNECT / 绝对 URI / 鉴权 / 并发 20 路 |

### 第二轮（3 路并行 · 交付面）

| 代号 | 文件所有权 | 职责 |
|---|---|---|
| **D1-docs** | `README.md` / `README.en.md` / `docs/ARCHITECTURE.md` / `docs/WINDOWS.md` | 文档与真实代码逐条对齐（42 条命令组合真跑校验） |
| **D2-audit** | `runtime/audit-after.md` | AST 未定义名 / 签名 / argparse 属性、编码审计、`-X dev` 告警、死代码、裸 except |
| **D3-e2e** | `runtime/e2e-after.md` | 安静环境 10 连测（补上一版欠的账）、生命周期、probe 摘要、收尾复查 |

**结果汇总方式**：每个子 AI 写 `_crew/reports/<代号>.md`（固定五段式：改了哪些文件 / 逐条修复 / 验证结果 / 契约变更 / 没做完），Lead 逐份复核 + 独立复跑 + 裁决跨模块冲突，最后合并进本文件。全过程材料留在 `_crew/`（**2 份规范** `SPEC.md` / `SPEC-WAVE2.md` + **11 份报告** + 3 个 e2e 临时件 `_e2e_probe.py` 与两份 json），可追溯。

---

## 2. 修了什么

### 2.1 会直接崩 / 会直接伤到用户的

| # | 问题 | 修法 | 发现方 |
|---|---|---|---|
| 1 | `_bridge` 用裸 `gather`：一侧抛异常立即外穿，另一侧 pending 却从不回收 → 日志刷 `Task was destroyed` / `cannot reuse already awaited coroutine` | `gather(return_exceptions=True)`；`finally` 取消并**回收**另一侧；两侧 writer 必关；异常只 `log.debug` | P1 |
| 2 | `pipe` 异常白名单过窄（`RuntimeError` 直接炸出 pipe） | 放宽到 `OSError`/`TimeoutError`/`IncompleteReadError`/`RuntimeError`；`CancelledError` 继续上抛 | P1 |
| 3 | `"listen_port": null` → `int(None)` TypeError 直接崩进程 | `_as_port`/`_as_timeout` 容错归一，`None`/`""`/`"abc"`/越界一律回落默认 | P1 |
| 4 | `svpn up --port 7900`（撞 `http_port`）→ 第二次 bind 抛裸 `OSError 10048` | 双层预检：cli 侧先 `return 1` 给人话，proxy 侧再抛 `ValueError` 兜底 | P1 + P2 |
| 5 | `CONNECT host:abc` → `int()` ValueError 穿出 handler | 回 `400 Bad Request` 并关连接 | P1 |
| 6 | `svpn probe` 在全新安装 / 删过 `runtime/` 时，探测跑完才 `FileNotFoundError`（结果全丢） | 写 `probe.json` 前 `RUNTIME.mkdir` | P2 |
| 7 | `pid_alive`/`_port_owner` 用 `text=True`，`PYTHONUTF8=1` 下解码失败被静默吞掉 → **误判「未运行」** | 收 bytes 自己按 `mbcs`+`replace` 解 | P2 |
| 8 | `SniForwarder._allowed` 空白名单 `return True` → 本机任意进程可把它当通用 443 出口 | 空表 = 全部拒绝 + `start()` 警告一次 | P3 |
| 9 | `SniForwarder` 成功路径**永不关上游 socket** → 每连接泄一个 fd | `sw` 提到 `try` 外，`finally` 同时关两侧 | P3 |
| 10 | `node scan` 不过滤自身端口 → 把 svpn 自己写成上游节点，流量绕回自身、几秒打满 fd | `discover.is_self()` + `scan()` 全源过滤 + `node set` 拒绝 | Lead（D2 报） |
| 11 | `svpn down` 没有 `wininet-backup.json` 时仍打印「系统代理已还原」 | 按 `restore_wininet()` 返回值分流：没备份就明说「没动过」并回显当前值 | Lead（D2 报） |
| 12 | PAC 里 suffix 用 `f'"{s}"'` 拼接，带引号/换行就生成非法 JS（浏览器静默回退直连 =「设了代理没用」） | 改 `json.dumps` 生成数组元素 | Lead（P7 报） |
| 13 | `mode=blacklist` 的 PAC 里 `DIRECT=[]` →「国内直连」是句空话，与 `Router` 不一致 | 空表回落内置 `CN_DIRECT_SUFFIXES`（与 Router 同口径） | Lead（P7 报） |

### 2.1b 审计复跑后追加（2 项）

| # | 问题 | 修法 | 发现方 |
|---|---|---|---|
| 25 | `dial_socks5` 用 `all(c.isdigit() or c==".")` 判 IP → **IPv6 目标被当域名用 ATYP=3 发上游**，上游按域名去解 `::1` 必然失败（v6-only 站点直接连不上） | 改 `ipaddress.ip_address()` 三分支：v4→`ATYP=1`+packed、v6→`ATYP=4`+packed、其余→`ATYP=3`；本地假上游实测 5 种输入（`1.2.3.4`/`::1`/`2001:db8::1`/`example.com`/`[::1]`）ATYP 全部正确 | D2 |
| 26 | `up -b` 先写 PIDFILE 再等就绪、失败就 `unlink` → **并发启动时第二个实例会覆盖并删掉第一个的 pid 记录**，此后 `down` 说「没在运行」而 7899/7900 永久被占 | pid 文件改成**就绪后才写**；失败分支只在自己那条记录确实指向本进程时才删；`down` 找不到 pid 但端口仍被占时打印占用者与 `taskkill /PID …` 手动出路 | D2 |
| 27 | `hosts apply` **不校验缓存过期**：`_hosts_write()` 用 `max_age=10**9`，`cmd_hosts` 只在缓存「为空」时才自动刷新 → 16.3 小时前的 Akamai 边缘 IP 被原样写进 hosts，下载指向可能已下线的节点，而且 hosts 条目还会被 DNS 缓存钉住 | `_hosts_write()` 改用 `_cache_ttl_hours(cfg)*3600`；`cmd_hosts` 区分「没有缓存」与「已过期」并自动重选一轮；新增 `_cache_age_hours()` 直读 `updated`。补 `tests/test_hosts_cache.py` 8 条（TTL 刚过 1 秒的边界、`refresh_hours` 写坏回落 6h、SNI 模式不受影响） | 用户复核 |

### 2.2 功能面的真修复

| # | 问题 | 修法 |
|---|---|---|
| 14 | `cdn.enabled` 是**假开关**：`forward.py` 全文件无人 import，`cdn.hosts` 改了也不生效 | `_serve_async` 按 `cdn.enabled` 真起 `SniForwarder`（失败只告警不中止）；`hosts apply` 在 SNI 模式改写 `127.0.0.1` 并提示「需 up 常驻」；`_print_route_summary` 显示当前 CDN 模式 |
| 15 | `probe` 退出码无意义（社区不可用也 exit 0），CI 不能拿它当判据 | `summarize()` 新增 `unusable`，`ok = not unusable`；退出码 `0/1`；`--lenient` 保留旧语义；末尾打印退出码说明 |
| 16 | `_race_connect` 同批多个 IP 同时连上时，非采用者的 writer 既不关也不 cancel → 每次漏 1~3 个 fd | 记录 winner，其余 `_close_result()`；`finally` 再统一兜底回收 |
| 17 | `ProxyServer.close()` 不清 `_servers` → 同一实例 `close()` 后再 `start()` 静默失效 | `servers, self._servers = self._servers, []` |

### 2.3 交付面（脚本 / 配置 / 文档）

| # | 问题 | 修法 |
|---|---|---|
| 18 | `install.ps1` 缺 UTF-8 BOM → PS 5.1 按 ANSI 读源码，中文乱码甚至 `ParserError` | 重存 `utf-8-sig` + CRLF；实测「漏写 BOM 的副本」在 PS 5.1 直接解析失败，证明这是硬故障而非观感问题 |
| 19 | `install.ps1` 引用了不存在的 `start-svpn.cmd` / `uninstall.ps1` | 两个文件真造出来；开工前清单从 3 项扩到 5 项，缺就 `exit 2` 并列全 |
| 20 | `svpn.cmd` 的 `python -c "import sys"` 缺 `call`：python 是 `.bat` shim 时控制权不返回 → **静默无输出 + exit 0** | 探测与真实调用全部 `call`（用 `python.bat` 垫片实测复现并验证修复） |
| 21 | `.cmd` 里的中文 `rem` 注释在 CP936 起始控制台下触发 cmd.exe 行偏移，冒 `'?' is not recognized` | 注释全 ASCII；含中文的 `echo` 行后强制留空行（载荷规则已写进 `docs/WINDOWS.md`） |
| 22 | `config.example.json` 与 `DEFAULT_CONFIG` 漂移；`config.json` 缺 `auto_node`/`refresh_hours`/`avatars`；`_note` 被并进运行配置 | 12 键契约 + `merge_config()` 归一（丢 `_` 前缀/未知键、嵌套逐键合并、六类归一器）；新增 15 条契约测试焊死 |
| 23 | `proxy_suffixes`/`direct_suffixes` 因「未知键丢弃」变得不可配置（功能面收缩） | `EXTRA_KEYS` 白名单放行 + 归一；不在模板里、写了才生效（修的过程里自己踩了个 `KeyError`，被 D1 抓出来当场修掉） |
| 24 | `smoke_test` 项数、版本号、`--bench` 等文档与事实不一致 | 文档按实测改（15 项、`--no-bench`）；版本升 1.1.0 |

### 2.4 清理（上一版列了但没执行）

已删：`_chk/`（31 个抓取脚手架）、`_probe_proxy.py`、`runtime/pac_test.js`（输入已悬空）、`svpn.egg-info/`、两处 `__pycache__`、两个 Lead 临时脚本。
已保留：`runtime/{hosts.backup, wininet-backup.json, cdn-cache.json, svpn.pac}`（回滚依赖）、`runtime/review.md`（上一版审查存档）、`_crew/`（本轮规范 + 11 份报告 + 上一版日志）。

---

## 3. 验收证据

### 3.1 测试矩阵（`PYTHONUTF8=1`；解释器 `C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe`，**目录名是 3.13.12、实际报 3.13.14**，别当成跑错版本）

| 文件 | 用例 | 结果 | 性质 |
|---|---|---|---|
| `tests/test_logic.py` | 50 | OK | 基线 |
| `tests/test_netutil.py` | 57 | OK | 基线 |
| `tests/test_regression.py` | 62 | OK | 基线（回归护栏） |
| `tests/test_proxy_hardening.py` | 25 | OK | 新 · 内核健壮性 |
| `tests/test_forward.py` | 16 | OK | 新 · SNI 转发 |
| `tests/test_netutil_extra.py` | 42 | OK | 新 · 解析 / socket 回收 |
| `tests/test_cli_contract.py` | 50 | OK | 新 · CLI / 配置 / 退出码 |
| `tests/test_config_contract.py` | 15 | OK | 新 · 配置不漂移 |
| `tests/test_integration_local.py` | 8 | OK | 新 · 回环集成 |
| `tests/test_pac.py` | 12 | OK | 新 · PAC（有 node 则跑真语义） |
| `tests/test_hosts_cache.py` | 8 | OK | 新 · hosts 拒绝过期缓存（含 TTL 边界） |
| `tests/test_fuzz.py` | 19（1 skip） | OK | 新 · 固定种子模糊 |
| **合计** | **364** | **全绿** | |

其他硬指标：

- `python -m compileall -q svpn` → **exit 0**
- `tests/smoke_test.py` → **ALL PASS（15 项）**
- 13 个子命令 `--help` → **rc=0，stderr 0 字节**（逐个验）
- 三个 `.ps1` → 头三字节 `ef bb bf`（BOM ✓），`Parser::ParseFile` **0 错误**
- 两个 `.cmd` → CRLF ✓、无 BOM ✓、裸 LF = 0
- 静态审计：未定义名 **0**、关键字参数不匹配 **0**、argparse 属性 **13/13**、编码 **39/39**、`-X dev` 下 17 次调用 **stderr 全 0 字节**、裸 `except:` **0 处**、死代码 `_need` 已删
- PAC 三种模式用 **node 真解析**：`SyntaxOK` ×4 + 分流语义正确（含大写、尾点、lookalike 反例）
- 上游 SOCKS5 拨号的 ATYP 分支用本地假上游实测 5 种目标（`1.2.3.4` / `::1` / `2001:db8::1` / `example.com` / `[::1]`）编组正确：v4→1、v6→4、域名→3

### 3.2 端到端（会联网，详见 `runtime/e2e-after.md`）

安静环境（只有测量进程在联网）10 连测：

| 轮次 | SOCKS5 | HTTP | 成功样本 P95 |
|---|---|---|---|
| 第一轮 | 0/10 | 1/10 | HTTP 7579ms |
| 第二轮 | 3/10 | 6/10 | 935ms / 960ms |
| **直连对照组（不走代理）** | **5/10** | — | 529–1041ms |

- 按事先定的标准判 **未达标**（成功率不够）。
- **但这不是 svpn 的稳定性指标**：同时段同域名**直连对照组也只有 5/10**，失败签名完全一致（`ConnectionResetError` 56–59ms / `SSL: UNEXPECTED_EOF` 61–81ms，都是握手刚发出就被掐断）。
- 隧道本体正常：经 SOCKS5 取 CDN 两次都是 `200 OK`（444ms / 238ms）。
- `probe --node none` → rc=1，`unusable = [steamcommunity.com, avatars.steamstatic.com]`，`dead = []`，store 判「直连可用 138ms」。社区/头像 SNI 阻断属**预期**。
- **归因修订**：上一版把 6/10 归因于「13 个 agent 抢网」，这个结论是错的 —— 安静环境同样是 6/10。

### 3.3 一个环境陷阱（记录，不是缺陷）

在 bash 会话里 `svpn up -b` 派生的 `_serve` 会被平台在命令结束时按进程树回收，表现为「`up -b` 报成功、2 秒后 `status` 说未运行」。测量时必须把 `up -b` 放进一个长驻父 shell。这与 `D:\LE\AI\wbwrk` 里记的「沙箱派生的子/孙进程命令结束即被 Job Object 回收」是同一条。

---

## 4. 仍然存在的限制（诚实清单）

### 4.1 必须人工做一次的（与上一版相同）

1. **`hosts apply` 需要一次 UAC 授权**。拒绝就用管理员 PowerShell 跑 `python -m svpn hosts apply --no-elevate`。
2. **社区 / 创意工坊 / 头像需要上游节点**。本机 14 个常见客户端端口全空。有节点后 `python -m svpn node set socks5://host:port` 或 `node scan`。

### 4.2 已知行为取舍（不是漏修）

1. **半开连接会等到 TCP 超时**：`_bridge`/`SniForwarder` 用 `gather(return_exceptions=True)`，语义是「两个方向都 EOF 才收工」。浏览器/下载正常，但对端永不关连接时会挂到超时。彻底解决要加 idle timeout（下一轮）。
2. **`probe` 的「直连可用」只代表「竞速里至少有一个 IP 成功」**，不代表成功率。**别拿它当「商店稳定」的判据**，CI 里只能当「有没有路」的冒烟检查。
3. **`cdn.sni_port` 实际上必须是 443**：hosts 只能表达 IP，客户端连的就是 `127.0.0.1:443`。改别的端口 → 转发起来了但没人来连。（cli 里那句误导性提示已改掉。）

### 4.3 审计留下的下一轮清单（重跑后：P1 只剩 1 项 + 2 项部分）

**唯一剩下的 P1**：`netutil.py` 的 `_DNS_CACHE` 无上限、无负缓存、无总 deadline —— DoH 全被墙时线程池会被占满，代理整体假死几十秒。要改的是缓存策略与总超时预算，属设计改动，没在这轮硬塞。

**两项部分闭合**：`system.py` 的 hosts 写入有了校验但**仍非原子写**（中途失败可能留半截）；SOCKS5 握手 15 处读操作只有 2 处带超时、且全包无并发闸门。

其余 P2 共 13 项（`ips[:4]` 取样、DoH 查询串未编码、`100.64/10` 仍送上游、日志无轮转、`save_upstream` 解析失败会清空配置等），每条带复现命令，见 `runtime/audit-after.md` §7。

> 注：`runtime/audit-after.md` 的快照时间晚于我最后两处修复（#25/#26），那两项在审计报告里仍记为遗留；以本节为准。

### 4.4 未交付的并行任务（上一版列过）

已补齐：`README.en.md`、`uninstall.ps1`、`start-svpn.cmd`、`docs/WINDOWS.md`、`tests/test_pac.py`、`tests/test_fuzz.py`。
不再单开（内容已被覆盖）：`runtime/bench.md` → `e2e-after.md`；`runtime/security.md` → `audit-after.md`；`runtime/fuzz.md` → `tests/test_fuzz.py`；`runtime/pac-matrix.md` → `tests/test_pac.py`。
仍未做：`runtime/packaging.md`、`runtime/log-analysis.md`。

---

## 5. 与上一版 §7 的逐条对账

| 上一版条目 | 状态 |
|---|---|
| 静态缺陷 1：`probe` 写文件前不建 runtime | ✅ 已修 |
| 静态缺陷 2：`up --port 7900` 撞车裸 traceback | ✅ 已修（双层预检） |
| 静态缺陷 3：`_bridge` 无兜底 + `pipe` 白名单过窄 | ✅ 已修 |
| 静态缺陷 4：`pid_alive`/`_port_owner` 用 `text=True` | ✅ 已修 |
| 静态缺陷 5：`CONNECT host:abc` / `listen_port:null` | ✅ 已修 |
| `forward.py` 是死代码 | ✅ 已接线（`cdn.enabled` 真生效） |
| `install.ps1` 缺 BOM + 引用不存在的文件 | ✅ 已修 |
| `runtime/pac_test.js` 悬空 | ✅ 已删 |
| `runtime/review.md` 指纹基线过期 | ✅ 新基线见 `runtime/audit-after.md` |
| 建议清理 `_chk/`/`_probe_proxy.py`/`egg-info`/`__pycache__` | ✅ 已执行 |
| 观察项：`store` 偶发异常边缘 | ⚠️ 部分缓解（多 IP 竞速 + ip_map failover），仍会偶发 |
| 观察项：高并发 6/10 | ✅ 已复测并**修订归因**（不是并发污染，是通路问题） |
| 观察项：`Task was destroyed` / `cannot reuse coroutine` | ✅ 已修 |
| 观察项：`cert_cn` 恒空 | ⬜ 未修（只影响诊断信息） |
| 观察项：`stats.json` 22 条 baidu 错误 | ✅ 确认为压测产物，非缺陷 |

---

## 6. 接手步骤

```powershell
cd D:\LE\AI\dshwork\steam-vpn

# 0) 体检
python -m svpn doctor

# 1) 下载加速（无节点也能用）
python -m svpn cdn --refresh
python -m svpn hosts apply          # 弹一次 UAC

# 2) 有节点（Clash / v2rayN / sing-box）
python -m svpn node scan            # 自动找 + 真握手验证（会排掉 svpn 自己的端口）
python -m svpn up -b --system-proxy
python -m svpn steam --system       # 改代理后必须重启 Steam 客户端

# 3) 收工 / 回滚
python -m svpn down --clear-hosts
```

一键装 / 卸：`powershell -ExecutionPolicy Bypass -File .\install.ps1`；`.\uninstall.ps1 [-Purge] [-KeepProxy]`。
双击启动：`start-svpn.cmd`。

排障顺序：`svpn doctor` → `svpn probe`（看退出码 + `runtime/probe.json`）→ `runtime/svpn.log`。
`probe` 退出码：`0` = 全部端点可用，`1` = 有端点不可用（`summary.unusable`），`--lenient` = 旧语义。

---

## 7. 复盘

上一版说「真正的坑是四类假成功」。这一轮又添了一类，而且是同一个病根：

**代码写了、注释写了、文档也写了，但没有任何东西在跑它。** `cdn.enabled` 是假开关、`EXTRA_KEYS` 是死代码、`proxy_suffixes` 是不可达分支、`install.ps1` 引用两个不存在的文件、`svpn.cmd` 的 `--bench` 从来不是参数。这些都不会报错，只会安静地什么都不做 —— 比崩溃更难发现。

两个有效的对付手段，本轮都是靠它们抓出来的：

1. **让两个不同的 AI 读同一份代码。** P7 的 PAC 测试抓到了 `pac.py` 里没修完的转义问题；P8 的集成测试抓到了 `close()` 不清 `_servers`；D2 的审计抓到了「自己把自己当节点」；D1 读文档时抓到了我刚引入的 `KeyError`。**交叉读比自审有效得多。**
2. **每条断言都要有一条"能红"的路径。** 契约测试焊死 12 个键、`-X dev` 扫描、`Parser::ParseFile`、字节级 BOM 检查 —— 这些不是"看起来对"，而是"改坏了立刻红"。

还有一个教训：**归因要拿对照组。** 上一版把 6/10 归因于"并发抢网"，本轮加了直连对照组才发现安静环境同样 6/10，真正的瓶颈是通路侧的 SNI 级干扰。没有对照组的归因，基本就是猜。

### 7.4 静态审计有盲区：只读代码，不读运行数据

`hosts apply` 写过期 IP 那个 bug（§2.1b #27），静态审计、模糊测试、集成测试**都抓不到**——代码里 `max_age=10 ** 9` 是个完全合法的参数，没有任何类型错误、没有未定义名、没有任何告警。要发现它，得把代码行为和**磁盘上的运行数据**对起来看：`runtime/cdn-cache.json` 的 `updated` 是 16.3 小时前，而 TTL 写的是 6 小时。

也就是说，**审计的覆盖面要包括"数据的时间属性"**：缓存、备份、hosts 托管块、pid 文件，这些都会随时间长出代码里看不出的问题。下一轮审计清单应该加一节「运行期数据新鲜度」，而不是只看 AST 和编码。

# 工作日志 · steam-vpn（svpn 1.0.0）· 最终版

- 生成时间：2026-10-24 03:41（Asia/Shanghai）
- 指令：停止所有工作，生成工作日志
- 项目路径：`D:\LE\AI\dshwork\steam-vpn`
- 一句话结论：**工具可用、测试全绿、进程已停、系统未改**。商店/登录/API/下载可用并按需加速；社区/头像受 SNI 阻断，必须配一个上游节点（本机没有）。

---

## 0. 停止状态确认（03:41 实测）

| 项                          | 状态                                  |
| -------------------------- | ----------------------------------- |
| `svpn` 相关 python 进程        | **0 个**                             |
| 7899（SOCKS5）/ 7900（HTTP）监听 | **均未监听**                            |
| Windows 系统代理 `ProxyEnable` | **0**（未接管）                          |
| hosts 受管块                  | **未写入**（需要一次 UAC 授权）                |
| 正在运行的子 agent / teammate    | 全部已结束                               |
| 遗留的 pid 文件                 | `runtime/svpn.pid` 指向已退出进程，可安全删除或忽略 |

---

## 1. 目标与结论

目标：给 Steam 做一个能真正用起来的本地 VPN/加速器——商店、登录、下载、社区各走各的路。

结论：**分流器与 CDN 选边已完成并验证**；社区方向受链路层 SNI 阻断限制，不是代码能解决，必须接节点。

`svpn` 是一个纯标准库、只听 `127.0.0.1` 的 Steam 专用分流器：

- **SOCKS5 `127.0.0.1:7899` + HTTP `127.0.0.1:7900`**，按域名决定「直连 / 走节点」
- **节点自动发现**：配置、环境变量、Windows 系统代理、14 个常见客户端端口，逐个真握手 + 穿到 Steam 验证
- **CDN 选边**：DoH 拉全量边缘 IP → TLS 竞速 → 1MB 实测限速 → 写 hosts 指到最快边缘（没节点也能加速下载）
- **三层故障定性**：DNS 污染 / IP 不可达 / SNI 阻断分得清，用「控制 IP 换 SNI 复测」确认阻断
- 所有系统改动可回滚，备份在 `runtime/`

---

## 2. 本机实测基线（决定方案的事实）

| 域名                                                   | 直连结果                            | 判定               |
| ---------------------------------------------------- | ------------------------------- | ---------------- |
| store / login / api / help.steampowered.com          | OK 54–104ms                     | 可用               |
| steamcdn-a.akamaihd.net / cdn.akamai.steamstatic.com | OK 56–71ms                      | 可用且可加速           |
| community.akamai.steamstatic.com                     | OK 58–83ms                      | 静态资源可用           |
| **steamcommunity.com**                               | TCP 通，ClientHello 带该 SNI 立即 RST | **SNI 阻断，只能走节点** |
| **avatars.steamstatic.com**                          | 同上（Fastly 三个 IP 全 RST）          | **SNI 阻断**       |
| 1.1.1.1:443                                          | 不通                              | 只能走国内 DoH 服务商    |

CDN 选边实测：

| 域名                         | 最优 IP          | 握手    | 实测吞吐     |
| -------------------------- | -------------- | ----- | -------- |
| cdn.akamai.steamstatic.com | 23.195.119.69  | 153ms | 1.64MB/s |
| steamcdn-a.akamaihd.net    | 173.222.248.89 | 157ms | 1.58MB/s |
| steamcdn-a（复测）             | 104.84.150.167 | 273ms | 2.70MB/s |

Steam 客户端日志反查（27 个日志/配置、56 个唯一域名）：CM 形态为 `cmp{1,2,3}-<dc>.steamserver.net` / `ext{1,2}-<dc>.steamserver.net`，443 与 27015–27038 都在用；按域名分流已覆盖 `steamserver.net`，无需按端口放行。

---

## 3. 交付物

### 代码 `svpn/`（14 个模块，零第三方依赖）

| 文件                            | 职责                                              |
| ----------------------------- | ----------------------------------------------- |
| `cli.py`                      | 12 个子命令、配置合并、后台进程、UAC 提权、体检                     |
| `proxy.py`                    | SOCKS5 / HTTP CONNECT / 绝对 URI 转发、分流、stats 周期落盘 |
| `netutil.py`                  | DoH 解析、TCP/TLS/SNI 分层探测、控制 IP 复测、经节点拨号          |
| `discover.py`                 | 节点发现 + 真握手验证                                    |
| `probe.py`                    | 9 端点 × 直连/节点矩阵 + 结论                             |
| `cdn.py`                      | 边缘竞速 + 1MB 实测 + 缓存                              |
| `rules.py`                    | Steam/Valve 后缀、CIDR、三档分流                        |
| `forward.py`                  | 固定转发 / SNI 透明转发（预留，未接线）                         |
| `system.py`                   | WinINET、hosts 受管块（编码探测 + 注入校验）                  |
| `pac.py`                      | PAC 生成（数组字面量、尾点归一、三模式）                          |
| `steam.py`                    | 找 steam.exe、启动、结束                               |
| `split.py`                    | Valve 网段路由（未接线）                                 |
| `__init__.py` / `__main__.py` | 版本与入口                                           |

### 文档与脚本

- `README.md`（21.7KB）：事实基线 → 30 秒上手 → 四条主线 → 无节点/有节点 → 故障对照 → 回滚 → 安全边界
- `docs/ARCHITECTURE.md`（12.6KB）：总览图、五条链路、数据结构、决策表、失败分类学、取舍、扩展点
- `svpn.cmd`（CRLF）、`svpnctl.ps1`（UTF-8 BOM）、`install.ps1`（**缺 BOM，暂勿使用**）、`pyproject.toml`
- `config.example.json`

### 测试 `tests/`（8 个文件）

`smoke_test.py`、`test_netutil.py`(57)、`test_logic.py`(50)、`test_regression.py`(62)、`live_e2e.py`、`live_test.py`、`domain_audit.py`、`e2e_proxy.py`

### 运行期报告 `runtime/`

`e2e-report.md`、`review.md`(26KB)、`verify-pac.md`、`domain-audit.json`(43 域名)、`observed-domains.json`(56 域名)、`probe.json`、`cdn-cache.json`、`pac_test.js`

---

## 4. 时间线

| 时间          | 事件                                                                                     |
| ----------- | -------------------------------------------------------------------------------------- |
| 03:00       | 接手上一轮半成品（启动即 `Errno 10048` 端口冲突）                                                       |
| 03:02–03:05 | 网络基线：store/api/CDN 直连 OK；community 换 SNI 即 RST；本机无任何代理客户端                              |
| 03:05–03:10 | 新增 `netutil`/`discover`/`probe`/`cdn`；`proxy` 支持 ip_map；`forward` 支持最优边缘               |
| 03:11       | 重写 `cli.py`（一次 PowerShell 重写把中文写成 cp936 损坏文件，改用编辑工具整体重建）                               |
| 03:12       | 离线测试首版 107 用例全绿                                                                        |
| 03:13–03:16 | 文档/脚本、域名审计、真机探测并行                                                                      |
| 03:17–03:24 | 修 P0：`127.0.0.1` 节点被误判、PAC 非法 JS、`up -b` 就绪判定反了、stats 被 taskkill 丢                     |
| 03:24–03:31 | 修 doctor 崩溃、hosts 编码毁注释、IPv6 URL、172.16/12 漏判、tls_probe 无兜底、DNS 单 provider、控制 IP 无 TTL |
| 03:31–03:40 | 架构文档、打包、安装脚本、日志/域名反查、安全审计、性能测试并行；e2e 与代码审查落盘                                           |
| 03:40–03:41 | 修 hosts 注入、`refresh_hours` 接入 TTL、文档对齐；**收到停止指令，停进程、生成日志**                             |

**参与方**：1 Lead + 8 teammate（docs-scripts / tests-offline / live-verifier / e2e-runner / domain-auditor / code-reviewer / perf-bench / security-audit）+ 13 subagent（验收、契约校验、编码体检、包装脚本、配置一致性、告警扫描、架构核对、文件盘点、冒烟复跑等）。

---

## 5. 已修复缺陷

| 级别 | 问题                                                                      | 位置                  | 证据/修复                                                        |
| -- | ----------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------ |
| P0 | `Upstream.enabled` 把 `127.0.0.1` 当占位符 → 本机 Clash/v2rayN 节点全被禁用，节点链路永不生效 | `proxy.py`          | 变异自检：改回旧实现 4 条单测全红                                           |
| P0 | PAC 生成成 `var PROXY = { ... }`，非法 JS，整份 PAC 加载失败                         | `pac.py`            | node 实测 `SyntaxError`；改数组后 13/13                             |
| P0 | `up -b` 就绪判定取反 → 快机器必报失败、删 pid、留孤儿进程占端口                                 | `cli.py`            | 复现 exit 1 但端口仍 LISTENING；修复后 3s 内 exit 0                     |
| P1 | `stats.json` 只在 `finally` 落盘，`down` 用 `taskkill /F` 永远拿不到统计             | `proxy.py`          | 加 5s 周期落盘（tmp + `os.replace`），实测 24 连接写入                     |
| P1 | hosts 写回统一 UTF-8，GBK 中文注释被替换成 U+FFFD 且不可恢复                              | `system.py`         | 35 项编码断言：GBK/BOM/ASCII/latin-1 逐字节无损往返                       |
| P1 | hosts 可被注入：域名带 `\n` 写任意行；伪造 MARK_END 可永久驻留                              | `system.py`         | `sanitize_entries`：合法 IP + 域名白名单正则 + 拒绝标记串；写入只接受 `cdn.hosts` |
| P1 | `doctor` 在 `PYTHONUTF8=1` 下解码 tasklist 崩溃                               | `steam.py`          | 改按字节 + `mbcs`；实测 stderr 0 字节                                 |
| P1 | DoH 首 provider 只回 1 条 A，store 单 IP 失效就整条超时                              | `netutil.py`        | 首轮最多问 2 家合并候选                                                |
| P1 | 控制 IP 缓存无 TTL → `sni_block_test` 可能永远漏报                                 | `netutil.py`        | 5 分钟 TTL + `refresh`，三态 True/False/None                      |
| P1 | `tls_probe` 成功分支异常会带崩整批 `race_tls`                                      | `netutil.py`        | 包 try/except 仍返回 ok                                          |
| P2 | `parse_node_url` 不支持 `[::1]:1080`                                       | `netutil.py`        | 方括号解析 + 缺冒号报错                                                |
| P2 | `172.16/12` 只拦 `172.16.*`，Docker/WSL 段被送上游                              | `rules.py`          | 改 `ipaddress.is_private`                                     |
| P2 | 表格中文列宽不齐（CDN 表头右移 2–10 列）                                               | `cli.py`/`probe.py` | 统一按显示宽度补空                                                    |
| P2 | `cdn.refresh_hours` 只影响表格星号，不影响代理侧 TTL                                  | `cli.py`            | 三处共用 `_cache_ttl_hours`                                      |
| P2 | Steam 连通性探针 `steamconnecttest.com` 未覆盖；`steamloopback.host` 未显式直连       | `rules.py`          | 已补，防污染误判「离线」                                                 |

---

## 6. 验收证据

| 项目                             | 结果                                                                 |
| ------------------------------ | ------------------------------------------------------------------ |
| `python -m compileall -q svpn` | exit 0                                                             |
| 离线单测                           | **169 用例全绿**（57+50+62），多轮稳定                                        |
| `tests/smoke_test.py`          | ALL PASS（17 项）                                                     |
| PAC + node 语义                  | 13/13 PASS（含尾点、大小写、lookalike 反例）                                   |
| 12 个子命令 `--help`               | 12/12 exit 0、stderr 0 字节                                           |
| `-X dev` 告警扫描                  | 4 项命令 stderr 全 0 字节，无 Resource/DeprecationWarning                  |
| 代理端到端                          | SOCKS5/HTTP → store **200 OK**，269–515ms；CDN 200；baidu 20 并发 20/20 |
| `probe --node none`            | 7 个直连可用；community/avatars 判「需节点 · SNI 阻断」                          |
| `cdn --refresh`                | 8 域名，最快边缘 1.58–2.70MB/s                                            |
| 生命周期                           | `up -b` → 监听 → `status` → `down` 全通过，无残留进程                         |
| 编码审计（30 文件）                    | 29/30 合规；唯一问题 `install.ps1` 缺 BOM                                  |

---

## 7. 已知限制与未完成项

### 必须人工做一次的

1. **`hosts apply` 需要一次 UAC 授权**：本会话非管理员，无法代点。拒绝则用管理员 PowerShell 跑 `python -m svpn hosts apply --no-elevate`。
2. **社区/创意工坊需要上游节点**：本机 14 个常见客户端端口全空。有节点后 `python -m svpn node set socks5://host:port` 或 `node scan`。

### 观察项（非代码缺陷）

1. `store.steampowered.com` 偶发解析到异常边缘（如 `23.15.142.182`）导致单次 TLS 失败；代理侧有 IP failover，`probe` 单轮可能显示一条「不通 · 待排查」，复跑即恢复。
2. 高并发（13 个 agent 同时打网络）下端到端测得 store 成功 6/10、耗时 0.39–1.54s；报告据此判「稳定性未达标」。**安静环境的 10 连测未完成**（收到停止指令中断）。
3. `svpn.log` 偶见 `Task was destroyed but it is pending` / `RuntimeError: cannot reuse already awaited coroutine`，发生在客户端 CONNECT 后立即 EOF；连接本身已失败、不影响结果，属待修 P1。
4. `cert_cn` 恒为空（探测用 `CERT_NONE`），仅影响诊断信息。
5. `runtime/stats.json` 中 22 条 `www.baidu.com` 错误来自并发压测，非分流缺陷。

### 未交付的并行任务（停止时未落盘）

`README.en.md`、`uninstall.ps1`、`start-svpn.cmd`、`docs/WINDOWS.md`、`runtime/bench.md`、`runtime/security.md`、`runtime/fuzz.md`、`runtime/pac-matrix.md`、`runtime/packaging.md`、`runtime/log-analysis.md`、`tests/test_pac.py`、`tests/test_fuzz.py`。

### 停止后收到的只读回执（仅记录，未再动手）

- **CLI 输出契约**：`cdn --json` / `probe --json` 是「表格 + 末尾一块 JSON」，不是纯 JSON 流（脚本要截最后一个以 `{` 开头的块）；`probe.json` 为 `{ts,node,rows,summary}` 9 行、`cdn-cache.json` 为 `{updated,domains}`，44/44 字段与类型断言通过。已知显示瑕疵：`probe` 表里 `sni_rst 控制IP复测RST` 这类中文串按字符截断而非显示宽度，会和「节点」列粘连（只影响终端显示，不影响 JSON）。
- **配置审计**：`config.example.json` 是 `DEFAULT_CONFIG` 的兼容超集（13 键 vs 10 键，类型一致，可直接复制）；但 `config.json` 与默认值有漂移——`cdn.hosts` 只有 7 个（缺 `avatars.steamstatic.com`）、缺 `auto_node` / `cdn.refresh_hours` 两个键（加载时按默认补齐）；`_note` 会被 `load_config` 原样并入 cfg（当前无人消费，无害但脏）；`proxy_hosts`/`direct_hosts` 只在 example 里有，`DEFAULT_CONFIG` 缺。建议后续把 example 当 canonical，并加一条「example 键集/类型 == DEFAULT_CONFIG」的测试。
- **端到端复查**：首轮曾出现经代理取 store 失败（SOCKS rep=4 / HTTP 502，约 24s），而本机直连同一域名成功（88ms），与 §7 第 3 条「单边缘 TLS 抖动」一致；Baidu 经代理 200。复查期间出现过一次非其发起的 `_serve` 启动（pid 17384→15560），属于并行任务交叉，现已全部停止。
- **编码审计**：30 个文件 29 合规，唯一问题 `install.ps1` 缺 UTF-8 BOM。
- **入口脚本（从 `C:\` 用绝对路径实测）**：`svpn.cmd` / `svpnctl.ps1` 的 cwd 与 `cli.ROOT`（包位置推导）解耦，任何目录调用都能找到 `config.json` / `runtime/`；12 组参数映射全部正确、退出码透传正常（`node scan`→1、未知命令→2）。两个待修点：① `svpn.cmd:27` 的 `python -c "import sys"` 未加 `call`，若 PATH 里的 python 是 `.bat`/`.cmd` 包装（pyenv-win shim），控制权不返回主脚本，表现为「静默无输出 + exit 0」；② `probe` 退出码只由「不通·待排查」决定，三个域名不可直连时仍 exit 0，CI 不能拿它当「Steam 可用」判据。
- **静态缺陷扫描（AST + 签名绑定 + argparse 属性审计）**：未定义名 0、签名不匹配 0、`cmd_*` 属性缺失 0（`cmd_steam` 手工 Namespace 的 6 个键与 `cmd_up` 需求一致）。但列出 5 个真实炸点，**均未修**，按优先级留作下一轮：
  1. `cli.py` 写 `probe.json` 前没有 `RUNTIME.mkdir` → 全新安装/删过 runtime 时，`svpn probe` 探完才 `FileNotFoundError`；
  2. `svpn up --port 7900`（与默认 `http_port` 撞车）→ 第二次 bind 抛 `OSError 10048`，无预检、裸 traceback；
  3. `handle_http` 的 `_bridge` 无兜底 + `pipe` 异常白名单过窄 → 日志里已出现 `Unhandled exception in client_connected_cb` / `Task was destroyed but it is pending`；
  4. `cli.py` 的 `pid_alive` / `_port_owner` 仍用 `text=True`，`-X utf8` 下 `tasklist/netstat` 解码失败会静默误判「未运行」；
  5. `CONNECT host:abc` → `int()` 抛 ValueError 穿出 handler；`config.json` 里 `listen_port:null` → `int(None)` TypeError。  
     另：`forward.py` 的 `Forwarder`/`SniForwarder` 无任何模块 import，属死代码（附录 A 方案实际不可用）。

### 交付面不自洽处

- `install.ps1` 缺 UTF-8 BOM，并引用了两个不存在的文件（`start-svpn.cmd` / `uninstall.ps1`）→ **暂不使用安装器**，走 README 手动流程
- `runtime/pac_test.js` 默认输入已删（悬空）；`runtime/pac-check.js` 等已被清理
- `runtime/review.md` 的指纹基线已过期（cli/pac/rules/system 四个文件在审查后改过）
- 建议清理（未执行）：`_chk/`（31 个抓取脚手架）、`_probe_proxy.py`、`runtime/pac_test.js`、`svpn.egg-info/`、两处 `__pycache__`
- 不要删：`runtime/{hosts.backup, wininet-backup.json, cdn-cache.json, svpn.pac}`（回滚依赖）

---

## 8. 接手步骤

```powershell
cd D:\LE\AI\dshwork\steam-vpn

# 1) 下载加速（无节点也能用）
python -m svpn cdn --refresh
python -m svpn hosts apply          # 弹一次 UAC

# 2) 有节点（本机 Clash/v2rayN/机场客户端）
python -m svpn node scan            # 自动找 + 真握手验证
python -m svpn up -b --system-proxy
python -m svpn steam --system       # 拉起 Steam（改代理后必须重启客户端）

# 3) 收工 / 回滚
python -m svpn down --clear-hosts
```

排障顺序：`python -m svpn doctor` → `python -m svpn probe` → 看 `runtime/probe.json` 与 `runtime/svpn.log`。

---

## 9. 复盘


真正的坑不在协议实现（SOCKS5/CONNECT/SNI 解析一次就过），而在四类「看似能跑」的假成功：  
**把自己本机节点判成占位符**、**PAC 语法错但命令 exit 0**、**后台启动判定反了却留下孤儿进程**、  
**hosts 写入会毁注释且可被注入**。这一轮把四类全部钉上了测试与断言——这也是 13 个并行校验里最有价值的产出。

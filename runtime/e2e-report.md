# svpn 本地代理 E2E 验证报告

- 执行时间：2026-10-24 03:14 – 03:29（Asia/Shanghai）
- 执行人：e2e-runner（独占 127.0.0.1:7899 / 7900 验证权）
- 环境：Windows / Python 3.12.10 / 纯直连分流（`upstream=NODE_HOST` → 未启用，whitelist 模式）
- 验证脚本：`tests/e2e_proxy.py`（纯标准库，SHA256 前缀 `6B41ADCE`）
- 实现未改动：本次只写 `tests/e2e_proxy.py` 与 `runtime/e2e-report.md`，`svpn/*.py` 零改动

## 0. 被测代码指纹（最终轮实际加载）

| 文件 | mtime | sha256 |
|---|---|---|
| cli.py | 03:26:58 | 55D5A67F |
| proxy.py | 03:27:37 | 49D51A38 |
| netutil.py | 03:27:37 | 4B66341C |
| rules.py | 03:24:08 | 482ADD2E |
| probe.py | 03:27:37 | C6EEB535 |
| discover.py | 03:17:25 | 622BDD38 |
| steam.py | 03:17:07 | FFC1C5F6 |

最终轮 `up` 于 03:27:56 起服务，加载的是上表版本。

## 1. 清场

- `python -m svpn down` → `[=] 没有在运行的实例`（pid 文件已被此前失败流程 unlink）
- 残留 `_serve` 进程：`taskkill /PID 22384 /T /F`（旧代码实例，03:15:28 启动）→ 已终止
- `netstat -ano -p TCP | findstr ":7899 :7900"` → 空（无 LISTENING、无 TIME_WAIT）
- 结论：**PASS**

> 顺带发现（已在 03:2x 报给 lead，现已修复）：`cli.py:273` 的 `if not _port_busy(...)` 判断反了，
> 导致 `up -b` 恒返回 1、删 pid 文件、留下孤儿 `_serve`。修复后复测通过。

## 2. `python -m svpn up -b --no-auto`

最终轮实际输出：

```
[+] 后台已启动 pid=5076  socks5://127.0.0.1:7899  http://127.0.0.1:7900
    节点 未配置（纯直连分流）   模式 whitelist
    CDN 已选边 7 个域名（缓存 cdn-cache.json）
```

| 断言 | 期望 | 实际 | 结果 |
|---|---|---|---|
| 退出码 | 0 | 0 | PASS |
| 启动耗时 | 合理（<3s） | 1.17s（修复前 12.9s：20×tasklist） | PASS |
| pid 文件 | 存在 | `runtime/svpn.pid` = 5076 | PASS |
| 7899 | LISTENING | LISTENING pid 5076 | PASS |
| 7900 | LISTENING | LISTENING pid 5076 | PASS |

## 3. `python tests/e2e_proxy.py`（最终轮，起服务后等待 ≥6s 再跑）

| # | 检查项 | 结果 | 耗时 | 明细 |
|---|---|---|---|---|
| 1 | preflight_ports | PASS | 0.03s | 7899/7900 均可连 |
| 2 | socks5_store_head（3 次） | PASS | 0.54s | 3/3 ok，med=541ms，range=401–885ms，TLSv1.3，codes=200,200,200 |
| 3 | http_connect_store_head | **FAIL** | 0.06s | `SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1010)` |
| 4 | socks5_baidu_head | PASS | 0.41s | HTTP 200，connect=284ms tls=89ms head=39ms |
| 5 | direct_vs_proxy | SKIP | 0.00s | 直连对照不可用：系统解析出的 store IP 在该时刻全部不可达（见 §5） |
| 6 | http_plain_absolute_uri | PASS | 0.15s | `GET http://www.baidu.com/` → HTTP 200 |
| 7 | concurrency_20_baidu | PASS | 0.27s | 20/20 ok，总耗时 0.3s，单条 med=219ms（range 182–256ms） |
| 8 | cdn_best_ip | PASS | 0.23s | best_ip=173.222.248.89，HTTP 200，233ms |

汇总：**6 PASS / 1 FAIL / 1 SKIP**，脚本退出码 1。

三轮结果对照（同一脚本）：

| 轮次 | 代码时点 | 结果 | store SOCKS | store HTTP CONNECT |
|---|---|---|---|---|
| 第 1 轮 | proxy 03:17 / cli 03:22 | 5 PASS / 2 FAIL / 1 SKIP | 2/3（1 次 10s 超时） | 超时 10s |
| 第 2 轮 | 同上 | 6 PASS / 1 FAIL / 1 SKIP | 2/3（1 次 SSLEOF 63ms） | PASS 409ms |
| 第 3 轮（最终） | proxy 03:27:37 | 6 PASS / 1 FAIL / 1 SKIP | 3/3（401–885ms） | SSLEOF 60ms |

## 4. `python -m svpn status`

```
进程     运行中 pid=5076
端口     127.0.0.1:7899  监听中
端口     127.0.0.1:7900  监听中
节点     未配置（纯直连）
模式     whitelist
上次统计 31 连接 / 下行 0.24MB / 错误 0
            23x www.baidu.com
             6x store.steampowered.com
             2x steamcdn-a.akamaihd.net
```

| 断言 | 实际 | 结果 |
|---|---|---|
| 端口「监听中」 | 7899/7900 均监听中 | PASS |
| pid 正常 | 5076，pid 文件一致 | PASS |
| stats 有连接数 | connections=31 > 0 | PASS |

## 5. `python -m svpn down --keep-system`

```
[+] 已停止 pid=5076
[i] 上次 31 连接 / 下行 0.24MB
```

| 断言 | 实际 | 结果 |
|---|---|---|
| pid 文件消失 | False（已删除） | PASS |
| 端口释放 | 无 LISTENING（仅 TIME_WAIT 残留，属正常回收） | PASS |
| 无残留 `_serve` | 0 个 | PASS |
| `runtime/stats.json` | connections=31、down_mb=0.24、top_hosts 正常 | PASS |

统计落盘时序：起服务后 6.5s 首次读到 `uptime_s=15`（5s 周期落盘生效）；`down` 用 `taskkill /F` 后
`stats.json` 仍为最后一轮完整数据（uptime_s=35.1、connections=31）→ 新的周期落盘修复有效。

## 6. 失败详情与最小复现

### 6.1 主问题：store.steampowered.com 关键路径「快但不可靠」

10 次连续 SOCKS5 → store:443 → TLS → HEAD /（最终版本，03:26 前后）：

```
6/10 ok in <3s
0:200 470ms | 1:200 398ms | 2:ERR SSLEOFError 62ms | 3:200 407ms | 4:ERR SSLEOFError 62ms
5:ERR SSLEOFError 75ms | 6:200 408ms | 7:ERR SSLEOFError 58ms | 8:200 388ms | 9:200 390ms
```

- 成功样本稳定在 **388–541ms（远低于 3s 目标）**，说明修复后的直连拨号在「选对 IP」时是达标的。
- 失败样本全部在 **58–75ms 内 SSLEOF**：SOCKS/CONNECT 隧道已建立，但 TLS ClientHello 发出去就被 RST/EOF。
  这个耗时不可能是跨境 RTT，是本地/中间设备对**被污染 IP** 的典型响应。
- 同一时刻的对照：`curl --resolve store.steampowered.com:443:23.77.15.30` → `200 / 0.414s`；
  而系统 DNS 解析出的 IP（23.15.142.182 / 23.36.254.11 / 104.119.106.101）在 `tls_probe` 下
  为 `tcp TIMEOUT` 或 `sni_rst`。即：**候选列表里好 IP 与坏 IP 并存**。

最小复现（当前版本仍可复现）：

```powershell
cd D:\LE\AI\dshwork\steam-vpn
python -m svpn up -b --no-auto; Start-Sleep 6
1..10 | % { curl.exe -sS -o NUL -w "%{http_code} %{time_total}s`n" -I --max-time 5 `
  --socks5-hostname 127.0.0.1:7899 https://store.steampowered.com/ }
python -m svpn down --keep-system
```

- 期望：10/10 `200`，单次 < 3s
- 实际：约 6/10 `200`（0.39–0.54s），其余 `000`（0.06–0.24s，TLS 被 RST）；HTTP CONNECT 路径同类偶发
- 机理：`proxy._race_connect` 只竞速 **TCP 建连**，谁先 SYN-ACK 用谁；被污染 IP 由中间设备**立刻**完成
  TCP 三次握手（甚至比真边缘更快），于是抢先赢下竞速，随后 TLS 阶段被 RST，整条请求失败且不再换 IP。
  候选里通常还有可用 IP，但当前实现不会在 TLS 失败后 failover。
- 建议方向（未改实现）：把竞速判据上移到 TLS 握手（或连接后首次读写）成功；失败时对剩余候选再试一次。
- 附注：这类失败在 `stats.json` 里记为 `errors: 0`（隧道建立成功，TLS 失败发生在管道内），指标有盲区。

### 6.2 次要问题：`handle_http` 内部异常（本次实例日志复现）

最终轮 03:28:11（服务运行中，非 taskkill 时刻）日志原文（截取）：

```
03:28:11 ERROR   Task was destroyed but it is pending!
task: <Task pending name='Task-76' coro=<ProxyServer.handle_http() running at ...\svpn\proxy.py:488> wait_for=<_GatheringFuture pending ...>>
03:28:11 ERROR   Task was destroyed but it is pending!
task: <Task cancelling name='Task-82' coro=<pipe() running at ...\svpn\proxy.py:231> ...>
03:28:11 ERROR   Unhandled exception in client_connected_cb
transport: <_ProactorSocketTransport closed>
RuntimeError: cannot reuse already awaited coroutine
03:28:11 ERROR   Unhandled exception in client_connected_cb
transport: <_ProactorSocketTransport closed>
RuntimeError: cannot reuse already awaited coroutine
```

- 出现时机与 6.1 的「客户端在 CONNECT 后立刻 EOF 断开」高度吻合（第 1 轮 03:23:38 也出现过同样的两条）。
- 影响：连接已失败，但 handler 任务以异常结束、`pipe` 任务被遗弃（pending 销毁），有任务/fd 泄漏风险，
  且 `stats.errors` 不计数。
- 说明：`runtime/svpn.log` 是共享日志（多进程同时写入，历史里有重复的 `HTTP listening` 行），
  但上述 Task 编号（76/82）与本次实例 31+ 连接的规模吻合；无法 100% 排除混写。
- 建议：单独用 `SVPN_DEBUG=1 python -m svpn _serve` 在另一端口复跑 6.1 的复现命令观察。

## 7. 结论

- **CLI 生命周期**：`up -b` → pid/双端口 → `status` → `down --keep-system` 全部通过；
  修复后的 pid 判定（1.17s 内成功）与 5s 周期 stats 落盘均验证有效。**PASS**
- **基础代理能力**：国内直连（baidu，含明文绝对 URI）、CDN 选边 IP、20 并发 20/20 全通过，无串行阻塞。**PASS**
- **store.steampowered.com 关键路径**：延迟达标（成功时 0.39–0.54s），但**稳定性不达标**（6/10，HTTP CONNECT
  亦偶发），且失败时不会 failover → 按「<3s 且可靠」的目标判 **FAIL**。
- 直连对照本轮 SKIP：系统 DNS 给出的 store IP 当时全部不可达；用已知可用边缘 IP 直连实测 0.414s，
  与代理成功样本同量级，说明**代理本身没有明显额外开销**，问题在候选 IP 选择而非转发性能。
- 全程无后台残留：7899/7900 无 LISTENING、无 `_serve` 进程。

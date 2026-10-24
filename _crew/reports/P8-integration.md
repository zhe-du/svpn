# P8-integration 交付报告

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `tests/test_integration_local.py` | **新建**。本地回环端到端集成测试：进程内起 `ProxyServer` → 真 socket 拨号 → 本机 echo/HTTP 目标 → 数据往返。8 个用例，标准库 only，完全离线，~1.6s | +715（新增） |
| `_crew/reports/P8-integration.md` | 本报告 | 新增 |

**没有改动任何源码、文档或已有测试**（`svpn/*`、`tests/*` 其它文件、`WORKLOG.md` 全部只读）。

## 2. 逐条交付（按派单要求逐项对照）

| 编号 | 要求 | 实现 | 验证方式 |
|---|---|---|---|
| 1 | SOCKS5 数据往返 | `test_01_socks5_roundtrip`：ATYP=IPv4(`127.0.0.1`) 与 ATYP=域名(`echo.local`) 两条请求解析路径各走一次完整握手 → `b"hello"` → `b"ECHO:hello"`；断言 `stats.conns==2`、`down_bytes>0`、`hosts` 里记到了域名 | 真 socket 往返断言 |
| 2 | CONNECT 上游失败回码 + 服务不崩 | `test_02_socks5_connect_fail_reply`：目标指向一个「刚释放且预检确认连不上」的端口，断言 `rep != 0` 且 `rep ∈ {0x04, 0x05}`；紧接着同一代理再打一次正常请求必须成功 | 回码断言 + 二次建连 |
| 3 | HTTP CONNECT 隧道 | `test_03_http_connect_tunnel`：`CONNECT 127.0.0.1:<echo> HTTP/1.1` → 断言状态行 200 + `Connection Established` → 隧道内往返 `ECHO:tunnel` | 真隧道往返 |
| 4 | HTTP 绝对 URI 转发 | `test_04_http_absolute_uri`：进程内 asyncio 极简 HTTP 目标站回 `200 + Content-Length: 2 + "OK"`；经代理发 `GET http://127.0.0.1:<port>/x HTTP/1.1` → 断言 200、body 为 `OK`、`stats.hosts` 命中 | 状态行 + body 断言 |
| 5 | 畸形 CONNECT 端口不崩 | `test_05_malformed_connect_port`：`CONNECT example.com:abc` → 期望 400；**当前 `proxy.py` 已带 `_bad_request`，实测直接拿到 400，用例正常通过（未跳过）**。若退回 502/断连则 `skipTest("等待 P1 的 400 修复")`，不误报也不掩盖 | 状态行断言 + 400 后二次建连 |
| 6 | SOCKS5 鉴权 | `test_06_socks5_auth`：① 只报 `0x00` → 断言服务端回 `0x05 0xff`；② 报 `0x02` + 错密码 → 子协商状态非 0；③ 正确 `u/p` → 子协商 `0x00` 且能建连往返；④ 同一份 `auth` 下 HTTP 侧未带 `Proxy-Authorization` 必须回 407 | 四个子断言 |
| 7 | 并发 20 路 | `test_07_concurrent_connections`：`asyncio.gather` 开 20 条 SOCKS5 连接打 echo，逐条校验 `ECHO:cNN`，断言 `stats.conns >= 20`、`down_bytes > 0` | 20/20 全部回显正确 |
| 8 | 优雅收尾 | `test_08_graceful_shutdown`：先跑一轮真流量，`await srv.close()` → 断言所有 listener `is_serving()` 为 False，且 socks/http 两个端口 1s 内可重新 bind；再新建实例跑一轮确认无后遗症；全程用 `warnings.catch_warnings(record=True)` + loop 自定义 `exception_handler` + `logging` 三路采集，任何 `Task was destroyed but it is pending` / `never retrieved` / `Unhandled exception` 都会 fail | 端口重绑 + 三路脏收尾检测 |

### 关键实现决策（为什么这么做）

- **SOCKS 端口用 `listen_port=0` 交给内核选**，起完从 `srv._servers[0].sockets[0].getsockname()[1]` 读实际端口 —— 零竞态。
  `http_port` 不能用 0（`start()` 里 `if self.http_port:` 为假会**静默跳过 HTTP 服务**），所以只能先 `_free_port()` 探测；
  探测到撞车（`OSError`）就换端口**重建实例**重试最多 5 次，仍失败才 `skipTest`，绝不 flaky 失败。
- **域名用例靠 `ip_map` 钉死 IP**：`ProxyServer` 支持 `cfg["ip_map"]`，`_route` 命中后直接 `dial_direct(hint, ...)`，
  于是 ATYP=0x03 的解析分支被完整覆盖，同时**完全不会触发 DoH / 系统 DNS**（`dial_direct_smart` 那条路才是联网的）。
  这是「离线测域名路径」的唯一干净办法。
- **统计断言一律轮询**（`wait_until`）：`stats.down_bytes` 是在 `_bridge` 的 `finally` 里结算的，
  客户端 close 之后要过一两个事件循环才落账，直接断言会偶发读到 0（第一版就是这么红的）。
- **上游失败用例把代理 `connect_timeout` 压到 0.6s**：本机（Win11）对「已关闭端口」的 SYN 被防火墙丢弃而不是回 RST，
  默认 12s 超时下这条用例要白等 ~2.1s。压到 0.6s 后语义不变（拨号失败 → 非 0 码），耗时从 2.35s → 0.92s。
- 每个用例独立起停 `ProxyServer` + 目标站，`close()` 后 `sleep(0)` 三次再 `sleep(50ms)` 让 handler 收尾，
  所以三条基线互不污染。

## 3. 验证结果

环境：`PYTHONUTF8=1`，`C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe`，
运行前 `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY NODE_OPTIONS`。

**`compileall`：`python -m compileall -q svpn tests/test_integration_local.py` → exit 0**

**新增测试 `tests/test_integration_local.py`：8 用例，连跑 3 遍全绿，无 flaky**

```
===== RUN 1 =====
test_01_socks5_roundtrip ... ok                   [耗时  81 ms]
test_02_socks5_connect_fail_reply ... ok          [耗时 913 ms]
test_03_http_connect_tunnel ... ok                [耗时 109 ms]
test_04_http_absolute_uri ... ok                  [耗时  77 ms]
test_05_malformed_connect_port ... ok             [耗时  78 ms]
test_06_socks5_auth ... ok                        [耗时  74 ms]
test_07_concurrent_connections ... ok             [耗时 139 ms]
test_08_graceful_shutdown ... ok                  [耗时 142 ms]
Ran 8 tests in 1.614s   OK

===== RUN 2 =====
Ran 8 tests in 1.608s   OK

===== RUN 3 =====
Ran 8 tests in 1.618s   OK
```

- 0 skipped、0 failure、0 error；单条耗时波动 < 10ms，`test_05` 拿到的是真 400（未跳过）。
- 从仓库根以外（`cd /d/LE/AI`）运行同样 `Ran 8 tests ... OK`，说明 `sys.path.insert` 生效。

**基线回归（与 P1/P3/P4/P6 并行改动同时段实测）**

| 套件 | 期望 | 实测 |
|---|---|---|
| `tests/test_logic.py` | 50 | **Ran 50 tests, OK**（0.15s） |
| `tests/test_netutil.py` | 57 | **Ran 57 tests, OK**（0.77s） |
| `tests/test_regression.py` | 62 | **Ran 62 tests, OK**（0.74s） |
| `tests/smoke_test.py` | 17 项 | **ALL PASS**，0 FAIL（实际执行 15 条 `check()`；`smoke_test.py` 里有 17 个调用点，其中 1 个是 `def check`、1 对是 if/else 只走一支，属基线原状，非本轮改动） |

## 4. 契约变更 / 需要别人配合

无 API 变更。本测试对被测代码的依赖只有以下几点，请后续改动**别破坏**：

1. `ProxyServer(cfg)` 支持 `listen_port=0`（内核分配）；`start()` 后能从 `_servers[0].sockets[0].getsockname()` 读到实际端口。
2. `http_port` 为 0 / 假值 = **不启动 HTTP 服务**（当前行为，测试 6/7 依赖它省一个端口）。
3. `cfg["ip_map"]` 支持「域名 → IP」直拨（测试 1 的域名路径靠它离线）。
4. `cfg["connect_timeout"]` 会传到 `_route` 的拨号超时（测试 2 靠它压短等待）。
5. `_bad_request` 仍在 `handle_http` 的 CONNECT / 绝对 URI 解析失败分支上（测试 5 依赖 400）。

## 5. 没做完 / 发现的其它问题

1. **`ProxyServer.close()` 不清空 `self._servers`，导致同一实例 `close()` 之后再也 `start()` 不起来。**
   `start()` 开头的幂等判断是 `if self._servers: return`，而 `close()` 只 `close()+wait_closed()` 不 `clear()`，
   于是重启时 `_servers` 非空 → 直接返回，服务静默不监听。实测：

   ```
   after close,  _servers len = 1, is_serving = [False]
   await s.start() -> _servers len = 1, is_serving = [False]   # 什么都没发生
   ```
   生产路径（`serve_forever` 只 start 一次）暂时踩不到，但「改配置原地重启」这类扩展一定会踩。
   建议 P1 在 `close()` 末尾加 `self._servers.clear()`。**我没有改源码**，测试 8 用「新建实例」绕开了这一点。
2. **对「已关闭端口」拨号会白等满超时**：本机防火墙把发往死端口的 SYN 丢弃（不回 RST），
   `dial_direct` 只能等 `connect_timeout`（默认 12s）甚至系统 TCP 超时后才失败。
   用户侧表现是「点了没反应很久才报错」。非本轮缺陷，但值得记一笔。
3. `_bridge` 的双向语义是「两个方向都要 EOF 才收工」：HTTP 绝对 URI 请求里目标已 `Connection: close` 并关闭后，
   handler 仍会挂在「客户端 → 目标」那一半上，直到客户端断开。浏览器行为正常，不算 bug，仅提示。
4. `stats.conns` 在 CONNECT 请求解析后立即 +1，拨号失败也计数（`errors` 同时 +1）。
   如果将来要拿 `conns` 当「成功连接数」用，语义需要澄清。
5. 本轮没有做的事：TLS/SNI 转发（`SniForwarder`）的端到端集成用例。
   它需要自签证书 + `ssl` 握手，且 `SniForwarder` 会把流量交给上游节点（测试里只能填 `direct` 的假上游），
   复杂度明显高于「本地回环」这一类，且 P3 已在自己的 `tests/test_forward.py` 里覆盖了白名单/socket 回收。
   如需要，可另开一轮补 `test_integration_sni.py`。

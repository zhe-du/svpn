# P1-proxy 交付报告

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `svpn/proxy.py` | 新增配置容错转换（`_as_port` / `_as_timeout`）、`_close_writer` / `_bad_request`、`PIPE_SILENT` 白名单常量；重写 `pipe()` 异常白名单、`_bridge()` 收尾；`ProxyServer.__init__` 端口/超时容错 + `listen_port` 只读属性；`start()` 同端口预检；`handle_socks` / `handle_http` 拆成「薄壳 + `_socks_session` / `_http_session`」并修端口解析 | 550 → 677（净 +127） |
| `tests/test_proxy_hardening.py` | 新建，25 个用例，覆盖 5 类缺陷 + 绝对 URI 转发不回归 | 0 → 347（新建） |

未触碰任何其它文件（README / docs / WORKLOG / runtime / 别人的模块一律未动）。

## 2. 逐条修复

| 编号 | 原缺陷 | 修法 | 验证方式 |
|---|---|---|---|
| 1 | `_bridge()` 直接 `await gather(t1, t2)`：一侧抛异常立刻外穿，另一侧仍 pending，`finally` 里 cancel 了却从不 await → `Task was destroyed but it is pending` / `Unhandled exception in client_connected_cb` / `cannot reuse already awaited coroutine` | `gather(..., return_exceptions=True)`；`finally` 里先 `cancel()` 未完成的一半，再 `await gather(*tasks, return_exceptions=True)` 把它们**回收掉**（外层被取消时吞掉这一次 `CancelledError`，保证收尾一定执行）；两侧 writer 一律 `_close_writer()`；异常只 `log.debug` | `TestBridge` 4 例：一侧 `ConnectionResetError` / `RuntimeError` 不抛且两侧 close、字节统计正确、`_pending_tasks()==[]`；外层 `task.cancel()` 后同样无残留、writer 已关 |
| 2 | `pipe()` 白名单只有 `ConnectionResetError` / `BrokenPipeError` / `IncompleteReadError` / `TimeoutError`，`RuntimeError`、其余 `OSError` 直接炸出 pipe | 提到模块常量 `PIPE_SILENT = (OSError, IncompleteReadError, TimeoutError, RuntimeError)`；**不 catch** `CancelledError`（它是 `BaseException`，照常上抛，注释写明原因） | `TestPipe`：`RuntimeError` / `ConnectionResetError` / `BrokenPipeError` / `TimeoutError` / `IncompleteReadError` 全部静默且仍 `write_eof`；`CancelledError` 用 `assertRaises` 确认照常上抛；`write_eof` 自己抛 `OSError` 也静默 |
| 3 | `int(cfg.get("listen_port", 7899))` / `int(cfg.get("http_port") or 0)` / `float(connect_timeout)`：`"listen_port": null` → `int(None)` TypeError 直接崩进程 | 新增 `_as_port(value, default)`：`None` / 空串 / 空白 / `bool` / 非数字 / 超出 0–65535 → 默认值（7899 / 7900），字符串先 `strip()`；`_as_timeout()` 对 `None` / 非法 / 非正数回落 12.0。`upstream.port` 同样走 `_as_port` | `TestConfigTolerance` 9 例：`None`/`""`/`"   "`/`"abc"`/`"7900abc"`/`True`/`70000`/`-1`/`[1]` 全部回落 7899；`connect_timeout` 的 `None`/`""`/`"abc"`/`0`/`-2` 回落 12.0；合法值 `"8100"`/`"5"` 正常生效 |
| 4 | `start()` 在 `listen_port == http_port` 时第二次 bind 抛裸 `OSError 10048`，无预检、裸 traceback | 首个 bind 之前预检，抛 `ValueError(f"listen_port 与 http_port 相同: {self.port}")`（中文 + 端口号） | `test_same_port_raises_value_error`：`assertRaises(ValueError)`，消息含 `"7900"` 与 `"相同"`，且 `srv._servers == []`（不会「绑一半」） |
| 5 | `CONNECT host:abc` → `int("abc")` ValueError 穿出 handler；绝对 URI 分支 `int(port_s or 80)` 同样 | 两处 `int()` 各自 `try/except ValueError`，异常或 `not 0 < port < 65536` 或 `host` 为空 → `_bad_request()` 回 `HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n` 后返回（finally 关连接） | `TestHttpBadPort` 5 例（真实 127.0.0.1 监听）：`CONNECT example.com:abc`、`CONNECT example.com:99999`、`GET http://example.com:abc/`、`GET http://:80/` 均回 400 且带 `Content-Length: 0`；`HELLO\r\n\r\n` 畸形请求静默关连接 |
| 5b | `handle_socks` / `handle_http` 的 `except` 在 `except` 或 `finally` 内再抛（`drain()` 已断、writer 已关）时仍会外穿，asyncio 打印 `Unhandled exception in client_connected_cb` | 两个 handler 改成薄壳：`handle_socks` → `_socks_session`、`handle_http` → `_http_session`，外壳 `try / except Exception（stats.errors += 1 + log.debug）/ finally: _close_writer(writer)`，签名不变；内部原来的细分异常处理保留（用于准确日志） | 测试里给事件循环装了 `set_exception_handler` 收集器，上述 5 个请求跑完断言 `seen == []`，即 `client_connected_cb` 一次都没被触发 |
| 附 | handler 内多处 `writer.drain()` 在连接已断时会抛 `OSError` 把收尾流程截断 | 新增 `_close_writer()`（吞 `OSError` / `RuntimeError`）；socks 失败回包后的 `drain()` 也包了 `try` | 由上述用例与 smoke 覆盖 |
| 附2 | `listen_host: null` 会让 `asyncio.start_server(None, ...)` 绑到全网卡（等于开放代理，违反 SPEC §1.3） | 非字符串 / 空白 → 回落 `"127.0.0.1"` | `test_listen_host_never_falls_back_to_all_interfaces`：`None` / `""` / `"   "` / `123` 均得 `127.0.0.1` |

**额外要求（不改签名）**：`Upstream` / `dial` / `pipe` / `Stats` 签名零改动（`grep` 确认 P3 的 `SniForwarder` 复用面不变）；`ProxyServer` 新增只读属性 `listen_port`（返回 `self.port`，`self.port` 照旧保留），`upstream` / `stats` / `ip_map` / `auth` / `timeout` 字段名全部保留。

## 3. 验证结果

全部命令统一：`export PYTHONUTF8=1` + `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy` + `PY=C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe`

- `compileall`: **exit 0**
- 新增测试 `tests/test_proxy_hardening.py`：**25 用例，OK（0.40 s）**
- 基线回归：
  - `tests/test_logic.py` → Ran **50** tests, OK
  - `tests/test_netutil.py` → Ran **57** tests, OK
  - `tests/test_regression.py` → Ran **62** tests, OK
  - `tests/smoke_test.py` → **ALL PASS**（实际 16 条 `check()`；SPEC 写的「17 项」是把 `def check` 这行也算进去了，`grep -c "PASS"` = 16）
- 附加手工验证（不落盘、不进测试）：本地起 echo HTTP origin，经 `handle_http` 走绝对 URI 分支 `GET http://127.0.0.1:PORT/hello?x=1` → 回 `HTTP/1.1 200 OK`，origin 收到改写后的 `GET /hello?x=1 HTTP/1.1`；`sleep(0.4)` 后 `asyncio.all_tasks()` 只剩两条 `accept_coro`（监听本身），无 pipe/bridge 残留。即改 `_bridge` 没有破坏正向转发。
- `python -X dev tests/test_proxy_hardening.py`：stderr 零 `Warning`（无 `ResourceWarning` / 未回收 socket）。

## 4. 契约变更 / 需要别人配合

1. **`listen_port` 属性已就位**（P2 可直接读 `srv.listen_port`，返回值与 `srv.port` 相同；`srv.port` 保留，smoke_test 仍在用）。它是只读 property，**不要给它赋值**。
2. **`http_port` 的「键缺失」与「值为空」语义不同，P2/P6 请注意**：
   - 键**整个不写** → `http_port = 0`（只开 SOCKS5，历史语义，`smoke_test.py` 依赖）；
   - 键写了但是 `null` / `""` / `"abc"` / 越界 → 回落 **7900**（SPEC §4.4 的容错要求）。
   `DEFAULT_CONFIG` 里始终带 `http_port`，所以线上行为就是 7900，不受影响；但若 P2 想「只开 SOCKS5」，得显式传 `0` 而不是删键。
3. 新增模块级名字，P3 可直接复用，均无副作用：`_as_port` / `_as_timeout` / `_close_writer` / `_bad_request` / `PIPE_SILENT`，以及常量 `DEFAULT_LISTEN_PORT=7899`、`DEFAULT_HTTP_PORT=7900`、`DEFAULT_TIMEOUT=12.0`。
4. `handle_socks` / `handle_http` 现在是薄壳，真正逻辑在 `_socks_session` / `_http_session`。只调用、不继承的话无感；若有人想 override 单个阶段，改 `_*_session` 而不是外壳（外壳负责兜底与关连接）。
5. `upstream.port` 现在也走容错（非法 → `0`），P6 的数值归一可以放心交给代理侧兜底。
6. `listen_host` 为 `null` / 空白 / 非字符串时回落 `127.0.0.1`。**P2 若想做「只监听回环」的硬保证，可以直接信任构造后的 `srv.host`。**

## 5. 没做完 / 发现的其它问题

1. **`pipe` 把 `RuntimeError` 一起吞了**：能盖住 `cannot reuse already awaited coroutine`，但同样会盖住我们自己的编程错误（如 writer 状态机用错）。取舍按 SPEC §4.4 执行；代价是这类 bug 只留 `log.debug`，不冒泡。已在 `PIPE_SILENT` 上方注释标明。
2. **半开连接仍会等 TCP 超时**：`_bridge` 一侧 EOF 后另一侧要等对端 FIN 或超时才会退出（`asyncio.wait_for` 只包了 `dial`）。这是既有行为，不在本轮范围；WAVE2 文档的「已知限制」可写这一条。
3. **`_route()` 返回的 `decision` 在 socks 分支未被使用**（`(sr, sw), decision = ...`，`decision` 只用于日志缺失）。属无害冗余，未动，留给静态审计判定。
4. **`ProxyServer.serve_forever()` 的 `gather` 在任一 server 异常时仍会 cancel 掉其余**（未加 `return_exceptions=True`），与 `_bridge` 同类隐患；`cli` 侧是 `SIGINT` 收敛，实际影响小，本轮未改（不在 5 类清单内）。
5. 报告格式之外：`_crew/reports/` 目前只有 `P3-sni.md` 与本文件，其它 agent 未落盘。

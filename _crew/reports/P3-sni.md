# P3-sni 交付报告

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `svpn/forward.py` | 修 `SniForwarder` 的三个真实缺陷（空白名单放行 / 上游 socket 泄漏 / `gather` 无 `return_exceptions`）；删死函数 `_need`；新增 `_bound_port()` 把 `port=0` 的实际端口回写；`Forwarder` 同步改 `gather(..., return_exceptions=True)`；非 TLS 首包立刻拒绝不再傻等 10 秒 | 202 → 235（+33） |
| `tests/test_forward.py` | 新增离线单元测试 16 例（白名单语义 / ClientHello 手工构造解析 / 端到端转发 / 拒绝路径 / 端口回写 / 绑定冲突 / 无残留 task） | 新增 294 |

`extract_sni` 的签名与解析逻辑**一字未改**（`smoke_test.py` 依赖它）；除上面两个文件外没有触碰任何其它文件。

## 2. 逐条修复

| 编号 | 原缺陷 | 修法 | 验证方式 |
|---|---|---|---|
| 1 | `_allowed()` 在 `allow` 为空时 `return True` —— 白名单没配就等于「任意 SNI 全放行」，本机任意进程可把 127.0.0.1:443 当通用出口 | `_allowed()`：`host` 空或 `self.allow` 空 → 恒 `False`；匹配为「精确相等 或 `host.endswith("." + s)`」；`host` 自己再 `strip().lower().rstrip(".")`。`start()` 里空白名单打一次 `log.warning("SNI 白名单为空，全部拒绝…")`。`__init__` 归一改成 `strip().lower().lstrip(".")`，并丢弃空串项 | `TestAllowed` 4 例：`allow_suffixes=None/[]/[""]/["  "]` 均 `.allow == []` 且 `_allowed("a.com") is False`；`test_no_partial_label_match` 断言 `evil-akamai.steamstatic.com`、`akamai.steamstatic.com.evil.com`、`xakamai.…` 全部不命中；`test_case_and_leading_dot` 验证大小写、前导点、结尾根点 |
| 2 | `_handle` 成功路径只关 `writer`，`sw`（上游）永不关闭 → 每条 SNI 连接漏一个上游 socket | `sw: StreamWriter \| None = None` 提到 `try` 之前；`finally` 统一遍历 `(writer, sw)`，`None` 跳过、`close()` 包 `try/except OSError` | `test_data_forwarded_and_upstream_closed`：在客户端连上后给 `asyncio.open_connection` 挂 spy，抓 SniForwarder 拨上游拿到的那条 writer，转发完成并关闭客户端后轮询断言 `writers[0].is_closing() is True` |
| 3 | 两条 `pipe` 用裸 `asyncio.gather` → 一侧抛异常另一侧不被取消，且异常会穿到 `client_connected_cb`（asyncio 打印 `Unhandled exception`）；`finally` 只关 client 侧 | 改成 `gather(pipe(...), pipe(...), return_exceptions=True)`；函数体整体 `except Exception`（只 `stats.errors += 1` + `log.debug`，不向外抛；`CancelledError` 属 `BaseException`，取消语义不受影响） | `test_no_leaked_tasks`：连续 3 次完整转发后轮询 `asyncio.all_tasks()`（滤掉监听 socket 常驻的 `accept_coro`）为空 |
| 4 | 死函数 `_need()` 从未被调用 | 删除 | `grep -n "_need" svpn/forward.py` 无结果；`compileall` 通过 |
| 5 | `port=0` 时 `self.listen_port` 仍是 0，调用方拿不到真实端口 | 新增模块级 `_bound_port(server, default)`，`start()` 成功后从 `server.sockets[0].getsockname()[1]` 回写 `self.listen_port`（`Forwarder.start()` 同样处理） | `test_data_forwarded_and_upstream_closed` / `test_empty_allowlist_starts_but_rejects_all` 断言 `listen_port > 0` |
| 6 | （顺手）`start()` 绑定失败要能正常上抛 `OSError`，供 P2 降级 | `start()` 不做任何 try 包裹；`close()` 改为 `srv, self._server = self._server, None` 后再关，可重复调用 | `test_bind_conflict_raises_oserror`：第二个 forwarder 绑同一端口，`assertRaises(OSError)` |
| 7 | （顺手）非 TLS 客户端（例如 HTTP 明文打到 443）会让读头循环按 `data[3:5]` 当长度等满 10 秒超时，占着连接 | 读头循环里 `len(head) >= 5` 且 `head[0] != 0x16` 时立即 `break` → 走统一拒绝分支 | `test_reject_non_tls_payload`：发 `GET / HTTP/1.1…` 后 3 秒内读回 `b""` |
| 8 | （顺手）`Forwarder._handle` 同一个 `gather` 坑 | 同样改 `return_exceptions=True` | `compileall` + 基线回归（该分支当前未被 cli 接线，仅结构性修复） |

`Forwarder` 类保留，构造函数签名未变；`SniForwarder` 的构造/`start()`/`close()` 签名与 SPEC §4.2 完全一致（`(up, listen_port=443, allow_suffixes=None, target_port=443, stats=None, max_head=16384, ip_map=None)`），P2 可照 SPEC 的样例代码直接接线。

## 3. 验证结果

- `compileall`: `python -m compileall -q svpn` → **exit 0**
- 新增测试：`tests/test_forward.py` → **16 用例，OK，1.1 秒**（含进程启动约 3.7 秒；不联网，只用 127.0.0.1 随机端口）
- 基线回归：
  - `tests/smoke_test.py` → **ALL PASS**（17 项，含 `extract_sni` 的 ClientHello 提取与非 TLS 返回 None）
  - `tests/test_logic.py` → **50 用例 OK**
  - `tests/test_netutil.py` → **57 用例 OK**
  - `tests/test_regression.py` → **62 用例 OK**

> 备注：smoke 首次执行时因 7900 端口被并行作业（他人测试进程）占用而报 `WinError 10048`，非本次改动引起；随后复跑 **ALL PASS**。smoke 自身用固定端口 7899/7900，并行跑测试期间偶发冲突。

## 4. 契约变更 / 需要别人配合

无破坏性变更。给 P2-cli 的三点确认：

1. `await sni_srv.start()` 之后 `sni_srv.listen_port` 一定是**实际绑定端口**（`sni_port=0` 也行），可直接拿来打日志。
2. 空白名单不再全放行：若 `cdn.hosts` 被用户清空，转发层会**全部拒绝**并只打一次 warning。P2 的 `allow = cdn_cfg.get("hosts") or list(cdnmod.CDN_DOMAINS)` 写法正好兜住这种情况，建议保持（即别把空列表当成「放行」）。
3. `start()` 绑定失败会抛 `OSError`（`EADDRINUSE`），按 SPEC 里的 `except OSError` 降级即可；`close()` 可重复调用，`finally` 里直接 `await sni_srv.close()` 安全。

## 5. 没做完 / 发现的其它问题

1. **`gather(..., return_exceptions=True)` 的固有局限（未改，属设计取舍）**：`return_exceptions=True` 的语义是「等两个 pipe 都自然结束」，并不会在第一个结束后取消另一个。若客户端只写不发、而对端也永不回包/不关连接，`s2c` 方向会一直挂着，直到 TCP 超时。彻底做法是 `asyncio.wait(FIRST_COMPLETED)` + `cancel()` 另一侧，但 SPEC §4.2 明确要求用 `gather(..., return_exceptions=True)`，本轮按契约执行；`svpn/proxy.py::_bridge` 是同样的形态（P1 范围内）。
2. **`_handle` 读头固定 10 秒超时、且 `max_head` 满了也不单独报错**：目前超时/超长都落到统一拒绝分支，行为正确但慢（10 秒）。是否缩短留给后续调优，本轮只加了「非 TLS 首字节立刻退」。
3. **`ip_map` 命中时连的是裸 IP，SNI 与证书校验由客户端自己做**，这里不做任何校验——本文件本来就只是透明字节管道，无新增风险，仅记录。
4. 未发现 `extract_sni` 的解析 bug（`smoke_test`、手工构造的多组畸形输入均符合预期）。IDNA 还原分支（`.xn--`）未单独加用例，因为现有构造器只发 ASCII 域名，且该分支 `except (UnicodeError, ValueError)` 已兜底。

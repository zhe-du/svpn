# P4-netutil 交付报告

## 1. 改了哪些文件

| 文件 | 改动摘要 | 行数变化 |
|---|---|---|
| `svpn/netutil.py` | `http_head` 状态行/响应头解析重写（新增 `_status_code` / `_parse_head`）；`tls_over` 失败即关 socket；`tls_probe` 握手失败路径回收 raw socket、成功分支证书解析整体兜住；新增 `_close_quietly`；`_doh_query` 全字段校验（非 200 / 截断 JSON / Answer 类型 / data 类型与合法性）；`resolve` 逐个 provider 整体兜异常 + 超长域名短路；`resolve_merged` 绝不抛、全挂返回 `([], "none")`；`parse_node_url` 端口 1–65535 校验 + 裸 IPv6 歧义报错（新增 `_split_bare`）；`open_via` 补文档说明三条分支的回收责任 | 609 → 737（+128） |
| `tests/test_netutil_extra.py` | 新增 42 用例，覆盖畸形状态行、端口边界、裸 IPv6、DoH 四类降级、socket 异常路径回收、域名边界、`sni_block_test` 三态 | 新增 470 |

未动任何其它文件；`tests/test_netutil.py`（57 基线）一字未改。

## 2. 逐条修复

| 编号 | 原缺陷 | 修法 | 验证方式 |
|---|---|---|---|
| 1 | `http_head` 的 `int(lines[0].split()[1])` 遇空行 / 只有 `HTTP/1.1` / `200OK` / 非 ascii 会穿 `IndexError`/`ValueError`；`k, v = line.split(":", 1)` 只在 `":" in line` 时安全，`": x"` 这种空名行会写进空字符串 key | 拆出 `_status_code()`：要求 `len(parts)>=2`、首段以 `HTTP/` 开头、状态码恰好 3 位数字、100–599，否则 `0`；拆出 `_parse_head()`：用 `partition(":")` 代替 `split`，空名行跳过、值 `strip()`；`http_head` 的 `settimeout/sendall/recv` 包 `except (OSError, ValueError)` → `(0, {})` | `test_netutil_extra.py::TestHttpHeadMalformed`（10 种畸形 + 正常/边界码/网络错误/无冒号头，共 5 用例） |
| 2 | socket 泄漏：`tls_probe` 的 `context.wrap_socket(raw)` 抛异常时 `raw` 从不关闭（每探测一次漏一个 fd）；`tls_over` 在 `settimeout`/`wrap_socket` 抛异常时把调用方传进来的 sock 丢在原地（`probe.py:65-71`、`cdn.py:88-89` 都是 `tls = tls_over(sock, ...)` 一步到位，抓不到）；`_race_connect` 失败分支见 §5 | `tls_probe`：`wrap_socket` 外层改 `except BaseException` → `_close_quietly(raw)` 后再按 `ConnectionResetError/socket.timeout/ssl.SSLError/OSError` 分类返回（非 `Exception` 仍 `raise`）；`tls_over`：`except BaseException: _close_quietly(sock); raise`；新增统一清理函数 `_close_quietly`（吞掉 close 自身的异常）；`tcp_probe`/`download_bench`/`socks5_connect`/`http_connect` 逐个复核，均已在 `finally` 或 `except BaseException` 里关，未改 | `TestSocketReclaim`（8 用例，用计数 close 的假 socket：tls_over wrap 失败 / settimeout 失败 → `closed==1`，成功时不提前关；tls_probe 四种握手失败 → raw `closed==1`，成功 → 只关 TLS；open_via 的 socks5 读失败 / HTTP 407 / 空响应 → `closed==1`；direct 失败正常上抛） |
| 3 | `resolve_merged` 只处理了部分异常：截断 JSON（`json.JSONDecodeError`）、非 200、`Answer` 非 list、`data` 缺失/非字符串/非 IP 都会漏；且 `resolve` 里 `f"DoH:{url.split('/')[2]}"` 自己也在 try 之外，provider 串畸形就 `IndexError` 穿出 | `_doh_query`：校验 `resp.status`（缺失时回落 `code`）必须 200、`json.loads` 包 `ValueError/UnicodeError`、payload 必须是 dict、`Answer` 为 `None` 视为正常空结果 / 非 list 抛错、每条 answer 必须 dict + `type` 匹配 + `data` 是 str + `ipaddress` 能解析且版本匹配，全部过滤后去重保序；`resolve`：把整段循环体（含 source 拼接）包进 `try/except Exception: continue`，`got` 非 list/tuple 直接跳过；`resolve_merged`：`resolve` 与 `system_resolve` 各包一层 `try/except Exception`，合并后为空时把 `""`/`"fail"` 归一成 `"none"` 返回 | `TestDohDegrade`（9 用例：截断 JSON / 503 / `Answer:null` / `Answer` 为 dict / payload 为 list / 脏 answer 过滤 / 全挂 → `([], "fail")` 与 `([], "none")` / `resolve` 抛 RuntimeError 时 `resolve_merged` 仍返回 `([], "none")` / 来源标签 `DoH:doh.pub+system` 未变） |
| 4 | `parse_node_url` 的 `port=0`、`70000`、`-1` 都能过（`Node.usable` 只在 `port>0` 挡住 0，负数/超界一路带到 `connect()`）；`socks5://::1:1080` 这种裸 IPv6 会被当成整段主机名 + 默认端口，静默连错地方 | 端口 `int()` 之后新增 `if not 1 <= port <= 65535: raise ValueError(f"端口超出范围 1-65535: {port}")`；新增 `_split_bare()`：冒号 ≥2 时若尾部是纯数字且去掉后仍是合法 IP 字面量，判为「地址:端口」歧义 → 抛中文错 `裸 IPv6 有歧义，请用方括号把地址括起来：socks5://[::1]:1080`；`2408::1`（去掉 `:1` 后 `2408:` 不是合法地址）仍按完整地址 + 默认端口处理 | `TestNodeUrlBoundaries`（8 用例：0/65536/-1/99999/`[::1]:70000` 报错，1 与 65535 正常，两种裸 IPv6 报错且信息含「裸 IPv6」「方括号」，`2408::1` 回归保护，方括号/`p@ss`/默认端口/`socks4`/空串旧行为不变） |
| 5 | `sni_block_test` 的三种返回与 `control_ip` 的 TTL/refresh 需要复验；`tls_probe` 成功分支里只有 `cert_cn`/`version` 的提取被 try/except 包住，`wrapped.getpeercert()` 本身抛异常仍会穿出（`out` 还会 UnboundLocal） | 复验：`CONTROL_TTL=300`，`refresh=True` 跳过缓存，`race_tls` 空结果时 `_CONTROL_IP.pop`，三态 `True/False/None` 未改；顺手把 `tls_probe` 成功分支改成先建好 `out`（含空 `cert_cn`/`version`），证书解析整体再包一层 `except Exception`，`finally` 里 `_close_quietly(wrapped)` | `TestSniBlockTriState`（6 用例：缓存命中只算一次、`refresh=True` 必重算、找不到对照 IP 清缓存、`None`/`True`/`False` 三态）；`TestSocketReclaim::test_tls_probe_closes_on_success`（`getpeercert` 返回空 dict 也不炸） |
| 6 | 顺手核对 `tcp_probe` 等其它 socket | `tcp_probe` 已有 `finally: close()`；`download_bench`、`socks5_connect`、`http_connect` 都已有回收；`_recv_exact` 不分配 socket。全文件已无「分配后无回收」的路径（`_race_connect` 在 `proxy.py`，见 §5） | 通读 + 上述 `TestSocketReclaim` |

## 3. 验证结果

环境：`export PYTHONUTF8=1`；PY = `C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe`（实际 3.13.14）

- `compileall`：`python -m compileall -q svpn` → **exit 0**
- 新增测试：`tests/test_netutil_extra.py` → **42 用例全绿，0.24s**（不联网：DoH 用假 `urlopen`，DNS/TCP/TLS 全 patch）
- 基线回归（硬指标）：
  - `tests/test_netutil.py` → **57 / OK**（文件未改动）
  - `tests/test_logic.py` → **50 / OK**
  - `tests/test_regression.py` → **62 / OK**
  - `tests/smoke_test.py` → **ALL PASS（17 项）**
- 备注：`smoke_test.py` 第一次跑时 `127.0.0.1:7900` 被占用（并行 agent 同时在跑），重跑即 ALL PASS，与本次改动无关。

## 4. 契约变更 / 需要别人配合

函数签名全部未变，但有几处**返回值语义**需要知会调用方（都已是最保守取向）：

1. `http_head()` 失败时返回 `(0, {})`。SPEC §4.5 写的是 `(0, b"")`，但本函数声明的返回类型是 `tuple[int, dict[str, str]]`，返回 `b""` 会让任何 `headers.get(...)` 的调用方 `AttributeError`。空 `dict` 与空 `bytes` 同为假值，语义等价。**另外它现在不再抛异常**（`settimeout/sendall/recv` 的 `OSError`/`ValueError` 一律变 `(0, {})`），对 `probe.py`/`cdn.py`/`discover.py` 是纯改善（原来 `cdn.py:_bench` 的 `try/finally` 没有 `except`，`http_head` 抛异常会直接穿到线程池）。
2. `tls_over()` 失败时会**关掉传入的 sock** 再抛。调用方（`probe.py`、`cdn.py`、`discover.py`）看到异常时不需要也不能再用那条 sock；成功路径不变。
3. `resolve()` 新增来源值 `"invalid"`（域名长度 > 253 时短路，不再浪费一次 DoH 超时）；`resolve_merged()` 两家全挂时来源从 `"fail"` 改为 `"none"`，`"(空)"` → `"empty"` 保持不变。
4. `parse_node_url()` 现在会对 `port ∉ [1,65535]` 和「裸 IPv6 + 冒号端口」抛 `ValueError`（中文消息）。`cli.py` 的 `--node` / `up --url` 已有 `ValueError` 捕获，不需要改。

## 5. 没做完 / 发现的其它问题

1. **`proxy.py:157-160`（`_race_connect`，属 P1）**：`for task in done: if task.exception() is None: return task.result()` —— 同一批 `done` 里若有多个 IP 同时连上，只有第一个被返回，其余的 `(reader, writer)` 既不 `close()` 也不 cancel（它们已完成），transport 由 event loop 持有 → 每个多成功批次漏 1~3 个 fd。这与 `runtime/review.md` 的 P2 条目同源，**我没有改（不是我的文件），请 P1 在 `_race_connect` 里补：非采用者立即 `w.close()`**。
2. `probe.py:110-116`（`_node_probe`）在 `tls_over` 成功后会 `tls.close()`，`finally` 里又 `sock.close()`。因为 `wrap_socket` 会把 fd 从原 socket detach，第二次 close 是无害空操作 —— 现在没问题，但若以后有人改成 `sock.detach()` 之外的写法要留意。
3. `netutil.resolve()` 只做去重不做排序，被污染的记录与真实记录混在一起交给调用方（`proxy.py` 只竞速前 4 个）。`runtime/review.md` 建议的「前 4 个里至少 2 个来自 DoH」属于 `proxy.py`/`resolve_merged` 的取样策略，本次未改（会改变竞速行为，且不在本任务范围）。
4. 没有在真机上回归「`svpn probe` 全流程」——需要节点和网络，按 SPEC 约束不做联网测试。

# svpn 架构与数据流

面向要改这个工具的人。README 讲「怎么用」，这里讲「为什么这么写、改哪里会炸」。

---

## 1. 总览

```
                      ┌──────────────── 浏览器 / Steam 内置 CEF ────────────────┐
                      │  WinINET 系统代理（可选）  或  PAC（可选）              │
                      └───────────────┬─────────────────────────────────────────┘
                                      │ 127.0.0.1:7900 (HTTP CONNECT)
┌──────────────┐    SOCKS5 7899      ▼
│ Steam 客户端 │──────────────► ┌──────────────────────────────────────────┐
│  (可用 env)  │                │  ProxyServer            svpn/proxy.py     │
└──────────────┘                │  handle_socks / handle_http               │
                                │        │                                  │
                                │        ▼                                  │
                                │  Router.decide(host)   svpn/rules.py      │
                                │    ├── direct   → dial_direct_smart()     │
                                │    └── upstream → dial(Upstream)          │
                                └────────┬───────────────────┬──────────────┘
                                         │                   │
                            直连：DoH+系统 DNS 合并      节点：SOCKS5 / HTTP CONNECT
                            多 IP happy-eyeballs          （含用户名密码）
                                         │                   │
                                         ▼                   ▼
                                   目标站点 / CDN        Clash / v2rayN / 机场

下载加速的两条旁路（互斥，由 cdn.enabled 切换）：

  A. cdn.enabled=false（默认，无常驻）
     cdn 选边 → runtime/cdn-cache.json → hosts 受管块 → 域名解析到「实测最快边缘 IP」

  B. cdn.enabled=true（SNI 透明转发，需常驻）
     hosts 受管块 → 域名解析到 127.0.0.1 → SniForwarder 读 ClientHello 的 SNI
                  → 命中 cdn-cache 的 best_ip 就直连边缘，否则 dial(upstream)
```

进程内只有一个 asyncio loop。`ip_map`（domain→最优 IP）在 `cli._serve_cfg()` 里从缓存注入
（`cdn.enabled` 时也同时注入给 `SniForwarder`），`ProxyServer._route()` 命中就直连该 IP，
失败自动回退到正常解析。

---

## 2. 六条主链路

### 2.1 `up`：起服务

> `cli.build_parser()` 一共注册 **13 个条目**：`init / up / down / status / probe / node / cdn / hosts /
> system / pac / steam / doctor` 这 **12 个是公开子命令**，第 13 个 `_serve` 是内部命令——它只有
> `--system-proxy` 一个开关，由 `up -b` 的父进程按用户传的选项拼好再 `Popen`，用户不需要也无法直接用它。

1. `cli.cmd_up()` 读 `config.json`（`load_config()` = `merge_config(json.loads(...))`）。
2. `--port` 越界 → `return 2`；`--node URL` → `netutil.parse_node_url` → `discover.save_upstream` 写盘。
3. 已有 pid 且进程活着 → 提示「已在运行」并 `return 0`，**不重启**（`read_pid()`）。
4. 没有可用上游且 `auto_node` 且没给 `--no-auto` → `discover.scan()`：配置 / 环境变量 / WinINET 系统代理 /
   14 个常见端口（排除自己），逐个 `verify()`（真实 SOCKS5/HTTP 握手 → TLS `store.steampowered.com`
   → HEAD 取状态码）；只有 `steam_ok` 的才写进 `config.json`。
5. 端口预检（全部在 `asyncio.run` **之前**，避免「协程建了没 await」）：
   - `listen_port == http_port`（且 http 非 0）→ `return 1`，给可照抄的命令；
   - `_port_busy()`（裸 bind 试探）占用 → 打印 `_port_owner()`（netstat+tasklist）→ `return 1`；
   - `cdn.enabled` 时再查 `sni_port`：与自身端口相同 → `return 1`；被别的程序占 → **只警告**（可选增强，不拖垮主服务）。
6. `-b`：`Popen([python,-m,svpn,_serve], DETACHED|NEW_GROUP)`，pid 写 `runtime/svpn.pid`，
   轮询「子进程存活 + 端口已被监听」最多 3s 判定就绪（预检已确认起跑前端口空闲，所以 busy 只可能是自己）；
   失败则打印日志尾部并删 pid → `return 1`。
7. 前台：`asyncio.run(_serve_async(cfg, system_proxy))`。

### 2.2 `_serve_async`：真正的服务体

顺序固定（`cmd_serve` 与 `up` 都走它）：

```
setup_log()                      # runtime/ + svpn.log
写 runtime/svpn.pac              # build_pac(listen_host, listen_port, proxy_suffixes, direct_suffixes, mode)
ProxyServer(_serve_cfg(cfg))     # 注入 ip_map
await srv.start()                # SOCKS5:listen_port + HTTP:http_port（http_port=0 则不开）
sni_srv = await _start_sni()     # ★ cdn.enabled 才启动；OSError 只警告并返回 None
system_proxy → 改 WinINET        # 放在起来之后，失败也不影响已有监听
await srv.serve_forever(STATSFILE)
finally: close sni_srv → close srv → 打统计 → 还原 WinINET
```

`_start_sni()` 就是 SPEC §4.2 那段接线的成品（`svpn/cli.py`）：

```python
SniForwarder(srv.upstream, sni_port, allow_suffixes=allow, stats=srv.stats,
             ip_map=cdnmod.best_ip_map(CDNCACHE, max_age=_cache_ttl_hours(cfg) * 3600))
```

- `allow` 来自 `_sni_allow(cdn_cfg)`：`cdn.hosts` 为空时退回 `CDN_DOMAINS`，**永不返回空表**
  （空表在转发层等于「全部拒绝」，那是保底而不是配置错误）；
- 成功打印「SNI 透明转发 127.0.0.1:443 白名单 N 个域名」；`OSError` → 打印 WARN 并返回 `None`，
  主服务照常跑（下载退回 hosts 直指 IP）。

### 2.3 CONNECT 隧道（HTTPS）

```
client ──CONNECT host:443──► handle_http → _http_session
   ├─ auth_required → 校验 Proxy-Authorization（Basic），失败 407
   ├─ 目标端口非法 → 400（_bad_request，绝不把 ValueError 抛出去）
   ├─ _route(host,443)
   │    ├─ Router.decide → upstream/direct
   │    ├─ direct + ip_map 命中 → dial_direct(ip)
   │    └─ direct 未命中 → dial_direct_smart
   │           resolve_merged(DoH + 系统) → _race_connect(前 4 个 IP, 0.12s 阶梯)
   ├─ 回 "HTTP/1.1 200 Connection Established"
   └─ _bridge()：双向 pipe，最后统一 close，字节数写 Stats
```

`_bridge()` 现在的合同（P1 修完后的样子，别改回去）：

- 两个方向各自 `create_task`，`gather(..., return_exceptions=True)`；
- `finally` 里：**没结束的先 `cancel()`，再 `await gather(*tasks, return_exceptions=True)`** 回收，
  否则解释器关 loop 时骂 `Task was destroyed but it is pending`；
- `finally` 里关两侧 writer（`_close_writer` 吞 `OSError`/`RuntimeError`）；
- `pipe()` 的静默异常白名单是 `PIPE_SILENT = (OSError, IncompleteReadError, TimeoutError, RuntimeError)`；
  **`CancelledError` 是 `BaseException`，不在白名单里，必须继续上抛**。

### 2.4 HTTP 绝对 URI（明文）

`_http_session` 解析绝对 URI，剥掉 `Proxy-*` / `Connection` 头，重写成 origin-form
（`GET /path HTTP/1.1`）再转发；响应直接盲转发（不解析 body，不做 chunk 重排）。
非 `http://` 的绝对 URI 一律 400。

### 2.5 `probe`：三层定性 + 控制 IP 复测

```
_direct_probe(host)
  resolve_merged()                     # DoH 真实记录 + 系统记录，去重
  use_tls ? race_tls(前 8 台)           # 并发送 TLS，取最快成功
      → ok: 再开一条取 HEAD 状态码
      → 全失败: probe_many(前 3 台) 看每台的分层结果
              reason ∈ {sni_rst, tcp, tls}
              if reason == tcp: sni_block_test(host)   # 见下
                   True  → 改判 sni_rst（"控制IP复测RST"）
                   False → 保持 tcp
                   None  → 保持 tcp，附注"对照不可用"
```

`sni_block_test` 是这套工具的核心判据：拿一台**确实能握手**的 IP（`control_ip`，
默认取 `store.steampowered.com` 的最快 IP，缓存 5 分钟），把 ClientHello 的 SNI 换成目标域名。
同一 IP 换 SNI 就被 RST ⇒ 阻断发生在 SNI 层，换 IP/DNS 全部无效，只能走节点。

`_node_probe` 用同一套 `open_via` + `tls_over` + `http_head`，但目标换成上游。

**`summarize()` 是退出码的唯一事实来源**（`svpn/probe.py`）：

| 键 | 含义 |
|---|---|
| `node` | 本次用的节点 URL（无则 `None`） |
| `direct_ok` | 结论为「直连可用」的域名 |
| `need_node` | 结论以「需节点」开头的域名 |
| `dead` | 结论以「不通」开头的域名（真的待排查） |
| `unusable` | `_usable()` 为假：直连不通 **且** 节点也不通（含没节点） |
| `ok` | `not unusable` |

`cli.cmd_probe()` 返回 `1 if unusable else 0`；加 `--lenient` 时返回 `1 if dead else 0`（旧语义）。
前五个键是历史契约（测试与 `--json` 都依赖），`unusable` 是只增不改的新键。

### 2.6 `cdn`：选边

```
for domain in CDN_DOMAINS:
    resolve_merged()                    # 全量 A 记录（DoH 常给十几条）
    race_tls(全量候选, timeout=3)        # 并发送 TLS，淘汰不可达 / 证书不对
    if bench and domain 有下载路径:
        GET Range: bytes=0-1MB          # 真实吞吐，只测握手最快的前 6 台
    按 (有吞吐 > 无吞吐, -吞吐, 握手ms) 排序
→ runtime/cdn-cache.json（带 ts）
```

`hosts apply` 取每域名 `best_ip`，**只允许 `cdn.hosts` 白名单里的域名**（防止被篡改的缓存把任意域名
指到别的 IP），TTL 由 `cdn.refresh_hours`（默认 6h）控制，`_serve_cfg` / `best_ip_map` / 表格星号
三处共用 `_cache_ttl_hours()`。

---

## 3. hosts 的两副面孔（`cli._hosts_write()`）

| | `cdn.enabled = false` | `cdn.enabled = true` |
|---|---|---|
| 条目 | `{best_ip: "domain1 domain2 …"}`，按 IP 聚合 | `{"127.0.0.1": "domain1 domain2 …"}`（`cdn.hosts` 全量） |
| 依赖缓存 | 是（没有就提示先 `cdn --refresh`，`cmd_hosts` 也会先跑一轮选边） | **否**（SNI 模式下缓存只影响转发的 ip_map，不影响 hosts 内容） |
| 落地方式 | `sysx.apply_hosts(entries, RUNTIME)` | 同一个函数 |
| 生效条件 | 写完即生效，不需要常驻 | **必须 `svpn up` 常驻**，否则这些域名全部连不上 |
| 端口 | 无关 | 隐含 443：hosts 条目表达不了端口，所以 `cdn.sni_port` 实际必须是 443 |

两条路径都**只走 `sysx.apply_hosts()`**，绝不自己拼文本写 hosts 文件——因此
`sanitize_entries()`（合法 IP + 合法域名 + 拒绝主机名位置出现 IP / 换行 / `#` / 标记伪造）
对 SNI 模式同样生效；被拒条目会汇总成一行 `WARN`，成功条数取 `res["entries"]` 而不是
「打算写几条」，不虚报。

---

## 4. SniForwarder（`svpn/forward.py`）

```
__init__(up, listen_port=443, allow_suffixes=None, target_port=443,
         stats=None, max_head=16384, ip_map=None)
start() -> None      # 绑 127.0.0.1:listen_port；listen_port=0 时把内核分配的真实端口写回
close() -> None      # 可重复调用
```

单连接流程：

```
读到 ≥5 字节 → 首字节不是 0x16 就立刻拒绝（不傻等 10 秒）
            → 按 TLS record 长度收满 ClientHello（最多 max_head=16KB，单次读超时 10s）
extract_sni()                          # 解析 server_name，失败返回 None
_allowed(sni)                          # 空白名单 ⇒ False；命中 = 精确相等或 ".suffix" 结尾
ip_map.get(sni) 命中 → open_connection(hint, target_port)  # 失败则回退 dial(up, sni)
未命中            → dial(up, sni, target_port)             # 无上游时 dial 走直连
sw.write(head) 原样送 ClientHello → gather(pipe×2, return_exceptions=True)
finally: 关 writer 和 sw（sw 必须在 try 外初始化，否则成功路径泄上游 socket）
```

三个曾经的坑，都已经是回归测试（`tests/test_forward.py`）钉住的：

1. **空白名单 = 全部拒绝**（早期是「没配就全放行」，等于把 127.0.0.1:443 变成开放出口）；
2. **成功路径要关 `sw`**（早期只关客户端侧，每条连接泄一个上游 socket）；
3. **两条 `pipe` 必须 `return_exceptions=True`**（早期裸 `gather`，一侧异常另一侧卡住，
   还把异常抛进 `client_connected_cb` 打 `Unhandled exception`）。

`Forwarder`（固定目标端口转发）保留但**当前没有调用方**；`split.py` 同样未接线。

---

## 5. 数据结构

### 5.1 config.json（12 个顶层键，多一个少一个都算漂移）

| 字段 | 默认 | 说明 |
|---|---|---|
| `listen_host` | `127.0.0.1` | 只监听回环；`null`/空串也回落回环 |
| `listen_port` / `http_port` | `7899` / `7900` | SOCKS5 / HTTP；`http_port: 0` = 不开 HTTP |
| `mode` | `whitelist` | `whitelist` / `all` / `blacklist` |
| `connect_timeout` | `12` | 建连与握手超时（秒） |
| `auto_node` | `true` | `up` 是否自动发现节点 |
| `upstream` | `socks5/NODE_HOST:1080` | `kind`（`socks5`/`http`/`direct`）、`host`、`port`、`username`、`password`；`NODE_HOST` 视为未配置 |
| `auth` | 空 | 本地鉴权（SOCKS5 用户名密码 / HTTP Basic），空 = 不鉴权 |
| `browser` | `{apply_system_proxy, use_pac, bypass}` | WinINET 接管开关、PAC 开关、绕过表 |
| `cdn` | `{enabled, refresh_hours, sni_port, hosts[8]}` | §3 的两种接法 + 缓存 TTL + 白名单 |
| `proxy_hosts` | `[]` | 强制走节点（Router 显式覆盖） |
| `direct_hosts` | `[]` | 强制直连（Router 显式覆盖，优先级最高） |

规范化在 `merge_config()` → `_normalize()` 里做完：`_` 前缀键与未注册键丢弃（`logging.debug`）、
嵌套 dict 逐键合并、数值/端口/布尔/名单各自有归一器（`_as_number`/`_as_port`/`_as_bool`/`_as_text`/`_as_host_list`），
**任何非法值都回落默认值，绝不抛**。

`config.json` 里手写的 `proxy_suffixes` / `direct_suffixes` 不在模板的 12 键里，由 `EXTRA_KEYS` 单独放行：
`merge_config()` 先用 `cfg.get(key)` 取基线（这两键不在深拷贝里，取到 `None` 就走「整体赋值」分支），
再把用户值过 `_as_host_list()`（字符串按逗号/空格切、嵌套 list/dict/`None` 逐项丢、非 list 非 str 回落空表）。
它们与 `proxy_hosts`/`direct_hosts` 一起构成三级优先级：

```
direct_hosts（精确，先判） > proxy_hosts（精确） > proxy_suffixes > 内置 STEAM_SUFFIXES
                                          （direct_suffixes 只在 mode=blacklist 的默认分支里起作用）
```

语义要点：

- **整表替换**：`Router(proxy_suffixes=[...])` 与 `build_pac(proxy_suffixes=[...])` 都是
  `tuple(x or STEAM_SUFFIXES)` / `sorted(set(x or STEAM_SUFFIXES))`，所以空表 = 用内置表，非空 = 只剩你写的那些；
- **`mode=blacklist` 的回落**：`build_pac` 里 `fallback = CN_DIRECT_SUFFIXES if mode == "blacklist" else ()`，
  再 `direct = sorted(set(direct_suffixes or fallback))`——即 `blacklist` 模式下 `direct_suffixes` 为空时，
  PAC 仍会带上内置国内清单（否则「国内直连」名存实亡）；`Router` 侧同理（`direct_suffixes or CN_DIRECT_SUFFIXES`）。
  写非空值时是你的表生效，回落不再发生；
- 这两个键**不进 `config.example.json`**，所以模板的 12 键契约（`tests/test_config_contract.py`）不受影响；
  但 `init --force` 用 `DEFAULT_CONFIG` 重写文件时会静默丢掉它们（`node set/clear` 走「读原文件再写回」，不会丢）。

### 5.2 runtime/*.json

| 文件 | 结构 | 写入时机 |
|---|---|---|
| `stats.json` | `{uptime_s, connections, errors, via_upstream, via_direct, up_mb, down_mb, top_hosts}` | 每 5s + 退出 |
| `probe.json` | `{ts, node, rows[], summary{ok,direct_ok,need_node,dead,unusable,node}}` | 每次 probe（写前 `RUNTIME.mkdir`） |
| `cdn-cache.json` | `{updated, domains{domain:{domain,source,candidates,best_ip,best_ms,best_mbps,tested,ts,top[]}}}`；`candidates` 是数量 int，`top` 才是 IP 明细 | 每次 cdn |
| `wininet-backup.json` | `{ProxyEnable, ProxyServer, ProxyOverride, AutoConfigURL}` | 首次 init / 改代理前（只存第一次） |
| `hosts.backup` | hosts 原始字节副本 | 首次写 hosts 前 |
| `hosts.backup-uninstall` | 同上，卸载脚本删块前 | `uninstall.ps1` |
| `svpn.pid` | 进程号 | `up -b` |

`Stats.hosts` 超过 4096 条会裁剪到 Top 1024，避免长跑吃内存。

---

## 6. 分流决策表（`Router.decide`，自上而下短路）

| 顺序 | 条件 | 结果 |
|---|---|---|
| 1 | 在 `direct_hosts` | direct |
| 2 | 在 `proxy_hosts` | upstream |
| 3 | `localhost` / `127.0.0.1` / `::1` / `steamloopback.host` / `*.localhost` | direct |
| 4 | IP 且在 `STEAM_CIDRS` | **upstream** |
| 5 | IP 且 `is_private/loopback/link_local/multicast/reserved` | direct |
| 6 | 命中 `proxy_suffixes`（Steam/Valve 后缀）或 `proxy_cidrs` | upstream |
| 7 | `mode=all` | upstream |
| 8 | `mode=blacklist`：命中 `direct_suffixes` → direct，否则 upstream | — |
| 9 | 其余（whitelist 默认） | direct |

注意 4 在 5 之前：Valve 公网段优先于「私有段直连」，否则 Steam 的 CM/CDN 段会被误判。
`proxy_suffixes` / `direct_suffixes` 在这里是**构造函数参数**：`ProxyServer.__init__` 把
`cfg.get("proxy_suffixes")` / `cfg.get("direct_suffixes")` 直接传进来（用户没写就是 `None`），
`None` 或空表时回落到 `rules.py` 的内置表——完整语义见 §5.1 那段。

---

## 7. 失败分类学

| 层 | 判定 | 典型错误 | 处置 |
|---|---|---|---|
| DNS | `resolve_merged` 空（`source="none"`） | NXDOMAIN / DoH 全挂 | 换 DoH、查域名是否真实存在 |
| TCP | `tcp_probe` 失败 | `TIMEOUT` / `RST` / `WinError 10054` / `10060` | IP 被封锁或路由不通；有节点就走节点 |
| SNI | `tls_probe` → `sni_rst` | TCP 通，ClientHello 发出即 RST | **换 IP 无效**，只能走节点 |
| TLS | 握手报错但不是 RST | 证书、协议版本、被中间设备改写 | 看 `err`，可能是 TLS 拦截 |

`tls_probe` 的 `status` 只在这四类里取值；`sni_block_test` 返回三态（True/False/None），
None 表示连对照组都不通，这时结论里会写「对照不可用」，不要当成「没被阻断」。

`netutil` 的返回值契约（改这些函数的人要注意）：

- `http_head()` 失败返回 `(0, {})`，**不抛**；
- `tls_over()` 失败时会**先关掉传入的 sock** 再抛，调用方不能再复用那条 sock；
- `resolve_merged()` 全失败时 `source` 是 `"none"`，域名超长短路时 `resolve()` 给 `"invalid"`；
- `parse_node_url()` 对端口越界、裸 IPv6 抛 `ValueError`（中文消息）。

---

## 8. 四个关键取舍

1. **DoH + 系统 DNS 合并**：DoH 拿真实记录（防污染），系统 DNS 通常给就近 CDN 节点。
   两边都收，交给 TLS 竞速淘汰。代价：解析多花 0.2–0.6s（缓存 5 分钟摊掉）。
2. **多 IP happy-eyeballs**：Windows 的 `asyncio.open_connection(域名)` 按 getaddrinfo 顺序串行尝试，
   第一个 IP 被墙就卡满超时（store 上实测过 6s）。改成前 4 个 IP 阶梯并发，谁先通用谁。
3. **`hosts → 边缘 IP` vs `hosts → 127.0.0.1` + SNI 转发**：
   - 前者不需要常驻进程、不占 443、没有额外故障面；代价是 IP 漂了要手动重写 hosts。
   - 后者 IP 漂了只换缓存、hosts 不用动；代价是必须常驻、必须占 443、多一跳转发。
   - 两种都在，靠 `cdn.enabled` 选，因为目标用户分两类（随手加速 vs 长期挂着）。**没有默认开 SNI**，
     因为「进程一停下载全挂」这种失败模式对普通用户太贵。
4. **`gather(..., return_exceptions=True)` 而不是 `wait(FIRST_COMPLETED)` + `cancel`**：
   前者语义是「两个方向都自然结束」，稳、不会截断字节，但**半开连接会一直等 TCP 超时**
   （客户端只发不收、对端也不关）。后者省 fd，却要处理「另一侧还有没冲刷完的字节」。
   `_bridge` 与 `SniForwarder._handle` 都选了前者；真要改，必须两侧一起改并补测试。

---

## 9. 扩展点

| 要加什么 | 改哪里 | 注意 |
|---|---|---|
| 新域名走节点 / 直连 | `svpn/rules.py` 的 `STEAM_SUFFIXES` / `CN_DIRECT_SUFFIXES`，或配置里 `proxy_hosts`/`direct_hosts` | 后缀匹配是「等于或子域」，别写 `*.x.com` |
| 新 CDN 参与选边 | `svpn/cdn.py::CDN_DOMAINS`，有稳定下载路径就给 path | path 只用于 1MB 实测，写错会退化成只测握手 |
| 新上游协议（ss/vmess/trojan） | 不要改 `proxy.py`：外部内核（sing-box/xray）开本地 socks5，`node set socks5://127.0.0.1:PORT` | 自实现加密协议等于重写一个客户端 |
| 新 CLI 子命令 | `cli.build_parser()` + `cmd_xxx(args)` | 保持「返回 int、失败打印下一步」的约定；同步更新 `svpnctl.ps1` 的 `switch` |
| 新探测端点 | `probe.ENDPOINTS`（host, port, use_tls, 用途） | 只加真实存在的域名，否则会多一条 `dns` 死端点 |
| 换 PAC 策略 | `pac.build_pac(..., mode=)` | PAC 里必须是数组字面量 `[]`，对象 `{}` 会直接语法错误（历史翻车点） |
| 改 SNI 白名单来源 | `cli._sni_allow()` | 空表必须退回 `CDN_DOMAINS`，绝不能把空表当「放行」 |

---

## 10. 线程 / 并发模型

- 一个 asyncio loop；CPU 侧只有 DNS/TLS 探测走 `ThreadPoolExecutor`（`netutil`、`cdn`、`probe`）。
- `dial_direct_smart` 的 `resolve_merged` 用 `asyncio.to_thread`，不阻塞 loop。
- 每条连接两个 pipe task，`_bridge` 用 `gather(return_exceptions=True)` 等两边都结束再统一 close，
  并 cancel + 回收残留 task。
- 没有全局连接数上限：`asyncio.start_server(backlog=512)`，靠 OS fd 限制。
  真要限流，在 `handle_socks/handle_http` 入口加 `asyncio.Semaphore`。
- `_stats_loop` 是独立 task，`serve_forever` 退出时 cancel；`_dump_stats` 用 tmp + `os.replace`
  保证不会读到半个 JSON。
- `ProxyServer.close()` 会把 `_servers` 换成空列表，因此 `start()` 的幂等判断
  （`if self._servers: return`）不会把「关掉再起」卡死。

---

## 11. 已知边界

- `probe` 的 `cert_cn` 恒为空：探测用 `CERT_NONE`，`getpeercert()` 返回 `{}`。
- `hosts` 写入需要管理员；`_elevate` 用 `Start-Process -Verb RunAs`，UAC 被拒时只能手动跑
  `--no-elevate`，成败按 hosts 实际内容判定。
- `parse_node_url` 只认 socks5/http；`ss://`、`vmess://` 等链接需要外部内核转成 socks5。
- 半开连接会等到 TCP 超时（见 §8 第 4 条）。
- `svpn/split.py` 与 `forward.Forwarder` 未接线；`SniForwarder` 只在 `cdn.enabled=true` 时由 cli 启动。
- 可选键 `proxy_suffixes` / `direct_suffixes` 是**整表替换**，写错会把 Steam 从名单里挤出去；
  `mode=blacklist` 且未写 `direct_suffixes` 时会回落内置国内清单（见 §5.1）。

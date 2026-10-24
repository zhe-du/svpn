# svpn 收尾工程 · 并行作业规范（SPEC）

> **第一步必须先读本文件，再动手。** 本文件是 8 个并行子任务的唯一契约来源。
> 项目根目录：`D:\LE\AI\dshwork\steam-vpn`（下文相对路径均相对它）

---

## 0. 背景

`svpn` 是一个 Steam 专用本地分流器：SOCKS5 `127.0.0.1:7899` + HTTP `127.0.0.1:7900`，
按域名决定「直连 / 走上游节点」，另有一套 CDN 选边（DoH 拿边缘 IP → TLS 竞速 → 1MB 实测）。

上一轮工作日志（`WORKLOG.md` §7）留了一批**真实缺陷**未修，且交付面不自洽
（安装器缺 BOM、引用了不存在的脚本、`probe` 退出码无意义、配置与默认值漂移）。
本轮目标：**把这些全部修掉，让交付面自洽、测试全绿、文档与代码一致。**

## 1. 不可违背的硬约束

1. **零第三方依赖**：只用 Python 标准库。不许新增 `requirements.txt` / `pip install`。
2. **Python 3.10+ 兼容**：可以用 `X | None`、`match`（尽量别用）、`asyncio.timeout` 需 3.11+ → **不要用**。
3. **只监听 `127.0.0.1`**，不得改为 `0.0.0.0`；不得引入开放代理能力。
4. **不联网跑测试**：离线用例必须能在没有网络的环境下全绿。需要联网的用例必须**显式跳过**
   （`unittest.SkipTest` 或 `@unittest.skipUnless`），并在文件头注明。
5. **所有系统改动可回滚**：写 hosts / 系统代理必须先备份（已有 `runtime/` 机制，别破坏）。
6. **不改公开 API 签名**，除非本 SPEC 明写。其它模块依赖：`Node`、`resolve_merged`、`open_via`、
   `tls_over`、`http_head`、`race_tls`、`probe_many`、`sni_block_test`、`Upstream`、`dial`、`pipe`、
   `Stats`、`Router`、`ProxyServer`、`build_pac`、`extract_sni`。
7. **中文注释、中文输出**保持现有风格；代码里的字符串用 ASCII 直引号。
8. **不要碰**：`README.md`、`docs/*`、`WORKLOG.md`、`runtime/*`、`svpn/{cdn,discover,rules,system,steam,pac,split,__init__,__main__}.py`。
   这些要么第二轮统一改，要么属别人。

## 2. 文件所有权（互斥，只准写自己那几行）

| 代号 | 只准写这些文件 | 职责 |
|---|---|---|
| **P1-proxy** | `svpn/proxy.py`、`tests/test_proxy_hardening.py` | 代理内核健壮性 |
| **P2-cli** | `svpn/cli.py`、`tests/test_cli_contract.py` | CLI 崩溃点 / 退出码 / 配置归一 / SNI 接线 |
| **P3-sni** | `svpn/forward.py`、`tests/test_forward.py` | SNI 透明转发的三个真实缺陷 |
| **P4-netutil** | `svpn/netutil.py`、`tests/test_netutil_extra.py` | HTTP/TLS 解析健壮性 + socket 回收 |
| **P5-scripts** | `install.ps1`、`uninstall.ps1`、`start-svpn.cmd`、`svpn.cmd`、`svpnctl.ps1` | 安装/卸载/入口脚本 |
| **P6-config** | `config.example.json`、`config.json`、`tests/test_config_contract.py` | 配置规范化 + 契约测试 |
| **P7-tests** | `tests/test_pac.py`、`tests/test_fuzz.py` | PAC 语义 + 模糊测试 |
| **P8-integration** | `tests/test_integration_local.py` | 本地回环端到端集成测试 |

写别的文件 = 冲突。发现别人的 bug 不要自己改，**写进报告**。

## 3. 统一环境与命令

```bash
cd /d/LE/AI/dshwork/steam-vpn
export PYTHONUTF8=1
PY="C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe"

"$PY" -m compileall -q svpn                # 必须 exit 0
"$PY" tests/test_logic.py                  # 基线 50 用例
"$PY" tests/test_netutil.py                # 基线 57
"$PY" tests/test_regression.py             # 基线 62
"$PY" tests/smoke_test.py                  # ALL PASS 17 项
```

新建测试文件必须：`unittest.TestCase` + 结尾 `unittest.main(verbosity=2)`，
`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`，标准库 only，< 5 秒。

**注意沙箱污染**：跑网络相关命令前 `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY NODE_OPTIONS`。
离线测试不需要网络，别把网络用例混进来。

## 4. 跨模块契约（各 agent 必须按这个实现/调用）

### 4.1 配置键集（P2 定义，P6 镜像）

`svpn/cli.py` 的 `DEFAULT_CONFIG` 必须**恰好**是这 12 个键（顶层）：

```python
DEFAULT_CONFIG = {
    "listen_host": "127.0.0.1",     # str
    "listen_port": 7899,            # int
    "http_port": 7900,              # int
    "mode": "whitelist",            # whitelist | all | blacklist
    "connect_timeout": 12,          # int/float
    "auto_node": True,              # bool
    "upstream": {"kind": "socks5", "host": "NODE_HOST", "port": 1080,
                 "username": "", "password": ""},
    "auth": {"username": "", "password": ""},
    "browser": {"apply_system_proxy": True, "use_pac": False,
                "bypass": "<local>;localhost;127.*;10.*;172.16.*;192.168.*;*.cn"},
    "cdn": {"enabled": False, "refresh_hours": 6, "sni_port": 443,
            "hosts": list(cdnmod.CDN_DOMAINS)},
    "proxy_hosts": [],              # list[str]，Router 消费
    "direct_hosts": [],             # list[str]，Router 消费
}
```

规则：
- 顶层键以 `_` 开头（如 `_note`）或不在 `DEFAULT_CONFIG` 里的，**加载时丢弃**并 `logging.debug` 记一条。
- 数值键做归一：`listen_port`/`http_port`/`connect_timeout`/`upstream.port`/`cdn.refresh_hours`/`cdn.sni_port`
  为 `None` 或非法值 → 回落默认值，**不许抛 `TypeError`**。
- P2 必须新增 `merge_config(user: dict) -> dict`：把用户 dict 合并进 `DEFAULT_CONFIG` 的深拷贝，
  返回规范化后的配置；`load_config()` = `merge_config(json.loads(config.json))`（文件不存在则 `merge_config({})`）。
  P6 的契约测试直接 import 这个函数。
- P6 的 `config.example.json` 键集（忽略 `_` 开头的键）必须与 `DEFAULT_CONFIG` **完全相等**、类型一致。

### 4.2 SNI 透明转发（P3 实现，P2 接线）

`svpn/forward.py` 的 `SniForwarder` 必须提供：

```python
SniForwarder(up, listen_port=443, allow_suffixes=None,
             target_port=443, stats=None, max_head=16384, ip_map=None)
async def start(self) -> None      # 绑定失败抛 OSError
async def close(self) -> None
```

行为要求（这三条是硬性修复）：
1. **空白名单 = 全部拒绝**（`allow_suffixes` 为空/None → `_allowed()` 恒 False，并在 `start()` 里
   `log.warning` 一次）。绝不能是「没配就全放行」。
2. **成功路径必须关闭上游 socket `sw`**：`sw` 在 `try` 外初始化为 `None`，`finally` 里关 `writer` 和 `sw`。
3. 两条 `pipe` 用 `asyncio.gather(..., return_exceptions=True)`，取消后回收，不留
   `Task was destroyed but it is pending`。
4. 删掉死函数 `_need`。`extract_sni` 保持签名不变（`smoke_test.py` 依赖它）。
5. 域名匹配：精确相等或子域后缀（`.steamstatic.com` 这种前导点要能吃掉）。

P2 在 `_serve_async` 里接线（`cmd_serve` 与 `up -b` 都走它）：

```python
sni_srv = None
cdn_cfg = cfg.get("cdn") or {}
if cdn_cfg.get("enabled"):
    from .forward import SniForwarder
    allow = cdn_cfg.get("hosts") or list(cdnmod.CDN_DOMAINS)
    sni_srv = SniForwarder(srv.upstream, int(cdn_cfg.get("sni_port") or 443),
                           allow_suffixes=allow, stats=srv.stats,
                           ip_map=cdnmod.best_ip_map(CDNCACHE, max_age=_cache_ttl_hours(cfg) * 3600))
    try:
        await sni_srv.start()
        print(f"{OK} SNI 透明转发 127.0.0.1:{sni_srv.listen_port} 白名单 {len(allow)} 个域名")
    except OSError as exc:
        print(f"{WARN} SNI 转发端口被占用，跳过（下载加速退回 hosts 直指 IP）: {exc}")
        sni_srv = None
# ... finally: await sni_srv.close()  (若不为 None)
```

配套：`cmd_hosts` 的 `_hosts_write()` 在 `cdn.enabled` 为真时，**不写最优 IP，改写 `127.0.0.1`**
（因为要交给 SniForwarder 读 SNI），并提示「hosts 指向 127.0.0.1，需要 svpn up 常驻」。
`cmd_up` 的端口预检在 `cdn.enabled` 时也要检查 `sni_port`。

### 4.3 `probe` 退出码语义（P2）

现状：只有「不通 · 待排查」算失败，社区不可用也 exit 0 → CI 没法用。

改成：
- `summarize()` 增加 `unusable` 列表 = 该端点在当前条件下**怎么都用不了**的域名
  （`直连可用` → 可用；`需节点 · X` 且 node 为空或 node 也失败 → 不可用；`不通 · 待排查` → 不可用）。
- `ok = not unusable`。`cmd_probe` 返回 `0 if ok else 1`。
- 新增 `--lenient`：只在 `dead`（真的待排查）时返回 1，保留旧行为。
- 在输出末尾打一行退出码说明，让 `echo $?` 有据可依。
- **`summarize()` 返回的 dict 必须保留原有的 `node/direct_ok/need_node/dead/ok` 五个键**（有测试依赖），
  新键只增不改。

### 4.4 `proxy.py` 内部约定（P1）

- `_bridge()`：`gather(t1, t2, return_exceptions=True)`；`finally` 里关两侧 writer，
  被取消的任务要 drain 掉，不留 pending task 警告。
- `pipe()` 的异常白名单放宽到 `OSError`（含 ConnectionReset/BrokenPipe）、`TimeoutError`、
  `IncompleteReadError`、`RuntimeError`；**`CancelledError` 必须继续上抛**。
- `ProxyServer.__init__` 的 `listen_port`/`http_port` 用容错转换（`None`/`""`/非法 → 默认 7899 / 7900）。
- `ProxyServer.start()`：若 `self.port == self.http_port`，抛 `ValueError("listen_port 与 http_port 相同")`。
- `handle_http`：`CONNECT host:abc` / 绝对 URI 端口非法 → 回 `400 Bad Request` 并关连接，**不许 ValueError 穿出**。
- `handle_http` / `handle_socks` 整个函数体外面再包一层 `except Exception`，保证
  `client_connected_cb` 不打印 `Unhandled exception`。

### 4.5 `netutil.py` 健壮性（P4）

已知炸点，逐条修（可用 grep 定位）：
- `http_head()`：`int(lines[0].split()[1])` 遇到畸形状态行会 `IndexError/ValueError` → 返回 `(0, b"")`。
- `open_via()` / `tls_over()`：异常路径上的 socket 泄漏，确保 `close()` 一定执行。
- `resolve_merged()`：DoH 返回截断 JSON / 非 200 / `Answer` 非列表 → 跳过该 provider，不抛。
- `parse_node_url()`：端口必须在 1–65535；裸 IPv6（不带方括号）给出明确报错。

## 5. 报告格式（每个 agent 必须写）

写到 `_crew/reports/<代号>.md`（代号就是表里那串，如 `P1-proxy.md`），结构固定：

```markdown
# <代号> 交付报告

## 1. 改了哪些文件
| 文件 | 改动摘要 | 行数变化 |

## 2. 逐条修复
| 编号 | 原缺陷 | 修法 | 验证方式 |

## 3. 验证结果
- `compileall`: exit ?
- 新增/相关测试：X 用例，结果 ?
- 基线回归：test_logic 50 / test_netutil 57 / test_regression 62 / smoke 17 —— 是否仍全绿

## 4. 契约变更 / 需要别人配合
（没有就写「无」）

## 5. 没做完 / 发现的其它问题
```

报告写完 = 你的任务结束。**不要改 `WORKLOG.md`，不要汇报给我以外的人。**

## 6. 完成判定

- `compileall` exit 0
- 新增测试全绿
- 三条基线（50/57/62）+ smoke（17）仍全绿
- 报告落盘

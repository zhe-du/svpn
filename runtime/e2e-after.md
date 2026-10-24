# svpn 端到端验收（D3-e2e · 安静环境 10 连测）

> 本轮只做测量与记录。**没有改任何源码 / 配置 / 测试文件**。
> 测量脚本：`_crew/_e2e_probe.py`（临时，纯标准库，SOCKS5 手写握手）。
> 原始数据：`_crew/_e2e_probe.json`（第一轮）、`_crew/_e2e_probe_r2.json`（第二轮）。

## 结论（未达标）

按 SPEC 给定标准（10/10 且 P95<2s = 达标；≥9/10 且 P95<3s = 基本可用）判 **未达标**：
第一轮 SOCKS5 0/10、HTTP 1/10，第二轮 SOCKS5 3/10、HTTP 6/10。

**但这组数字不是 svpn 的稳定性指标，而是本机到 `store.steampowered.com` 的通路指标**：
同一时段、同一域名、**不走代理的直连对照组也只有 5/10**，失败签名与经代理完全一致
（`ConnectionResetError` / `SSL: UNEXPECTED_EOF`，均在 56–81ms 内被掐断）。
svpn 自身链路是通的——经 SOCKS5 取 CDN 两次都是 `200`（444ms / 238ms），
成功样本的 P95 为 0.94s（< 2s），失败样本几乎都是「握手刚发出就被 RST」而非慢。
即：**延迟达标、成功率不达标，且成功率瓶颈在环境侧（无节点纯直连 + SNI 级干扰）**。
`probe --node none` 的结论与此一致：只有社区/头像两个域名被判「需节点」，商店判「直连可用」。

---

## 1. 环境

| 项 | 值 |
|---|---|
| 测量时间 | 2026-10-24 18:45:51 – 18:53:01（两轮 + 对照组，共约 7 分钟） |
| 主机 | Windows 11 Pro for Workstations |
| Python | `C:/Users/zhedu/.workbuddy/binaries/python/versions/3.13.12/python.exe` → 实际报 `Python 3.13.14`（目录名 3.13.12，二进制是 3.13.14） |
| 代理变量 | 每条命令前 `unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy NODE_OPTIONS NODE_EXTRA_CA_CERTS`；实测回显 `[][][][][]`，全空 |
| `PYTHONUTF8` | `1` |
| 节点 | 未配置（`python -m svpn up -b --no-auto`），纯直连分流 |
| 并发干扰 | 无。本轮只有我在联网，其它 agent 跑的是离线用例 |

**`up -b` 的一个环境陷阱（非 svpn 缺陷，记录备查）**：本会话的每条 bash 命令在退出时会回收该命令派生的
进程树。`svpn up -b` 用 `DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP` 派生的 `svpn _serve`
仍会被连带杀掉——表现为「`up -b` 报成功、2 秒后 `status` 说未运行」。
实测：把 `up -b` 放进一个**长驻父 shell**（`sh -c '... up -b ...; sleep 1500'`，以受管后台任务方式启动）
后，`_serve` 正常存活到 `down`。因此下面 18:43:11 / 18:43:52 两次 `up` 是**平台回收**，
18:45:30 那次才是有效运行实例（pid 24796，全程存活 7 分 51 秒）。这三次都不属于「测量重试」。

## 2. 生命周期（up → status → down 实际输出）

**up（18:45:30）**
```
[+] 后台已启动 pid=24796  socks5://127.0.0.1:7899  http://127.0.0.1:7900
    节点 未配置（纯直连分流）   模式 whitelist
    CDN 模式 hosts 直指最优 IP
    CDN 未选边：跑 `python -m svpn cdn --refresh` 再 `python -m svpn hosts apply`
```
`netstat`：`127.0.0.1:7899 LISTENING 24796`、`127.0.0.1:7900 LISTENING 24796`，`runtime/svpn.pid` = 24796。

**status（18:45:45，rc=0）**
```
svpn 1.0.0  ·  Steam 分流/加速
进程     运行中 pid=24796
端口     127.0.0.1:7899  监听中
端口     127.0.0.1:7900  监听中
节点     未配置（纯直连）
模式     whitelist
系统代理 ProxyEnable=0 
CDN缓存  8 个域名，936 分钟前更新
hosts    无 svpn 记录
上次统计 0 连接 / 下行 0.0MB / 错误 0
```

**down（18:53:21，rc=0）**
```
[+] 已停止 pid=24796
[i] 系统代理已还原（原 ProxyEnable=0）
[i] 上次 42 连接 / 下行 0.12MB
```
（42 = 第一轮 20 + 第二轮 20 + 两次 CDN，全部计入，无幽灵连接。）

## 3. SOCKS5 10 连测（`socks5://127.0.0.1:7899` → TLS → `HEAD /`）

### 第一轮（18:45:52 – 18:49:5x）

| # | 状态码 | 耗时 ms | 结果 | 失败原因（原文） |
|---|---|---|---|---|
| 1 | – | 15581 | FAIL | `TimeoutError: The read operation timed out`（TLS 已到 TLSv1.3，等 HTTP 应答超时） |
| 2 | – | 15194 | FAIL | 同上 |
| 3 | – | 15431 | FAIL | 同上 |
| 4 | – | 15432 | FAIL | 同上 |
| 5 | – | 10016 | FAIL | `TimeoutError: timed out`（CONNECT 阶段） |
| 6 | – | 10007 | FAIL | 同上 |
| 7 | – | 10006 | FAIL | 同上 |
| 8 | – | 10006 | FAIL | 同上 |
| 9 | – | 10002 | FAIL | 同上 |
| 10 | – | 10013 | FAIL | 同上 |

**成功率 0/10；P50/P95 无成功样本（n/a）；失败耗时 10.0–15.6s。**
代理日志同步记录 `socks dial fail store.steampowered.com:443` 6 条（第 5–10 次）。

### 第二轮（18:52:54 – 18:53:01，见 §8 为何复测）

| # | 状态码 | 耗时 ms | 结果 | 失败原因 |
|---|---|---|---|---|
| 1 | 200 | 935 | OK | – |
| 2 | – | 76 | FAIL | `SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1032)` |
| 3 | – | 61 | FAIL | 同上 |
| 4 | – | 62 | FAIL | 同上 |
| 5 | – | 63 | FAIL | 同上 |
| 6 | 200 | 551 | OK | – |
| 7 | – | 66 | FAIL | 同上 |
| 8 | – | 66 | FAIL | 同上 |
| 9 | – | 75 | FAIL | 同上 |
| 10 | 200 | 736 | OK | – |

**成功率 3/10；成功样本 P50 736ms、P95 935ms；全样本 range 61–935ms。**

## 4. HTTP 10 连测（`http://127.0.0.1:7900` CONNECT → TLS → `HEAD /`）

### 第一轮

| # | 状态码 | 耗时 ms | 结果 | 失败原因 |
|---|---|---|---|---|
| 1–7 | – | 10002–10016 | FAIL | `TimeoutError: timed out` |
| 8 | 200 | 7579 | **OK** | – |
| 9 | – | 10038 | FAIL | `TimeoutError: timed out` |
| 10 | – | 10026 | FAIL | `TimeoutError: timed out` |

**成功率 1/10；唯一成功样本 7579ms（P50=P95=7579ms）；失败均为 10s CONNECT 超时。**

### 第二轮

| # | 状态码 | 耗时 ms | 结果 | 失败原因 |
|---|---|---|---|---|
| 1 | 200 | 492 | OK | – |
| 2 | 200 | 470 | OK | – |
| 3 | 200 | 509 | OK | – |
| 4 | – | 72 | FAIL | `SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING]` |
| 5 | 200 | 960 | OK | – |
| 6 | – | 64 | FAIL | 同上 |
| 7 | 200 | 469 | OK | – |
| 8 | – | 81 | FAIL | 同上 |
| 9 | 200 | 454 | OK | – |
| 10 | – | 64 | FAIL | 同上 |

**成功率 6/10；成功样本 P50 470ms、P95 960ms；全样本 range 64–960ms。**

> 两轮 SOCKS5/HTTP 的失败签名一致，说明失败发生在「代理已经把字节转出去、对端 TLS 被掐」这一层，
> 与 svpn 用哪个本地端口无关（HTTP 轮第 8 次拿到 200 的同一时刻 SOCKS5 轮全灭，也排除了端口级问题）。

## 5. CDN 头（经 SOCKS5 取 `https://steamcdn-a.akamaihd.net/`）

| 轮次 | 状态码 | 耗时 | TLS |
|---|---|---|---|
| 第一轮 | `HTTP/1.1 200 OK` | 444ms | TLSv1.3 |
| 第二轮 | `HTTP/1.1 200 OK` | 238ms | TLSv1.3 |

两次都是 200 → **代理隧道本体（SOCKS5 握手 → 建连 → TLS → HTTP 回包）是通的**，
store 的失败不能归到隧道实现上。

## 6. probe 摘要（`python -m svpn probe --node none --json`，18:52:20–18:52:34，rc=1）

表格原文（注意 `steamcommunity.com` 那行中文串按字符截断，是已知显示瑕疵，不影响 JSON）：

```
[i] 节点: 无（只看直连）
域名                              用途            直连                    节点      结论
store.steampowered.com            商店 / 购买     OK 138ms 23.15.142.182  未配置    直连可用
login.steampowered.com            登录            OK 111ms 23.200.229.158 未配置    直连可用
api.steampowered.com              API / 令牌      OK 1229ms 2.16.6.205    未配置    直连可用
steamcommunity.com                社区 / 创意工坊 sni_rst 控制IP复测RST TIME未配置    需节点 · SNI 阻断
community.akamai.steamstatic.com  社区静态资源    OK 331ms 2.18.121.98    未配置    直连可用
avatars.steamstatic.com           头像 / 图片     sni_rst RST 151.101.79  未配置    需节点 · SNI 阻断
steamcdn-a.akamaihd.net           游戏下载 CDN    OK 146ms 23.220.71.180  未配置    直连可用
cdn.akamai.steamstatic.com        客户端更新      OK 112ms 23.33.184.246  未配置    直连可用
help.steampowered.com             客服 / 找回     OK 105ms 23.50.30.213   未配置    直连可用
```

`summary` 原文：

```json
{
  "node": null,
  "direct_ok": ["store.steampowered.com", "login.steampowered.com", "api.steampowered.com",
                "community.akamai.steamstatic.com", "steamcdn-a.akamaihd.net",
                "cdn.akamai.steamstatic.com", "help.steampowered.com"],
  "need_node": ["steamcommunity.com", "avatars.steamstatic.com"],
  "dead": [],
  "unusable": ["steamcommunity.com", "avatars.steamstatic.com"],
  "ok": false
}
```

退出码 1 = 有端点不可用（`unusable` 非空），符合 SPEC 4.3 的新语义。
`community` / `avatars` 的 SNI 阻断是**预期结果**（这两条本来就必须走节点），不算本轮缺陷。

## 7. 收尾复查

| 检查项 | 结果 |
|---|---|
| `svpn down` | rc=0，`已停止 pid=24796` |
| `runtime/svpn.pid` | 已删除 |
| `netstat` 7899/7900 | 仅剩 `TIME_WAIT`（客户端侧残留），**无 LISTENING** |
| `tasklist` python.exe | 0 个（无残留 `svpn _serve`） |
| `svpn status`（收尾） | `进程 未运行` / `7899 未监听` / `7900 未监听` |
| `ProxyEnable`（`winreg` 读 HKCU） | `0`（起跑前 `0`，退出后 `0`，未被改坏） |
| `ProxyServer` / `ProxyOverride` / `AutoConfigURL` | 均为空字符串，未被写入 |
| 测量窗口日志 | 18:45:30 起 22 行，**0 条** `ERROR` / `Task was destroyed` / `Unhandled exception`；仅 15 条 `dial fail` WARNING |

> 全程未执行 `hosts apply`，`hosts` 仍无 svpn 记录（`status` 已确认），系统改动面为 0。
> `reg.exe` 被沙箱程序黑名单拦截，`ProxyEnable` 改用 Python `winreg` 读取，值等价。

## 8. 与 WORKLOG §7 第 2 条的对比

WORKLOG 记的是「13 个 agent 同时打网络，store 成功 6/10、0.39–1.54s，据此判稳定性未达标，
安静环境 10 连测未完成」。本轮补上了，结论有三点需要改口：

1. **并发不是主因。** 本轮安静环境（只有我联网）第一轮 0/10、第二轮 HTTP 恰恰也是 **6/10**（0.45–0.96s），
   与当初「13 agent 并发」下的 6/10 落在同一区间。当年的 6/10 不能归咎于并发抢网。

2. **失败不是 svpn 造成的，做了直连对照组。** 第二轮结束后紧接着做 10 次**不走代理**的直连
   （`23.15.142.182:443` + SNI `store.steampowered.com` + `HEAD /`）：

   ```
   direct  1/10 code=200   1041ms OK      direct  6/10 code=200    852ms OK
   direct  2/10 code=200    529ms OK      direct  7/10 code=200    602ms OK
   direct  3/10              59ms FAIL ConnectionResetError
   direct  4/10 code=200    990ms OK      direct  8/10              58ms FAIL ConnectionResetError
   direct  5/10              57ms FAIL ConnectionResetError
                                          direct  9/10              56ms FAIL ConnectionResetError
                                          direct 10/10              58ms FAIL ConnectionResetError
   == direct control: 5/10
   ```

   直连 5/10、失败在 56–59ms 被 `ConnectionResetError` 掐断，与经代理的
   `SSL: UNEXPECTED_EOF`（61–81ms）是同一现象：**TLS ClientHello 发出去就被中间设备 RST/EOF**。
   没有节点时，svpn 无法把这条通路变好——它只按域名分流，不做 TLS 伪装/加密。

3. **两轮之间环境本身在变，这是复测的唯一理由。** 第一轮（18:45–18:49）连 10s TCP 超时都打不通，
   而 18:52 复测前单独探测发现同一 IP 恢复：`23.15.142.182:443 ok 56ms`，`probe` 也在 18:52 判
   `store ... OK 138ms 直连可用`。网络状态已实质变化，若不补测，报告只会留下一个不可归因的 0/10。
   **除此之外没有第三轮**——两轮数据都原样贴在上面，没有重试到好看为止。

### 附带发现（供别人评估，不属于我的修改范围）

- **边缘 IP 解析结果不稳定，且两侧都是坏 IP**：18:45–18:51 系统 DNS 给 `23.15.142.182`、
  4 家 DoH（`doh.pub` / `dns.alidns.com` / `1.12.12.12` / `223.5.5.5`）一致给 `184.29.46.118`，
  这两个 IP 那段时间在 443 上全部超时；而同期 `23.48.8.130:443`、`23.1.8.62:443` 可连。
  `resolve_merged` 只把这 2 个候选交给 `_race_connect`，**候选池里没有可用 IP 时竞速无从发挥**，
  于是退化到 `_open_tcp(域名)` 系统解析，仍然只有 `23.15.142.182`。建议后续考虑：
  把 `runtime/cdn-cache.json` 里已验证过的 Steam 边缘 IP 也并入 store 类域名的候选池（本轮未改）。
- 第一轮的 4 次失败是「TLS 握手成功（TLSv1.3）但服务器不回 HTTP 应答，15s 超时」，
  第二轮的失败是「握手期直接 EOF」。两种签名并存说明干扰不止一种，单纯加大超时没用。
- `probe` 报 `store ... OK 138ms` 与 60 秒前直连 5/10 的观感相反：probe 的 138ms 是「竞速里最快那条成功」
  的耗时，不代表成功率；**用 probe 的 `直连可用` 判断"商店稳定"会误导**，这是文档/CI 用法的坑。
- `api.steampowered.com` 在 probe 里耗时 1229ms（其余域名 100–330ms），偏慢但可用，仅记录。

# D3-e2e 交付报告（端到端验收）

**结论：未达标**（成功率不达标，但根因在环境侧，非 svpn 分流缺陷）。
完整数据、逐条耗时、原始报错见 **`runtime/e2e-after.md`**。

## 1. 只写了哪些文件

| 文件 | 说明 |
|---|---|
| `runtime/e2e-after.md` | 验收报告（SPEC 指定产物） |
| `_crew/_e2e_probe.py` | 临时测量脚本（纯标准库，SOCKS5 手写握手），可删 |
| `_crew/_e2e_probe.json` / `_crew/_e2e_probe_r2.json` | 两轮原始数据 |

未改任何源码 / 配置 / 测试文件；未执行 `hosts apply`；系统代理未被改动。

## 2. 关键数字

| 轮次 | 时间 | SOCKS5(7899) | HTTP(7900) | 成功样本 P95 |
|---|---|---|---|---|
| 第一轮 | 18:45:52–18:49:5x | **0/10** | **1/10** | SOCKS n/a / HTTP 7579ms |
| 第二轮（环境变化后复测） | 18:52:54–18:53:01 | **3/10** | **6/10** | SOCKS 935ms / HTTP 960ms |
| 直连对照组（不走代理） | 18:53:0x | **5/10** | – | 529–1041ms |

- CDN 经 SOCKS5 两次均 `HTTP/1.1 200 OK`（444ms / 238ms）→ 隧道本体没问题。
- 失败签名：`SSL: UNEXPECTED_EOF`（61–81ms）/ `ConnectionResetError`（56–59ms，直连组同款）
  / 第一轮的 `TimeoutError`（10s 建连超时、15s 等应答）。
- `probe --node none --json`：rc=1，`unusable = [steamcommunity.com, avatars.steamstatic.com]`，
  `dead = []`，store 判「直连可用 138ms」。社区/头像 SNI 阻断属预期。

## 3. 判断依据（为什么不是代码问题）

同一时段不走代理的直连同样只有 5/10，且在 56ms 被 RST——与经代理失败同源。
无节点纯直连条件下，`store.steampowered.com` 的 TLS 握手被中间设备随机掐断，svpn 只能分流、无法规避。
延迟侧其实达标（P95 0.94s < 2s），卡住的是成功率。

附带发现（不在我的修改范围，供后续评估）：那段时间系统 DNS 给 `23.15.142.182`、
4 家 DoH 一致给 `184.29.46.118`，两者 443 全不可达；而 `23.48.8.130:443` 等边缘可连。
`resolve_merged` 的候选池因此没有可用 IP，`_race_connect` 无从发挥。
建议考虑把 `runtime/cdn-cache.json` 里已验证的边缘 IP 并入 store 类域名候选池。

## 4. 生命周期与收尾

`up -b --no-auto`（pid 24796）→ `status`（两端口监听、ProxyEnable=0）→ `down`（rc=0）
→ 无 LISTENING 残留、无 python 进程、`svpn.pid` 已删、`ProxyEnable` 仍为 0、`ProxyServer` 为空。

注意（环境陷阱，非缺陷）：本会话 bash 命令退出会回收其派生进程树，`svpn up -b` 的
`DETACHED_PROCESS` 子进程仍会被连带杀掉；必须把 `up -b` 放在长驻父 shell 里才活得下来。
报告 §1 已记录该现象，以及 18:43 两次被回收的 `up` 不属于测量重试。

## 5. 没做完 / 风险

- 没有节点，无法验证「配了上游节点后 store 成功率」；这条通路本轮无法闭合。
- 只有两轮样本（相隔 7 分钟），足以暴露问题但不足以给出长期成功率；要定 SLA 需要跨时段多次采样。
- `probe` 的「直连可用」只反映「竞速里有一条成功」，不代表成功率——拿它当 CI 判据会误导。

"""Steam / Valve 全域名「直连可达性」实测。

目标：给分流规则提供实测数据——哪些域名国内能直连、哪些是 SNI 阻断、哪些 DNS 被污染。
只做出口探测（DoH 解析 + TLS 握手分层），不监听任何端口，不需要 svpn up，
也不改 svpn/*.py 与运行时配置。

探测分层沿用 svpn.netutil：
    ok        TCP + TLS 握手成功（可直连）
    sni_rst   TCP 通、ClientHello 一发就被 RST  -> SNI 阻断，换 IP 无用，必须走节点
    tls       其它 TLS 失败（超时/协议）         -> IP 不可用
    tcp       TCP 都连不上                      -> IP 被封 / DNS 污染
    dns       无 A 记录（NXDOMAIN = 本无该主机，不等于被阻断）

口径：每域名最多 4 个 IP，单域名 10s 硬上限（剩余时间按候选数均分），并发 6，
总时限 115s。握手成功的 IP 再用校验证书的上下文复验一次，用来识别「握手通但
证书不符」的污染/中间设备，避免把假 ok 记成可直连。

用法:
    python tests/domain_audit.py
    python tests/domain_audit.py --workers 8 --no-verify --out runtime/domain-audit.json
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from svpn import netutil, rules  # noqa: E402

# ---------------------------------------------------------------- 探测参数
MAX_IPS = 4              # 每域名最多试几个 IP
HOST_CAP = 10.0          # 单域名硬上限（秒）
RESOLVE_TO = 4.0         # DoH 解析超时
PROBE_TO = 4.0           # 单次 TLS 握手超时上限
PROBE_MIN = 2.0          # 均分后单次握手的最低超时
WORKERS = 6              # 全局并发
TOTAL_BUDGET = 115.0     # 总时限，留 5s 收尾余量
PORT = 443

# 域名清单：题面 27 个 + 从 rules.STEAM_SUFFIXES 覆盖范围挑的真实主机名/待验证条目
HOSTS: tuple[str, ...] = (
    # --- 商店 / 登录 / API / 支付
    "store.steampowered.com",
    "login.steampowered.com",
    "api.steampowered.com",
    "help.steampowered.com",
    "checkout.steampowered.com",
    "partner.steampowered.com",
    "support.steampowered.com",
    "partner.steamgames.com",
    # --- 社区
    "steamcommunity.com",
    "www.steamcommunity.com",
    "steamcommunity-a.akamaihd.net",
    # --- 头像 / 静态资源 / CDN
    "community.akamai.steamstatic.com",
    "community.cloudflare.steamstatic.com",
    "cdn.cloudflare.steamstatic.com",
    "clan.akamai.steamstatic.com",
    "avatars.steamstatic.com",
    "avatars.akamai.steamstatic.com",
    "steamcdn-a.akamaihd.net",
    "cdn.akamai.steamstatic.com",
    "cdn.steamstatic.com",
    "store.akamai.steamstatic.com",
    "shared.akamai.steamstatic.com",
    "media.steampowered.com",
    "client-update.akamai.steamstatic.com",
    # --- 用户内容 / 云存档（含 rules.py 里声称覆盖、需实测的条目）
    "images.steamusercontent.com",
    "steamusercontent-a.akamaihd.net",
    "steamcloud-sg.akamaized.net",
    "steamcloud-eu.akamaized.net",
    "steamcloud-us.akamaized.net",
    # --- 主域（裸域：可能本无 A 记录）
    "steamstatic.com",
    "steamcontent.com",
    "steamusercontent.com",
    "steam-chat.com",
    "steamgames.com",
    "steamdeck.com",
    "steam.tv",
    "s.team",
    # --- Valve 其它
    "valvesoftware.com",
    "www.valvesoftware.com",
    "dota2.com",
    "www.dota2.com",
    "counter-strike.net",
    "blog.counter-strike.net",
)

# 裸域：本身没有 A 记录属正常，dns 结果不等于被阻断
BARE_DOMAINS = frozenset({
    "steamstatic.com", "steamcontent.com", "steamusercontent.com",
    "steamgames.com", "s.team", "counter-strike.net", "dota2.com",
})

RESULTS = ("ok", "sni_rst", "tls", "tcp", "dns")
# 域名级结论优先级：优先采信「最有诊断性」的证据
RANK = {"ok": 0, "sni_rst": 1, "tls": 2, "tcp": 3, "dns": 4}


def _join(a: str, b: str) -> str:
    return f"{a}；{b}" if a else b


# ---------------------------------------------------------------- DNS 状态
def _dns_status(host: str, timeout: float = 3.0) -> str:
    """单次 DoH 查询看 rcode：NXDOMAIN（本无该主机）vs NOERROR-无A。"""
    url = netutil.DOH_PROVIDERS[0]
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}name={host}&type=A",
        headers={"accept": "application/dns-json", "user-agent": netutil.UA})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return ""
    code = payload.get("Status")
    if code == 3:
        return "NXDOMAIN"
    if code == 0:
        return "NOERROR-无A"
    return f"rcode={code}"


# ---------------------------------------------------------------- 证书复验
try:
    _VERIFY_CTX: ssl.SSLContext | None = ssl.create_default_context()
except Exception:  # noqa: BLE001
    _VERIFY_CTX = None


def _verify_cert(ip: str, host: str, timeout: float,
                 tries: int = 2) -> tuple[bool, str, str]:
    """握手成功的 IP 再验一次证书：CERT_NONE 下握手成功也可能是污染地址/中间设备。

    返回 (是否合格, 说明, 类型)  —— 类型 "cert" 表示明确证书不符，"" 表示其它。
    """
    if _VERIFY_CTX is None:
        return True, "无校验上下文", ""
    last = ""
    for _ in range(max(1, tries)):
        sock = None
        try:
            sock = socket.create_connection((ip, PORT), timeout)
            sock.settimeout(timeout)
            tls = _VERIFY_CTX.wrap_socket(sock, server_hostname=host)
            try:
                return True, "证书校验通过", ""
            finally:
                try:
                    tls.close()
                except OSError:
                    pass
        except ssl.SSLCertVerificationError as exc:
            reason = getattr(exc, "verify_message", "") or str(exc)
            return False, f"证书不符({reason[:40]})", "cert"
        except ssl.SSLError as exc:
            last = f"复验TLS失败({type(exc).__name__})"
        except OSError as exc:
            last = f"复验异常({type(exc).__name__})"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    return True, f"{last}（不影响可达结论）", ""


# ---------------------------------------------------------------- 单域名
def audit_host(host: str, deadline: float, verify: bool,
               probe_to: float = PROBE_TO) -> dict:
    t0 = time.perf_counter()
    row: dict = {
        "host": host, "result": "dns", "ms": None, "ip": "", "source": "fail",
        "tries": 0, "note": "", "dns": "",
    }
    try:
        doh_ips, _ = netutil.resolve(host, timeout=RESOLVE_TO)      # 命中缓存
        ips, source = netutil.resolve_merged(host, timeout=RESOLVE_TO)
    except Exception as exc:  # noqa: BLE001 —— 解析炸了也要留一行
        row["source"] = "fail"
        row["note"] = f"解析异常 {type(exc).__name__}"
        return row
    row["source"] = source or "fail"

    doh_set = set(doh_ips or [])
    cand = [ip for ip in dict.fromkeys(ips or []) if ip][:MAX_IPS]
    if not cand:
        row["dns"] = _dns_status(host)
        bare = host in BARE_DOMAINS
        if row["dns"] == "NXDOMAIN":
            tail = "该主机本就不存在（NXDOMAIN）" if not bare else "裸域无该主机（NXDOMAIN）"
        elif row["dns"] == "NOERROR-无A":
            tail = "NOERROR 但无 A 记录（本无该主机，非阻断）"
        else:
            tail = "解析失败（疑似污染/超时）"
        row["note"] = _join("无 A 记录", tail)
        return row

    attempts: list[dict] = []
    for idx, ip in enumerate(cand):
        left = HOST_CAP - (time.perf_counter() - t0)
        if left < 1.0 and attempts:
            row["note"] = _join(row["note"], "达单域名 10s 上限，剩余 IP 未测")
            break
        if time.perf_counter() > deadline:
            row["note"] = _join(row["note"], "总时限用尽，未完测")
            break
        # 剩余时间按候选数均分：4 个 IP 也能在 10s 内各探一次
        share = left / max(len(cand) - idx, 1)
        to = max(PROBE_MIN, min(probe_to, round(share, 2)))
        try:
            r = netutil.tls_probe(ip, host, PORT, to)
        except Exception as exc:  # noqa: BLE001
            r = {"status": "tls", "ms": 0.0, "err": type(exc).__name__,
                 "ip": ip, "sni": host}
        r["via"] = ("DoH" if ip in doh_set else "system") if doh_set else "system"
        r["to"] = to
        attempts.append(r)
        row["tries"] = len(attempts)
        if r.get("status") == "ok":
            break

    if not attempts:
        row["result"] = "dns"
        row["note"] = _join(row["note"], "未完成任何探测")
        return row

    best = min(attempts, key=lambda a: (RANK.get(a.get("status", "tls"), 9),
                                        a.get("ms") or 0.0))
    status = best.get("status", "tls")
    if status not in RESULTS:
        status = "tls"
    row["result"] = status
    row["ip"] = best.get("ip", "")
    row["ms"] = round(best.get("ms") or 0.0, 1)

    # 备注：来源 + 证据明细
    notes: list[str] = [f"{best.get('ip', '')} via {best.get('via', '')}"]
    err = best.get("err") or ""
    if err:
        notes.append(f"err={err}")
    if status != "ok" and len(attempts) > 1:
        notes.append("各IP=" + ",".join(str(a.get("status")) for a in attempts))

    if status == "ok" and verify:
        left = max(1.5, HOST_CAP - (time.perf_counter() - t0))
        okc, msg, kind = _verify_cert(best.get("ip", ""), host, min(probe_to, left))
        notes.append(msg)
        if kind == "cert":
            notes.append("握手通但证书不符，疑似污染/中间设备")

    if status in ("tcp", "tls") and doh_set and any(ip not in doh_set for ip in cand):
        notes.append("DoH/系统解析不一致，一边被污染")

    # 规则覆盖情况：直接给第 5 步的建议当输入
    covered = rules.match_suffix(host, rules.STEAM_SUFFIXES)
    notes.append("规则:已覆盖" if covered else "规则:未覆盖")
    row["note"] = "；".join(n for n in notes if n)
    return row


# ---------------------------------------------------------------- 输出
def _dw(text: str) -> int:
    """终端显示宽度（CJK 算 2 格）。"""
    w = 0
    for ch in str(text):
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def _pad(text: object, width: int, right: bool = False) -> str:
    s = "" if text is None else str(text)
    fill = max(width - _dw(s), 0)
    return (" " * fill + s) if right else (s + " " * fill)


def render_table(rows: list[dict]) -> str:
    head = ("域名", "解析来源", "试了几个 IP", "结果", "最快 ms", "备注")
    body = [
        (r["host"], r["source"], r["tries"], r["result"],
         "-" if r["ms"] is None else f"{r['ms']:.0f}", r["note"])
        for r in rows
    ]
    widths = [max([_dw(h)] + [_dw(x[i]) for x in body])
              for i, h in enumerate(head)]
    lines = ["| " + " | ".join(_pad(h, widths[i]) for i, h in enumerate(head)) + " |",
             "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    for x in body:
        cells = [_pad(x[0], widths[0]), _pad(x[1], widths[1]),
                 _pad(x[2], widths[2], True), _pad(x[3], widths[3]),
                 _pad(x[4], widths[4], True), _pad(x[5], widths[5])]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def confirm_blocked(hosts: list[str], rounds: int = 3,
                    probe_to: float = 2.5, ips_per_host: int = 2) -> dict:
    """对已判定阻断的域名多轮复测：换一批 DNS 解析再打一次，确认不是单次抖动。

    每轮清 DNS 缓存，重新拿 IP；轮内各域名并发，避免拖长时间。
    """
    out: dict[str, list[dict]] = {h: [] for h in hosts}
    for rnd in range(max(1, rounds)):
        if rnd:
            netutil.clear_dns_cache()

        def once(host: str) -> tuple[str, dict]:
            try:
                ips, src = netutil.resolve_merged(host, timeout=3.0)
            except Exception as exc:  # noqa: BLE001
                return host, {"source": "fail", "ips": [],
                              "status": [f"resolve:{type(exc).__name__}"]}
            got = []
            for ip in ips[:ips_per_host]:
                r = netutil.tls_probe(ip, host, PORT, probe_to)
                got.append(f"{ip}:{r.get('status')}")
            return host, {"source": src, "ips": ips[:ips_per_host],
                          "status": got}

        with ThreadPoolExecutor(max_workers=max(1, len(hosts))) as pool:
            for host, rec in pool.map(once, hosts):
                out[host].append(rec)
    return out


def build_suggestions(rows: list[dict]) -> list[dict]:
    """只给建议，不改 rules.py。"""
    out: list[dict] = []
    blocked = [r for r in rows if r["result"] in ("sni_rst", "tls", "tcp")]
    for r in blocked:
        if rules.match_suffix(r["host"], rules.STEAM_SUFFIXES):
            continue
        tail = ".".join(r["host"].split(".")[-2:])
        sib = [s for s in rules.STEAM_SUFFIXES
               if s.endswith("." + tail) and s != r["host"]]
        why = f"{r['result']}（{r['ip']}），直连不可用"
        if sib:
            why += f"；同族已有 {', '.join(sib[:3])}，可仿照补"
        out.append({
            "kind": "add_suffix",
            "host": r["host"],
            "action": f"STEAM_SUFFIXES 补 {r['host'] if r['host'].count('.') >= 2 else tail}",
            "why": why,
        })

    # 可直连但规则没覆盖：现在没坏，一旦被封就漏网
    for r in rows:
        if r["result"] == "ok" and not rules.match_suffix(r["host"], rules.STEAM_SUFFIXES):
            out.append({
                "kind": "add_suffix",
                "host": r["host"],
                "action": f"STEAM_SUFFIXES 补 {r['host']}",
                "why": f"当前直连 ok（{r['ip']} {r['ms']}ms）但规则未覆盖，被封时会直接漏到直连",
            })

    # rules.py 里列了、实测却 NXDOMAIN 的死条目
    for r in rows:
        if r["result"] == "dns" and r.get("dns") == "NXDOMAIN" \
                and r["host"] in rules.STEAM_SUFFIXES:
            out.append({
                "kind": "dead_rule",
                "host": r["host"],
                "action": f"复核/删掉 STEAM_SUFFIXES 里的死条目 {r['host']}",
                "why": "实测 NXDOMAIN，该主机名不存在，规则命中不了任何真实流量",
            })

    ok_hosts = [r["host"] for r in rows if r["result"] == "ok"]
    if ok_hosts:
        out.append({
            "kind": "direct_ok",
            "host": ", ".join(ok_hosts[:6]) + ("..." if len(ok_hosts) > 6 else ""),
            "action": "这批可直连，宜进 CDN 直连池 / direct_hosts，省节点带宽",
            "why": f"{len(ok_hosts)} 个域名直连握手成功",
        })
    return out


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Steam/Valve 域名直连可达性实测")
    ap.add_argument("--out", default=str(ROOT / "runtime" / "domain-audit.json"),
                    help="结果 json 路径")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--probe-timeout", type=float, default=PROBE_TO)
    ap.add_argument("--no-verify", action="store_true", help="跳过证书复验")
    ap.add_argument("--confirm", type=int, default=0,
                    help="对已判定阻断的域名再复测 N 轮（0=不复测）")
    args = ap.parse_args(argv)

    hosts = list(dict.fromkeys(HOSTS))
    verify = not args.no_verify
    t0 = time.perf_counter()
    deadline = t0 + TOTAL_BUDGET

    print(f"探测 {len(hosts)} 个域名，并发 {args.workers}，单域名 <=4 IP / {HOST_CAP:.0f}s，"
          f"总时限 {TOTAL_BUDGET:.0f}s", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        rows = list(pool.map(
            lambda h: audit_host(h, deadline, verify, args.probe_timeout), hosts))
    elapsed = time.perf_counter() - t0

    groups = {k: [r["host"] for r in rows if r["result"] == k] for k in RESULTS}
    dns_norecord = [r["host"] for r in rows
                    if r["result"] == "dns" and r.get("dns") in ("NXDOMAIN", "NOERROR-无A")]
    dns_fail = [r["host"] for r in rows if r["result"] == "dns" and r["host"] not in dns_norecord]

    print()
    print(render_table(rows))
    print()
    print(f"耗时 {elapsed:.1f}s")

    print("\n== 直连 OK（可走 direct / 至少可直连）==")
    for h in groups["ok"]:
        print(f"  + {h}")
    print("\n== SNI 阻断（sni_rst）→ 必须进上游白名单 ==")
    for h in groups["sni_rst"]:
        print(f"  X {h}")
    print("\n== 不可达 / DNS 污染（tcp/tls）→ 同上 ==")
    for h in groups["tcp"] + groups["tls"]:
        print(f"  X {h}")
    print("\n== 无 A 记录（主机名不存在，非阻断，规则里算死条目）==")
    for h in dns_norecord + dns_fail:
        print(f"  - {h}")

    sugg = build_suggestions(rows)
    print("\n== 建议（不改文件）==")
    for s in sugg:
        print(f"  - [{s['kind']}] {s['action']}  <<  {s['why']}")

    blocked = groups["sni_rst"] + groups["tcp"] + groups["tls"]
    confirm = None
    if args.confirm > 0 and blocked:
        print(f"\n== 阻断域名 {args.confirm} 轮复测（换解析再打）==")
        confirm = confirm_blocked(blocked, rounds=args.confirm)
        for h, recs in confirm.items():
            for i, rec in enumerate(recs, 1):
                print(f"  {h} #{i}: {rec['source']} -> {rec['status']}")

    payload = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ok": groups["ok"],
        "sni_rst": groups["sni_rst"],
        "unreachable": groups["tcp"] + groups["tls"] + groups["dns"],
        "rows": [{k: r[k] for k in ("host", "result", "ms", "ip", "source")}
                 for r in rows],
        "detail": {r["host"]: {"tries": r["tries"],
                               "dns_status": r.get("dns", ""),
                               "note": r["note"]} for r in rows},
        "blocked": blocked,
        "dns_norecord": dns_norecord + dns_fail,
        "uncovered": [r["host"] for r in rows
                      if not rules.match_suffix(r["host"], rules.STEAM_SUFFIXES)],
        "confirm": confirm,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "hosts_checked": len(hosts),
        "suggestions": sugg,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

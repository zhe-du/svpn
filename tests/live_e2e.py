"""svpn 真机联网 E2E 验证：DoH / TLS / 全链路体检 / CDN 选边 / 缓存往返。

运行：python tests/live_e2e.py
只依赖标准库，可直接联网跑。任何一项网络失败都记 FAIL 并继续，
不会中途崩。退出码 0 = 全部 PASS（SKIP 不算失败）。

注意：本文件只做观测，不修改 svpn/ 下任何模块。
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svpn import cdn, netutil, probe  # noqa: E402

STORE = "store.steampowered.com"
COMM = "steamcommunity.com"
CDN_DOMAIN = "steamcdn-a.akamaihd.net"

RESULTS: list[tuple[str, str, float, str]] = []   # name, PASS/FAIL/SKIP, seconds, detail
STATE: dict = {}


# ------------------------------------------------------------------ 输出工具
def _flat(text: object, limit: int = 400) -> str:
    s = str(text).replace("\r", " ").replace("\n", " | ")
    return s if len(s) <= limit else s[:limit] + "…"


def _indent(text: str, prefix: str = "        ") -> None:
    for line in str(text).splitlines():
        print(f"{prefix}{line}")


def step(name: str):
    """装饰器：跑一步，捕获任何异常成 FAIL，记录耗时。"""
    def wrap(fn):
        def inner(*a, **kw):
            t0 = time.perf_counter()
            print(f"\n>>> {name}")
            try:
                ok, detail = fn(*a, **kw)
                status = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
            except Exception as exc:  # noqa: BLE001 —— 网络/断言异常都要收成 FAIL
                tb = traceback.format_exc(limit=3)
                print(tb.rstrip())
                status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
            secs = time.perf_counter() - t0
            RESULTS.append((name, status, secs, _flat(detail, 600)))
            print(f"<<< {status}  {name}  {secs:.2f}s  {_flat(detail, 300)}")
            return ok
        return inner
    return wrap


# ------------------------------------------------------------------ 1. DoH
@step("1. netutil.resolve(store) 走 DoH")
def t_doh():
    netutil.clear_dns_cache()                     # 强制真打 DoH，不吃缓存
    t0 = time.perf_counter()
    ips, source = netutil.resolve(STORE, timeout=6.0)
    ms = (time.perf_counter() - t0) * 1000
    print(f"    ips={ips[:6]}  source={source}  {ms:.0f}ms")
    STATE["store_ips"] = ips
    if not ips:
        return False, f"解析为空 source={source}"
    if not source.startswith("DoH"):
        return False, f"source={source}（未走 DoH）ips={ips[:3]}"
    return True, f"{len(ips)} ips via {source}, 首答 {ms:.0f}ms, ip[0]={ips[0]}"


# ------------------------------------------------------------------ 2. TLS
@step("2. netutil.tls_probe 对 store 真实 IP")
def t_tls():
    ips = STATE.get("store_ips") or netutil.resolve(STORE, timeout=6.0)[0]
    if not ips:
        raise RuntimeError("没有可用的 store IP（第 1 步解析失败）")
    tried = []
    result = None
    for ip in ips[:4]:
        r = netutil.tls_probe(ip, STORE, 443, timeout=6.0)
        tried.append(f"{ip}:{r.get('status')}")
        print(f"    {ip:<16} status={r.get('status'):<8} ms={r.get('ms')} "
              f"cn={r.get('cert_cn', '')} ver={r.get('version', '')} err={r.get('err', '')}")
        if r.get("status") == "ok":
            result = (ip, r)
            break
    if not result:
        return False, "全部 IP 未完成 TLS 握手: " + ", ".join(tried)
    ip, r = result
    STATE["store_ok_ip"] = ip
    return True, (f"{ip} ok {r.get('ms')}ms {r.get('version')} "
                  f"CN={r.get('cert_cn')}（尝试 {len(tried)} 个）")


# ------------------------------------------------------------------ 3. probe
@step("3. probe.run(node=None) 全表体检")
def t_probe():
    t0 = time.perf_counter()
    rows = probe.run(node=None, timeout=4.0)
    secs = time.perf_counter() - t0
    _indent(probe.format_table(rows))
    summ = probe.summarize(rows, None)
    print(f"    summarize: {json.dumps(summ, ensure_ascii=False)}")
    print(f"    {len(rows)} 行 / {len(probe.ENDPOINTS)} 端点，{secs:.1f}s")

    problems: list[str] = []
    if len(rows) != len(probe.ENDPOINTS):
        problems.append(f"行数 {len(rows)} != 端点 {len(probe.ENDPOINTS)}")

    store = next((r for r in rows if r.host == STORE), None)
    comm = next((r for r in rows if r.host == COMM), None)
    store_v = store.verdict if store else "<缺行>"
    comm_v = comm.verdict if comm else "<缺行>"
    print(f"    store  verdict = {store_v}")
    print(f"    comm   verdict = {comm_v}")

    # store 期望直连可用
    if store_v != "直连可用":
        d = store.direct if store else {}
        problems.append(f"store 非直连可用（实际 {store_v}；reason={d.get('reason')} "
                        f"err={_flat(d.get('err', ''), 60)}）")

    # steamcommunity 期望以「需节点」开头；node=None 时按 probe._verdict 逻辑会落
    # 「不通 · 待排查」，所以只如实比对并打印，不做会影响后续步骤的硬断言。
    if not comm_v.startswith("需节点"):
        d = comm.direct if comm else {}
        problems.append(f"comm 未给「需节点」（实际 {comm_v}；"
                        f"reason={d.get('reason')} err={_flat(d.get('err', ''), 60)}）")

    detail = f"store={store_v} / comm={comm_v} / {len(rows)} 行 / {secs:.1f}s"
    if problems:
        return False, detail + " ;; " + " ;; ".join(problems)
    return True, detail


# ------------------------------------------------------------------ 3b. 有节点时补测
@step("3b. probe.run(node=配置节点) —— 需配置真实节点")
def t_probe_node():
    cfg_path = ROOT / "config.json"
    if not cfg_path.exists():
        return None, "无 config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"config.json 读不了: {exc}"
    u = (cfg.get("upstream") or {})
    host = str(u.get("host") or "")
    if u.get("kind") not in ("socks5", "http", "https") or host in ("", "NODE_HOST"):
        return None, f"上游仍是占位 {u.get('kind')}://{host}:{u.get('port')} → 跳过"
    node = netutil.Node(kind="socks5" if u["kind"] == "socks5" else "http",
                        host=host, port=int(u.get("port") or 0),
                        username=u.get("username") or "",
                        password=u.get("password") or "",
                        label="config")
    ok, ms, err = netutil.tcp_probe(node.host, node.port, 4.0)
    print(f"    节点 {node.url()} 可达={ok} {ms:.0f}ms {err}")
    if not ok:
        return False, f"节点不可达 {node.url()} {err}"
    rows = probe.run(node=node, timeout=4.0)
    _indent(probe.format_table(rows))
    comm = next((r for r in rows if r.host == COMM), None)
    store = next((r for r in rows if r.host == STORE), None)
    detail = (f"store={store.verdict if store else '?'} / "
              f"comm={comm.verdict if comm else '?'}")
    print(f"    {detail}")
    need = [r.host for r in rows if r.needs_node]
    return (comm is not None and comm.verdict.startswith("需节点")), detail + \
        f" / 需节点 {len(need)} 个"


# ------------------------------------------------------------------ 4. CDN
@step(f"4. cdn.rank_domain({CDN_DOMAIN}, bench=True)")
def t_cdn():
    res = cdn.rank_domain(CDN_DOMAIN, bench=True, timeout=3)
    print(f"    source={res.source}  candidates={len(res.candidates)}  "
          f"edges={len(res.edges)}")
    for i, e in enumerate(res.edges[:5], 1):
        print(f"    #{i} {e.ip:<16} {e.ms:>7.1f}ms  {e.mbps:>6.2f}MB/s  "
              f"code={e.code} got={e.bytes_got}B err={e.err}")
    best = res.best
    d = res.to_dict()
    STATE["cdn_top"] = [e.to_dict() for e in res.edges[:3]]
    STATE["cdn_result"] = d
    if not res.edges:
        return False, f"无 edge（source={res.source}, candidates={len(res.candidates)}）"
    if not best or not best.ip:
        return False, "best_ip 为空"
    if not best.mbps:
        return False, (f"有 {len(res.edges)} 个 edge 但没测出吞吐；"
                       f"best={best.ip} {best.ms:.0f}ms err={best.err}")
    return True, (f"best={best.ip} {best.ms:.0f}ms {best.mbps:.2f}MB/s "
                  f"(edges={len(res.edges)}, source={res.source})")


# ------------------------------------------------------------------ 5. 缓存
@step("5. cdn.save_cache / load_cache / best_ip_map 往返")
def t_cache():
    cache = dict(STATE.get("cdn_result") and
                 {CDN_DOMAIN: STATE["cdn_result"]} or {})
    if not cache:
        cache = {CDN_DOMAIN: cdn.DomainResult(domain=CDN_DOMAIN,
                                              edges=[cdn.Edge(ip="127.0.0.1",
                                                              ms=1.0, mbps=1.0)]
                                              ).to_dict()}
    cache["bad.example"] = {"domain": "bad.example", "best_ip": "not-an-ip"}
    problems = []
    with tempfile.TemporaryDirectory(prefix="svpn-e2e-") as td:
        p = Path(td) / "nested" / "cdn-cache.json"
        out = cdn.save_cache(cache, p)
        print(f"    写入 {out} ({out.stat().st_size}B)")
        loaded = cdn.load_cache(p)
        print(f"    读回域名 {sorted(loaded)}")
        if CDN_DOMAIN not in loaded:
            problems.append("load_cache 丢了 CDN 域名")
        else:
            want = cache[CDN_DOMAIN].get("best_ip", "")
            got = loaded[CDN_DOMAIN].get("best_ip", "")
            print(f"    best_ip 往返: {want!r} -> {got!r}")
            if want != got:
                problems.append(f"best_ip 不一致 {want!r} vs {got!r}")

        m = cdn.best_ip_map(p)
        print(f"    best_ip_map -> {json.dumps(m, ensure_ascii=False)}")
        if "bad.example" in m:
            problems.append("best_ip_map 未过滤非 IP 条目")
        if CDN_DOMAIN not in m:
            problems.append("best_ip_map 缺 CDN 域名")

        stale = cdn.load_cache(p, max_age=-1)
        print(f"    max_age=-1 -> {len(stale)} 域名（应为 0）")
        if stale:
            problems.append("max_age 过期未生效")

        miss = cdn.load_cache(Path(td) / "nope.json")
        print(f"    不存在的文件 -> {len(miss)} 域名（应为 0）")
        if miss:
            problems.append("缺失文件应返回空 dict")

    if problems:
        return False, " ;; ".join(problems)
    return True, f"往返 OK，{len(cache)} 条目，best_ip_map={len(m)} 项"


# ------------------------------------------------------------------ main
def main() -> int:
    print(f"svpn 真机 E2E  -  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"python {sys.version.split()[0]}  root={ROOT}")

    t_all = time.perf_counter()
    for fn in (t_doh, t_tls, t_probe, t_probe_node, t_cdn, t_cache):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 —— 兜底，正常不会走到
            print(f"!!! 步骤外异常 {fn.__name__}: {type(exc).__name__}: {exc}")

    total = time.perf_counter() - t_all
    n_pass = sum(1 for r in RESULTS if r[1] == "PASS")
    n_fail = sum(1 for r in RESULTS if r[1] == "FAIL")
    n_skip = sum(1 for r in RESULTS if r[1] == "SKIP")

    print("\n" + "=" * 78)
    print(f"{'结果':<6}{'耗时':>9}  项目")
    print("-" * 78)
    for name, status, secs, detail in RESULTS:
        print(f"{status:<6}{secs:>8.2f}s  {name}")
        if status != "PASS":
            print(f"          └─ {detail}")
    print("-" * 78)
    print(f"汇总: PASS {n_pass} / FAIL {n_fail} / SKIP {n_skip}   总耗时 {total:.1f}s")
    if STATE.get("cdn_top"):
        top = STATE["cdn_top"][0]
        print(f"CDN 最优: {top['ip']}  {top['ms']}ms  {top['mbps']}MB/s")
    print("=" * 78)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

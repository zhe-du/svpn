"""svpn 本地代理 E2E 验证（纯标准库）。

运行：
    python tests/e2e_proxy.py

校验对象（进程外黑盒，不改 svpn/ 任何代码）：
    1. SOCKS5 CONNECT store.steampowered.com:443 -> TLS -> HEAD /（3 次，均应 < 3s）
    2. HTTP   CONNECT store.steampowered.com:443 -> TLS -> HEAD /
    3. SOCKS5 CONNECT www.baidu.com:443        -> TLS -> HEAD /  (200)
    4. 直连对照：同一 HEAD 不走代理，代理不得比直连慢太多
    5. HTTP 明文代理（绝对 URI）http://www.baidu.com/           (200)
    6. 并发 20 条 SOCKS5 -> www.baidu.com:443，全部成功
    7. 可选：runtime/cdn-cache.json 有 best_ip 时，SOCKS5 到
       steamcdn-a.akamaihd.net:443 -> 200/206

输出：逐项 PASS/FAIL/SKIP + 耗时 + 失败原始报错（截 300 字符），末尾汇总。
退出码：0 = 无 FAIL（SKIP 不算失败）；1 = 有 FAIL。
"""
from __future__ import annotations

import json
import socket
import ssl
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CDN_CACHE = ROOT / "runtime" / "cdn-cache.json"

HOST = "127.0.0.1"
SOCKS_PORT = 7899
HTTP_PORT = 7900
UA = "svpn-e2e/1.0"

CONNECT_TIMEOUT = 10.0
HEAD_TIMEOUT = 10.0
STORE_BUDGET_S = 3.0
BAIDU = "www.baidu.com"
STORE = "store.steampowered.com"
CDN_HOST = "steamcdn-a.akamaihd.net"

RESULTS: list[dict] = []


def _flat(text: object, limit: int = 300) -> str:
    s = str(text).replace("\r", " ").replace("\n", " | ")
    return s if len(s) <= limit else s[:limit] + "..."


def record(name: str, status: str, seconds: float, detail: str) -> None:
    RESULTS.append({"name": name, "status": status,
                    "seconds": round(seconds, 3), "detail": _flat(detail)})
    print(f"[{len(RESULTS):>2}] {name:<34} {status:<4} {seconds:7.2f}s  {_flat(detail)}")


def skip(name: str, detail: str) -> None:
    record(name, "SKIP", 0.0, detail)


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # 只看「能不能握手」，证书链另有人管
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


CTX = _ctx()


# ------------------------------------------------------------------ 连接器
def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("连接被提前关闭")
        buf += chunk
    return buf


def socks5_open(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    """经本地 SOCKS5 建立到 host:port 的隧道（域名交给代理解析）。"""
    sock = socket.create_connection((HOST, SOCKS_PORT), timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise ConnectionError("SOCKS5 不接受无认证方式")
        hb = host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb
                     + struct.pack(">H", port))
        ver, rep, _rsv, atyp = _recv_exact(sock, 4)
        if ver != 0x05:
            raise ConnectionError(f"SOCKS5 版本异常 {ver}")
        if rep != 0x00:
            raise ConnectionError(f"SOCKS5 CONNECT 失败 rep={rep}")
        if atyp == 0x01:
            _recv_exact(sock, 6)
        elif atyp == 0x03:
            n = _recv_exact(sock, 1)[0]
            _recv_exact(sock, n + 2)
        elif atyp == 0x04:
            _recv_exact(sock, 18)
        return sock
    except BaseException:
        sock.close()
        raise


def http_connect_open(host: str, port: int,
                      timeout: float = CONNECT_TIMEOUT) -> socket.socket:
    """经本地 HTTP 代理（127.0.0.1:7900）建立 CONNECT 隧道。"""
    sock = socket.create_connection((HOST, HTTP_PORT), timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall((f"CONNECT {host}:{port} HTTP/1.1\r\n"
                      f"Host: {host}:{port}\r\n"
                      "Proxy-Connection: keep-alive\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("HTTP 代理在 CONNECT 应答前关闭")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("CONNECT 应答过长")
        line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise ConnectionError(f"CONNECT 应答异常: {line}")
        code = int(parts[1])
        if code // 100 != 2:
            raise ConnectionError(f"CONNECT 被拒: {line}")
        return sock
    except BaseException:
        sock.close()
        raise


def _read_head(sock: socket.socket, timeout: float = HEAD_TIMEOUT) -> tuple[int, str]:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    if not buf:
        raise ConnectionError("没有收到任何 HTTP 应答")
    line = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split()
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    return code, line


def head_via(connector, host: str, port: int = 443, path: str = "/") -> dict:
    """connector(host, port) -> 已连通的 socket；返回各阶段耗时与状态码。"""
    out = {"code": 0, "line": "", "connect_ms": 0.0, "tls_ms": 0.0,
           "head_ms": 0.0, "total_ms": 0.0, "tls": "", "err": ""}
    raw = None
    tls = None
    t0 = time.perf_counter()
    try:
        tnode = time.perf_counter()
        raw = connector(host, port)
        out["connect_ms"] = (time.perf_counter() - tnode) * 1000
        tls = CTX.wrap_socket(raw, server_hostname=host)
        out["tls_ms"] = (time.perf_counter() - tnode) * 1000 - out["connect_ms"]
        out["tls"] = tls.version() or ""
        tls.sendall((f"HEAD {path} HTTP/1.1\r\nHost: {host}\r\n"
                     f"User-Agent: {UA}\r\nAccept: */*\r\n"
                     "Connection: close\r\n\r\n").encode())
        th = time.perf_counter()
        out["code"], out["line"] = _read_head(tls)
        out["head_ms"] = (time.perf_counter() - th) * 1000
    except Exception as exc:  # noqa: BLE001
        out["err"] = f"{type(exc).__name__}: {exc}"
    finally:
        out["total_ms"] = (time.perf_counter() - t0) * 1000
        for s in (tls, raw):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
    return out


def direct_head_race(host: str, port: int = 443, path: str = "/",
                     timeout: float = 6.0) -> dict:
    """直连对照：系统解析出多个 IP 后并发尝试，取最先握手成功的那个。

    单纯 socket.create_connection(域名) 会按 getaddrinfo 顺序串行试，
    第一个 IP 被墙就必然超时，拿来对照会把「环境被污染」误判成「代理慢」。
    """
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        return {"err": f"getaddrinfo: {exc}", "total_ms": 0.0, "code": 0,
                "line": "", "ip": ""}
    ips = list(dict.fromkeys(info[4][0] for info in infos
                             if info[4] and info[4][0]))[:6]
    if not ips:
        return {"err": "系统解析无 A 记录", "total_ms": 0.0, "code": 0,
                "line": "", "ip": ""}

    def attempt(ip: str) -> dict:
        r = head_via(lambda _h, _p: socket.create_connection((ip, port), timeout),
                     host, port, path)
        r["ip"] = ip
        return r

    t0 = time.perf_counter()
    pool = ThreadPoolExecutor(max_workers=len(ips))
    try:
        futures = [pool.submit(attempt, ip) for ip in ips]
        for fut in as_completed(futures, timeout=timeout + 8):
            r = fut.result()
            if not r["err"] and r["code"] in (200, 301, 302):
                return r
        return {"err": "所有直连 IP 均失败", "total_ms": (time.perf_counter() - t0) * 1000,
                "code": 0, "line": "", "ip": ",".join(ips)}
    except Exception as exc:  # noqa: BLE001
        return {"err": f"{type(exc).__name__}: {exc}",
                "total_ms": (time.perf_counter() - t0) * 1000,
                "code": 0, "line": "", "ip": ",".join(ips)}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# ------------------------------------------------------------------ 检查项
def check_preflight() -> bool:
    t0 = time.perf_counter()
    bad = []
    for port in (SOCKS_PORT, HTTP_PORT):
        try:
            socket.create_connection((HOST, port), 3).close()
        except OSError as exc:
            bad.append(f"{HOST}:{port} {type(exc).__name__}: {exc}")
    if bad:
        record("preflight_ports", "FAIL", time.perf_counter() - t0,
               "代理端口不可连: " + "; ".join(bad))
        return False
    record("preflight_ports", "PASS", time.perf_counter() - t0,
           f"{HOST}:{SOCKS_PORT}(socks5) + {HOST}:{HTTP_PORT}(http) 均可连")
    return True


def check_store_socks() -> float:
    """跑 3 次「SOCKS5 -> TLS -> HEAD /」，返回成功样本的中位耗时（ms）。"""
    runs = []
    for _ in range(3):
        runs.append(head_via(socks5_open, STORE))
    good = [r for r in runs if not r["err"] and r["code"] in (200, 301, 302)
            and r["total_ms"] < STORE_BUDGET_S * 1000]
    times = sorted(r["total_ms"] for r in runs)
    med = times[len(times) // 2] if times else 0.0
    detail = (f"{len(good)}/3 ok, med={med:.0f}ms "
              f"range={times[0]:.0f}-{times[-1]:.0f}ms, "
              f"tls={runs[0]['tls'] or '-'}, codes="
              + ",".join(str(r["code"]) for r in runs))
    if len(good) == 3:
        record("socks5_store_head", "PASS", med / 1000, detail)
    else:
        errs = [r["err"] or f"HTTP {r['code']} {r['line']}" for r in runs
                if r not in good]
        record("socks5_store_head", "FAIL", med / 1000,
               detail + " | 失败: " + "; ".join(errs[:3]))
    return med


def check_store_http() -> None:
    r = head_via(http_connect_open, STORE)
    ok = not r["err"] and r["code"] in (200, 301, 302)
    detail = (f"HTTP {r['code']} {r['line'][:40]} · connect={r['connect_ms']:.0f}ms "
              f"tls={r['tls_ms']:.0f}ms head={r['head_ms']:.0f}ms "
              f"total={r['total_ms']:.0f}ms")
    if r["err"]:
        detail = r["err"]
    record("http_connect_store_head", "PASS" if ok else "FAIL",
           r["total_ms"] / 1000, detail)


def check_baidu_socks() -> None:
    r = head_via(socks5_open, BAIDU)
    ok = not r["err"] and r["code"] == 200
    detail = (f"HTTP {r['code']} · connect={r['connect_ms']:.0f}ms "
              f"tls={r['tls_ms']:.0f}ms head={r['head_ms']:.0f}ms "
              f"total={r['total_ms']:.0f}ms")
    if r["err"]:
        detail = r["err"]
    record("socks5_baidu_head", "PASS" if ok else "FAIL", r["total_ms"] / 1000, detail)


def check_direct_compare(proxy_med_ms: float) -> None:
    r = direct_head_race(STORE)
    if r["err"] or r["code"] not in (200, 301, 302):
        skip("direct_vs_proxy", "直连对照不可用（无法比较）: "
             + (r["err"] or f"HTTP {r['code']} {r['line'][:40]}"))
        return
    allowed = max(r["total_ms"] * 2.0, r["total_ms"] + 1500)
    ok = proxy_med_ms <= allowed
    detail = (f"直连 {r['total_ms']:.0f}ms@{r['ip']} vs 代理 {proxy_med_ms:.0f}ms "
              f"(比值 {proxy_med_ms / max(r['total_ms'], 1):.2f}x, 上限 {allowed:.0f}ms)")
    record("direct_vs_proxy", "PASS" if ok else "FAIL", r["total_ms"] / 1000, detail)


def check_http_plain() -> None:
    t0 = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((HOST, HTTP_PORT), CONNECT_TIMEOUT)
        sock.settimeout(HEAD_TIMEOUT)
        sock.sendall((f"GET http://{BAIDU}/ HTTP/1.1\r\nHost: {BAIDU}\r\n"
                      f"User-Agent: {UA}\r\nAccept: */*\r\n"
                      "Connection: close\r\n\r\n").encode())
        code, line = _read_head(sock)
        ok = code == 200
        record("http_plain_absolute_uri", "PASS" if ok else "FAIL",
               time.perf_counter() - t0, f"HTTP {code} {line[:60]}")
    except Exception as exc:  # noqa: BLE001
        record("http_plain_absolute_uri", "FAIL", time.perf_counter() - t0,
               f"{type(exc).__name__}: {exc}")
    finally:
        if sock is not None:
            sock.close()


def _one_baidu(_i: int) -> dict:
    r = head_via(socks5_open, BAIDU, 443)
    r["ok"] = (not r["err"]) and r["code"] == 200
    return r


def check_concurrency(n: int = 20) -> None:
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=n) as pool:
        runs = list(pool.map(_one_baidu, range(n)))
    elapsed = time.perf_counter() - t0
    ok = [r for r in runs if r["ok"]]
    times = sorted(r["total_ms"] for r in runs)
    med = times[len(times) // 2]
    errs = [r["err"] or f"HTTP {r['code']}" for r in runs if not r["ok"]]
    detail = (f"{len(ok)}/{n} ok · 并发总耗时 {elapsed:.1f}s · "
              f"单条 med={med:.0f}ms range={times[0]:.0f}-{times[-1]:.0f}ms")
    if errs:
        detail += " | 失败: " + "; ".join(errs[:3])
    record(f"concurrency_{n}_baidu", "PASS" if len(ok) == n else "FAIL",
           elapsed, detail)


def check_cdn() -> None:
    if not CDN_CACHE.exists():
        skip("cdn_best_ip", f"没有 {CDN_CACHE.relative_to(ROOT)}，跳过")
        return
    try:
        data = json.loads(CDN_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        skip("cdn_best_ip", f"cdn-cache.json 读不了: {exc}")
        return
    domains = data.get("domains") if isinstance(data.get("domains"), dict) else data
    info = domains.get(CDN_HOST) or {}
    best_ip = info.get("best_ip") or ""
    if not best_ip:
        skip("cdn_best_ip", f"{CDN_HOST} 无可用 best_ip")
        return
    r = head_via(socks5_open, CDN_HOST)
    ok = not r["err"] and r["code"] in (200, 206)
    detail = f"best_ip={best_ip} HTTP {r['code']} · total={r['total_ms']:.0f}ms"
    if r["err"]:
        detail = f"best_ip={best_ip} " + r["err"]
    record("cdn_best_ip", "PASS" if ok else "FAIL", r["total_ms"] / 1000, detail)


# ------------------------------------------------------------------ main
def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except OSError:
            pass
    print("=" * 96)
    print(f"svpn proxy E2E  socks5://{HOST}:{SOCKS_PORT}  http://{HOST}:{HTTP_PORT}")
    print("=" * 96)
    t_all = time.perf_counter()

    if not check_preflight():
        for name in ("socks5_store_head", "http_connect_store_head", "socks5_baidu_head",
                     "direct_vs_proxy", "http_plain_absolute_uri",
                     "concurrency_20_baidu", "cdn_best_ip"):
            skip(name, "代理端口不可连，未执行")
        print_summary(time.perf_counter() - t_all)
        return 1

    store_med = check_store_socks()
    check_store_http()
    check_baidu_socks()
    check_direct_compare(store_med)
    check_http_plain()
    check_concurrency(20)
    check_cdn()
    return print_summary(time.perf_counter() - t_all)


def print_summary(total: float) -> int:
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("-" * 96)
    print(f"SUMMARY  {counts['PASS']} PASS / {counts['FAIL']} FAIL / "
          f"{counts['SKIP']} SKIP   total {total:.1f}s")
    for r in RESULTS:
        if r["status"] == "FAIL":
            print(f"  FAIL {r['name']}: {r['detail']}")
    print("RESULT   " + ("PASS" if counts["FAIL"] == 0 else "FAIL"))
    return 0 if counts["FAIL"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

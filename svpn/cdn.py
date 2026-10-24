"""CDN 选边：给 Steam 下载/更新挑一条当前最快的 Akamai 边缘节点。

Steam 的下载走 CDN 的裸 TCP + HTTP，代理帮不上忙；真正决定速度的是
「你连到了哪台边缘机器」。同一个域名 DoH 会给出十几台候选，实际快慢能差 10 倍。

做法：
    1. 用 DoH 拿全量 A 记录（顺便绕开被污染的本地 DNS）；
    2. 每台 IP 用正确 SNI 做 TLS 握手，淘汰不可达 / 证书不对的；
    3. 对前几名发 Range: bytes=0-1MB 的真实下载请求，按实测吞吐排序；
    4. 结果落盘，hosts + SNI 转发器按它拨号。
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import netutil

# 域名 → 测速用的真实路径（None = 只测握手延迟）
CDN_DOMAINS: dict[str, str | None] = {
    "steamcdn-a.akamaihd.net": "/client/installer/SteamSetup.exe",
    "cdn.akamai.steamstatic.com": "/client/installer/SteamSetup.exe",
    "client-update.akamai.steamstatic.com": None,
    "shared.akamai.steamstatic.com": None,
    "store.akamai.steamstatic.com": None,
    "community.akamai.steamstatic.com": None,
    "media.steampowered.com": None,
    "avatars.steamstatic.com": None,
}

BENCH_BYTES = 1_048_576


@dataclass
class Edge:
    ip: str
    ms: float = 0.0
    mbps: float = 0.0
    bytes_got: int = 0
    code: int = 0
    err: str = ""

    @property
    def score(self) -> tuple:
        # 有实测吞吐的优先，吞吐高的优先，其次握手快的
        return (0 if self.mbps > 0 else 1, -self.mbps, self.ms)

    def to_dict(self) -> dict:
        return {"ip": self.ip, "ms": round(self.ms, 1), "mbps": round(self.mbps, 2),
                "code": self.code, "err": self.err}


@dataclass
class DomainResult:
    domain: str
    source: str = ""
    candidates: list[str] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def best(self) -> Edge | None:
        return self.edges[0] if self.edges else None

    def to_dict(self) -> dict:
        b = self.best
        return {
            "domain": self.domain,
            "source": self.source,
            "candidates": len(self.candidates),
            "best_ip": b.ip if b else "",
            "best_ms": round(b.ms, 1) if b else 0,
            "best_mbps": round(b.mbps, 2) if b else 0,
            "tested": len(self.edges),
            "ts": self.ts,
            "top": [e.to_dict() for e in self.edges[:5]],
        }


def _bench(domain: str, ip: str, path: str | None, timeout: float,
           byte_count: int) -> Edge:
    edge = Edge(ip=ip)
    t0 = time.perf_counter()
    try:
        sock = netutil.open_via(netutil.Node("direct"), ip, 443, timeout)
        tls = netutil.tls_over(sock, domain, timeout)
    except Exception as exc:  # noqa: BLE001
        edge.err = type(exc).__name__
        edge.ms = (time.perf_counter() - t0) * 1000
        return edge
    edge.ms = (time.perf_counter() - t0) * 1000
    try:
        if path:
            got, mbps, err = netutil.download_bench(lambda: tls, domain, path,
                                                    byte_count, timeout)
            edge.bytes_got, edge.mbps, edge.err = got, mbps, err
        else:
            code, _ = netutil.http_head(tls, domain, "/", timeout)
            edge.code = code
            edge.err = "" if code in (200, 301, 302, 403, 404) else f"HTTP {code}"
    finally:
        try:
            tls.close()
        except OSError:
            pass
    return edge


def rank_domain(domain: str, bench: bool = True, timeout: float = 3.0,
                top: int = 6, byte_count: int = BENCH_BYTES) -> DomainResult:
    path = CDN_DOMAINS.get(domain)
    ips, source = netutil.resolve_merged(domain, timeout=timeout + 2)
    res = DomainResult(domain=domain, source=source, candidates=ips)
    if not ips:
        return res
    good = netutil.race_tls(domain, ips, 443, timeout=timeout)
    if not good:
        return res
    picks = [Edge(ip=g["ip"], ms=g["ms"]) for g in good[:max(top, 3)]]
    if bench:
        with ThreadPoolExecutor(max_workers=min(6, len(picks))) as pool:
            edges = list(pool.map(
                lambda e: _bench(domain, e.ip, path, timeout + 5, byte_count), picks))
        # 测出吞吐的按吞吐排；没测出但握手通的留在后面
        edges.sort(key=lambda e: e.score)
        res.edges = edges
    else:
        res.edges = picks
    return res


def refresh(domains: list[str] | None = None, bench: bool = True,
            timeout: float = 3.0, workers: int = 4) -> dict[str, dict]:
    names = domains or list(CDN_DOMAINS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda d: rank_domain(d, bench, timeout), names))
    return {r.domain: r.to_dict() for r in results}


# ---------------------------------------------------------------- 缓存
def save_cache(cache: dict, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated": time.time(), "domains": cache}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_cache(path: Path, max_age: float = 6 * 3600) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if time.time() - float(data.get("updated", 0)) >= max_age:
        return {}
    return data.get("domains") or {}


def best_ip_map(path: Path, max_age: float = 6 * 3600) -> dict[str, str]:
    """domain -> 最优 IP，供代理/转发器直接使用。"""
    cache = load_cache(path, max_age)
    out = {}
    for domain, info in cache.items():
        ip = (info or {}).get("best_ip")
        if ip and netutil.is_ip(ip):
            out[domain] = ip
    return out

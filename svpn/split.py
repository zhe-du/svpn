"""split-tunnel 路由：只把 Valve/Steam 网段塞进 VPN 网卡（需管理员）。

全隧道（AllowedIPs=0.0.0.0/0）最省事；这里给的是「只让 Steam 走 VPN」的办法：
路由优先级高于默认网关即可，删除即回滚。
"""
from __future__ import annotations

import ipaddress
import subprocess
import sys

from .rules import STEAM_CIDRS

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000


def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW if IS_WIN else 0)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def add_routes(cidrs=STEAM_CIDRS, gateway: str = "", if_index: int = 0) -> list[dict]:
    if not gateway or not if_index:
        raise ValueError("需要 gateway(VPN 对端地址) 与 if_index(VPN 网卡索引)")
    results = []
    for c in cidrs:
        net = ipaddress.ip_network(c, strict=False)
        cmd = ["route", "add", str(net.network_address), "mask",
               str(net.netmask), gateway, "metric", "5", "if", str(if_index)]
        code, out = _run(cmd)
        results.append({"cidr": str(net), "ok": code == 0, "out": out.strip()[:200]})
    return results


def del_routes(cidrs=STEAM_CIDRS, gateway: str = "", if_index: int = 0) -> list[dict]:
    results = []
    for c in cidrs:
        net = ipaddress.ip_network(c, strict=False)
        cmd = ["route", "delete", str(net.network_address), "mask", str(net.netmask)]
        if gateway:
            cmd += [gateway]
        if if_index:
            cmd += ["if", str(if_index)]
        code, out = _run(cmd)
        results.append({"cidr": str(net), "ok": code == 0, "out": out.strip()[:200]})
    return results


def list_vpn_adapters() -> list[dict]:
    code, out = _run(["powershell", "-NoProfile", "-Command",
                      "Get-NetAdapter | Where-Object {$_.InterfaceDescription -match "
                      "'WireGuard|Wintun|TAP|OpenVPN|Tailscale|ZeroTier'} | "
                      "Select-Object -Property Name,InterfaceDescription,ifIndex,Status | ConvertTo-Json -Compress"])
    if code != 0:
        return []
    import json
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]

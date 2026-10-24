"""Windows 系统层改动：WinINET 代理 / 环境变量 / hosts。

所有改动先落备份（runtime/），`restore_*` 可完整回滚。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
NET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
HOSTS = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/drivers/etc/hosts"
MARK_BEGIN = "# >>> steam-vpn managed block (do not edit) >>>"
MARK_END = "# <<< steam-vpn managed block <<<"


def runtime_dir(base: Path | None = None) -> Path:
    d = (base or Path.cwd()) / "runtime"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ wininet
def _winreg():
    import winreg  # noqa: PLC0415
    return winreg


def read_wininet() -> dict:
    if not IS_WIN:
        return {}
    winreg = _winreg()
    out = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NET_KEY, 0, winreg.KEY_READ) as k:
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL"):
            try:
                out[name] = winreg.QueryValueEx(k, name)[0]
            except FileNotFoundError:
                out[name] = None
    return out


def backup_wininet(store: Path) -> Path:
    f = store / "wininet-backup.json"
    if f.exists():          # 只保留最开始那份真·原始值
        return f
    data = read_wininet()
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def _notify_change() -> None:
    """通知 WinINET 立即刷新，不然浏览器要重启才生效。"""
    if not IS_WIN:
        return
    try:
        import ctypes  # noqa: PLC0415
        wininet = ctypes.windll.Wininet
        wininet.InternetSetOptionW(0, 39, 0, 0)   # SETTINGS_CHANGED
        wininet.InternetSetOptionW(0, 37, 0, 0)   # REFRESH
    except Exception:  # noqa: BLE001
        pass


def apply_wininet(proxy: str, bypass: str = "", pac_url: str | None = None,
                  store: Path | None = None) -> dict:
    if not IS_WIN:
        return {"ok": False, "reason": "not windows"}
    store = store or runtime_dir()
    backup_wininet(store)
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NET_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if pac_url:
            winreg.SetValueEx(k, "AutoConfigURL", 0, winreg.REG_SZ, pac_url)
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        else:
            winreg.SetValueEx(k, "AutoConfigURL", 0, winreg.REG_SZ, "")
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, proxy)
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ,
                              bypass or "<local>")
    _notify_change()
    return read_wininet()


def restore_wininet(store: Path | None = None) -> dict:
    if not IS_WIN:
        return {}
    store = store or runtime_dir()
    f = store / "wininet-backup.json"
    if not f.exists():
        return {"ok": False, "reason": "no backup"}
    data = json.loads(f.read_text(encoding="utf-8"))
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NET_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD,
                          int(data.get("ProxyEnable") or 0))
        winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ,
                          data.get("ProxyServer") or "")
        winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ,
                          data.get("ProxyOverride") or "")
        winreg.SetValueEx(k, "AutoConfigURL", 0, winreg.REG_SZ,
                          data.get("AutoConfigURL") or "")
    _notify_change()
    return read_wininet()


# ------------------------------------------------------------------ env vars
PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
             "https_proxy", "all_proxy")


def set_user_env(proxy: str) -> None:
    if not IS_WIN:
        return
    for name in PROXY_ENV:
        subprocess.run(["setx", name, proxy], capture_output=True, text=True,
                       creationflags=0x08000000)


def clear_user_env() -> None:
    if not IS_WIN:
        return
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_ALL_ACCESS) as k:
        for name in PROXY_ENV:
            try:
                winreg.DeleteValue(k, name)
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------ hosts
def _read_hosts_raw() -> bytes:
    try:
        return HOSTS.read_bytes()
    except OSError:
        return b""


def _decode_hosts(raw: bytes) -> tuple[str, str, str]:
    """返回 (文本, 编码, 换行符)。

    hosts 是用户文件：老编辑器/国产工具可能用 GBK 存中文注释，直接
    utf-8 + errors="replace" 再写回会把注释变成乱码且不可逆。这里按字节探测，
    用原编码写回。
    """
    nl = "\r\n" if b"\r\n" in raw else "\n"
    if raw.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    else:
        enc = ""
        for candidate in ("utf-8", "gbk", "mbcs"):
            try:
                raw.decode(candidate)
                enc = candidate
                break
            except (UnicodeDecodeError, LookupError):
                continue
        enc = enc or "latin-1"
    try:
        text = raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        text, enc = raw.decode("latin-1"), "latin-1"
    return text, enc, nl


def read_hosts() -> str:
    return _decode_hosts(_read_hosts_raw())[0]


def _write_hosts(text: str, enc: str) -> None:
    # strict：编码不了的字符宁可报错，也不要静默变成 '?'（那是数据损失）
    HOSTS.write_bytes(text.encode(enc, errors="strict"))


_SAFE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def sanitize_entries(entries: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """只允许「合法 IP + 合法域名」，挡住换行/空格/#/伪造标记的注入。

    缓存文件是可以被替换的（`runtime/cdn-cache.json`），不能信里面的字符串。
    """
    import ipaddress  # noqa: PLC0415
    clean: dict[str, str] = {}
    rejected: list[str] = []
    for ip, blob in entries.items():
        try:
            ipaddress.ip_address(str(ip).strip())
        except ValueError:
            rejected.append(f"ip={ip!r}")
            continue
        good = []
        for host in str(blob).split():
            host = host.strip()
            if not host or len(host) > 253 or not _SAFE_HOST.match(host):
                rejected.append(host or "<empty>")
                continue
            try:                      # 主机名位置出现 IP 一律拒绝（防伪造多行语义）
                ipaddress.ip_address(host)
                rejected.append(host)
                continue
            except ValueError:
                pass
            if MARK_BEGIN in host or MARK_END in host or "#" in host:
                rejected.append(host)
                continue
            good.append(host)
        if good:
            clean[str(ip).strip()] = " ".join(good)
    return clean, rejected


def hosts_block(entries: dict[str, str], nl: str = "\n") -> str:
    lines = [MARK_BEGIN]
    for ip, host in entries.items():
        lines.append(f"{ip:<16}{host}")
    lines.append(MARK_END)
    return nl.join(lines)


def _split_managed(text: str) -> list[str]:
    out: list[str] = []
    skip = False
    for line in text.splitlines():
        if line.strip() == MARK_BEGIN:
            skip = True
            continue
        if line.strip() == MARK_END:
            skip = False
            continue
        if not skip:
            out.append(line)
    return out


def apply_hosts(entries: dict[str, str], store: Path | None = None) -> dict:
    store = store or runtime_dir()
    clean, rejected = sanitize_entries(entries)
    if not clean:
        return {"ok": False, "reason": "没有合法的 hosts 条目（IP/域名校验未通过）",
                "rejected": rejected}
    entries = clean
    raw = _read_hosts_raw()
    bak = store / "hosts.backup"
    if not bak.exists() and raw:
        shutil.copy2(HOSTS, bak)
    text, enc, nl = _decode_hosts(raw)
    body = nl.join(_split_managed(text)).rstrip() + nl + nl \
        + hosts_block(entries, nl) + nl
    try:
        _write_hosts(body, enc)
        return {"ok": True, "path": str(HOSTS), "entries": len(entries),
                "encoding": enc, "rejected": rejected}
    except PermissionError:
        return {"ok": False, "reason": "需要管理员权限写 hosts"}
    except OSError as exc:
        return {"ok": False, "reason": f"写 hosts 失败: {exc}"}


def clear_hosts(store: Path | None = None) -> dict:
    raw = _read_hosts_raw()
    text, enc, nl = _decode_hosts(raw)
    body = nl.join(_split_managed(text)).rstrip() + nl
    try:
        _write_hosts(body, enc)
        return {"ok": True, "encoding": enc}
    except PermissionError:
        return {"ok": False, "reason": "需要管理员权限写 hosts"}
    except OSError as exc:
        return {"ok": False, "reason": f"写 hosts 失败: {exc}"}


def flush_dns() -> None:
    subprocess.run(["ipconfig", "/flushdns"], capture_output=True,
                   creationflags=0x08000000)

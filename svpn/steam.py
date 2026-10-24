"""Steam 客户端发现 / 启动 / 收尾。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

CANDIDATES = (
    r"C:\Program Files (x86)\Steam\steam.exe",
    r"C:\Program Files\Steam\steam.exe",
    r"D:\Steam\steam.exe",
    r"D:\app\bapp\st\steam.exe",
    r"E:\Steam\steam.exe",
)


def find_steam() -> Path | None:
    if IS_WIN:
        try:
            import winreg  # noqa: PLC0415
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for key in (r"Software\Valve\Steam", r"Software\WOW6432Node\Valve\Steam"):
                    try:
                        with winreg.OpenKey(root, key) as k:
                            for name in ("SteamExe", "SteamPath"):
                                try:
                                    v = winreg.QueryValueEx(k, name)[0]
                                except FileNotFoundError:
                                    continue
                                p = Path(str(v).replace("/", "\\"))
                                if p.suffix.lower() == ".exe" and p.exists():
                                    return p
                                cand = p / "steam.exe"
                                if cand.exists():
                                    return cand
                    except FileNotFoundError:
                        continue
        except Exception:  # noqa: BLE001
            pass
    for c in CANDIDATES:
        p = Path(c)
        if p.exists():
            return p
    return None


def _tasklist() -> str:
    """tasklist 在中文系统输出 cp936；PYTHONUTF8=1 时 text=True 会直接解码崩。"""
    try:
        raw = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe", "/NH"],
                             capture_output=True,
                             creationflags=CREATE_NO_WINDOW).stdout or b""
    except OSError:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("mbcs", errors="replace") if IS_WIN else \
            raw.decode("utf-8", errors="replace")
    return str(raw)


def is_running() -> bool:
    return "steam.exe" in _tasklist().lower()


def launch(steam_exe: Path, proxy: str | None = None,
           extra_args: list[str] | None = None, wait: bool = False) -> subprocess.Popen:
    env = os.environ.copy()
    if proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"):
            env[name] = proxy
        env["NO_PROXY"] = env.get("NO_PROXY", "") + ",localhost,127.0.0.1"
    args = [str(steam_exe)]
    # -tcp：强制 TCP，弱网/丢 UDP 时登录成功率高；-console 出调试台
    args += ["-tcp", "-noreactlogin", "-console", "-nointro"]
    if extra_args:
        args += extra_args
    cwd = str(steam_exe.parent)
    return subprocess.Popen(args, cwd=cwd, env=env,
                            creationflags=0x00000010 if IS_WIN else 0,  # DETACHED
                            close_fds=True)


def stop(force: bool = False) -> None:
    names = ["steam.exe", "steamwebhelper.exe", "steamservice.exe",
             "GameOverlayUI.exe"]
    for n in names:
        cmd = ["taskkill", "/IM", n, "/T"] + (["/F"] if force else [])
        subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW)

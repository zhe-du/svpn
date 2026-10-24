@echo off
chcp 65001 >nul
rem ============================================================
rem  start-svpn.cmd -- double-click launcher (the desktop shortcut
rem  "svpn launch.lnk" points here; the real name is localized).
rem    1) start the proxy in background and take over the system proxy
rem    2) launch Steam so it uses the local proxy
rem    3) print one status receipt
rem
rem  Any failure stops and pauses, so the window never flashes away.
rem  To stop: svpn down --clear-hosts (restores proxy + clears hosts)
rem
rem  ENCODING RULES (do not break these, they are load-bearing):
rem   * file is UTF-8 without BOM, CRLF line endings
rem   * comments are ASCII only
rem   * a line holding any Chinese text must be followed by one EMPTY line
rem  Reason: after "chcp 65001" cmd.exe mis-tracks byte offsets when a line
rem  holds multi-byte text (console started at CP936); it then eats the head
rem  of the NEXT line and reports "'?' is not recognized". An empty line
rem  absorbs that, and it is verified by running from CP936 before shipping.
rem ============================================================
setlocal EnableExtensions
pushd "%~dp0"
if errorlevel 1 (
    echo [x] 进不到脚本目录：%~dp0

    pause
    exit /b 1
)

rem Pick a python that actually runs. The Microsoft Store python.exe alias
rem fails on -c, so it is skipped. "call" is mandatory here: the python on
rem PATH may be a pyenv-win / conda .bat/.cmd wrapper, and without "call"
rem control never comes back to this script (silent exit 0).
set "PY="
call python -c "import sys" >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    call py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo [x] 没找到可用的 Python。装 3.10+，安装时勾选 "Add python.exe to PATH"。

    popd
    pause
    exit /b 9009
)
echo [i] Python: %PY%
echo [i] 目录: %CD%

echo.
echo == 1/3 启动代理（后台常驻 + 接管系统代理）

rem "call" is needed on every %PY% use, not just the probes: when PY is a
rem .bat/.cmd wrapper, invoking it without "call" ends this script silently.
call %PY% -m svpn up -b --system-proxy
if errorlevel 1 goto :up_failed

echo.
echo == 2/3 拉起 Steam

call %PY% -m svpn steam --system
set "STEAMRC=%ERRORLEVEL%"
if "%STEAMRC%"=="2" goto :steam_missing
if "%STEAMRC%"=="0" goto :steam_ok
echo [!] Steam 这一步返回 %STEAMRC%，代理本身不受影响，可以继续用。

goto :steam_done

:steam_missing
echo [i] 没找到 steam.exe（返回 2，属于正常）：代理照常跑，可只用来加速下载与商店。

goto :steam_done

:steam_ok
echo [i] Steam 已接管完成。

:steam_done
echo.
echo == 3/3 当前状态

call %PY% -m svpn status
echo.
echo [i] 这个窗口可以关掉，代理在后台继续跑。

echo     收工（还原系统代理 + 清 hosts）: %PY% -m svpn down --clear-hosts

popd
pause
exit /b 0

:up_failed
echo.
echo [x] 代理没起来，先看上面的报错。常见原因：

echo       - 端口 7899 / 7900 被别的程序占了（svpn status 会报占用者）

echo       - config.json 写坏了（删掉重跑 svpn init）

echo     排查: %PY% -m svpn status

echo           %PY% -m svpn doctor
popd
pause
exit /b 1

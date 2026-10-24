@echo off
chcp 65001 >nul
rem ============================================================
rem  svpn.cmd -- Windows one-shot entry, equals: python -m svpn %*
rem  Working directory is pinned to this script's folder
rem  (config.json / runtime/ live there).
rem
rem  Usage:
rem    svpn init
rem    svpn up -b --system-proxy
rem    svpn down --clear-hosts
rem    svpn probe
rem    svpn cdn --refresh
rem    svpn cdn --refresh --no-bench
rem    svpn hosts apply
rem    svpn node scan
rem    svpn status
rem    svpn doctor
rem  No argument = svpn status.
rem
rem  NOTE: keep comments ASCII-only. When the console starts at CP936 and a
rem  "rem" line holds multi-byte text, cmd.exe mis-tracks line offsets and
rem  leaks "'?' is not recognized" noise. Chinese only inside echo lines.
rem ============================================================
setlocal EnableExtensions
pushd "%~dp0" || (
    echo [x] 进不到脚本目录：%~dp0

    exit /b 1
)

rem Pick a python that actually runs. The Microsoft Store python.exe alias
rem fails on -c, so it is skipped. "call" is mandatory: a .bat/.cmd wrapper
rem (pyenv-win, conda) would otherwise never return control to this script.
set "PY="
call python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
    call py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [x] 没找到可用的 Python。装 3.10+，安装时勾选 "Add python.exe to PATH"。

    popd
    exit /b 9009
)

if "%~1"=="" goto :noargs

call %PY% -m svpn %*
set "RC=%ERRORLEVEL%"
goto :done

:noargs
call %PY% -m svpn status
set "RC=%ERRORLEVEL%"

:done
popd
exit /b %RC%

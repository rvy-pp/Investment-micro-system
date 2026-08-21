@echo off
REM ============================================================
REM  Stops the Investment Micro-System server, freeing port 8770.
REM  Closing the browser does NOT do this.
REM
REM  Port 8770, NOT 8765 - 8765 is the vault's node dashboard and
REM  killing that is not what you came here to do.
REM
REM  NOTE ON THE ECHO BELOW: it says "PID %%p" with no brackets on
REM  purpose. An unescaped ")" inside a "do ( ... )" block closes
REM  the block early - the vault's Stop Dashboard.bat writes
REM  "(PID %%p)..." and dies with "... was unexpected at this
REM  time." Use ^( ^) if you ever want the brackets back.
REM ============================================================
setlocal
set "PORT=8770"
set "FOUND="

for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:"127.0.0.1:%PORT%" ^| findstr /C:"LISTENING"') do (
    set "FOUND=1"
    echo Stopping server on port %PORT% - PID %%p
    taskkill /PID %%p /F >nul 2>&1
)

if not defined FOUND (
    echo Nothing was listening on port %PORT%.
) else (
    echo Stopped.
)

REM A ping, not `timeout`, purely to hold the window open for a moment.
REM timeout.exe needs a real console and dies with "Input redirection is not
REM supported" whenever stdin is redirected, which is every automated test of
REM this script. ping has no such requirement. (git-bash's /usr/bin/timeout
REM also shadows the Windows one, so the bare name was wrong twice over.)
ping -n 3 127.0.0.1 >nul
exit /b 0

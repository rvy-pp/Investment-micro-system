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

"%SystemRoot%\System32	imeout.exe" /t 2 /nobreak >nul
exit /b 0

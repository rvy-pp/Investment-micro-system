@echo off
REM ============================================================
REM  Registers a Windows Scheduled Task that refreshes the scores
REM  every weekday morning. Run this ONCE. Re-running replaces it.
REM
REM  WEEKDAYS ONLY, and at 08:15 IST — before the 09:15 NSE open,
REM  after Yahoo has settled the previous session's closes. A
REM  weekend run would write rows carrying Friday's prices under a
REM  Saturday date, which is not wrong exactly, but it pads the
REM  history with dates on which nothing could have been traded.
REM
REM  /RL LIMITED, not HIGHEST: this writes to a SQLite file in the
REM  user profile and needs no elevation. Asking for it would make
REM  the install prompt for admin every time for no benefit.
REM ============================================================
setlocal
set "TASK=IMS Daily Refresh"
set "REPO=%~dp0.."
pushd "%REPO%"
set "REPO=%CD%"
popd

REM Resolve the real python.exe. SCHTASKS does not read PATH the same way an
REM interactive shell does, so a bare "python" can work here and fail at 08:15.
for /f "delims=" %%p in ('where python 2^>nul') do (
    set "PYEXE=%%p"
    goto :got
)
:got
if not defined PYEXE (
    echo Could not find python on PATH. Install it or edit this file to
    echo hardcode the full path to python.exe.
    pause
    exit /b 1
)

echo Task   : %TASK%
echo Python : %PYEXE%
echo Script : %REPO%\packages\refresh.py
echo Runs   : weekdays 08:15
echo.

schtasks /Create /TN "%TASK%" /F ^
  /TR "\"%PYEXE%\" \"%REPO%\packages\refresh.py\"" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:15 ^
  /RL LIMITED

if errorlevel 1 (
    echo.
    echo FAILED to register the task.
    pause
    exit /b 1
)

echo.
echo Registered. Useful commands:
echo    schtasks /Query  /TN "%TASK%" /V /FO LIST     ^<-- last result, next run
echo    schtasks /Run    /TN "%TASK%"                 ^<-- test it now
echo    schtasks /Delete /TN "%TASK%" /F              ^<-- remove it
echo.
echo A task that silently stops looks exactly like a quiet market, so the
echo dashboard header shows a refresh light. Check it, not this window.
echo.
pause

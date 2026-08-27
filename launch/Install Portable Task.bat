@echo off
REM ============================================================
REM  Registers a Windows Scheduled Task that republishes the
REM  PORTABLE copy every weekday morning. Run this ONCE.
REM  Re-running replaces it.
REM
REM  08:40, NOT 08:15. "IMS Daily Refresh" runs at 08:15 and this
REM  must not overlap it: the builder reads the SQLite store, and
REM  snapshotting a half-finished refresh would publish a page
REM  that looks complete and is not. 25 minutes is the gap. If
REM  the refresh ever grows past that, move this later - not the
REM  refresh earlier, which would put it in front of Yahoo's
REM  settled closes.
REM
REM  A SEPARATE TASK rather than an edit to refresh.py, on
REM  purpose: publishing is not part of the refresh contract, and
REM  a failure to publish must never mark the refresh failed.
REM
REM  /RL LIMITED, same as the refresh task. This reads a SQLite
REM  file and writes one HTML file into the user's own OneDrive.
REM  Nothing here needs elevation.
REM ============================================================
setlocal
set "TASK=IMS Publish Portable"
set "REPO=%~dp0.."
pushd "%REPO%"
set "REPO=%CD%"
popd

REM Resolve the real python.exe. SCHTASKS does not read PATH the same way an
REM interactive shell does, so a bare "python" can work here and fail at 08:40.
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
echo Script : %REPO%\packages\review\build_portable.py
echo Runs   : weekdays 08:40
echo.

schtasks /Create /TN "%TASK%" /F ^
  /TR "\"%PYEXE%\" \"%REPO%\packages\review\build_portable.py\"" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 08:40 ^
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
echo The published page carries its own build date in the corner pill.
echo If that date stops moving, this task has stopped - check it there,
echo not in this window.
echo.
pause

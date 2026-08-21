@echo off
REM ============================================================
REM  Runs the daily refresh VISIBLY, and keeps the window open.
REM
REM  Same thing the scheduled task and the .vbs launcher run, but
REM  where you can read it. Use this when the dashboard's refresh
REM  light has gone amber or red and you want to see why.
REM
REM  It does NOT cover: Wind zinc, broker mail, the Daily Metals
REM  Pack, or extraction — those need an agent or a person. The
REM  run prints that list at the end every time.
REM ============================================================
cd /d "%~dp0.."
echo Repo: %CD%
echo.
python packages\refresh.py
echo.
REM Checked HIGH to LOW: `if errorlevel N` is true for anything >= N, so
REM testing 1 first would swallow 2 and report a stale feed as a failure.
if errorlevel 2 (
    echo ------------------------------------------------------------
    echo RAN OK, but one or more FEEDS ARE STALE - see the list above.
    echo Scores for the affected names are WITHHELD, not guessed.
    echo Fix by supplying the input: drop the metals pack, pull Wind
    echo zinc, or have an agent capture the westmetall LME table.
    echo ------------------------------------------------------------
) else if errorlevel 1 (
    echo ------------------------------------------------------------
    echo REFRESH FAILED - see the output above and data\refresh\
    echo ------------------------------------------------------------
) else (
    echo Refresh OK, all feeds current.
)
echo.
pause

@echo off
REM ============================================================
REM  Rebuilds the PORTABLE copy of the dashboard into OneDrive:
REM
REM    Obsidian Vault\Modular Investment System\
REM        Investment Micro-System.html
REM
REM  That file is a self-contained snapshot - the real page plus
REM  every API answer welded into it - so it opens on a phone or
REM  any machine with no Python and no server. It is a PHOTOGRAPH:
REM  it shows the data as of the moment you ran this, and the
REM  page says so in a pill in the bottom-right corner.
REM
REM  Run this AFTER a refresh, not before. "Update Now.bat" pulls
REM  today's numbers; this one publishes them.
REM
REM  The server: if the dashboard is already up on 8770 this uses
REM  it and LEAVES IT RUNNING. Otherwise it starts a private one
REM  on 8771 and stops it again. Either way the database is only
REM  read, never written.
REM ============================================================
cd /d "%~dp0.."
echo Repo: %CD%
echo.
python packages\review\build_portable.py
echo.
if errorlevel 1 (
    echo ------------------------------------------------------------
    echo PUBLISH FAILED - see the output above. The previous copy in
    echo OneDrive is untouched: the builder writes to a .tmp file and
    echo renames it only on success, so a failed run cannot leave a
    echo half-written page to sync out.
    echo ------------------------------------------------------------
) else (
    echo Published. Give OneDrive a minute to sync, then open the file
    echo from the vault folder on the other device.
)
echo.
pause

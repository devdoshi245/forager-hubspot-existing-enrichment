@echo off
REM Free, read-only count of what's in HubSpot + projected cost. Spends NOTHING.
cd /d "%~dp0"
echo ============================================================
echo   FREE ESTIMATE - counts records, spends ZERO credits
echo ============================================================
python -m bulk.cli estimate
echo.
pause

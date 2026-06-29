@echo off
REM Enriches ONE company (Forager firmographics + Claude ICP/logo/Tier).
REM Already-enriched companies are skipped, so re-run this for the next one.
REM (Code hard-caps any run at 5 max regardless.)
cd /d "%~dp0"
echo ============================================================
echo   ENRICH 1 COMPANY  (spends Forager search + Claude credits)
echo   - Does exactly one, then stops.
echo   - Skips already-done companies; re-run for the next one.
echo ============================================================
echo.
set /p ok="Type Y then Enter to proceed (anything else cancels): "
if /I not "%ok%"=="Y" (
  echo Cancelled. Nothing was spent.
  pause
  exit /b
)
python -m bulk.cli run companies --execute --max-records 1
echo.
echo Done. The line above starting with "records" shows WHICH company was processed.
pause

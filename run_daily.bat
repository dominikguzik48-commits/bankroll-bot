@echo off
REM Daily paper session. Schedule with Task Scheduler at 3:45 PM US/Eastern on weekdays.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m bot run-day >> logs\run_daily.log 2>&1

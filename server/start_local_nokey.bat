@echo off
setlocal
cd /d "%~dp0"

REM Fully local intent routing: safe keyword parser, no model API call and no API key.
set "AGENT_NLU_MODE=keyword"
set "PYTHONIOENCODING=utf-8"

echo Starting server with AGENT_NLU_MODE=keyword (no model API calls)...
python -u main.py
exit /b %errorlevel%

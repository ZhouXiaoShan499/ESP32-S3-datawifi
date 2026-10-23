@echo off
setlocal
cd /d "%~dp0"

REM Keep the board-facing Uvicorn service on HTTP:8000, and expose an HTTPS
REM browser-facing proxy on 8443 so Web Speech gets a secure context.
REM Generate certificates first: powershell -ExecutionPolicy Bypass -File make_dev_https.ps1

set "SENSOR_HTTPS_PORT=8443"
if not "%~1"=="" set "SENSOR_HTTPS_PORT=%~1"
set "SENSOR_BACKEND_PORT=8000"
if defined SENSOR_PORT set "SENSOR_BACKEND_PORT=%SENSOR_PORT%"
if not "%~2"=="" set "SENSOR_BACKEND_PORT=%~2"

python https_proxy.py --listen-port %SENSOR_HTTPS_PORT% ^
  --upstream-port %SENSOR_BACKEND_PORT% ^
  --certfile certs\dev-cert.pem ^
  --keyfile certs\dev-key.pem
exit /b %errorlevel%

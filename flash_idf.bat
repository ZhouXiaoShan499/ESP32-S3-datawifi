@echo off
REM Flash the firmware to COM5 (or a port passed as %1) in the background and tee the output to a log.
REM NOTE: no "monitor" here on purpose - monitor is interactive and would hang
REM       any non-interactive caller. Use it manually after flashing if needed.
REM The ESP-IDF install path differs per machine (same probe as build_idf.bat):
REM IDF_PATH first, then the known locations newest-first.
cd /d "%~dp0"
set "COM_PORT=%~1"
set "FLASH_LOG=%TEMP%\flash_log.txt"
if "%COM_PORT%"=="" set "COM_PORT=COM5"

set "IDF_EXPORT="
if defined IDF_PATH if exist "%IDF_PATH%\export.bat" set "IDF_EXPORT=%IDF_PATH%\export.bat"
if not defined IDF_EXPORT if exist "C:\esp\v5.5.4\esp-idf\export.bat" set "IDF_EXPORT=C:\esp\v5.5.4\esp-idf\export.bat"
if not defined IDF_EXPORT if exist "D:\Espressif\frameworks\esp-idf-v5.4.4\export.bat" set "IDF_EXPORT=D:\Espressif\frameworks\esp-idf-v5.4.4\export.bat"
if not defined IDF_EXPORT if exist "D:\Espressif\frameworks\esp-idf-v5.4.3\export.bat" set "IDF_EXPORT=D:\Espressif\frameworks\esp-idf-v5.4.3\export.bat"

if not defined IDF_EXPORT (
    echo No ESP-IDF export.bat found. Set IDF_PATH or add your install to flash_idf.bat. >"%FLASH_LOG%"
    echo FLASH_EXIT=1 >>"%FLASH_LOG%"
    exit /b 1
)

echo Using IDF export: %IDF_EXPORT% >"%FLASH_LOG%"
echo Flashing on port: %COM_PORT% >>"%FLASH_LOG%"
call "%IDF_EXPORT%" >>"%FLASH_LOG%" 2>&1
idf.py -p %COM_PORT% flash >>"%FLASH_LOG%" 2>&1
echo FLASH_EXIT=%errorlevel% >> "%FLASH_LOG%"

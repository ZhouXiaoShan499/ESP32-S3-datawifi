@echo off
REM Flash the firmware to COM5 in the background and tee the output to a log.
REM NOTE: no "monitor" here on purpose - monitor is interactive and would hang
REM       any non-interactive caller. Use it manually after flashing if needed.
cd /d "%~dp0"
set "FLASH_LOG=%TEMP%\flash_log.txt"
call "D:\Espressif\frameworks\esp-idf-v5.4.4\export.bat" > "%FLASH_LOG%" 2>&1
idf.py -p COM5 flash >> "%FLASH_LOG%" 2>&1
echo FLASH_EXIT=%errorlevel% >> "%FLASH_LOG%"

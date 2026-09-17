@echo off
REM Run ESP-IDF build in the background and tee the output to build_log.txt.
cd /d "%~dp0"
call "D:\Espressif\frameworks\esp-idf-v5.4.4\export.bat" > "build_log.txt" 2>&1
idf.py build >> "build_log.txt" 2>&1
echo BUILD_EXIT=%errorlevel% >>"build_log.txt"

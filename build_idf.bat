@echo off
REM Run ESP-IDF build in the background and tee the output to build_log.txt.
REM The ESP-IDF install path differs per machine, so probe the known installs
REM instead of hard-coding one: IDF_PATH first (if already exported), then the
REM newest install that has actually been used with this project. Ordering
REM matters because build/ is configured for one specific IDF version.
cd /d "%~dp0"

set "IDF_EXPORT="
if defined IDF_PATH if exist "%IDF_PATH%\export.bat" set "IDF_EXPORT=%IDF_PATH%\export.bat"
if not defined IDF_EXPORT if exist "C:\esp\v5.5.4\esp-idf\export.bat" set "IDF_EXPORT=C:\esp\v5.5.4\esp-idf\export.bat"
if not defined IDF_EXPORT if exist "D:\Espressif\frameworks\esp-idf-v5.4.4\export.bat" set "IDF_EXPORT=D:\Espressif\frameworks\esp-idf-v5.4.4\export.bat"
if not defined IDF_EXPORT if exist "D:\Espressif\frameworks\esp-idf-v5.4.3\export.bat" set "IDF_EXPORT=D:\Espressif\frameworks\esp-idf-v5.4.3\export.bat"

if not defined IDF_EXPORT (
    echo No ESP-IDF export.bat found. Set IDF_PATH or add your install to build_idf.bat. >"build_log.txt"
    echo BUILD_EXIT=1 >>"build_log.txt"
    exit /b 1
)

echo Using IDF export: %IDF_EXPORT% >"build_log.txt"
call "%IDF_EXPORT%" >>"build_log.txt" 2>&1
idf.py build >>"build_log.txt" 2>&1
echo BUILD_EXIT=%errorlevel% >>"build_log.txt"

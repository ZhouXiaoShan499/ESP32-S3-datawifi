@echo off
REM Run ESP-IDF build in the background and tee the output to build_log.txt.
REM The ESP-IDF install path differs per machine, so probe the known installs
REM instead of hard-coding one: IDF_PATH first (if already exported), then the
REM newest install that has actually been used with this project. Ordering
REM matters because build/ is configured for one specific IDF version.
cd /d "%~dp0"

REM Clear MSYSTEM before calling ESP-IDF's export.bat. Git Bash / MSYS2 exports
REM MSYSTEM, and export.bat starts with a guard that aborts when it is set:
REM   if defined MSYSTEM (echo This .bat file is for Windows CMD.EXE shell only.)
REM Symptom when it triggers: BUILD_EXIT=9009 plus "idf.py is not recognized".
REM The quoted assignment form is required -- writing  set MSYSTEM= & foo  would
REM leave the space before the separator as the value, so MSYSTEM stays defined
REM and the guard still fires.
set "MSYSTEM="
set "MSYSTEM_PREFIX="
set "MINGW_PREFIX="

set "IDF_EXPORT="

REM 候选安装：路径 + 版本号。版本号只用于和 build\config.env 里记录的版本对账
REM （不解析 JSON —— 路径里的盘符冒号会让 for /f delims 解析变成一团乱麻）。
set "IDF_C1=C:\esp\v5.5.4\esp-idf"
set "IDF_V1=5.5.4"
set "IDF_C2=D:\Espressif\frameworks\esp-idf-v5.4.4"
set "IDF_V2=5.4.4"
set "IDF_C3=D:\Espressif\frameworks\esp-idf-v5.4.3"
set "IDF_V3=5.4.3"

REM build\config.env 里记着「build/ 是用哪个版本配置出来的」（IDF_VERSION 与各 Kconfig
REM 路径都带版本号）。用它做一次对账：命中哪个候选就把那个候选当成首选。
REM 这正是原来那个 bug：探测顺序是「新→旧」，于是没导出 IDF_PATH 时会挑 5.5.4，
REM 而 build/ 是 5.4.3 配置的 ⇒ 拿另一个 IDF 去重配同一个 build/，报错或诡异重编。
set "IDF_RECORDED=none"
if exist "build\config.env" (
    findstr /c:"%IDF_V3%" "build\config.env" >nul 2>&1 && set "IDF_RECORDED=%IDF_V3%"
    findstr /c:"%IDF_V2%" "build\config.env" >nul 2>&1 && set "IDF_RECORDED=%IDF_V2%"
    findstr /c:"%IDF_V1%" "build\config.env" >nul 2>&1 && set "IDF_RECORDED=%IDF_V1%"
)

REM ① IDF_PATH 已导出且可用 —— 用户显式指定，优先级最高
if defined IDF_PATH if exist "%IDF_PATH%\export.bat" set "IDF_EXPORT=%IDF_PATH%\export.bat"

REM ② 与 build\config.env 记录版本一致的那个安装
if not defined IDF_EXPORT if "%IDF_RECORDED%"=="%IDF_V3%" if exist "%IDF_C3%\export.bat" set "IDF_EXPORT=%IDF_C3%\export.bat"
if not defined IDF_EXPORT if "%IDF_RECORDED%"=="%IDF_V2%" if exist "%IDF_C2%\export.bat" set "IDF_EXPORT=%IDF_C2%\export.bat"
if not defined IDF_EXPORT if "%IDF_RECORDED%"=="%IDF_V1%" if exist "%IDF_C1%\export.bat" set "IDF_EXPORT=%IDF_C1%\export.bat"

REM ③ 兜底：build/ 还没生成（config.env 不存在）或版本对不上时，按「新→旧」探测
if not defined IDF_EXPORT if exist "%IDF_C1%\export.bat" set "IDF_EXPORT=%IDF_C1%\export.bat"
if not defined IDF_EXPORT if exist "%IDF_C2%\export.bat" set "IDF_EXPORT=%IDF_C2%\export.bat"
if not defined IDF_EXPORT if exist "%IDF_C3%\export.bat" set "IDF_EXPORT=%IDF_C3%\export.bat"

if not defined IDF_EXPORT (
    echo No ESP-IDF export.bat found. Set IDF_PATH or add your install to build_idf.bat. >"build_log.txt"
    echo BUILD_EXIT=1 >>"build_log.txt"
    exit /b 1
)

REM 选中版本与记录版本都写进日志：对不上时一眼可见（不要再照日志里的
REM "idf.py fullclean" 建议去做 —— 那是版本错配，不是缓存问题，fullclean 只会白费一次全量编译）。
echo Using IDF export: %IDF_EXPORT%   [build/config.env recorded: %IDF_RECORDED%] >"build_log.txt"
call "%IDF_EXPORT%" >>"build_log.txt" 2>&1
idf.py build >>"build_log.txt" 2>&1
echo BUILD_EXIT=%errorlevel% >>"build_log.txt"

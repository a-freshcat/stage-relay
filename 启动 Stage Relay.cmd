@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 日常入口：无论已运行还是新启动，都用同一份代码、同一个数据目录。
rem 不在 release 下挑文件夹，也不判断"新旧 exe"。
rem
rem 打印出来的版本是"这个程序（app.py 当前代码）的版本"和
rem "实际在跑的服务的版本"，两者可能不同 —— 见下方说明。

echo.
echo ============================================================
echo   Stage Relay
echo ============================================================
echo.

python -c "import sys; sys.path.insert(0, r'%~dp0'); from relay import __version__; print('  启动器代码版本: ' + __version__)"
if errorlevel 1 (
  echo   [错误] 找不到 Python 或代码不完整。
  echo   请确认本目录下有 app.py 与 relay\ 目录。
  echo.
  pause
  exit /b 1
)

echo.
python app.py --open %*
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
  echo.
  echo   [提示] 启动未成功，返回码 %RC%。
  echo   上面的文字说明了原因；完整日志见数据目录下的 desktop.log 或本次输出。
  echo.
  pause
)

endlocal
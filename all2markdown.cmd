@chcp 65001 >nul
@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 尚未初始化，请先运行 init.cmd
  exit /b 3
)
"%~dp0.venv\Scripts\python.exe" "%~dp0all2markdown.py" %*
exit /b %ERRORLEVEL%

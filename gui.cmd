@chcp 65001 >nul
@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 尚未初始化，请先运行 init.cmd
  exit /b 3
)
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0gui.py" %*
) else (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0gui.py" %*
)
exit /b %ERRORLEVEL%

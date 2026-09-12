@echo off
setlocal EnableExtensions
chcp 65001 >nul
set "PYTHONUTF8=1"
set "SCRIPT_DIR=%~dp0"
set "GUI_PY=%SCRIPT_DIR%gui.py"

if exist "%SCRIPT_DIR%.venv\Scripts\pythonw.exe" (
  start "" "%SCRIPT_DIR%.venv\Scripts\pythonw.exe" "%GUI_PY%" %*
  exit /b 0
)
if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
  "%SCRIPT_DIR%.venv\Scripts\python.exe" "%GUI_PY%" %*
  exit /b %ERRORLEVEL%
)

rem 未初始化：用系统 Python 启动引导界面（状态检测 + 一键初始化）
where pyw >nul 2>nul
if not errorlevel 1 (
  pyw -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
  if not errorlevel 1 (
    start "" pyw -3 "%GUI_PY%" %*
    exit /b 0
  )
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
  if not errorlevel 1 (
    start "" pyw -3 "%GUI_PY%" %*
    if not errorlevel 1 exit /b 0
    start "" py -3 "%GUI_PY%" %*
    exit /b 0
  )
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  pythonw -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
  if not errorlevel 1 (
    start "" pythonw "%GUI_PY%" %*
    exit /b 0
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
  if not errorlevel 1 (
    start "" pythonw "%GUI_PY%" %*
    if not errorlevel 1 exit /b 0
    start "" python "%GUI_PY%" %*
    exit /b 0
  )
)

echo 未找到 Python 3.8+，无法启动图形界面。
echo 请先安装 Windows x64 Python 3.8 或更高版本，然后重新双击 gui.cmd。
pause
exit /b 2

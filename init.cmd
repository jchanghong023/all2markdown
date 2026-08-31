@chcp 65001 >nul
@echo off
set "PYTHONUTF8=1"
py -3 -c "import platform,sys; raise SystemExit(0 if sys.version_info >= (3, 8) and platform.system() == 'Windows' and platform.machine().lower() in ('amd64', 'x86_64') and sys.maxsize.bit_length() == 63 else 1)" >nul 2>nul
if not errorlevel 1 goto use_py
python -c "import platform,sys; raise SystemExit(0 if sys.version_info >= (3, 8) and platform.system() == 'Windows' and platform.machine().lower() in ('amd64', 'x86_64') and sys.maxsize.bit_length() == 63 else 1)" >nul 2>nul
if not errorlevel 1 goto use_python
echo 未找到可用于初始化的 Windows x64 Python 3.8+；请先安装 Python 后重新运行 init.cmd
exit /b 2

:use_py
py -3 "%~dp0src\init_env.py"
exit /b %ERRORLEVEL%

:use_python
python "%~dp0src\init_env.py"
exit /b %ERRORLEVEL%

@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if not exist "app.py" goto :missing
if not exist "database.py" goto :missing
if not exist "requirements.txt" goto :missing

if exist ".venv\Scripts\python.exe" goto :install
py -3 -c "import sys; raise SystemExit(sys.version_info < (3,10))" >nul 2>&1
if not errorlevel 1 (
    py -3 -m venv .venv
    if errorlevel 1 goto :failed
    goto :install
)
python -c "import sys; raise SystemExit(sys.version_info < (3,10))" >nul 2>&1
if errorlevel 1 goto :no_python
python -m venv .venv
if errorlevel 1 goto :failed

:install
echo 正在安装或检查依赖，首次运行需要联网，请耐心等待...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed
echo 正在启动，请保持此窗口打开。关闭此窗口将停止系统。
echo 如果浏览器没有自动打开，请访问 http://localhost:8501
".venv\Scripts\python.exe" -m streamlit run app.py --server.address 127.0.0.1 --server.headless false
if errorlevel 1 goto :failed
exit /b 0

:no_python
echo 未找到可用的 Python 3.10 或更高版本，建议安装 Python 3.12。
echo 下载地址：https://www.python.org/downloads/
echo 安装时勾选 Add python.exe to PATH，然后重新打开此文件。
pause
exit /b 1

:missing
echo 项目文件不完整。请先完整解压 ZIP，不要在压缩包里直接启动。
pause
exit /b 1

:failed
echo.
echo 启动失败，请保留并截图上方的错误信息。
pause
exit /b 1

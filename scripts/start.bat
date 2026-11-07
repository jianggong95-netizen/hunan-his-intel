@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv 虚拟环境。
    echo 请先执行：python -m venv .venv  然后  .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [提示] 未找到 .env，已从 .env.example 复制，请按需修改后重启。
    copy /y ".env.example" ".env" >nul
)

echo 正在启动 湖南省公立医院 HIS 中标情报系统 ...
echo 浏览器访问：http://127.0.0.1:8765  （默认账号 admin / admin123）
".venv\Scripts\python.exe" main.py serve
pause

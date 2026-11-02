@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/3] 检查 Claude Code 登录...
where claude >nul 2>nul || (echo 未找到 claude 命令，请先安装 Claude Code 并运行 claude 登录你的订阅账号 & pause & exit /b 1)
echo [2/3] 创建虚拟环境并安装依赖...
where uv >nul 2>nul && (
  uv venv .venv --python 3.13
  uv pip install --python .venv -r requirements.txt
) || (
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
)
echo [3/3] 完成。双击「启动 Bot.vbs」运行；按 Ctrl+Alt+B 呼出面板。
.venv\Scripts\python -c "from bot.app import write_launcher, LAUNCHER; write_launcher(LAUNCHER)"
pause

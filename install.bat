@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo sky15 安装脚本
echo ------------------------------------------------------------

where python >nul 2>nul
if errorlevel 1 (
  echo 没找到 python。请先装 Python 3.10~3.13，安装时勾选 "Add python.exe to PATH"。
  echo 下载：https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 建虚拟环境 .venv
  python -m venv .venv
  if errorlevel 1 ( echo 建 venv 失败 & pause & exit /b 1 )
) else (
  echo [1/3] 已有 .venv，跳过
)

echo [2/3] 装依赖（约 700 MB，第一次要几分钟）
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo 第一次失败了，换成镜像 + 不用缓存再试一次
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple
  if errorlevel 1 ( echo 还是失败，把上面的报错贴到 issue 里 & pause & exit /b 1 )
)

rem Transkun 的依赖里有 ncls（PyPI 上没有 Windows 轮子，要 MSVC 才能编译），
rem 推理用不到它，所以这里 --no-deps 单独装。
echo [2/3] 装转谱模型 transkun
".venv\Scripts\python.exe" -m pip install -q --no-deps transkun
if errorlevel 1 (
  echo 装 transkun 失败：".venv\Scripts\python.exe" -m pip install --no-deps transkun -i https://pypi.tuna.tsinghua.edu.cn/simple
  pause
  exit /b 1
)

echo [3/3] 体检
".venv\Scripts\python.exe" -m sky15.cli doctor

echo.
echo 用法：
echo   .venv\Scripts\python.exe -m sky15.cli audio 你的录音.mp3 -o out\demo.smc
echo   双击 sky15\web\start-ui.bat 打开界面（也可以直接跑 .venv\Scripts\python.exe -m sky15.web.server）
echo.
pause

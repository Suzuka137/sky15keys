@echo off
chcp 65001 >nul
cd /d %~dp0
set PYTHONUTF8=1
start "" http://127.0.0.1:8777/
python server.py

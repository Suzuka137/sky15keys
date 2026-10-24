@echo off
chcp 65001 >nul
cd /d %~dp0
set PYTHONUTF8=1
rem open the browser a few seconds later, from its own window: server.py needs a
rem moment to bind 8777, and opening the page first just lands on an error page.
start "" /min cmd /c "ping -n 4 127.0.0.1 >nul & start http://127.0.0.1:8777/"
python server.py

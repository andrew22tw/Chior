@echo off
chcp 65001 >nul
cd /d %~dp0
python -c "import playwright" 2>nul || (pip install -r requirements.txt && python -m playwright install chromium)
python ucc_gui.py
pause

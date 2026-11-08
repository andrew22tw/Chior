#!/bin/bash
cd "$(dirname "$0")"
python3 -c "import playwright" 2>/dev/null || (pip3 install -r requirements.txt && python3 -m playwright install chromium)
python3 ucc_gui.py

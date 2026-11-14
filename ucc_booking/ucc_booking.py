#!/usr/bin/env python3
"""
檢查工具：開啟預班網頁，把所有表單元素／按鈕輸出到 inspect_report.txt 與 inspect.png。
自動登記若失敗，把這兩個檔案（或登入後的 inspect_report_error.txt）提供給開發者調整。

  python ucc_booking.py              # 只看登入頁
  python ucc_booking.py --pause      # 開瀏覽器讓你手動登入／點日期，按 Enter 後再輸出
"""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

import ucc_core as core

HERE = Path(__file__).resolve().parent

ap = argparse.ArgumentParser()
ap.add_argument("--url", default=core.DEFAULT_URL)
ap.add_argument("--pause", action="store_true")
a = ap.parse_args()

with sync_playwright() as p:
    browser = core.launch_browser(p, False)
    b = core.Booker({"url": a.url})
    b.page = browser.new_page()
    b.page.goto(a.url, wait_until="domcontentloaded", timeout=60000)
    b.wait_ready()
    if a.pause:
        input("請在瀏覽器操作到想檢查的畫面（例如登入後點一個日期），然後按 Enter…")
    (HERE / "inspect_report.txt").write_text(b.dump(), encoding="utf-8")
    b.page.screenshot(path=str(HERE / "inspect.png"), full_page=True)
    print("已輸出 inspect_report.txt 與 inspect.png")
    browser.close()

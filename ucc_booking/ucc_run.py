#!/usr/bin/env python3
"""
無介面版（雲端或排程用）：讀取 GUI 匯出的 wishes.json，時間到同時送出。

  UCC_ID=員編 UCC_PW=密碼 python ucc_run.py wishes.json
  python ucc_run.py wishes.json            # 未設環境變數時會詢問（密碼不顯示）
  python ucc_run.py wishes.json --now      # 不等開放時間，立即送出

帳號密碼只在記憶體中使用，不寫入任何檔案。
"""
import argparse
import datetime as dt
import getpass
import json
import os
import sys
from pathlib import Path

import ucc_core as core
import ucc_fast

ap = argparse.ArgumentParser()
ap.add_argument("wishes")
ap.add_argument("--now", action="store_true", help="立即送出，不等開放時間")
ap.add_argument("--open-time", help="覆寫開放時間，例如 2026-11-14 09:00:00（台灣時間）")
ap.add_argument("--chrome", default="/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None)
a = ap.parse_args()

data = json.loads(Path(a.wishes).read_text(encoding="utf-8"))
wishes = [{"date": dt.date.fromisoformat(w["date"]), "site": w["site"], "shift": w["shift"],
           "clinics": w.get("clinics") or ["診1", "診2", "備援"]} for w in data["wishes"]]
uid = os.environ.pop("UCC_ID", "") or input("ID：").strip()
pw = os.environ.pop("UCC_PW", "") or getpass.getpass("密碼（不顯示）：")

open_ts = None
if not a.now:
    tw = dt.timezone(dt.timedelta(hours=8))          # 開放時間以台灣時間計，雲端主機時區不影響
    t = dt.datetime.strptime(a.open_time or data["open_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tw)
    open_ts = t.timestamp()

log = lambda m: print(f"[{dt.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {m}", flush=True)
offset, src = core.sync_time(log)
log(f"ID：{uid}；{len(wishes)} 筆；開放時間 {'立即' if open_ts is None else data.get('open_time')}")
ucc_fast.FastBooker({"url": data.get("url") or core.DEFAULT_URL, "user_id": uid, "password": pw,
                     "wishes": wishes, "open_ts": open_ts, "offset": offset, "preload_sec": 40,
                     "retry_sec": 120, "capture": True, "headless": True, "keep_open": False,
                     "chrome_path": a.chrome}, log=log).run()

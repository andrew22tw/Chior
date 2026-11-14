#!/usr/bin/env python3
"""每日檢查：讀取 UCC 預班系統的開放設定（不送出任何預約、不讀取他人資料）。"""
import datetime as dt
import json
import os

from playwright.sync_api import sync_playwright

URL = ("https://script.google.com/macros/s/AKfycbwJqVMzejJMXYFv1o5b20WV2rUSAisgXbGTuvwrMKh"
       "AjYmf9OcaIXDhs3YINjXVQFgZ8g/exec")
CHROME = "/opt/pw-browsers/chromium"

JS = r"""
() => new Promise(ok => google.script.run.withSuccessHandler(r => {
  const s = r.systemSettings || {};
  const per = (r.locations || []).length * (r.shifts || []).length * 3;
  const count = {};
  (r.bookings || []).forEach(b => { const k = String(b.date).slice(0, 7); count[k] = (count[k] || 0) + 1; });
  ok({mode: s.mode, openDay: s.openDay, start: s.openStartTime, end: s.openEndTime,
      days: (r.scheduleDays || []).map(d => [d.date, d.note || '']), count, per});
}).withFailureHandler(e => ok({err: String(e)})).getInitialData())
"""

TW = dt.timezone(dt.timedelta(hours=8))


def summarize(r):
    """算出下一次開放時間，以及那次開放可以預約的月份。"""
    now = dt.datetime.now(TW)
    day = int(r["openDay"])
    sh, sm = map(int, str(r["start"]).split(":")[:2])
    eh, em = map(int, str(r["end"]).split(":")[:2])
    y, m = now.year, now.month
    end_this = dt.datetime(y, m, day, eh, em, tzinfo=TW)
    if now > end_this:                                  # 這個月已經結束，下一次是下個月
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    nxt = dt.datetime(y, m, day, sh, sm, tzinfo=TW)
    ty, tm = (y + 1, 1) if m == 12 else (y, m + 1)      # 開放時預約「下個月」
    key = f"{ty}-{tm:02d}"
    days = [d for d, _ in r["days"] if d.startswith(key)]
    return {"mode": r["mode"], "open_rule": f"每月 {day} 日 {r['start']}–{r['end']}",
            "next_open": nxt.strftime("%Y-%m-%d %H:%M"), "is_open_now": nxt <= now <= nxt.replace(hour=eh, minute=em),
            "target_month": key, "target_days": [d[5:] for d in days],
            "target_booked": r["count"].get(key, 0), "target_capacity": len(days) * r["per"],
            "checked_at": now.strftime("%Y-%m-%d %H:%M")}


kw = {"executable_path": CHROME} if os.path.exists(CHROME) else {}
if os.environ.get("HTTPS_PROXY"):
    kw["proxy"] = {"server": os.environ["HTTPS_PROXY"]}
with sync_playwright() as p:
    b = p.chromium.launch(**kw)
    pg = b.new_page()
    pg.goto(URL, wait_until="domcontentloaded", timeout=60000)
    fr = None
    for _ in range(400):
        for f in pg.frames:
            try:
                if f.evaluate("() => typeof google!=='undefined' && !!(google.script&&google.script.run)"):
                    fr = f
                    break
            except Exception:
                pass
        if fr:
            break
        pg.wait_for_timeout(100)
    if not fr:
        print(json.dumps({"err": "系統網頁載入失敗"}, ensure_ascii=False))
    else:
        r = fr.evaluate(JS)
        print(json.dumps(r if r.get("err") else summarize(r), ensure_ascii=False))
    b.close()

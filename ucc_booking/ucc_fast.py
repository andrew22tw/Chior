"""
UCC 預班 — 快速模式（依實際系統原始碼設計）

系統是 Google Apps Script，預約只是一個伺服器呼叫：
    google.script.run.createBooking(日期, 院區, 班別, 診別, 員工編號, 密碼)
畫面上「未開放預約」是用你電腦的時鐘判斷、而且要重新點日期才會更新；
每次預約成功後畫面還會重新下載全部資料（約 4 秒）。

所以快速模式：
  1. 提前開好網頁，等系統載入完成
  2. 標準時間一到（不會提早），在網頁裡直接呼叫 createBooking
  3. 所有日期「同時」送出（日期由後往前排）；同一天依清單順序：診1 → 診2 → 備援，
     被搶走立刻送下一個候選
  4. 每人每天只能一個班：同一天的多筆清單＝備選，第一個成功就停
  5. 完成後讀取系統資料，列出你名下的預約確認
"""
import datetime as dt
import json
import os
import threading
import time

import ucc_core as core

ORCH_JS = r"""
async ({plan, uid, pw, retryMs, deadline}) => {
  const log = (...a) => console.log('[UCC]' + JSON.stringify(a));
  const call = (fn, ...args) => new Promise(res =>
    google.script.run
      .withSuccessHandler(r => res({ok: true, r}))
      .withFailureHandler(e => res({ok: false, e: String((e && e.message) || e)}))[fn](...args));
  let stopAll = false;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const one = async (day) => {
    for (const c of day.cands) {
      let netTries = 0;
      while (!stopAll) {
        const t0 = performance.now();
        log('send', c.idx, day.date, c.location, c.shift, c.clinic);
        const r = await call('createBooking', day.date, c.location, c.shift, c.clinic, uid, pw);
        const ms = Math.round(performance.now() - t0);
        const msg = r.ok ? String((r.r && r.r.message) || '') : r.e;
        if (r.ok && r.r && r.r.success) { log('ok', c.idx, day.date, c.location, c.shift, c.clinic, ms, msg); return 'ok'; }
        if (/一天只能|已經有預約|已有預約|當日已|同一天/.test(msg)) { log('daydone', c.idx, day.date, '', '', c.clinic, ms, msg); return 'daydone'; }
        if (/已被|額滿|已滿|被預約|已有人/.test(msg)) { log('taken', c.idx, day.date, c.location, c.shift, c.clinic, ms, msg); break; }
        if (/密碼|查無|帳號|員工編號|驗證失敗/.test(msg)) { stopAll = true; log('auth', c.idx, day.date, '', '', c.clinic, ms, msg); return 'auth'; }
        if (/未開放|尚未|開放時間|非開放|不在開放/.test(msg) && Date.now() < deadline) {
          log('notopen', c.idx, day.date, c.location, c.shift, c.clinic, ms, msg); await sleep(retryMs); continue; }
        if (!r.ok && netTries++ < 2) { log('neterr', c.idx, day.date, c.location, c.shift, c.clinic, ms, msg); continue; }
        log('fail', c.idx, day.date, c.location, c.shift, c.clinic, ms, msg);
        break;
      }
      if (stopAll) return 'stopped';
    }
    log('none', -1, day.date);
    return 'none';
  };
  const res = await Promise.allSettled(plan.map(one));
  return res.map(x => x.status === 'fulfilled' ? x.value : String(x.reason));
}
"""

MY_BOOKINGS_JS = r"""
(uid) => new Promise(res => google.script.run
  .withSuccessHandler(r => res((r && r.bookings || []).filter(b => String(b.empId) === String(uid))
     .map(b => [b.date, b.location, b.shift, b.clinicType || '診1'])))
  .withFailureHandler(e => res('ERR ' + e)).getInitialData())
"""


def build_plan(wishes):
    """wishes: [{date, site, shift, clinics}] → 依日期分組（由後往前），每天的候選依清單順序 × 診別順序。"""
    days = {}
    for idx, w in enumerate(wishes):
        d = w["date"].isoformat() if hasattr(w["date"], "isoformat") else str(w["date"])
        for c in (w.get("clinics") or ["診1", "診2", "備援"]):
            days.setdefault(d, []).append({"idx": idx, "location": w["site"], "shift": w["shift"], "clinic": c})
    return [{"date": d, "cands": days[d]} for d in sorted(days, reverse=True)]


class FastBooker:
    """
    opts = {url, user_id, password, wishes, open_ts, offset, preload_sec, retry_sec,
            capture, headless, keep_open, chrome_path}
    """

    def __init__(self, opts, log=print, stop=None, on_status=None):
        self.o = opts
        self.log = log
        self.stop = stop or threading.Event()
        self.on_status = on_status or (lambda i, t: None)
        self.result = {}
        self._cap = core.Booker({"url": opts["url"], "password": opts.get("password", ""),
                                 "capture": opts.get("capture", True)}, log, self.stop)

    # ---------------------------------------------------------------- helpers
    def _find_app_frame(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if self.stop.is_set():
                raise core.Stopped()
            for f in self.page.frames:
                try:
                    if f.evaluate("() => typeof google !== 'undefined' && !!(google.script && google.script.run)"):
                        return f
                except Exception:
                    pass
            self.page.wait_for_timeout(100)
        raise RuntimeError("系統網頁載入逾時")

    def _on_console(self, m):
        t = m.text
        if not t.startswith("[UCC]"):
            return
        try:
            kind, idx, date, loc, shift, clinic, *rest = json.loads(t[5:]) + [""] * 8
        except Exception:
            return
        ms, msg = (rest + ["", ""])[:2]
        d = date[5:].replace("-", "/") if date else ""
        sec = f"{ms/1000:.2f}s" if isinstance(ms, (int, float)) else ""
        name = f"{d} {loc} {shift} {clinic}".strip()
        text = {
            "send": None,
            "ok": f"✓ {clinic} 成功（{sec}）",
            "taken": f"… {clinic} 已被選走，換下一個（{sec}）",
            "daydone": f"- 這天已有你的預約（{msg[:20]}）",
            "auth": f"✗ 帳號或密碼錯誤：{msg[:30]}",
            "notopen": f"… 伺服器回覆尚未開放，重試中",
            "neterr": f"… 連線錯誤，重試（{msg[:30]}）",
            "fail": f"✗ {clinic} 失敗：{msg[:30]}（{sec}）",
            "none": None,
        }.get(kind)
        if kind == "send":
            self.log(f"→ 送出 {name}")
            if idx not in self.result:
                self.on_status(idx, f"送出中：{clinic}")
            return
        if kind == "none":
            self.log(f"✗ {d}：清單上的班都沒搶到")
            return
        self.log(f"{name}：{text}  {msg if kind in ('ok', 'fail') else ''}")
        if idx is not None and idx >= 0 and self.result.get(idx, "").startswith("✓") is False:
            self.result[idx] = text
            self.on_status(idx, text)

    # ---------------------------------------------------------------- main
    def run(self):
        from playwright.sync_api import sync_playwright
        o = self.o
        plan = build_plan(o["wishes"])
        with sync_playwright() as p:
            kw = {"headless": o.get("headless", False)}
            if o.get("chrome_path"):
                kw["executable_path"] = o["chrome_path"]
            if os.environ.get("HTTPS_PROXY"):
                kw["proxy"] = {"server": os.environ["HTTPS_PROXY"]}
            try:
                browser = p.chromium.launch(**kw)
            except Exception:
                browser = p.chromium.launch(channel="chrome", **kw)
            ctx = browser.new_context()
            ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "font", "media")
                      else r.continue_())
            self.page = ctx.new_page()
            cap = self._cap
            cap.page = self.page
            self.page.on("console", self._on_console)
            self.page.on("request", cap._on_request)
            self.page.on("response", cap._on_response)
            try:
                open_ts, off = o.get("open_ts"), o.get("offset", 0.0)
                if open_ts:
                    core.wait_until(open_ts - o.get("preload_sec", 30), off, self.stop, self.log, "距離預先載入 ")
                self.log("載入系統網頁…")
                t = time.time()
                self.page.goto(o["url"], wait_until="domcontentloaded", timeout=60000)
                frame = self._find_app_frame()
                self.log(f"系統就緒（{time.time() - t:.1f} 秒）")
                cap.capture("載入")
                if open_ts:
                    # 不提早：準時在標準時間送出
                    core.wait_until(open_ts, off, self.stop, self.log, "距離開放 ")
                now = dt.datetime.fromtimestamp(time.time() + off).strftime("%H:%M:%S.%f")[:-3]
                self.log(f"[標準時間 {now}] 同時送出 {len(plan)} 天：" + "、".join(x["date"][5:] for x in plan))
                t0 = time.time()
                res = frame.evaluate(ORCH_JS, {"plan": plan, "uid": o["user_id"], "pw": o["password"],
                                               "retryMs": 300,
                                               "deadline": int((time.time() + o.get("retry_sec", 90)) * 1000)})
                ok = sum(r == "ok" for r in res)
                self.log(f"完成：{ok}/{len(plan)} 天成功，總共 {time.time() - t0:.1f} 秒")
                for i, w in enumerate(o["wishes"]):
                    if i not in self.result:
                        self.on_status(i, "（同一天已有其他候選成功或未輪到）")
                self.log("向系統確認你名下的預約…")
                mine = frame.evaluate(MY_BOOKINGS_JS, o["user_id"])
                if isinstance(mine, list):
                    for b in sorted(mine):
                        self.log(f"  系統紀錄：{b[0]} {b[1]} {b[2]} {b[3]}")
                    if not mine:
                        self.log("  系統中沒有你的預約")
                else:
                    self.log(f"  確認失敗：{mine}")
                cap.capture("完成")
            except core.Stopped:
                self.log("已停止")
            except Exception as e:
                self.log(f"錯誤：{e}")
                try:
                    cap.capture("錯誤")
                except Exception:
                    pass
            o["password"] = ""
            if o.get("keep_open", True):
                self.log("瀏覽器保持開啟；按『停止』關閉。")
                while not self.stop.is_set():
                    try:
                        self.page.wait_for_timeout(300)
                    except Exception:
                        break
            browser.close()
            cap._secret = ""

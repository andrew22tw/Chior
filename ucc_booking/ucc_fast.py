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
        if (netTries++ < 3) { log(r.ok ? 'retry' : 'neterr', c.idx, day.date, c.location, c.shift, c.clinic, ms, msg); continue; }
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


SOURCE_JS = r"""
() => {
  const html = document.documentElement.outerHTML;
  const calls = Array.from(html.matchAll(/\.createBooking\(([^)]*)\)/g)).map(m => m[1].replace(/\s+/g, ' '));
  // 只比對系統的程式碼（<script>），畫面內容會隨資料變動，不納入比對
  const code = Array.from(document.scripts).map(s => s.textContent).join('\n');
  return {html, code, calls, title: document.title};
}
"""

DRY_JS = r"""
(uid) => new Promise(res => google.script.run
  .withSuccessHandler(r => {
    if (!r || !r.success) return res({err: (r && r.message) || '讀取失敗'});
    const me = (r.staffList || []).find(s => String(s.empId).toUpperCase() === String(uid).toUpperCase());
    res({settings: r.systemSettings, locations: r.locations, shifts: (r.shifts || []).map(s => s.name),
         days: (r.scheduleDays || []).map(d => [d.date, d.note || '']),
         me: me ? {name: me.name, zone: me.zone, batch: me.batch} : null,
         mine: (r.bookings || []).filter(b => String(b.empId).toUpperCase() === String(uid).toUpperCase())
                 .map(b => [b.date, b.location, b.shift, b.clinicType || '診1']),
         taken: (r.bookings || []).map(b => [b.date, b.location, b.shift, b.clinicType || '診1'])});
  })
  .withFailureHandler(e => res({err: String(e)})).getInitialData())
"""

EXPECTED_CALL = "currentBookingSlot.date, currentBookingSlot.location, currentBookingSlot.shift, currentBookingSlot.clinicType, empId, password"


SHOW_JS = r"""
(date) => { try {
  if (typeof fetchSystemData === 'function')
    fetchSystemData(() => { try { if (date && typeof selectDate === 'function') selectDate(date); } catch (e) {} });
  else if (date && typeof selectDate === 'function') selectDate(date);
} catch (e) {} return true; }
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
            "neterr": f"… 連線錯誤，立即重送（{msg[:30]}）",
            "retry": f"… 未成功（{msg[:30]}），立即重送",
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

    def _show(self, frame, date):
        """讓系統網頁自己重新整理並跳到該日期，你可以在瀏覽器上直接看到結果。"""
        if self.o.get("headless"):
            return
        try:
            frame.evaluate(SHOW_JS, date)
            self.log(f"瀏覽器畫面已更新到 {date[5:] if date else ''}（可在系統畫面上點其他日期查看）")
        except Exception:
            pass

    def check_source(self, frame):
        """每次都讀系統原始碼：存檔、和上次比較、確認 createBooking 參數沒變。"""
        import hashlib
        from pathlib import Path
        src = frame.evaluate(SOURCE_JS)
        h = hashlib.sha256(src["code"].encode()).hexdigest()[:12]
        d = core.app_dir() / "source_history"
        d.mkdir(exist_ok=True)
        last = d / "latest.txt"
        prev = last.read_text().strip() if last.exists() else ""
        f = d / f"{dt.date.today().isoformat()}_{h}.html"
        if not f.exists():
            f.write_text(src["html"], encoding="utf-8")
        last.write_text(h)
        if not prev:
            self.log(f"已保存系統原始碼（{src['title']}，版本 {h}）")
        elif prev == h:
            self.log(f"系統原始碼和上次相同（版本 {h}）")
        else:
            self.log(f"⚠ 系統程式碼已變更（{prev} → {h}），新版保存在 {f.name}")
        ok = any(c.strip() == EXPECTED_CALL for c in src["calls"])
        if ok:
            self.log("✓ createBooking(日期, 院區, 班別, 診別, 員編, 密碼) 參數和程式一致")
        else:
            self.log(f"⚠ createBooking 呼叫方式可能變了：{src['calls'][:2]} — 請把 source_history 給開發者")
        return ok

    def dry_run(self, frame):
        """測試模式：不送出任何預約，只檢查。"""
        o = self.o
        self.log("【測試模式】不會送出任何預約")
        r = frame.evaluate(DRY_JS, o["user_id"])
        if r.get("err"):
            self.log(f"✗ 讀取系統資料失敗：{r['err']}")
            return
        st = r["settings"] or {}
        self.log(f"系統設定：模式 {st.get('mode')}，每月 {st.get('openDay')} 日 "
                 f"{st.get('openStartTime')}–{st.get('openEndTime')} 開放")
        self.log(f"院區 {r['locations']}；班別 {r['shifts']}")
        if r["me"]:
            m = r["me"]
            self.log(f"✓ ID {o['user_id']} 存在：{m['name']}（{m['zone']} {m['batch']}）— 密碼只能在正式送出時驗證")
        else:
            self.log(f"✗ 系統名單中找不到 ID {o['user_id']}，請確認")
        months = sorted({w["date"].strftime("%Y-%m") for w in o["wishes"]}) or []
        for mth in months:
            ds = [d for d, n in r["days"] if d.startswith(mth)]
            self.log(f"{mth} 可排日期：{'、'.join(x[5:] for x in ds) or '（系統尚未設定）'}")
        if r["mine"]:
            for b in sorted(r["mine"]):
                self.log(f"  你已有的預約：{b[0]} {b[1]} {b[2]} {b[3]}")
        taken = {tuple(x) for x in r["taken"]}
        days = {d for d, _ in r["days"]}
        mine_days = {b[0] for b in r["mine"]}
        self.log("預定送出順序（同時送出各天；同一天依序）：")
        for day in build_plan(o["wishes"]):
            dd = day["date"]
            warn = [] if dd in days else ["⚠ 不是系統的排班日"]
            if dd in mine_days:
                warn.append("⚠ 這天你已有預約，系統會拒絕")
            self.log(f"  {dd} {' '.join(warn)}")
            for c in day["cands"]:
                bad = []
                if c["location"] not in r["locations"]:
                    bad.append("院區名稱不符")
                if c["shift"] not in r["shifts"]:
                    bad.append("班別名稱不符")
                state = "已被約" if (dd, c["location"], c["shift"], c["clinic"]) in taken else "目前空"
                self.log(f"    {c['location']} {c['shift']} {c['clinic']}：{state} {' '.join(bad)}")
                self.on_status(c["idx"], f"測試OK（{state}）" if not bad else "✗ " + " ".join(bad))

    # ---------------------------------------------------------------- main
    def run(self):
        from playwright.sync_api import sync_playwright
        o = self.o
        plan = build_plan(o["wishes"])
        with sync_playwright() as p:
            browser = core.launch_browser(p, o.get("headless", False), o.get("chrome_path"))
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
                self.check_source(frame)
                if o.get("dry_run"):
                    self.dry_run(frame)
                    self._show(frame, min(w["date"] for w in o["wishes"]).isoformat() if o["wishes"] else None)
                    raise core.Stopped()
                if open_ts:
                    # 不提早：準時在標準時間送出
                    core.wait_until(open_ts, off, self.stop, self.log, "距離開放 ")
                now = dt.datetime.fromtimestamp(time.time() + off).strftime("%H:%M:%S.%f")[:-3]
                self.log(f"[標準時間 {now}] 同時送出 {len(plan)} 天：" + "、".join(x["date"][5:] for x in plan))
                t0 = time.time()
                todo, mine, rounds = plan, [], o.get("max_rounds", 3)
                for rnd in range(1, rounds + 1):
                    res = frame.evaluate(ORCH_JS, {"plan": todo, "uid": o["user_id"], "pw": o["password"],
                                                   "retryMs": 300,
                                                   "deadline": int((time.time() + o.get("retry_sec", 90)) * 1000)})
                    ok = sum(r == "ok" for r in res)
                    self.log(f"第 {rnd} 輪：{ok}/{len(todo)} 天回報成功（累計 {time.time() - t0:.1f} 秒）")
                    if "auth" in res:
                        self.log("帳號或密碼錯誤，不再重送")
                        break
                    # 送出後一定向系統查證：以系統紀錄為準
                    self.log("向系統查證預約紀錄…")
                    mine = frame.evaluate(MY_BOOKINGS_JS, o["user_id"])
                    if not isinstance(mine, list):
                        self.log(f"  查證失敗（{mine}），改以伺服器回報為準")
                        todo = [d for d, r in zip(todo, res) if r not in ("ok", "daydone")]
                    else:
                        have = {b[0] for b in mine}
                        for d in plan:
                            if d["date"] in have:
                                b = next(x for x in mine if x[0] == d["date"])
                                for c in d["cands"]:
                                    if (c["location"], c["shift"], c["clinic"]) == (b[1], b[2], b[3]):
                                        self.result[c["idx"]] = f"✓ 已確認：{b[3]}"
                                        self.on_status(c["idx"], f"✓ 系統已確認：{b[1]} {b[2]} {b[3]}")
                        todo = [d for d in plan if d["date"] not in have]
                    if not todo:
                        self.log("✓ 全部日期都已在系統中確認")
                        break
                    if rnd < rounds:
                        self.log(f"⚠ 系統中仍沒有：{'、'.join(d['date'][5:] for d in todo)} → 立即重送（第 {rnd + 1} 輪）")
                    else:
                        self.log(f"✗ 重送 {rounds} 輪後仍沒有：{'、'.join(d['date'][5:] for d in todo)}")
                for i, w in enumerate(o["wishes"]):
                    if i not in self.result:
                        self.on_status(i, "（同一天已有其他候選成功或未輪到）")
                if isinstance(mine, list):
                    for b in sorted(mine):
                        self.log(f"  系統紀錄：{b[0]} {b[1]} {b[2]} {b[3]}")
                    if not mine:
                        self.log("  系統中沒有你的預約")
                self._show(frame, plan[-1]["date"] if plan else None)
                cap.capture("完成")
            except core.Stopped:
                self.log("測試完成（沒有送出任何預約）" if o.get("dry_run") else "已停止")
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

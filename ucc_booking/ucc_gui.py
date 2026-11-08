#!/usr/bin/env python3
"""
UCC 預班自動登記 — 圖形介面

1. 選月份 → 點選要登記的日期
2. 勾選偏好：院區、診1/診2/備援、白班/夜班（由上到下＝優先順序）
3. 輸入 ID / 密碼、開放時間
4. 按「校時」對齊國家標準時間 → 按「開始排程」
   程式會提前開好網頁並預填，時間一到（標準時間）立刻按登入，然後依序選日期、選班。
"""
import calendar
import datetime as dt
import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import ucc_core as core

HERE = Path(__file__).resolve().parent
SAVE = HERE / "gui_settings.json"     # 不儲存密碼
WEEK = "一二三四五六日"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UCC 預班自動登記")
        self.geometry("980x720")
        self.minsize(900, 640)
        self.q = queue.Queue()
        self.offset = 0.0
        self.offset_src = "本機時間（尚未校時）"
        self.worker = None
        self.stop = threading.Event()

        nxt = (dt.date.today().replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        self.year = tk.IntVar(value=nxt.year)
        self.month = tk.IntVar(value=nxt.month)
        self.day_vars = {}                    # date -> BooleanVar
        self.selected = set()

        self.site_vars = {s: tk.BooleanVar(value=True) for s in core.SITES}
        self.clinic_vars = {c: tk.BooleanVar(value=(c != "備援")) for c in core.CLINICS}
        self.shift_vars = {s: tk.BooleanVar(value=True) for s in core.SHIFTS}
        self.uid = tk.StringVar()
        self.pw = tk.StringVar()
        self.url = tk.StringVar(value=core.DEFAULT_URL)
        prev = nxt - dt.timedelta(days=1)
        self.open_time = tk.StringVar(value=f"{prev.year}-{prev.month:02d}-14 09:00:00")
        self.preload = tk.IntVar(value=20)
        self.advance = tk.IntVar(value=0)
        self.retries = tk.IntVar(value=30)
        self.max_per_date = tk.IntVar(value=1)
        self.submit_mode = tk.StringVar(value="none")

        self._load()
        self._build()
        self._draw_calendar()
        self.after(100, self._tick)
        self.after(300, lambda: threading.Thread(target=self._sync, daemon=True).start())

    # ------------------------------------------------------------ UI ----
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", **pad)
        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True, **pad)

        # --- 月曆 ---
        cal = ttk.LabelFrame(left, text="① 選擇日期")
        cal.pack(fill="x", **pad)
        top = ttk.Frame(cal)
        top.pack(fill="x")
        ttk.Button(top, text="◀", width=3, command=lambda: self._shift_month(-1)).pack(side="left")
        self.month_lbl = ttk.Label(top, font=("", 12, "bold"), anchor="center")
        self.month_lbl.pack(side="left", expand=True, fill="x")
        ttk.Button(top, text="▶", width=3, command=lambda: self._shift_month(1)).pack(side="left")
        self.grid_frame = ttk.Frame(cal)
        self.grid_frame.pack(**pad)
        q = ttk.Frame(cal)
        q.pack(fill="x")
        for txt, fn in [("全選", lambda d: True), ("週末", lambda d: d.weekday() >= 5),
                        ("平日", lambda d: d.weekday() < 5), ("清除", lambda d: False)]:
            ttk.Button(q, text=txt, width=6, command=lambda fn=fn: self._quick(fn)).pack(side="left", padx=2)

        # --- 偏好 ---
        pref = ttk.LabelFrame(left, text="② 喜歡的班（選完日期後在右邊點的班）")
        pref.pack(fill="x", **pad)
        for title, vars_ in [("院區", self.site_vars), ("診別", self.clinic_vars), ("班別", self.shift_vars)]:
            row = ttk.Frame(pref)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=title, width=5).pack(side="left")
            for k, v in vars_.items():
                ttk.Checkbutton(row, text=k, variable=v).pack(side="left", padx=3)
        row = ttk.Frame(pref)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="每天最多選").pack(side="left")
        ttk.Spinbox(row, from_=0, to=20, width=4, textvariable=self.max_per_date).pack(side="left")
        ttk.Label(row, text="班（0=不限；依 院區→診別→班別 順序優先）").pack(side="left")

        # --- 帳號 ---
        acc = ttk.LabelFrame(left, text="③ 帳號")
        acc.pack(fill="x", **pad)
        ttk.Label(acc, text="ID").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(acc, textvariable=self.uid, width=24).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(acc, text="密碼").grid(row=1, column=0, sticky="e", **pad)
        ttk.Entry(acc, textvariable=self.pw, show="●", width=24).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(acc, text="（密碼不會儲存）", foreground="gray").grid(row=1, column=2, sticky="w")

        # --- 右側：時間 ---
        tm = ttk.LabelFrame(right, text="④ 開放時間（國家標準時間）")
        tm.pack(fill="x", **pad)
        self.clock = ttk.Label(tm, font=("Consolas", 22, "bold"))
        self.clock.grid(row=0, column=0, columnspan=4, sticky="w", **pad)
        self.src_lbl = ttk.Label(tm, foreground="gray")
        self.src_lbl.grid(row=1, column=0, columnspan=3, sticky="w", padx=6)
        ttk.Button(tm, text="重新校時", command=lambda: threading.Thread(target=self._sync, daemon=True).start()
                   ).grid(row=1, column=3, sticky="e", padx=6)
        ttk.Label(tm, text="開放時間").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(tm, textvariable=self.open_time, width=22).grid(row=2, column=1, sticky="w", **pad)
        self.countdown = ttk.Label(tm, foreground="#c0392b", font=("", 11, "bold"))
        self.countdown.grid(row=2, column=2, columnspan=2, sticky="w")

        adv = ttk.LabelFrame(right, text="進階")
        adv.pack(fill="x", **pad)
        ttk.Label(adv, text="提前幾秒開網頁預填").grid(row=0, column=0, sticky="e", **pad)
        ttk.Spinbox(adv, from_=5, to=300, width=6, textvariable=self.preload).grid(row=0, column=1, sticky="w")
        ttk.Label(adv, text="提早按登入(毫秒，可負)").grid(row=0, column=2, sticky="e", **pad)
        ttk.Spinbox(adv, from_=-2000, to=2000, increment=50, width=7, textvariable=self.advance).grid(row=0, column=3, sticky="w")
        ttk.Label(adv, text="未開放時重試次數").grid(row=1, column=0, sticky="e", **pad)
        ttk.Spinbox(adv, from_=1, to=200, width=6, textvariable=self.retries).grid(row=1, column=1, sticky="w")
        ttk.Label(adv, text="送出方式").grid(row=1, column=2, sticky="e", **pad)
        ttk.Combobox(adv, textvariable=self.submit_mode, width=26, state="readonly",
                     values=["none", "each", "end"]).grid(row=1, column=3, sticky="w")
        ttk.Label(adv, text="none=不送出(自己確認)  each=每天選完就送出  end=全部選完才送出",
                  foreground="gray").grid(row=2, column=0, columnspan=4, sticky="w", padx=6)
        ttk.Label(adv, text="網址").grid(row=3, column=0, sticky="e", **pad)
        ttk.Entry(adv, textvariable=self.url).grid(row=3, column=1, columnspan=3, sticky="we", **pad)
        adv.columnconfigure(3, weight=1)

        btns = ttk.Frame(right)
        btns.pack(fill="x", **pad)
        self.btn_sched = ttk.Button(btns, text="⏰ 開始排程（時間到自動登入）", command=lambda: self._start(True))
        self.btn_sched.pack(side="left", padx=3)
        self.btn_now = ttk.Button(btns, text="▶ 立即執行", command=lambda: self._start(False))
        self.btn_now.pack(side="left", padx=3)
        self.btn_stop = ttk.Button(btns, text="■ 停止", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left", padx=3)

        logf = ttk.LabelFrame(right, text="執行紀錄")
        logf.pack(fill="both", expand=True, **pad)
        self.log_txt = tk.Text(logf, height=12, wrap="word", font=("Consolas", 10))
        sb = ttk.Scrollbar(logf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _draw_calendar(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        y, m = self.year.get(), self.month.get()
        self.month_lbl.config(text=f"{y} 年（民國 {y-1911}）{m} 月")
        for i, w in enumerate(WEEK):
            ttk.Label(self.grid_frame, text=w, width=4, anchor="center",
                      foreground="#c0392b" if i >= 5 else "").grid(row=0, column=i)
        self.day_vars = {}
        for r, week in enumerate(calendar.Calendar(firstweekday=0).monthdatescalendar(y, m), start=1):
            for c, d in enumerate(week):
                if d.month != m:
                    continue
                v = tk.BooleanVar(value=d in self.selected)
                self.day_vars[d] = v
                tk.Checkbutton(self.grid_frame, text=str(d.day), variable=v, indicatoron=False, width=3,
                               selectcolor="#7fd3a0", fg="#c0392b" if c >= 5 else "black",
                               command=lambda d=d, v=v: self._toggle(d, v)).grid(row=r, column=c, padx=1, pady=1)

    def _toggle(self, d, v):
        (self.selected.add if v.get() else self.selected.discard)(d)

    def _quick(self, fn):
        for d, v in self.day_vars.items():
            v.set(fn(d))
            self._toggle(d, v)

    def _shift_month(self, k):
        y, m = self.year.get(), self.month.get() + k
        y, m = (y - 1, 12) if m < 1 else (y + 1, 1) if m > 12 else (y, m)
        self.year.set(y)
        self.month.set(m)
        self._draw_calendar()

    # -------------------------------------------------------- clock ----
    def _sync(self):
        self.offset, self.offset_src = core.sync_time(self.log)

    def _tick(self):
        now = dt.datetime.fromtimestamp(time.time() + self.offset)
        self.clock.config(text=now.strftime("%Y-%m-%d %H:%M:%S.") + f"{now.microsecond//100000}")
        self.src_lbl.config(text="來源：" + self.offset_src)
        try:
            t = dt.datetime.strptime(self.open_time.get().strip(), "%Y-%m-%d %H:%M:%S")
            left = (t - now).total_seconds()
            if left > 0:
                h, r = divmod(int(left), 3600)
                self.countdown.config(text=f"倒數 {h:02d}:{r//60:02d}:{r%60:02d}")
            else:
                self.countdown.config(text="已過開放時間")
        except ValueError:
            self.countdown.config(text="時間格式：2026-12-14 09:00:00")
        while not self.q.empty():
            self.log_txt.insert("end", self.q.get() + "\n")
            self.log_txt.see("end")
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self._set_running(False)
        self.after(100, self._tick)

    def log(self, msg):
        t = dt.datetime.fromtimestamp(time.time() + self.offset).strftime("%H:%M:%S.%f")[:-3]
        self.q.put(f"[{t}] {msg}")

    # -------------------------------------------------------- run -----
    def _start(self, scheduled):
        dates = sorted(self.selected)
        if not dates:
            return messagebox.showwarning("提醒", "請先在月曆選擇日期")
        if not self.uid.get().strip() or not self.pw.get():
            return messagebox.showwarning("提醒", "請輸入 ID 與密碼")
        pick = lambda vs: [k for k, v in vs.items() if v.get()]
        sites, clinics, shifts = pick(self.site_vars), pick(self.clinic_vars), pick(self.shift_vars)
        if not (sites and clinics and shifts):
            return messagebox.showwarning("提醒", "院區、診別、班別都至少要勾一個")
        open_ts = None
        if scheduled:
            try:
                open_ts = dt.datetime.strptime(self.open_time.get().strip(), "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                return messagebox.showwarning("提醒", "開放時間格式應為 2026-12-14 09:00:00")
            if open_ts < time.time() + self.offset:
                if not messagebox.askyesno("提醒", "開放時間已過，要立即執行嗎？"):
                    return
                open_ts = None
        self._save()
        opts = {
            "url": self.url.get().strip(), "user_id": self.uid.get().strip(), "password": self.pw.get(),
            "dates": dates, "sites": sites, "clinics": clinics, "shifts": shifts,
            "open_ts": open_ts, "offset": self.offset,
            "preload_sec": self.preload.get(), "advance_ms": self.advance.get(),
            "retries": self.retries.get(), "max_per_date": self.max_per_date.get(),
            "submit_mode": self.submit_mode.get(), "headless": False,
        }
        self.log(f"日期 {', '.join(f'{d.month}/{d.day}' for d in dates)}；院區 {sites}；診別 {clinics}；班別 {shifts}")
        self.log("排程開始，請勿關閉本視窗與電腦（並關閉睡眠）" if open_ts else "立即執行")
        self.stop = threading.Event()
        self.worker = threading.Thread(target=core.Booker(opts, self.log, self.stop).run, daemon=True)
        self.worker.start()
        self._set_running(True)

    def _stop(self):
        self.stop.set()
        self.log("停止中…")

    def _set_running(self, on):
        self.btn_sched.config(state="disabled" if on else "normal")
        self.btn_now.config(state="disabled" if on else "normal")
        self.btn_stop.config(state="normal" if on else "disabled")

    # ------------------------------------------------------ settings ----
    def _save(self):
        data = {"uid": self.uid.get(), "url": self.url.get(), "open_time": self.open_time.get(),
                "dates": [d.isoformat() for d in sorted(self.selected)],
                "sites": {k: v.get() for k, v in self.site_vars.items()},
                "clinics": {k: v.get() for k, v in self.clinic_vars.items()},
                "shifts": {k: v.get() for k, v in self.shift_vars.items()},
                "preload": self.preload.get(), "advance": self.advance.get(), "retries": self.retries.get(),
                "max_per_date": self.max_per_date.get(), "submit_mode": self.submit_mode.get()}
        try:
            SAVE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load(self):
        try:
            d = json.loads(SAVE.read_text(encoding="utf-8"))
        except Exception:
            return
        self.uid.set(d.get("uid", ""))
        self.url.set(d.get("url") or core.DEFAULT_URL)
        self.open_time.set(d.get("open_time") or self.open_time.get())
        self.selected = {dt.date.fromisoformat(x) for x in d.get("dates", [])}
        if self.selected:
            first = min(self.selected)
            self.year.set(first.year)
            self.month.set(first.month)
        for key, vs in (("sites", self.site_vars), ("clinics", self.clinic_vars), ("shifts", self.shift_vars)):
            for k, v in d.get(key, {}).items():
                if k in vs:
                    vs[k].set(v)
        for k, var in (("preload", self.preload), ("advance", self.advance), ("retries", self.retries),
                       ("max_per_date", self.max_per_date), ("submit_mode", self.submit_mode)):
            if k in d:
                var.set(d[k])


if __name__ == "__main__":
    try:
        import playwright  # noqa: F401
    except ImportError:
        import sys
        sys.exit("請先執行：pip install playwright && python -m playwright install chromium")
    App().mainloop()

#!/usr/bin/env python3
"""
UCC 預班自動登記 — 圖形介面

1. 月曆點選日期（可多選）→ 勾院區、白/夜、診別順序 →「加入清單」。同一天可加很多筆。
2. 設定開放時間（上方時鐘是國家標準時間）
3. 按「開始排程」→ 程式詢問 ID / 密碼（只在這次有效，不存檔）
4. 時間一到立刻開始：日期由後往前，點日期 → 點班（診1 沒有就診2，再沒有才備援）→ 填帳密送出
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
SAVE = HERE / "gui_settings.json"     # 只存清單與時間設定，不存 ID / 密碼
WEEK = "一二三四五六日"
CLINIC_ORDERS = {
    "診1→診2→備援": ["診1", "診2", "備援"],
    "診1→診2": ["診1", "診2"],
    "診2→診1→備援": ["診2", "診1", "備援"],
    "只要診1": ["診1"],
    "只要診2": ["診2"],
    "只要備援": ["備援"],
}


class CredDialog(tk.Toplevel):
    """詢問 ID / 密碼，只回傳給呼叫者，不存任何地方。"""

    def __init__(self, master):
        super().__init__(master)
        self.title("輸入帳號密碼（本次有效，不會記憶）")
        self.resizable(False, False)
        self.result = None
        self.uid, self.pw = tk.StringVar(), tk.StringVar()
        f = ttk.Frame(self, padding=14)
        f.pack()
        ttk.Label(f, text="ID").grid(row=0, column=0, sticky="e", pady=4)
        e1 = ttk.Entry(f, textvariable=self.uid, width=24)
        e1.grid(row=0, column=1, pady=4)
        ttk.Label(f, text="密碼").grid(row=1, column=0, sticky="e", pady=4)
        e2 = ttk.Entry(f, textvariable=self.pw, show="●", width=24)
        e2.grid(row=1, column=1, pady=4)
        self.show = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="顯示密碼（檢查用）", variable=self.show,
                        command=lambda: e2.config(show="" if self.show.get() else "●")).grid(row=2, column=1, sticky="w")
        self.preview = ttk.Label(f, foreground="#2c7a4b", font=("Consolas", 11))
        self.preview.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.uid.trace_add("write", lambda *a: self._upd())
        self.pw.trace_add("write", lambda *a: self._upd())
        self.show.trace_add("write", lambda *a: self._upd())
        self._upd()
        b = ttk.Frame(f)
        b.grid(row=4, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(b, text="確定", command=self._ok).pack(side="left", padx=4)
        ttk.Button(b, text="取消", command=self.destroy).pack(side="left", padx=4)
        e1.focus_set()
        e1.bind("<Return>", lambda e: e2.focus_set())
        e2.bind("<Return>", lambda e: self._ok())
        self.transient(master)
        self.grab_set()
        self.wait_window()

    def _upd(self):
        uid, pw = self.uid.get(), self.pw.get()
        shown = pw if self.show.get() else "●" * len(pw)
        warn = "  ⚠ 前後有空白" if uid != uid.strip() or pw != pw.strip() else ""
        self.preview.config(text=f"ID：[{uid.strip()}]\n密碼：[{shown}]（{len(pw)} 個字）{warn}")

    def _ok(self):
        if self.uid.get().strip() and self.pw.get():
            self.result = (self.uid.get().strip(), self.pw.get())
            self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UCC 預班自動登記")
        self.geometry("1120x760")
        self.minsize(1000, 680)
        self.q = queue.Queue()
        self.offset, self.offset_src = 0.0, "本機時間（尚未校時）"
        self.worker = None
        self.stop = threading.Event()
        self.row_items = []                   # 執行中：wish index -> tree item

        nxt = (dt.date.today().replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        prev = nxt - dt.timedelta(days=1)
        self.year, self.month = tk.IntVar(value=nxt.year), tk.IntVar(value=nxt.month)
        self.picked = set()                   # 月曆上暫選的日期
        self.wishes = []                      # [{date, site, shift, order}]
        self.site_vars = {s: tk.BooleanVar(value=(s == core.SITES[0])) for s in core.SITES}
        self.shift_vars = {s: tk.BooleanVar(value=(s == "白班")) for s in core.SHIFTS}
        self.order = tk.StringVar(value="診1→診2→備援")
        self.url = tk.StringVar(value=core.DEFAULT_URL)
        self.open_time = tk.StringVar(value=f"{prev.year}-{prev.month:02d}-14 09:00:00")
        self.preload = tk.IntVar(value=20)
        self.advance = tk.IntVar(value=0)
        self.retry_sec = tk.IntVar(value=90)
        self.capture = tk.BooleanVar(value=True)

        self._load()
        self._build()
        self._draw_calendar()
        self._refresh_list()
        self.after(100, self._tick)
        self.after(300, lambda: threading.Thread(target=self._sync, daemon=True).start())

    # ================================================================ UI ====
    def _build(self):
        P = {"padx": 6, "pady": 4}
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", **P)
        mid = ttk.Frame(self)
        mid.pack(side="left", fill="both", expand=True, **P)

        # ① 月曆
        cal = ttk.LabelFrame(left, text="① 點選日期（可多選）")
        cal.pack(fill="x", **P)
        top = ttk.Frame(cal)
        top.pack(fill="x")
        ttk.Button(top, text="◀", width=3, command=lambda: self._shift_month(-1)).pack(side="left")
        self.month_lbl = ttk.Label(top, font=("", 12, "bold"), anchor="center")
        self.month_lbl.pack(side="left", expand=True, fill="x")
        ttk.Button(top, text="▶", width=3, command=lambda: self._shift_month(1)).pack(side="left")
        self.grid_frame = ttk.Frame(cal)
        self.grid_frame.pack(**P)
        qk = ttk.Frame(cal)
        qk.pack(fill="x")
        for txt, fn in [("全選", lambda d: True), ("週末", lambda d: d.weekday() >= 5),
                        ("平日", lambda d: d.weekday() < 5), ("清除", lambda d: False)]:
            ttk.Button(qk, text=txt, width=6, command=lambda fn=fn: self._quick(fn)).pack(side="left", padx=2)

        # ② 班
        pref = ttk.LabelFrame(left, text="② 選班")
        pref.pack(fill="x", **P)
        for title, vs in (("院區", self.site_vars), ("班別", self.shift_vars)):
            r = ttk.Frame(pref)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=title, width=5).pack(side="left")
            for k, v in vs.items():
                ttk.Checkbutton(r, text=k, variable=v).pack(side="left", padx=3)
        r = ttk.Frame(pref)
        r.pack(fill="x", pady=2)
        ttk.Label(r, text="診別", width=5).pack(side="left")
        ttk.Combobox(r, textvariable=self.order, values=list(CLINIC_ORDERS), state="readonly", width=16).pack(side="left")
        ttk.Label(pref, text="診別順序：前一個額滿/被搶就立刻選下一個", foreground="gray").pack(anchor="w", padx=4)
        ttk.Button(pref, text="➕ 加入清單（日期 × 院區 × 班別）", command=self._add).pack(fill="x", padx=4, pady=6)

        # ④ 時間
        tm = ttk.LabelFrame(left, text="④ 開放時間（國家標準時間）")
        tm.pack(fill="x", **P)
        self.clock = ttk.Label(tm, font=("Consolas", 18, "bold"))
        self.clock.pack(anchor="w", padx=6)
        self.src_lbl = ttk.Label(tm, foreground="gray", wraplength=330)
        self.src_lbl.pack(anchor="w", padx=6)
        r = ttk.Frame(tm)
        r.pack(fill="x", **P)
        ttk.Entry(r, textvariable=self.open_time, width=20).pack(side="left")
        ttk.Button(r, text="重新校時", command=lambda: threading.Thread(target=self._sync, daemon=True).start()
                   ).pack(side="right")
        self.countdown = ttk.Label(tm, foreground="#c0392b", font=("", 11, "bold"))
        self.countdown.pack(anchor="w", padx=6, pady=(0, 4))

        # ③ 清單
        lf = ttk.LabelFrame(mid, text="③ 願望清單（執行時日期由後往前；同一天依清單順序）")
        lf.pack(fill="both", expand=True, **P)
        cols = ("date", "wd", "site", "shift", "order", "status")
        self.tree = ttk.Treeview(lf, columns=cols, show="headings", height=12, selectmode="extended")
        for c, t, w in zip(cols, ("日期", "星期", "院區", "班別", "診別順序", "結果"), (70, 45, 60, 60, 120, 220)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w" if c == "status" else "center", stretch=(c == "status"))
        sb = ttk.Scrollbar(lf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        bt = ttk.Frame(lf)
        bt.pack(side="right", fill="y", padx=4)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Delete>", lambda e: self._del())
        self.edit_btns = []
        for txt, fn in (("▲ 上移", lambda: self._move(-1)), ("▼ 下移", lambda: self._move(1)),
                        ("刪除", self._del), ("清空", self._clear)):
            b = ttk.Button(bt, text=txt, width=8, command=fn)
            b.pack(pady=2)
            self.edit_btns.append(b)
        self.count_lbl = ttk.Label(bt, foreground="gray")
        self.count_lbl.pack(pady=8)

        adv = ttk.LabelFrame(mid, text="進階")
        adv.pack(fill="x", **P)
        ttk.Label(adv, text="提前開網頁(秒)").grid(row=0, column=0, sticky="e", **P)
        ttk.Spinbox(adv, from_=5, to=300, width=5, textvariable=self.preload).grid(row=0, column=1, sticky="w")
        ttk.Label(adv, text="提早開始(毫秒)").grid(row=0, column=2, sticky="e", **P)
        ttk.Spinbox(adv, from_=-2000, to=2000, increment=50, width=6, textvariable=self.advance).grid(row=0, column=3, sticky="w")
        ttk.Label(adv, text="未開放持續重試(秒)").grid(row=0, column=4, sticky="e", **P)
        ttk.Spinbox(adv, from_=10, to=600, width=5, textvariable=self.retry_sec).grid(row=0, column=5, sticky="w")
        ttk.Checkbutton(adv, text="保存網頁原始碼與網路請求（captures/，密碼會遮蔽）", variable=self.capture
                        ).grid(row=1, column=0, columnspan=6, sticky="w", **P)
        ttk.Label(adv, text="網址").grid(row=2, column=0, sticky="e", **P)
        ttk.Entry(adv, textvariable=self.url).grid(row=2, column=1, columnspan=5, sticky="we", **P)
        adv.columnconfigure(5, weight=1)

        btns = ttk.Frame(mid)
        btns.pack(fill="x", **P)
        self.btn_sched = ttk.Button(btns, text="⏰ 開始排程（時間到立即開始）", command=lambda: self._start(True))
        self.btn_now = ttk.Button(btns, text="▶ 立即執行", command=lambda: self._start(False))
        self.btn_cap = ttk.Button(btns, text="🔍 手動操作並錄下原始碼", command=self._capture_only)
        self.btn_stop = ttk.Button(btns, text="■ 停止", command=self._stop, state="disabled")
        for b in (self.btn_sched, self.btn_now, self.btn_cap, self.btn_stop):
            b.pack(side="left", padx=3)

        logf = ttk.LabelFrame(mid, text="執行紀錄")
        logf.pack(fill="both", expand=True, **P)
        self.log_txt = tk.Text(logf, height=10, wrap="word", font=("Consolas", 10))
        sb2 = ttk.Scrollbar(logf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb2.set)
        self.log_txt.pack(side="left", fill="both", expand=True)
        sb2.pack(side="right", fill="y")

    # ----------------------------------------------------------- 月曆 ----
    def _draw_calendar(self):
        for w in self.grid_frame.winfo_children():
            w.destroy()
        y, m = self.year.get(), self.month.get()
        self.month_lbl.config(text=f"{y}（民國 {y-1911}）年 {m} 月")
        for i, w in enumerate(WEEK):
            ttk.Label(self.grid_frame, text=w, width=4, anchor="center",
                      foreground="#c0392b" if i >= 5 else "").grid(row=0, column=i)
        listed = {w["date"] for w in self.wishes}
        self.day_vars = {}
        for r, week in enumerate(calendar.Calendar().monthdatescalendar(y, m), start=1):
            for c, d in enumerate(week):
                if d.month != m:
                    continue
                v = tk.BooleanVar(value=d in self.picked)
                self.day_vars[d] = v
                tk.Checkbutton(self.grid_frame, text=str(d.day) + ("•" if d in listed else ""), variable=v,
                               indicatoron=False, width=3, selectcolor="#7fd3a0",
                               fg="#c0392b" if c >= 5 else "black",
                               command=lambda d=d, v=v: self._toggle(d, v)).grid(row=r, column=c, padx=1, pady=1)

    def _toggle(self, d, v):
        (self.picked.add if v.get() else self.picked.discard)(d)

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

    # ----------------------------------------------------------- 清單 ----
    def _add(self):
        sites = [k for k, v in self.site_vars.items() if v.get()]
        shifts = [k for k, v in self.shift_vars.items() if v.get()]
        if not self.picked:
            return messagebox.showwarning("提醒", "請先在月曆點選日期")
        if not sites or not shifts:
            return messagebox.showwarning("提醒", "院區、班別至少各勾一個")
        have = {(w["date"], w["site"], w["shift"]) for w in self.wishes}
        n = 0
        for d in sorted(self.picked):
            for s in sites:
                for h in shifts:
                    if (d, s, h) not in have:
                        self.wishes.append({"date": d, "site": s, "shift": h, "order": self.order.get()})
                        n += 1
        self.picked.clear()
        self._draw_calendar()
        self._refresh_list()
        self._save()
        self.log(f"加入 {n} 筆")

    def _refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        for i, w in enumerate(self.wishes):
            d = w["date"]
            self.tree.insert("", "end", iid=str(i), values=(f"{d.month}/{d.day}", WEEK[d.weekday()], w["site"],
                                                            w["shift"], w["order"], w.get("status", "")))
        self.count_lbl.config(text=f"共 {len(self.wishes)} 筆")

    def _sel(self):
        return sorted(int(i) for i in self.tree.selection())

    def _move(self, k):
        sel = self._sel()
        if not sel:
            return
        idx = sel if k < 0 else sel[::-1]
        for i in idx:
            j = i + k
            if 0 <= j < len(self.wishes) and str(j) not in self.tree.selection():
                self.wishes[i], self.wishes[j] = self.wishes[j], self.wishes[i]
        new = [i + k for i in sel if 0 <= i + k < len(self.wishes)]
        self._refresh_list()
        self.tree.selection_set([str(i) for i in new])
        self._save()

    def _del(self):
        sel = set(self._sel())
        self.wishes = [w for i, w in enumerate(self.wishes) if i not in sel]
        self._refresh_list()
        self._draw_calendar()
        self._save()

    def _clear(self):
        if self.wishes and messagebox.askyesno("清空", "確定清空整個清單？"):
            self.wishes = []
            self._refresh_list()
            self._draw_calendar()
            self._save()

    # ----------------------------------------------------------- 時間 ----
    def _sync(self):
        self.offset, self.offset_src = core.sync_time(self.log)

    def _tick(self):
        now = dt.datetime.fromtimestamp(time.time() + self.offset)
        self.clock.config(text=now.strftime("%Y-%m-%d %H:%M:%S.") + str(now.microsecond // 100000))
        self.src_lbl.config(text="來源：" + self.offset_src)
        try:
            t = dt.datetime.strptime(self.open_time.get().strip(), "%Y-%m-%d %H:%M:%S")
            left = (t - now).total_seconds()
            if left > 0:
                h, r = divmod(int(left), 3600)
                self.countdown.config(text=f"距離開放 {h:02d}:{r//60:02d}:{r%60:02d}")
            else:
                self.countdown.config(text="已過開放時間")
        except ValueError:
            self.countdown.config(text="格式：2026-12-14 09:00:00")
        while not self.q.empty():
            kind, *a = self.q.get()
            if kind == "log":
                self.log_txt.insert("end", a[0] + "\n")
                self.log_txt.see("end")
            elif kind == "status":
                i, text = a
                if 0 <= i < len(self.wishes):
                    self.wishes[i]["status"] = text
                    if self.tree.exists(str(i)):
                        self.tree.set(str(i), "status", text)
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self._running(False)
        self.after(100, self._tick)

    def log(self, msg):
        t = dt.datetime.fromtimestamp(time.time() + self.offset).strftime("%H:%M:%S.%f")[:-3]
        self.q.put(("log", f"[{t}] {msg}"))

    # ----------------------------------------------------------- 執行 ----
    def _start(self, scheduled):
        if not self.wishes:
            return messagebox.showwarning("提醒", "清單是空的，請先加入要登記的班")
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
        cred = CredDialog(self).result
        if not cred:
            return
        for w in self.wishes:
            w["status"] = "等待中"
        self._refresh_list()
        self._save()
        opts = {
            "url": self.url.get().strip(), "user_id": cred[0], "password": cred[1],
            "wishes": [{"date": w["date"], "site": w["site"], "shift": w["shift"],
                        "clinics": CLINIC_ORDERS[w["order"]]} for w in self.wishes],
            "open_ts": open_ts, "offset": self.offset, "preload_sec": self.preload.get(),
            "advance_ms": self.advance.get(), "retry_sec": self.retry_sec.get(),
            "capture": self.capture.get(), "headless": False,
        }
        self.log(f"使用 ID：{cred[0]}（密碼 {len(cred[1])} 個字，不顯示、不存檔）")
        del cred
        self.log(f"共 {len(self.wishes)} 筆；" + ("排程中，請勿關閉視窗、勿讓電腦睡眠" if open_ts else "立即執行"))
        self._launch(lambda b: b.run(), opts)

    def _capture_only(self):
        self.log("開啟網頁，請手動操作一輪（點日期、點班、輸入帳密）；結束按『停止』")
        self._launch(lambda b: b.run_capture_only(), {"url": self.url.get().strip(), "user_id": "", "password": ""})

    def _launch(self, fn, opts):
        self.stop = threading.Event()
        booker = core.Booker(opts, self.log, self.stop, on_status=lambda i, t: self.q.put(("status", i, t)))
        self.worker = threading.Thread(target=lambda: self._safe(fn, booker), daemon=True)
        self.worker.start()
        self._running(True)

    def _safe(self, fn, booker):
        try:
            fn(booker)
        except Exception as e:
            self.log(f"錯誤：{e}")

    def _stop(self):
        self.stop.set()
        self.log("停止中…")

    def _running(self, on):
        st = "disabled" if on else "normal"
        for b in [self.btn_sched, self.btn_now, self.btn_cap] + self.edit_btns:
            b.config(state=st)
        self.btn_stop.config(state="normal" if on else "disabled")

    # ----------------------------------------------------------- 設定 ----
    def _save(self):
        data = {"url": self.url.get(), "open_time": self.open_time.get(),
                "wishes": [{"date": w["date"].isoformat(), "site": w["site"], "shift": w["shift"],
                            "order": w["order"]} for w in self.wishes],
                "preload": self.preload.get(), "advance": self.advance.get(),
                "retry_sec": self.retry_sec.get(), "capture": self.capture.get()}
        try:
            SAVE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load(self):
        try:
            d = json.loads(SAVE.read_text(encoding="utf-8"))
        except Exception:
            return
        self.url.set(d.get("url") or core.DEFAULT_URL)
        self.open_time.set(d.get("open_time") or self.open_time.get())
        for w in d.get("wishes", []):
            try:
                self.wishes.append({"date": dt.date.fromisoformat(w["date"]), "site": w["site"],
                                    "shift": w["shift"], "order": w.get("order") if w.get("order") in CLINIC_ORDERS
                                    else "診1→診2→備援"})
            except Exception:
                pass
        if self.wishes:
            first = min(w["date"] for w in self.wishes)
            self.year.set(first.year)
            self.month.set(first.month)
        for k, var in (("preload", self.preload), ("advance", self.advance),
                       ("retry_sec", self.retry_sec), ("capture", self.capture)):
            if k in d:
                var.set(d[k])


if __name__ == "__main__":
    try:
        import playwright  # noqa: F401
    except ImportError:
        import sys
        sys.exit("請先執行：pip install playwright && python -m playwright install chromium")
    App().mainloop()

"""
UCC 預班自動登記 — 核心邏輯（GUI 與命令列共用）

- 標準時間校時：SNTP 向國家標準時間 (time.stdtime.gov.tw) 等伺服器取得時差
- 精準等待：先粗略 sleep，最後 50ms 忙等，誤差約數毫秒
- 提前載入網頁並預填 ID/PW，時間一到立即按登入；未開放則自動重新整理重試
- 依「日期 × 院區 × 診別 × 班別」勾選班表，可選擇自動送出
"""
import datetime as dt
import re
import socket
import statistics
import struct
import threading
import time
import email.utils
import urllib.request

DEFAULT_URL = ("https://script.google.com/macros/s/AKfycbwJqVMzejJMXYFv1o5b20WV2rUSAisgXbGTuvwrMKh"
               "AjYmf9OcaIXDhs3YINjXVQFgZ8g/exec")
SITES = ["林森", "昆明", "廣慈"]
CLINICS = {"診1": r"診\s*[1１一壹]|[1１一壹]\s*診|診間\s*[1１一]|第一診",
           "診2": r"診\s*[2２二貳]|[2２二貳]\s*診|診間\s*[2２二]|第二診",
           "備援": r"備援|支援|備班|候補"}
SHIFTS = {"白班": r"白|日班|早班|上午",
          "夜班": r"夜|晚班|晚上|下午"}
ANY_DATE = r"\d{1,4}\s*[-/.年]\s*\d{1,2}(?:\s*[-/.月]\s*\d{1,2})?|\d{1,2}\s*月\s*\d{1,2}"
LOGIN_WORDS = r"登入|登錄|登陸|進入|login|sign\s*in|確認"
SUBMIT_WORDS = r"送出|提交|確認送出|確定送出|預約|登記|儲存|存檔|submit|save"
NTP_SERVERS = ["time.stdtime.gov.tw", "tock.stdtime.gov.tw", "watch.stdtime.gov.tw",
               "clock.stdtime.gov.tw", "time.google.com", "pool.ntp.org"]


class Stopped(Exception):
    pass


# ============================================================ 標準時間 ====

def _sntp(server, timeout=2.0):
    """回傳 (offset秒, 往返延遲秒)。offset = 標準時間 - 本機時間"""
    pkt = b"\x1b" + 47 * b"\0"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        t0 = time.time()
        s.sendto(pkt, (server, 123))
        data, _ = s.recvfrom(512)
        t3 = time.time()
    if len(data) < 48:
        raise ValueError("bad NTP reply")
    def ts(off):
        sec, frac = struct.unpack("!II", data[off:off + 8])
        return sec - 2208988800 + frac / 2**32
    t1, t2 = ts(32), ts(40)
    return ((t1 - t0) + (t2 - t3)) / 2, (t3 - t0) - (t2 - t1)


def _http_offset(url="https://www.google.com"):
    """NTP 被擋時的備案：用 HTTP Date 標頭（精度約 ±0.5 秒）。"""
    t0 = time.time()
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=5) as r:
        date = r.headers["Date"]
    t1 = time.time()
    server = email.utils.parsedate_to_datetime(date).timestamp() + 0.5  # 秒數被截斷，取中間值
    return server - (t0 + t1) / 2, t1 - t0


def sync_time(log=print, samples=4):
    """回傳 (offset, 來源說明)。"""
    for srv in NTP_SERVERS:
        results = []
        for _ in range(samples):
            try:
                results.append(_sntp(srv))
            except Exception:
                pass
        if results:
            results.sort(key=lambda r: r[1])          # 取延遲最小的幾筆
            best = results[: max(1, len(results) // 2 + 1)]
            off = statistics.median(r[0] for r in best)
            delay = best[0][1]
            msg = f"{srv}（NTP），本機時差 {off*1000:+.0f} ms，網路延遲 {delay*1000:.0f} ms"
            log(f"校時成功：{msg}")
            return off, msg
    try:
        off, delay = _http_offset()
        msg = f"Google HTTP 時間（精度約 ±0.5 秒），本機時差 {off*1000:+.0f} ms"
        log(f"NTP 無法連線，改用 {msg}")
        return off, msg
    except Exception as e:
        log(f"校時失敗，改用本機時間：{e}")
        return 0.0, "本機時間（未校時）"


def wait_until(target_ts, offset, stop: threading.Event, log=print, label=""):
    """等到『標準時間』= target_ts（epoch 秒）。"""
    last = None
    while True:
        if stop.is_set():
            raise Stopped()
        now = time.time() + offset
        left = target_ts - now
        if left <= 0:
            return
        sec = int(left)
        if sec != last and (sec <= 10 or sec % 30 == 0):
            log(f"{label}倒數 {sec} 秒")
            last = sec
        if left > 0.2:
            time.sleep(min(left - 0.15, 0.1))
        # 最後 0.2 秒忙等以取得毫秒級精準度


# ========================================================== 日期比對 ====

def expand_dates(items):
    out = []
    for it in items:
        it = str(it).strip()
        if "~" in it:
            a, b = (dt.date.fromisoformat(x.strip()) for x in it.split("~"))
            while a <= b:
                out.append(a)
                a += dt.timedelta(days=1)
        elif it:
            out.append(dt.date.fromisoformat(it))
    return sorted(set(out))


def date_regex(d):
    m, dd, y, roc = d.month, d.day, d.year, d.year - 1911
    M, D = f"0?{m}", f"0?{dd}"
    return "|".join([
        rf"(?<!\d)(?:{y}|{roc})\s*[-/.年]\s*{M}\s*[-/.月]\s*{D}(?!\d)",
        rf"(?<![\d/])\s*{M}\s*/\s*{D}(?![\d/])",
        rf"(?<!\d){M}\s*月\s*{D}\s*日?(?!\d)",
        rf"(?<!\d){y}{m:02d}{dd:02d}(?!\d)",
    ])


# ======================================================== 頁面操作 JS ====

# groups: [{want: regex, any: regex}]
# 每一組條件先看「近處文字」(label/自身/欄標題)，近處沒提到這類資訊才看「整列文字」。
SCAN_JS = r"""
(groups) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const near = el => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t += l.innerText + ' '; }
    const lab = el.closest('label'); if (lab) t += lab.innerText + ' ';
    t += (el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.title || '') + ' ';
    for (const a of ['data-date','data-site','data-shift','data-clinic','data-day','data-value','value','name'])
      if (el.getAttribute(a) && el.getAttribute(a) !== 'on') t += el.getAttribute(a) + ' ';
    const p = el.parentElement; if (p && clean(p.innerText).length < 40) t += p.innerText + ' ';
    const td = el.closest('td,th');
    if (td) {
      t += td.innerText + ' ';
      const table = td.closest('table');
      const idx = Array.from(td.parentElement.children).indexOf(td);
      const heads = table ? table.querySelectorAll('thead tr') : [];
      const rows = heads.length ? heads : (table ? [table.querySelector('tr')] : []);
      rows.forEach(r => { if (r && r.children[idx]) t += ' ' + r.children[idx].innerText; });
    }
    return clean(t);
  };
  const far = el => {
    const row = el.closest('tr,li,.card,.row,.item,.day,.cell,fieldset,section');
    return clean(row ? row.innerText.slice(0, 400) : '');
  };
  const sel = 'input[type=checkbox],input[type=radio],button,[role=button],[role=checkbox],a[onclick],td[onclick],div[onclick],span[onclick],.slot,.shift';
  const out = [];
  Array.from(document.querySelectorAll(sel)).filter(vis).forEach((el, i) => {
    const n = near(el), f = far(el);
    if (/送出|提交|登出|取消|logout/i.test(n.slice(0, 10))) return;
    const ok = groups.every(g => {
      const any = new RegExp(g.any), want = new RegExp(g.want);
      return any.test(n) ? want.test(n) : want.test(f);
    });
    if (!ok) return;
    const id = 'ucc' + Date.now() + '_' + i;
    el.setAttribute('data-ucc-id', id);
    out.push({id, type: el.type || '', checked: !!el.checked,
              disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
              text: (n + ' | ' + f).slice(0, 120)});
  });
  return out;
}
"""

# 找月曆上的日期格：優先找有完整日期文字的，其次找「只顯示日數」的格子（排除上/下月灰色格）
DATE_CELL_JS = r"""
({full, day, month}) => {
  const rf = new RegExp(full);
  const rd = new RegExp('^0?' + day + '(?!\\d)');
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const bad = el => /other|prev|next|disabled|muted|outside|old|new|gray|grey/i.test(el.className + ' ' + (el.parentElement ? el.parentElement.className : ''))
                    || el.getAttribute('aria-disabled') === 'true' || el.disabled;
  const els = Array.from(document.querySelectorAll('td,th,button,a,div,span,li,label')).filter(vis);
  const own = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const attr = el => ['data-date','data-day','data-value','value','title','aria-label'].map(a => el.getAttribute(a) || '').join(' ');
  // 1) 完整日期（文字或屬性）— 取最內層的元素
  let c = els.filter(el => (rf.test(own(el)) && own(el).length < 40) || rf.test(attr(el)));
  c = c.filter(el => !c.some(o => o !== el && el.contains(o)));
  // 2) 只有日數
  if (!c.length) {
    c = els.filter(el => own(el).length <= 12 && rd.test(own(el)) && !bad(el));
    c = c.filter(el => !c.some(o => o !== el && el.contains(o)));
  }
  if (!c.length) return null;
  const el = c.find(e => getComputedStyle(e).cursor === 'pointer') || c[0];
  const id = 'd' + Date.now();
  el.setAttribute('data-ucc-date', id);
  return id;
}
"""

DUMP_JS = r"""
() => {
  const q = s => Array.from(document.querySelectorAll(s));
  const desc = el => ({tag: el.tagName.toLowerCase(), id: el.id, name: el.name, cls: el.className && String(el.className).slice(0,80),
                       type: el.type, text: (el.innerText || el.value || el.placeholder || '').replace(/\s+/g,' ').slice(0,120)});
  return {
    title: document.title,
    selects: q('select').map(s => ({...desc(s), options: Array.from(s.options).map(o => o.value + '=' + o.text)})),
    inputs: q('input,textarea').map(desc),
    buttons: q('button,[role=button],input[type=submit],input[type=button],a[onclick]').map(desc),
    text: (document.body ? document.body.innerText : '').slice(0, 3000),
  };
}
"""


# ============================================================ 主流程 ====

class Booker:
    """
    opts = {
      url, user_id, password, dates:[date], sites:[...], clinics:[診1,診2], shifts:[白班,夜班],
      open_ts: epoch 秒（標準時間）或 None 表示立即, offset: 校時差,
      preload_sec: 提前幾秒開網頁預填, advance_ms: 提早幾毫秒按登入(可負),
      max_per_date: 每天最多選幾班(0=不限), submit_mode: none|each|end,
      retries: int, headless: bool,
      selectors: {user_id, password, login_button, submit_button}
    }
    """

    def __init__(self, opts, log=print, stop=None):
        self.o = opts
        self.log = log
        self.stop = stop or threading.Event()
        self.sel = opts.get("selectors") or {}
        self.dialogs = []

    # ---------- helpers ----------
    def _check(self):
        if self.stop.is_set():
            raise Stopped()

    def frames(self):
        fr = list(self.page.frames)
        fr.sort(key=lambda f: ("userCodeAppPanel" in f.url or "googleusercontent" in f.url), reverse=True)
        return fr

    def find_first(self, css):
        for f in self.frames():
            try:
                loc = f.locator(css)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        return f, loc.nth(i)
            except Exception:
                pass
        return None, None

    def find_button(self, words, override=""):
        if override:
            return self.find_first(override)
        rx = re.compile(words, re.I)
        for f in self.frames():
            try:
                loc = f.locator("button, input[type=submit], input[type=button], [role=button], a.btn, .btn")
                for i in range(loc.count()):
                    el = loc.nth(i)
                    if el.is_visible() and rx.search((el.inner_text() or el.get_attribute("value") or "").strip()):
                        return f, el
            except Exception:
                pass
        return None, None

    def wait_ready(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            self._check()
            for f in self.frames():
                try:
                    if f.locator("input, select, button").count() > 0:
                        return
                except Exception:
                    pass
            time.sleep(0.2)
        raise RuntimeError("網頁載入逾時（若需 Google 帳號登入，請先在瀏覽器登入後再試）")

    def dump(self):
        import json
        out = []
        for i, f in enumerate(self.page.frames):
            try:
                out.append(f"===== frame {i}: {f.url[:150]}\n" +
                           json.dumps(f.evaluate(DUMP_JS), ensure_ascii=False, indent=2))
            except Exception as e:
                out.append(f"===== frame {i}: {e}")
        return "\n".join(out)

    # ---------- steps ----------
    def load_and_fill(self):
        self.page.goto(self.o["url"], wait_until="domcontentloaded", timeout=60000)
        self.wait_ready()
        uid = self.o["user_id"]
        f, el = self.find_first(self.sel.get("user_id") or
                                "input[type=text], input:not([type]), input[type=number], input[type=tel], input[type=email], select")
        if el is None:
            raise RuntimeError("找不到 ID 欄位（可在設定的 selectors.user_id 指定）")
        if el.evaluate("e => e.tagName") == "SELECT":
            opts = el.evaluate("e => Array.from(e.options).map(o => o.text)")
            hit = [o for o in opts if uid and uid in o]
            if not hit:
                raise RuntimeError(f"下拉清單中找不到「{uid}」")
            el.select_option(label=hit[0])
        else:
            el.fill(uid)
        f, pw = self.find_first(self.sel.get("password") or "input[type=password]")
        if pw is not None:
            pw.fill(self.o["password"])
        self._pw = pw

    def click_login(self):
        f, btn = self.find_button(LOGIN_WORDS, self.sel.get("login_button", ""))
        if btn is not None:
            btn.click(timeout=3000)
        elif self._pw is not None:
            self._pw.press("Enter")
        else:
            raise RuntimeError("找不到登入按鈕（可在 selectors.login_button 指定）")

    def logged_in(self, timeout=8):
        """登入成功判斷：密碼欄消失，或出現日期格子。"""
        end = time.time() + timeout
        while time.time() < end:
            self._check()
            if self.dialogs:
                return False
            _, pw = self.find_first(self.sel.get("password") or "input[type=password]")
            if pw is None:
                return True
            time.sleep(0.15)
        return False

    # ---------- 步驟 1：點選左邊的日期 ----------
    def click_date(self, d):
        """支援：<input type=date>、日期下拉、或月曆格子（顯示完整日期或只顯示日數）。"""
        iso = d.isoformat()
        for f in self.frames():
            try:
                di = f.locator("input[type=date]")
                if di.count() and di.first.is_visible():
                    di.first.fill(iso)
                    di.first.dispatch_event("change")
                    return True
                sels = f.locator("select")
                rx = re.compile(date_regex(d))
                for i in range(sels.count()):
                    s = sels.nth(i)
                    hit = [t for t in s.evaluate("e => Array.from(e.options).map(o => o.text + ' ' + o.value)") if rx.search(t)]
                    if hit and s.is_visible():
                        opts = s.evaluate("e => Array.from(e.options).map(o => o.text)")
                        vals = s.evaluate("e => Array.from(e.options).map(o => o.text + ' ' + o.value)")
                        s.select_option(label=opts[vals.index(hit[0])])
                        return True
            except Exception:
                pass
        for _ in range(4):                      # 必要時切換月份
            for f in self.frames():
                try:
                    r = f.evaluate(DATE_CELL_JS, {"full": date_regex(d), "day": d.day, "month": d.month})
                except Exception:
                    continue
                if r:
                    f.locator(f"[data-ucc-date='{r}']").first.click(timeout=3000)
                    return True
            if not self._next_month(d):
                break
        return False

    def _next_month(self, d):
        for f in self.frames():
            try:
                txt = f.evaluate("() => document.body ? document.body.innerText : ''")
                if re.search(rf"(?<!\d){d.month}\s*月|(?<!\d)0?{d.month}\s*/\s*\d{{4}}|\d{{3,4}}\s*[/-]\s*0?{d.month}(?!\d)", txt):
                    return False                # 已經在正確月份
            except Exception:
                continue
        _, btn = self.find_button(r"^\s*(>|›|»|→|下\s*一?\s*個?\s*月|next)\s*$")
        if btn is None:
            return False
        btn.click()
        time.sleep(0.8)
        return True

    # ---------- 步驟 2：在右邊勾選喜歡的班 ----------
    def click_option(self, site, clinic, shift):
        groups = []
        if site:
            groups.append({"want": re.escape(site), "any": "|".join(SITES)})
        if clinic:
            groups.append({"want": CLINICS[clinic], "any": "|".join(CLINICS.values())})
        if shift:
            groups.append({"want": SHIFTS[shift], "any": "|".join(SHIFTS.values())})
        hits = []
        for f in self.frames():
            try:
                hits += [(f, r) for r in f.evaluate(SCAN_JS, groups)]
            except Exception:
                pass
        res = []
        for f, r in hits:
            if r["disabled"]:
                res.append(("full", r["text"]))
                continue
            if r["checked"]:
                res.append(("ok", r["text"]))
                continue
            el = f.locator(f"[data-ucc-id='{r['id']}']")
            try:
                el.check(timeout=2000) if r["type"] in ("checkbox", "radio") else el.click(timeout=2000)
                res.append(("ok", r["text"]))
                break                           # 同一組合只點一個
            except Exception as e:
                res.append(("err", str(e)))
        return res

    def pick_slots(self):
        """依序：選日期 → 依偏好順序（院區 > 診別 > 班別）點右邊的班。"""
        o = self.o
        combos = [(s, c, h) for s in (o["sites"] or [None])
                  for c in (o["clinics"] or [None]) for h in (o["shifts"] or [None])]
        limit = o.get("max_per_date", 0) or len(combos)
        total = 0
        for d in o["dates"]:
            self._check()
            label = f"{d.month}/{d.day}"
            if not self.click_date(d):
                self.log(f"✗ {label}：找不到這一天（可能不開放）")
                continue
            time.sleep(o.get("date_delay", 0.8))
            got = 0
            for s, c, h in combos:
                if got >= limit:
                    break
                name = " ".join(x for x in (s, c, h) if x)
                res = self.click_option(s, c, h)
                if not res:
                    continue
                st, txt = res[-1]
                if st == "ok":
                    got += 1
                    self.log(f"  ✓ {label} {name}  [{txt[:50]}]")
                elif st == "full":
                    self.log(f"  - {label} {name}：額滿")
                else:
                    self.log(f"  ! {label} {name}：點選失敗 {txt[:60]}")
            if not got:
                self.log(f"  ✗ {label}：沒有符合偏好的空班")
            total += got
            if got and o.get("submit_mode") == "each":
                self.press_submit()
        return total

    def press_submit(self):
        _, btn = self.find_button(SUBMIT_WORDS, self.sel.get("submit_button", ""))
        if btn is None:
            self.log("找不到送出按鈕，請在瀏覽器手動送出")
            return False
        btn.click()
        self.log("已按下送出")
        time.sleep(1.5)
        return True

    # ---------- main ----------
    def run(self):
        from playwright.sync_api import sync_playwright
        o = self.o
        with sync_playwright() as p:
            kw = {"headless": o.get("headless", False)}
            if o.get("chrome_path"):
                kw["executable_path"] = o["chrome_path"]
            try:
                browser = p.chromium.launch(**kw)
            except Exception:
                browser = p.chromium.launch(channel="chrome", **kw)   # 用電腦已安裝的 Chrome
            self.page = browser.new_page()
            self.page.on("dialog", self._on_dialog)
            try:
                open_ts = o.get("open_ts")
                off = o.get("offset", 0.0)
                if open_ts:
                    wait_until(open_ts - o.get("preload_sec", 20), off, self.stop, self.log, "距離預先載入 ")
                self.log("載入網頁並預填 ID / 密碼…")
                self.load_and_fill()
                if open_ts:
                    self.log("已預填完成，等待開放時間…")
                    wait_until(open_ts - o.get("advance_ms", 0) / 1000, off, self.stop, self.log, "距離開放 ")
                for attempt in range(1, o.get("retries", 30) + 1):
                    self._check()
                    self.dialogs.clear()
                    t = dt.datetime.fromtimestamp(time.time() + off).strftime("%H:%M:%S.%f")[:-3]
                    self.log(f"[標準時間 {t}] 按下登入（第 {attempt} 次）")
                    self.click_login()
                    if self.logged_in():
                        self.log("登入成功！")
                        break
                    self.log("尚未登入成功（可能未開放或密碼錯誤），重新整理重試…")
                    if any("密碼" in m and ("錯" in m or "不正確" in m) for m in self.dialogs):
                        raise RuntimeError("密碼錯誤，停止重試")
                    time.sleep(0.5)
                    self.load_and_fill()
                else:
                    raise RuntimeError("重試次數用完仍無法登入")

                time.sleep(1.0)
                self.log("開始勾選班表…")
                n = self.pick_slots()
                self.log(f"共勾選 {n} 格")
                if o.get("submit_mode") == "end" and n:
                    self.press_submit()
                    time.sleep(2)
                elif o.get("submit_mode") != "each":
                    self.log("未自動送出，請在瀏覽器確認後手動送出")
            except Stopped:
                self.log("已停止")
            except Exception as e:
                self.log(f"錯誤：{e}")
                try:
                    from pathlib import Path
                    p_ = Path(__file__).with_name("inspect_report_error.txt")
                    p_.write_text(self.dump(), encoding="utf-8")
                    self.log(f"已輸出網頁結構到 {p_.name}，可傳給開發者調整")
                except Exception:
                    pass
            # 保持瀏覽器開啟，直到使用者按停止或關閉視窗
            self.log("瀏覽器會保持開啟；按『停止』關閉。")
            while not self.stop.is_set():
                try:
                    self.page.wait_for_timeout(500)
                except Exception:
                    break
            browser.close()

    def _on_dialog(self, d):
        self.dialogs.append(d.message)
        self.log(f"[網頁訊息] {d.message}")
        try:
            d.accept()
        except Exception:
            pass

"""
UCC 預班自動登記 — 核心邏輯（GUI 與命令列共用）

- 標準時間校時：SNTP 向國家標準時間 (time.stdtime.gov.tw) 等伺服器取得時差
- 精準等待：先粗略 sleep，最後 50ms 忙等，誤差約數毫秒
- 提前載入網頁，時間一到立即開始；未開放則自動重新整理重試
- 依願望清單：點日期 → 點班（診1 沒有就診2，再沒有才備援）→ 系統要求時填 ID/密碼 → 送出
- 日期由後往前選，避免和其他人同時搶前面的格子
- 自動保存網頁原始碼與網路請求（captures/），方便之後改寫更快的版本
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
           "備援": r"備援|備班|候補|備"}
SHIFTS = {"白班": r"白|日班|早班|上午",
          "夜班": r"夜|晚班|晚上|下午"}
ANY_DATE = r"\d{1,4}\s*[-/.年]\s*\d{1,2}(?:\s*[-/.月]\s*\d{1,2})?|\d{1,2}\s*月\s*\d{1,2}"
SUBMIT_WORDS = r"確認|確定|送出|提交|預約|登記|預班|登入|儲存|OK|submit|save"
OK_WORDS = r"成功|完成|已登記|已預約|已送出|已報名|感謝"
FAIL_WORDS = r"額滿|已滿|滿額|失敗|錯誤|不正確|重複|無法|不可|已被|沒有名額|尚未開放|未開放"
FULL_WORDS = r"額滿|已滿|滿額|剩餘?\s*[:：]?\s*0(?!\d)|名額\s*[:：]?\s*0(?!\d)|0\s*/\s*\d+\s*$"
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

# 找右邊的班別選項。groups: [{want, any}] 每組先看「近處文字」，近處沒提到這類資訊才看「整列文字」。
# 回傳最內層的符合元素（避免點到外框容器）。
SCAN_JS = r"""
(groups) => {
  const vis = el => { const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false; const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const near = el => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t += l.innerText + ' '; }
    const lab = el.closest('label'); if (lab) t += lab.innerText + ' ';
    t += (el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.title || '') + ' ';
    for (const a of ['data-site','data-shift','data-clinic','data-value','data-name','value'])
      if (el.getAttribute(a) && el.getAttribute(a) !== 'on') t += el.getAttribute(a) + ' ';
    const p = el.parentElement; if (p && clean(p.innerText).length < 40) t += p.innerText + ' ';
    const td = el.closest('td,th');
    if (td && td.parentElement) {
      t += td.innerText + ' ';
      const table = td.closest('table');
      const idx = Array.from(td.parentElement.children).indexOf(td);
      const heads = table ? table.querySelectorAll('thead tr') : [];
      const rows = heads.length ? Array.from(heads) : (table ? [table.querySelector('tr')] : []);
      rows.forEach(r => { if (r && r.children[idx]) t += ' ' + r.children[idx].innerText; });
    }
    return clean(t);
  };
  const far = el => {
    const row = el.closest('tr,li,.card,.row,.item,.cell,fieldset,section,.group');
    return clean(row ? row.innerText.slice(0, 300) : '');
  };
  const base = 'input[type=checkbox],input[type=radio],button,[role=button],[role=option],[role=checkbox],a,[onclick],li,.card,.slot,.shift,.option,[class*=slot],[class*=shift],[class*=option],div,span,label';
  const res = groups.map(g => ({any: new RegExp(g.any), want: new RegExp(g.want)}));
  let hits = [];
  for (const el of document.querySelectorAll(base)) {
    const tag = el.tagName;
    const clickable = /^(INPUT|BUTTON|A|LI|LABEL)$/.test(tag) || el.hasAttribute('onclick') || el.getAttribute('role')
                      || getComputedStyle(el).cursor === 'pointer';
    if (!clickable || !vis(el)) continue;
    if (tag === 'INPUT' && !/checkbox|radio/.test(el.type)) continue;
    const n = near(el);
    if (!n || n.length > 200 || /送出|提交|登出|取消|logout|關閉/i.test(n.slice(0, 8))) continue;
    let f = null;
    const ok = res.every(g => g.any.test(n) ? g.want.test(n) : g.want.test(f === null ? (f = far(el)) : f));
    if (ok) hits.push([el, n]);
  }
  // 只留最內層；label 包住 input 時保留 input
  hits = hits.filter(([el]) => !hits.some(([o]) => o !== el && el.contains(o)));
  return hits.map(([el, n], i) => {
    const id = 'u' + Date.now().toString(36) + i;
    el.setAttribute('data-ucc-id', id);
    const c = el.closest('[class]');
    return {id, type: el.type || '', checked: !!el.checked,
            disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
                      || /disabled|full|soldout|unavailable/i.test((c && c.className) || ''),
            text: n.slice(0, 120)};
  });
}
"""

# 找月曆上的日期格：優先找有完整日期文字的，其次找「只顯示日數」的格子（排除上/下月灰色格）
DATE_CELL_JS = r"""
({full, day}) => {
  const rf = new RegExp(full);
  const rd = new RegExp('^0?' + day + '(?!\\d)');
  const vis = el => { const r = el.getBoundingClientRect(); if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el); return s.visibility !== 'hidden' && s.display !== 'none'; };
  const bad = el => /other|prev|next|disabled|muted|outside|old|new|gray|grey/i.test(el.className + ' ' + (el.parentElement ? el.parentElement.className : ''))
                    || el.getAttribute('aria-disabled') === 'true' || el.disabled;
  const own = el => (el.innerText || '').replace(/\s+/g, ' ').trim();
  const attr = el => ['data-date','data-day','data-value','value','title','aria-label'].map(a => el.getAttribute(a) || '').join(' ');
  const els = Array.from(document.querySelectorAll('td,th,button,a,div,span,li,label,option')).filter(vis);
  const inner = c => c.filter(el => !c.some(o => o !== el && el.contains(o)));
  let c = inner(els.filter(el => (rf.test(own(el)) && own(el).length < 40) || rf.test(attr(el))));
  if (!c.length) c = inner(els.filter(el => own(el).length <= 12 && rd.test(own(el)) && !bad(el)));
  if (!c.length) return null;
  const score = el => (getComputedStyle(el).cursor === 'pointer' ? 2 : 0)
                    + (el.closest('table,[class*=cal],[class*=date],[class*=day]') ? 1 : 0);
  c.sort((a, b) => score(b) - score(a));
  const id = 'd' + Date.now().toString(36);
  c[0].setAttribute('data-ucc-date', id);
  return id;
}
"""

# 找『要求輸入帳號/密碼』的對話框：回傳 ID 欄、密碼欄、確認鈕
CRED_JS = r"""
(words) => {
  const vis = el => { const r = el.getBoundingClientRect(); if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el); return s.visibility !== 'hidden' && s.display !== 'none'; };
  const pw = Array.from(document.querySelectorAll('input[type=password]')).find(vis);
  if (!pw) return null;
  let box = pw.parentElement, idEl = null;
  for (let k = 0; k < 8 && box && !idEl; k++, box = box.parentElement) {
    idEl = Array.from(box.querySelectorAll('input:not([type]),input[type=text],input[type=number],input[type=tel],input[type=email],select'))
           .find(e => vis(e) && e !== pw);
    if (idEl) break;
  }
  box = box || document.body;
  const rx = new RegExp(words, 'i');
  let btn = null;
  for (let b = pw.parentElement, k = 0; b && !btn && k < 10; b = b.parentElement, k++)
    btn = Array.from(b.querySelectorAll('button,input[type=submit],input[type=button],[role=button],a.btn,.btn'))
          .find(e => vis(e) && rx.test(e.innerText || e.value || ''));
  const t = Date.now().toString(36);
  pw.setAttribute('data-ucc-pw', t);
  if (idEl) idEl.setAttribute('data-ucc-uid', t);
  if (btn) btn.setAttribute('data-ucc-ok', t);
  return {t, hasId: !!idEl, idTag: idEl ? idEl.tagName : '', hasBtn: !!btn};
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
      url, user_id, password,                # 只存在記憶體，不寫檔
      wishes: [{date, site, shift, clinics:[診1,診2,備援]}],   # 願望清單，同一天可多筆
      open_ts: 開放時間 epoch 秒（標準時間）或 None 立即, offset: 校時差,
      preload_sec, advance_ms, retry_sec（未開放時持續重試秒數）, headless,
      capture: 是否保存原始碼（預設 True）
    }
    on_status(index, text): 回報每一筆的結果（給 GUI 顯示）
    """

    def __init__(self, opts, log=print, stop=None, on_status=None):
        self.o = opts
        self.log = log
        self.stop = stop or threading.Event()
        self.on_status = on_status or (lambda i, t: None)
        self.dialogs = []
        self.frame = None          # 快取：班表所在的 frame
        self.cur_date = None
        self._secret = opts.get("password") or ""   # 只用來遮蔽保存檔中的密碼
        self.cap_dir = None
        self.responses = []
        self.net = []
        self.step = 0

    # ---------------------------------------------------------- 基本 ----
    def _check(self):
        if self.stop.is_set():
            raise Stopped()

    def frames(self):
        fr = list(self.page.frames)
        fr.sort(key=lambda f: ("userCodeAppPanel" in f.url or "googleusercontent" in f.url), reverse=True)
        if self.frame in fr:
            fr.remove(self.frame)
            fr.insert(0, self.frame)
        return fr

    def ev(self, js, arg=None):
        """在每個 frame 執行，回傳第一個有結果的 (frame, 結果)。"""
        for f in self.frames():
            try:
                r = f.evaluate(js, arg)
            except Exception:
                continue
            if r:
                return f, r
        return None, None

    def poll(self, fn, timeout, step=0.03):
        end = time.time() + timeout
        while True:
            self._check()
            r = fn()
            if r or time.time() > end:
                return r
            time.sleep(step)

    def wait_ready(self, timeout=60):
        if not self.poll(lambda: self.ev("() => document.querySelectorAll('input,select,button,td,a').length > 3")[1],
                         timeout, 0.1):
            raise RuntimeError("網頁載入逾時（若需 Google 帳號登入，請先在瀏覽器登入後再試）")

    def goto(self):
        self.frame = None
        self.cur_date = None
        self.page.goto(self.o["url"], wait_until="domcontentloaded", timeout=60000)
        self.wait_ready()

    # ------------------------------------------------------ 原始碼保存 ----
    def _redact(self, s):
        if not s:
            return s
        pw = self._secret
        if pw:
            import urllib.parse
            for v in {pw, urllib.parse.quote(pw), urllib.parse.quote_plus(pw), json_escape(pw)}:
                s = s.replace(v, "***")
        return s

    def _on_request(self, r):
        if r.resource_type in ("xhr", "fetch", "document") or r.method == "POST":
            try:
                pd = r.post_data
            except Exception:
                pd = None
            self.net.append({"t": round(time.time(), 3), "dir": "req", "method": r.method, "type": r.resource_type,
                             "url": r.url, "post": self._redact(pd)[:20000] if pd else None})

    def _on_response(self, r):
        if r.request.resource_type in ("xhr", "fetch", "document", "script"):
            self.responses.append(r)

    def capture(self, tag):
        """保存所有 frame 的 HTML、截圖、以及到目前為止的網路請求/回應。"""
        if not self.o.get("capture", True):
            return
        try:
            from pathlib import Path
            import json
            if self.cap_dir is None:
                self.cap_dir = Path(__file__).with_name("captures") / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                self.cap_dir.mkdir(parents=True, exist_ok=True)
                self.log(f"原始碼保存於 {self.cap_dir}")
            self.step += 1
            pre = f"{self.step:02d}_{tag}"
            for i, f in enumerate(self.page.frames):
                try:
                    (self.cap_dir / f"{pre}_frame{i}.html").write_text(
                        f"<!-- {f.url} -->\n" + self._redact(f.content()), encoding="utf-8")
                except Exception:
                    pass
            try:
                self.page.screenshot(path=str(self.cap_dir / f"{pre}.png"), full_page=True)
            except Exception:
                pass
            rs, self.responses = self.responses, []
            for r in rs:
                try:
                    body = r.text()
                except Exception:
                    body = None
                self.net.append({"t": round(time.time(), 3), "dir": "res", "status": r.status, "url": r.url,
                                 "type": r.request.resource_type,
                                 "body": self._redact(body)[:300000] if body else None})
            with open(self.cap_dir / "network.jsonl", "a", encoding="utf-8") as fp:
                for n in self.net:
                    fp.write(json.dumps(n, ensure_ascii=False) + "\n")
            self.net = []
        except Exception as e:
            self.log(f"（保存原始碼失敗：{e}）")

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

    # --------------------------------------------------------- 對話框 ----
    def _on_dialog(self, d):
        msg = d.message or ""
        try:
            if d.type == "prompt":          # 用瀏覽器 prompt() 問帳號密碼
                if re.search(r"密碼|password|pw", msg, re.I):
                    d.accept(self.o["password"])
                    self.log("[網頁詢問密碼] 已自動填入")
                else:
                    d.accept(self.o["user_id"])
                    self.log(f"[網頁詢問] {msg} → 已填 ID")
                return
            self.dialogs.append(msg)
            self.log(f"[網頁訊息] {msg}")
            d.accept()
        except Exception:
            pass

    # ------------------------------------------------------- 單一步驟 ----
    def click_date(self, d):
        full = date_regex(d)
        # <input type=date> / 日期下拉
        for f in self.frames():
            try:
                di = f.locator("input[type=date]:visible")
                if di.count():
                    di.first.fill(d.isoformat())
                    di.first.dispatch_event("change")
                    self.frame, self.cur_date = f, d
                    return True
                for s in f.locator("select:visible").all():
                    opts = s.evaluate("e => Array.from(e.options).map(o => [o.value, o.text])")
                    hit = [v for v, t in opts if re.search(full, f"{t} {v}")]
                    if hit:
                        s.select_option(value=hit[0])
                        self.frame, self.cur_date = f, d
                        return True
            except Exception:
                pass
        f, r = self.ev(DATE_CELL_JS, {"full": full, "day": d.day})
        if not r:
            return False
        before = self.page_text()
        f.locator(f"[data-ucc-date='{r}']").first.click(timeout=2000)
        self.frame, self.cur_date = f, d
        # 等右邊面板換成這一天：文字有變化且班別出現（最多 2 秒）
        changed = [False]
        def ready():
            if not changed[0]:
                changed[0] = self.page_text() != before
            return changed[0] and self.any_option_visible()
        if not self.poll(ready, 2.0) and before:
            self.log(f"  （{d.month}/{d.day} 面板沒有變化，仍繼續）")
        return True

    def find_option(self, site, clinic, shift):
        groups = [{"want": re.escape(site), "any": "|".join(SITES)},
                  {"want": SHIFTS[shift], "any": "|".join(SHIFTS.values())}]
        if clinic:
            groups.append({"want": CLINICS[clinic], "any": "|".join(CLINICS.values())})
        f, hits = self.ev(SCAN_JS, groups)
        if not hits:
            return None, []
        for h in hits:
            if re.search(FULL_WORDS, h["text"]):
                h["disabled"] = True
        return f, hits

    def any_option_visible(self):
        f, hits = self.ev(SCAN_JS, [{"want": "|".join(SITES), "any": "|".join(SITES)},
                                    {"want": "|".join(SHIFTS.values()), "any": "|".join(SHIFTS.values())}])
        return bool(hits)

    def fill_credentials(self, timeout):
        """選班後若跳出帳密欄位就填入並確認。回傳 True 表示有填。"""
        f, r = None, None
        def look():
            nonlocal f, r
            f, r = self.ev(CRED_JS, SUBMIT_WORDS)
            return r or self.dialogs
        self.poll(look, timeout)
        if not r:
            return False
        t = r["t"]
        if r["hasId"]:
            el = f.locator(f"[data-ucc-uid='{t}']")
            if r["idTag"] == "SELECT":
                opts = el.evaluate("e => Array.from(e.options).map(o => [o.value, o.text])")
                hit = [v for v, x in opts if self.o["user_id"] in (v, x) or self.o["user_id"] in x]
                if hit:
                    el.select_option(value=hit[0])
            else:
                el.fill(self.o["user_id"])
        pw = f.locator(f"[data-ucc-pw='{t}']")
        pw.fill(self.o["password"])
        if r["hasBtn"]:
            f.locator(f"[data-ucc-ok='{t}']").click(timeout=2000)
        else:
            pw.press("Enter")
        return True

    def press_submit(self):
        f, r = self.ev(r"""(w) => { const rx = new RegExp(w, 'i');
            const b = Array.from(document.querySelectorAll('button,input[type=submit],input[type=button],[role=button],.btn'))
              .filter(e => e.offsetParent !== null && rx.test(e.innerText || e.value || '')).pop();
            if (!b) return null; const t = 's' + Date.now().toString(36); b.setAttribute('data-ucc-sub', t); return t; }""",
                       SUBMIT_WORDS)
        if r:
            f.locator(f"[data-ucc-sub='{r}']").click(timeout=2000)
            return True
        return False

    def page_text(self):
        out = []
        for f in self.frames():
            try:
                out.append(f.evaluate("() => document.body ? document.body.innerText : ''"))
            except Exception:
                pass
        return "\n".join(out)

    def wait_result(self, before, timeout):
        """等網頁回應：alert 訊息，或頁面『新出現』成功/失敗字樣（和點選前的文字比較）。"""
        rx = re.compile(r"[^\n]{0,20}(?:" + OK_WORDS + "|" + FAIL_WORDS + r")[^\n]{0,20}")
        old = {}
        for m in rx.findall(before):
            old[m] = old.get(m, 0) + 1
        def look():
            if self.dialogs:
                return self.dialogs[-1]
            cnt = {}
            for m in rx.findall(self.page_text()):
                cnt[m] = cnt.get(m, 0) + 1
                if cnt[m] > old.get(m, 0):
                    return m.strip()
            return None
        return self.poll(look, timeout, 0.05) or ""

    # ------------------------------------------------------- 一筆願望 ----
    def book(self, w):
        d = w["date"]
        name = f"{d.month}/{d.day} {w['site']} {w['shift']}"
        clinics = w.get("clinics") or ["診1", "診2", "備援"]
        tried_full = []
        for clinic in clinics:
            self._check()
            # 換日期才點（重複點同一天可能會取消選取）
            if (self.cur_date != d or not self.any_option_visible()) and not self.click_date(d):
                return "✗ 找不到日期"
            got = self.poll(lambda: self.find_option(w["site"], clinic, w["shift"]), 2.5)
            f, hits = got if got else (None, [])
            avail = [h for h in hits if not h["disabled"] and not h["checked"]]
            if not avail:
                tried_full.append(clinic)
                continue
            h = avail[0]
            self.dialogs.clear()
            before = self.page_text()
            el = f.locator(f"[data-ucc-id='{h['id']}']")
            if h["type"] in ("checkbox", "radio"):
                el.check(timeout=2000)
            else:
                el.click(timeout=2000)
            # 選了就會要求帳密；沒跳出的話按一下送出/確認再等
            if not self.fill_credentials(1.5):
                if self.dialogs:
                    pass
                elif self.press_submit():
                    self.fill_credentials(2.0)
            msg = self.wait_result(before, 8)
            if re.search(OK_WORDS, msg):
                return f"✓ {clinic} 成功：{msg[:40]}"
            if re.search(r"額滿|已滿|滿額|已被|沒有名額", msg):
                tried_full.append(clinic)
                self.log(f"  {name} {clinic} 被搶走（{msg[:30]}），改選下一個")
                continue
            if re.search(r"密碼|帳號|不正確", msg):
                raise RuntimeError(f"帳號或密碼錯誤：{msg}")
            return f"? {clinic} 已送出，未確認結果：{msg[:40] or '無訊息'}"
        return f"✗ 額滿（{'/'.join(tried_full)}）"

    # ------------------------------------------------------------ 主程式 ----
    def run(self):
        from playwright.sync_api import sync_playwright
        o = self.o
        wishes = sorted(enumerate(o["wishes"]), key=lambda x: x[1]["date"], reverse=True)  # 日期由後往前
        with sync_playwright() as p:
            kw = {"headless": o.get("headless", False)}
            if o.get("chrome_path"):
                kw["executable_path"] = o["chrome_path"]
            try:
                browser = p.chromium.launch(**kw)
            except Exception:
                browser = p.chromium.launch(channel="chrome", **kw)
            ctx = browser.new_context()
            # 不載入圖片/字型/影片，加快速度
            ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "font", "media")
                      else r.continue_())
            self.page = ctx.new_page()
            self.page.on("dialog", self._on_dialog)
            self.page.on("request", self._on_request)
            self.page.on("response", self._on_response)
            try:
                open_ts, off = o.get("open_ts"), o.get("offset", 0.0)
                if open_ts:
                    wait_until(open_ts - o.get("preload_sec", 20), off, self.stop, self.log, "距離預先載入 ")
                self.log("載入網頁…")
                self.goto()
                self.capture("載入")
                if open_ts:
                    self.log("網頁已就緒，等待開放時間…")
                    wait_until(open_ts - o.get("advance_ms", 0) / 1000, off, self.stop, self.log, "距離開放 ")

                # 確認已開放：第一個日期點得到、右邊出現班別
                first = wishes[0][1]["date"]
                give_up = time.time() + o.get("retry_sec", 60)
                attempt = 0
                while True:
                    attempt += 1
                    self._check()
                    t = dt.datetime.fromtimestamp(time.time() + off).strftime("%H:%M:%S.%f")[:-3]
                    self.log(f"[標準時間 {t}] 開始（第 {attempt} 次）")
                    self.dialogs.clear()
                    if self.click_date(first) and self.poll(self.any_option_visible, 2.0):
                        break
                    if time.time() > give_up:
                        raise RuntimeError("持續重試仍未出現班表，請檢查網頁")
                    if attempt <= 3 or attempt % 10 == 0:
                        self.log("尚未開放或班表未出現，重新整理…")
                    self.goto()
                self.capture("班表")

                t0 = time.time()
                ok = 0
                for idx, w in wishes:
                    self.on_status(idx, "處理中…")
                    try:
                        res = self.book(w)
                    except Stopped:
                        raise
                    except RuntimeError:
                        raise
                    except Exception as e:
                        res = f"! 錯誤 {str(e)[:60]}"
                    ok += res.startswith("✓")
                    d = w["date"]
                    self.log(f"{d.month}/{d.day} {w['site']} {w['shift']}：{res}")
                    self.on_status(idx, res)
                    if self.step < 4:
                        self.capture("選班後")
                self.log(f"完成：{ok}/{len(wishes)} 筆成功，花費 {time.time() - t0:.1f} 秒")
                self.capture("完成")
            except Stopped:
                self.log("已停止")
            except Exception as e:
                self.log(f"錯誤：{e}")
                try:
                    from pathlib import Path
                    Path(__file__).with_name("inspect_report_error.txt").write_text(self.dump(), encoding="utf-8")
                    self.capture("錯誤")
                    self.log("已保存網頁結構（inspect_report_error.txt 與 captures/）")
                except Exception:
                    pass
            self.o["password"] = ""          # 用完即清除
            self.log("瀏覽器保持開啟，可手動檢查；按『停止』關閉。")
            while not self.stop.is_set():
                try:
                    self.page.wait_for_timeout(300)
                except Exception:
                    break
            if self.o.get("capture", True) and self.cap_dir:
                self.capture("關閉前")
            browser.close()
            self._secret = ""

    def run_capture_only(self):
        """只開網頁並錄下原始碼與網路請求，讓你手動操作一輪。"""
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            self.page = browser.new_page()
            self.page.on("dialog", self._on_dialog)
            self.page.on("request", self._on_request)
            self.page.on("response", self._on_response)
            self.goto()
            self.capture("載入")
            self.log("請在瀏覽器手動操作（點日期、點班、輸入帳密）；每 5 秒自動保存一次，按『停止』結束。")
            last = time.time()
            while not self.stop.is_set():
                try:
                    self.page.wait_for_timeout(300)
                except Exception:
                    break
                if time.time() - last > 5:
                    self.capture("手動")
                    last = time.time()
            try:
                self.capture("結束")
            except Exception:
                pass
            browser.close()
            self.log(f"已保存：{self.cap_dir}")


def json_escape(s):
    import json
    return json.dumps(s)[1:-1]

#!/usr/bin/env python3
"""
UCC 支援護理人力預班 自動登入／登記工具

用法：
  python ucc_booking.py inspect            # 開啟網頁，把所有表單元素輸出到 inspect_report.txt（第一次先跑這個）
  python ucc_booking.py book               # 登入並勾選指定日期／院區，停在送出前讓你確認
  python ucc_booking.py book --submit      # 勾選後自動按送出
  python ucc_booking.py book --wait        # 等到 config 的 open_time 才開始（例如 09:00 開放）

可用參數覆寫 config：
  --name 王小明 --site 林森 --site 昆明 --date 2026-10-03 --date 2026-10-10~2026-10-12

本工具只用來登記「你自己」的班，請勿替他人登記或大量重複送出。
"""
import argparse
import datetime as dt
import getpass
import json
import re
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("請先安裝：pip install playwright && python -m playwright install chromium")

HERE = Path(__file__).resolve().parent
LOGIN_WORDS = r"登入|登錄|登陸|進入|login|sign\s*in|確認"
SUBMIT_WORDS = r"送出|提交|確認送出|確定送出|預約|登記|儲存|存檔|submit|save"

# ---------------------------------------------------------------- config ----

def load_config(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"找不到設定檔 {p}，請複製 config.example.json 為 config.json 再修改。")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def expand_dates(items):
    """'2026-10-03' 或 '2026-10-10~2026-10-12' -> [date, ...]"""
    out = []
    for it in items:
        it = it.strip()
        if "~" in it:
            a, b = (dt.date.fromisoformat(x.strip()) for x in it.split("~"))
            d = a
            while d <= b:
                out.append(d)
                d += dt.timedelta(days=1)
        elif it:
            out.append(dt.date.fromisoformat(it))
    return sorted(set(out))


def date_regexes(d):
    """產生能比對網頁上各種日期寫法的 regex（避免 10/1 誤中 10/12）。"""
    m, dd, y, roc = d.month, d.day, d.year, d.year - 1911
    M, D = f"0?{m}", f"0?{dd}"
    pats = [
        rf"(?<!\d)(?:{y}|{roc})[-/.年]\s*{M}[-/.月]\s*{D}(?!\d)",  # 2026-10-03 / 115/10/03 / 115年10月3日
        rf"(?<![\d/])\s*{M}\s*/\s*{D}(?![\d/])",                     # 10/3
        rf"(?<!\d){M}\s*月\s*{D}\s*日?(?!\d)",                        # 10月3日
        rf"(?<!\d){y}{m:02d}{dd:02d}(?!\d)",                           # 20261003
    ]
    return pats


# ----------------------------------------------------------- page helpers --

# 在 frame 內掃描可互動元素並附上「上下文文字」
SCAN_JS = r"""
(args) => {
  const {datePats, siteWords, shiftWords, mark} = args;
  const dateRes = (datePats || []).map(p => new RegExp(p));
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const ctx = el => {
    let t = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t += l.innerText + ' '; }
    const lab = el.closest('label'); if (lab) t += lab.innerText + ' ';
    t += (el.innerText || el.value || el.getAttribute('aria-label') || el.title || '') + ' ';
    for (const a of ['data-date','data-site','data-day','data-value','value','name','id'])
      if (el.getAttribute(a)) t += el.getAttribute(a) + ' ';
    // 往上找所在的 row / card，加上表頭（欄位名稱）
    const row = el.closest('tr,li,.card,.row,.item,.day,.cell,fieldset');
    if (row) t += ' | ' + row.innerText.slice(0, 300);
    const td = el.closest('td,th');
    if (td) {
      const table = td.closest('table');
      const idx = Array.from(td.parentElement.children).indexOf(td);
      const head = table && table.querySelector('thead tr, tr');
      if (head && head.children[idx]) t += ' | ' + head.children[idx].innerText;
    }
    return t.replace(/\s+/g, ' ').trim();
  };
  const sel = 'input[type=checkbox],input[type=radio],button,[role=button],[role=checkbox],a[onclick],td[onclick],div[onclick],span[onclick],.btn,.slot,.shift';
  const els = Array.from(document.querySelectorAll(sel)).filter(vis);
  const out = [];
  els.forEach((el, i) => {
    const c = ctx(el);
    let ok = true;
    if (dateRes.length) ok = ok && dateRes.some(r => r.test(c));
    if (siteWords && siteWords.length) ok = ok && siteWords.some(w => c.includes(w));
    if (shiftWords && shiftWords.length) ok = ok && shiftWords.some(w => c.includes(w));
    if (!mark || ok) {
      const id = 'ucc' + i;
      if (mark) el.setAttribute('data-ucc-id', id);
      out.push({id, tag: el.tagName.toLowerCase(), type: el.type || '', checked: !!el.checked,
                disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true', text: c.slice(0, 200)});
    }
  });
  return out;
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
    datalists: q('datalist').map(d => ({id: d.id, options: Array.from(d.options).map(o => o.value)})),
    scripts: q('script').map(s => s.innerText).join('\n').match(/google\.script\.run[\s\S]{0,200}?\.\w+\([^)]*\)/g) || [],
    text: (document.body ? document.body.innerText : '').slice(0, 3000),
  };
}
"""


def app_frames(page):
    """GAS 網頁會包兩層 iframe（sandboxFrame → userHtmlFrame），把所有 frame 都回傳，內層優先。"""
    frames = list(page.frames)
    frames.sort(key=lambda f: ("userCodeAppPanel" in f.url or "googleusercontent" in f.url), reverse=True)
    return frames


def wait_app_ready(page, timeout=60):
    """等到任何 frame 裡出現 input/select/button。"""
    end = time.time() + timeout
    while time.time() < end:
        for f in app_frames(page):
            try:
                if f.locator("input, select, button").count() > 0:
                    return f
            except Exception:
                pass
        time.sleep(0.5)
    raise RuntimeError("網頁載入逾時（若需要 Google 帳號登入，請在跳出的瀏覽器中手動登入後再試）")


def find_first(page, css):
    for f in app_frames(page):
        try:
            loc = f.locator(css)
            n = loc.count()
            for i in range(n):
                if loc.nth(i).is_visible():
                    return f, loc.nth(i)
        except Exception:
            pass
    return None, None


def find_button(page, words, override=""):
    if override:
        return find_first(page, override)
    rx = re.compile(words, re.I)
    for f in app_frames(page):
        try:
            loc = f.locator("button, input[type=submit], input[type=button], [role=button], a.btn, .btn")
            for i in range(loc.count()):
                el = loc.nth(i)
                if not el.is_visible():
                    continue
                t = (el.inner_text() or el.get_attribute("value") or "").strip()
                if rx.search(t):
                    return f, el
        except Exception:
            pass
    return None, None


# ------------------------------------------------------------------ steps --

def do_inspect(page, out_path):
    wait_app_ready(page)
    time.sleep(2)
    lines = []
    for i, f in enumerate(page.frames):
        try:
            info = f.evaluate(DUMP_JS)
        except Exception as e:
            info = {"error": str(e)}
        lines.append(f"===== frame {i}: {f.url[:150]}")
        lines.append(json.dumps(info, ensure_ascii=False, indent=2))
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    page.screenshot(path=str(HERE / "inspect.png"), full_page=True)
    print(f"已輸出 {out_path} 與 inspect.png")


def do_login(page, cfg, sel):
    wait_app_ready(page)
    name = cfg.get("name", "")

    # --- 姓名 ---
    f, name_el = find_first(page, sel.get("name") or "select")
    if name_el is not None and name_el.evaluate("e => e.tagName") == "SELECT":
        options = [o.strip() for o in name_el.evaluate("e => Array.from(e.options).map(o => o.text)")]
        choices = [o for o in options if o and not re.search(r"請選|選擇|--", o)]
        if not name or name not in options:
            if name:
                print(f"清單中找不到「{name}」，請從下列選擇：")
            name = pick_from_list(choices, "請選擇你的姓名")
        name_el.select_option(label=name)
    else:
        f, name_el = find_first(page, sel.get("name") or
                                "input[type=text], input:not([type]), input[type=search], input[type=tel], input[type=number]")
        if name_el is None:
            raise RuntimeError("找不到姓名欄位，請先執行 inspect 並在 config.selectors.name 填入 CSS selector")
        if not name:
            name = input("請輸入姓名／員工編號：").strip()
        name_el.fill(name)
        time.sleep(0.5)
        # 若是自動完成清單，點選出現的項目
        try:
            f.get_by_text(name, exact=True).last.click(timeout=1500)
        except Exception:
            pass
    print(f"姓名：{name}")

    # --- 密碼 ---
    f, pw_el = find_first(page, sel.get("password") or "input[type=password]")
    if pw_el is not None:
        pw = cfg.get("password") or getpass.getpass("請輸入密碼（不會顯示）：")
        pw_el.fill(pw)
    else:
        print("（沒有偵測到密碼欄位，略過）")

    # --- 登入按鈕 ---
    f, btn = find_button(page, LOGIN_WORDS, sel.get("login_button", ""))
    if btn is None:
        if pw_el is not None:
            pw_el.press("Enter")
        else:
            raise RuntimeError("找不到登入按鈕，請在 config.selectors.login_button 填入 selector")
    else:
        btn.click()
    print("已送出登入，等待頁面…")
    time.sleep(3)


def select_site_dropdown(page, site):
    """如果有『院區/站點』下拉選單，選擇它；回傳是否有選到。"""
    for f in app_frames(page):
        try:
            sels = f.locator("select")
            for i in range(sels.count()):
                s = sels.nth(i)
                opts = s.evaluate("e => Array.from(e.options).map(o => o.text)")
                hit = [o for o in opts if site in o]
                if hit and s.is_visible():
                    s.select_option(label=hit[0])
                    time.sleep(1.5)
                    return True
        except Exception:
            pass
    return False


def click_matches(page, dates, sites, shifts):
    """在頁面中勾選符合日期（＋院區＋班別）的格子。"""
    total = []
    for d in dates:
        pats = date_regexes(d)
        for site in sites or [None]:
            used_dropdown = site and select_site_dropdown(page, site)
            site_words = [] if (used_dropdown or not site) else [site]
            hits = []
            for f in app_frames(page):
                try:
                    res = f.evaluate(SCAN_JS, {"datePats": pats, "siteWords": site_words,
                                               "shiftWords": shifts, "mark": True})
                except Exception:
                    continue
                for r in res:
                    # 排除「送出」「登出」之類的按鈕被誤判
                    if re.search(r"送出|提交|登出|取消|logout", r["text"][:20], re.I):
                        continue
                    hits.append((f, r))
            label = f"{d.isoformat()} {site or ''}".strip()
            if not hits:
                print(f"  ✗ {label}：找不到可勾選的項目")
                continue
            for f, r in hits:
                if r["disabled"]:
                    print(f"  - {label}：已額滿／不可選 -> {r['text'][:60]}")
                    continue
                if r["checked"]:
                    print(f"  ✓ {label}：已勾選 -> {r['text'][:60]}")
                    continue
                el = f.locator(f"[data-ucc-id='{r['id']}']")
                try:
                    if r["type"] in ("checkbox", "radio"):
                        el.check(timeout=3000)
                    else:
                        el.click(timeout=3000)
                    print(f"  ✓ {label}：已點選 -> {r['text'][:60]}")
                    total.append(label)
                    time.sleep(0.3)
                except Exception as e:
                    print(f"  ! {label}：點選失敗 {e}")
    return total


def pick_from_list(items, prompt):
    if not items:
        return input(prompt + "：").strip()
    for i, it in enumerate(items, 1):
        print(f"  {i:3d}. {it}")
    while True:
        s = input(f"{prompt}（輸入編號或文字）：").strip()
        if s.isdigit() and 1 <= int(s) <= len(items):
            return items[int(s) - 1]
        m = [it for it in items if s and s in it]
        if len(m) == 1:
            return m[0]
        print("  無法辨識，請重新輸入")


def wait_until(t):
    target = dt.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
    while True:
        left = (target - dt.datetime.now()).total_seconds()
        if left <= 0:
            return
        print(f"\r距離開放 {t} 還有 {int(left)} 秒…", end="", flush=True)
        time.sleep(min(left, 1))


# ------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(description="UCC 預班自動登記")
    ap.add_argument("mode", choices=["inspect", "book"])
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--name")
    ap.add_argument("--site", action="append", help="院區，可重複：--site 林森 --site 昆明")
    ap.add_argument("--date", action="append", help="日期或區間：2026-10-03 或 2026-10-10~2026-10-12")
    ap.add_argument("--shift", action="append", help="班別關鍵字（白班/小夜/大夜…），可省略")
    ap.add_argument("--submit", action="store_true", help="勾選後自動按送出（預設會停下來讓你確認）")
    ap.add_argument("--wait", action="store_true", help="等到 open_time 才開始")
    ap.add_argument("--headless", action="store_true")
    a = ap.parse_args()

    cfg = load_config(a.config) if Path(a.config).exists() or a.mode == "book" else \
        load_config(HERE / "config.example.json")
    if a.name: cfg["name"] = a.name
    if a.site: cfg["sites"] = a.site
    if a.date: cfg["dates"] = a.date
    if a.shift: cfg["shifts"] = a.shift
    sel = cfg.get("selectors", {})

    if a.wait and cfg.get("open_time"):
        wait_until(cfg["open_time"])
        print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=a.headless)
        page = browser.new_page()
        page.on("dialog", lambda d: (print(f"[網頁訊息] {d.message}"), d.accept()))
        page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60000)

        try:
            if a.mode == "inspect":
                do_inspect(page, HERE / "inspect_report.txt")
                return

            do_login(page, cfg, sel)
            dates = expand_dates(cfg.get("dates", []))
            if not dates:
                sys.exit("config 裡沒有設定 dates")
            print(f"開始登記：{len(dates)} 天 × 院區 {cfg.get('sites') or '不限'}")
            done = click_matches(page, dates, cfg.get("sites", []), cfg.get("shifts", []))
            print(f"共點選 {len(done)} 項")

            if a.submit and done:
                f, btn = find_button(page, SUBMIT_WORDS, sel.get("submit_button", ""))
                if btn is None:
                    print("找不到送出按鈕，請手動在瀏覽器按送出")
                else:
                    btn.click()
                    print("已按下送出")
                    time.sleep(3)
            page.screenshot(path=str(HERE / "result.png"), full_page=True)
            print("已存結果截圖 result.png")
        except Exception as e:
            print(f"發生錯誤：{e}")
            try:
                do_inspect(page, HERE / "inspect_report_error.txt")
            except Exception:
                pass

        if not a.headless:
            input("請在瀏覽器確認結果（需要的話手動送出），按 Enter 關閉…")
        browser.close()


if __name__ == "__main__":
    main()

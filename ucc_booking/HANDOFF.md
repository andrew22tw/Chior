# 交班檔：UCC 支援護理人力預班 自動登記程式

> 給接手實作的 AI／工程師。讀完這份就能從零重做，或接續現有程式。
> 建立：2026-11-14　｜　參考實作：repo `andrew22tw/Chior`，分支 `claude/hopeful-turing-uecx9m`，資料夾 `ucc_booking/`
> **本文件不含任何帳號、密碼、員工姓名。** 實作時也不要把這些寫進程式碼、檔案、log 或 git。

---

## 0. 一句話目標

在預班系統「開放的那一秒」（國家標準時間，**不可提早**），替使用者（及經本人同意的同事）依願望清單自動送出預約；被搶就換下一個診別；送出後向系統查證，沒成功就重送；全程可在桌面程式畫面上看到，也能在雲端無人值守執行。

---

## 1. 目標系統（已從原始碼與實際資料確認）

| 項目 | 內容 |
|---|---|
| 名稱 | UCC 週日及國定假日排班系統（假日輕急症中心排班系統） |
| 技術 | Google Apps Script 網頁應用程式，**不需 Google 登入** |
| 網址 | `https://script.google.com/macros/s/AKfycbwJqVMzejJMXYFv1o5b20WV2rUSAisgXbGTuvwrMKhAjYmf9OcaIXDhs3YINjXVQFgZ8g/exec` |
| 內層頁面主機 | `n-hhh6afqd52blwmotsdr2sqrk6crkw7tp4wilvlq-0lu-script.googleusercontent.com`（系統重新部署可能改變） |
| 院區 | 林森、昆明、廣慈 |
| 班別 | 白班（8AM–4PM）、夜班（4PM–12AM） |
| 診別 | 每個「日期×院區×班別」有 **診1、診2、備援** 三格 |
| 可排日期 | 只有週日與國定假日（由系統 `scheduleDays` 決定） |
| 開放規則 | 系統設定 `systemSettings`：`mode`、`openDay`、`openStartTime`、`openEndTime`。**目前「每月 13 日 09:00–11:00」，預約「下個月」**。⚠ 管理者會改（11 月從 14 日改成 13 日，造成錯過），**一律以系統當下設定為準，不可寫死** |
| 每人每天限 1 班 | 前端檢查且實際資料吻合 |
| 備援規則 | 該 日期×院區×班別 的診1、診2 都被約走後，備援才可登記 |
| 速度實測 | 頁面就緒 2.7–3.0 秒；`getInitialData` 3.5–5.8 秒；`createBooking` 未實測（推估 1–3 秒） |
| 競爭實況（2026/12 那次） | 90 格 47 人，09:00:03 第一筆、09:09:35 全滿；至少 1 人疑似用程式（第 3、6、11 秒連拿 3 格） |

### 1.1 伺服器 API（`google.script.run`）

只能在網頁「內層 frame」裡呼叫（該 frame 有 `google.script.run`）。

| 函式 | 參數 | 回傳 | 用途 |
|---|---|---|---|
| `getInitialData()` | 無 | `{success, systemSettings, locations[], shifts[{name,note}], scheduleDays[{date,weekday,note}], bookings[{date,location,shift,clinicType,empId,name,zone,timestamp}], staffList[...], admins, bulletinItems, replaceRecords}` | 讀取全部資料 |
| `createBooking(date, location, shift, clinicType, empId, password)` | `date`=`YYYY-MM-DD`；`location`=院區名；`shift`=`白班`/`夜班`；`clinicType`=`診1`/`診2`/`備援` | `{success: bool, message: string}` | **預約（核心）** |

其他函式（管理用，**不要呼叫**）：`deleteBooking`、`changeStaffPassword`、`handleForgotPassword`、`addStaff`、`updateStaff`、`deleteStaff`、`saveSystemSettings`、`replaceBooking`、`adminLogin` 等。

前端呼叫寫法（用來偵測系統是否改版）：
```
.createBooking(currentBookingSlot.date, currentBookingSlot.location, currentBookingSlot.shift, currentBookingSlot.clinicType, empId, password)
```

`bookings` 中 `clinicType` 為空時視為 `診1`。

### 1.2 前端行為（為什麼不能「點畫面」）

- 「未開放預約」由**使用者電腦時鐘**（`new Date()`）判斷，且**要重新點日期才會重畫**；08:59 開的頁面到 09:00 仍顯示鎖住。
- 每次預約成功後前端呼叫 `fetchSystemData()` 重新下載全部資料（約 4 秒）。
- 前端可用的函式：`fetchSystemData(onComplete)`、`selectDate(dateStr)`（用來在結束後刷新畫面給使用者看）。

### 1.3 ⚠ 已知的系統資安問題（不要利用）

`getInitialData` 不需登入就回傳全體人員員編、姓名、電話與所有預約。**實作只能讀使用者本人（及同意者）的資料；任何輸出、log、報告都不可列出他人身分。** 分析競爭狀況時一律匿名（人員A、B…）。已建議使用者轉告系統管理者。

---

## 2. 功能需求

### 2.1 願望清單
- 一筆 = `日期 + 院區 + 班別（白/夜）`，外加診別順序。
- 診別順序**預設 診1 → 診2 → 備援**；可個別改成 **診2 → 診1 → 備援**（只有這兩種）。
- 同一天可多筆 = **備選順序**（因為一天只能一班），第一個成功就停。
- 多帳號：每個帳號一份清單、各自執行、同時送出。只在**本人同意**下才可替他人登記，且是本人要上的班。

### 2.2 送出規則
1. 標準時間到「開放時間」才送出，**絕不提早**。
2. 所有日期**同時**送出（`Promise.allSettled`），計畫排序為**日期由後往前**。
3. 每一天依「清單順序 × 診別順序」逐一嘗試，伺服器回覆分類：

| 回覆（`message` 比對） | 動作 |
|---|---|
| `success: true` | 這天完成 |
| `一天只能｜已經有預約｜已有預約｜當日已｜同一天` | 這天完成（已有班） |
| `已被｜額滿｜已滿｜被預約｜已有人` | 立刻試下一個候選 |
| `密碼｜查無｜帳號｜員工編號｜驗證失敗` | **全部停止**（帳密錯） |
| `未開放｜尚未｜開放時間｜非開放｜不在開放` | 每 300ms 重試同一格，直到 deadline（預設 90–120 秒） |
| 連線錯誤／其他不明訊息 | **同一格立即重送**，最多 3 次，再換下一個 |

   ⚠ 這些文字是依前端程式推測的，**伺服器真實訊息尚未實測**；第一次正式執行後要依實際訊息修正。
4. **每輪結束一定呼叫 `getInitialData` 查證**，以系統紀錄（`empId` 相符）為準；系統中沒有的日期（含「回報成功但沒存」）**立即再送一輪**，最多 3 輪。帳密錯誤不重送。
5. 結束後列出該帳號在目標月份的系統預約，並讓瀏覽器畫面 `fetchSystemData` + `selectDate` 刷新到該日期。

### 2.3 時間
- 優先 NTP：`time.stdtime.gov.tw`、`tock.stdtime.gov.tw`、`watch.stdtime.gov.tw`、`clock.stdtime.gov.tw`、`time.google.com`、`pool.ntp.org`（每台取 4 次，取延遲最小者的中位數）。
- NTP 被擋（雲端常見）：對 `https://script.google.com` 發 HEAD，**不要跟隨轉址**（302 也有 `Date`），取樣 8 次，用「秒數跳動」交集縮小誤差到 ±0.1–0.2 秒。
- 等待：粗 sleep，最後 0.2 秒忙等。
- 開放時間一律以**台灣時間（UTC+8）**解讀（雲端主機是 UTC）。

### 2.4 讀取系統設定（每次都要）
- 由 `openDay/openStartTime/openEndTime` 算「下次開放時間」：本月開放結束時間已過就算下個月。
- 該次開放可預約的月份 = 開放月份的下個月；可排日期 = `scheduleDays` 中該月的日期（可能尚未公布）。

### 2.5 原始碼監控
- 每次載入後取內層頁面所有 `<script>` 的文字做 SHA-256，存 `source_history/`，和上次比較；**只比 script，不比整頁**（整頁內容隨資料變動會誤報）。
- 檢查 `createBooking(...)` 參數字串仍等於上面那串；不同就警告並保存新版。

### 2.6 測試模式（不送出）
- 載入系統、確認 ID 存在（只顯示本人姓名）、列出本人已有預約、檢查每筆清單的日期是否為可排日、院區/班別名稱是否正確、每格目前空或已被約。**絕不呼叫 `createBooking`**。密碼只能在正式送出時驗證。

---

## 3. 介面需求（桌面程式）

- 左：① 月曆（可多選；**只有系統公布的日期可點、綠色**；有 全選/週末/平日/清除）→ ② 院區、白/夜勾選 →「加入清單」→ ④ 開放時間（**開啟時自動從系統讀取並填入**、有「從系統更新」按鈕）、大時鐘顯示校正後標準時間、倒數。
- 右：③ 願望清單表格（日期、星期、院區、班別〔診2先時顯示「白班（診2先）」〕、結果）；按鈕：上移、下移、⇄ 診2先、刪除、清空、匯出雲端。
- 動作按鈕：⏰ 開始排程、▶ 立即執行、🧪 測試（不送出）、🔍 手動操作並錄原始碼、■ 停止；下方執行紀錄。
- 按開始時跳出 **ID／密碼視窗**：可勾「顯示密碼」檢查；預覽 `ID：[…]`、`密碼：[…]（N 個字）`，前後空白要警告；**只在本次有效，不存檔**。
- **每次開啟清單都是空的**（使用者要自己在畫面上設定；不要記住上次清單）。只記住進階設定。
- 開瀏覽器（**非無頭**）讓使用者看到系統畫面；結束後刷新到結果日期。
- 一個視窗一個帳號；多帳號就開多個視窗。
- 瀏覽器：電腦內建 **Edge** → Chrome → Playwright Chromium（打包 exe 時不用另外下載）。
- 進階：提前開網頁秒數（預設 40）、未開放持續重試秒數（預設 90）、保存原始碼（預設開）。

---

## 4. 雲端需求

- **無介面執行**：`UCC_ID=… UCC_PW=… python ucc_run.py wishes.json`（`--dry-run` 測試、`--now` 立即）。帳密從環境變數或互動輸入，讀後即從環境移除，不落地。
- `wishes.json`：
```json
{"open_time": "2026-12-13 09:00:00",
 "wishes": [{"date": "2027-01-03", "site": "林森", "shift": "白班", "clinics": ["診1","診2","備援"]}]}
```
- 網路白名單（雲端 limited network 時）：`script.google.com`、內層頁面主機（見 §1）、`script.googleusercontent.com`、`www.gstatic.com`、`ssl.gstatic.com`、`www.google.com`、`accounts.google.com`。
- 排程（現行做法，Claude Code 雲端排程）：
  - **每日 16:48** 讀系統設定並推播（變更時警告、3 天內開放提醒）。
  - **每月 8–18 日 08:40** 讀系統開放日；**今天是開放日才搶**，用最新清單；開放時間若晚於 90 分鐘，排一個開放前 15 分鐘的喚醒。
- 雲端主機在資料中心、時區 UTC，必須用台灣時間解讀開放時間。

---

## 5. 架構（參考實作）

| 檔案 | 職責 |
|---|---|
| `ucc_core.py` | 校時（SNTP / HTTP Date）、`wait_until`、`launch_browser`（Edge→Chrome→Chromium，雲端自動帶 `HTTPS_PROXY`）、`app_dir()`（exe 時為 exe 旁）、原始碼與網路紀錄保存（密碼遮蔽）、舊的「點畫面」模式（備用） |
| `ucc_fast.py` | **快速模式**：找內層 frame、原始碼監控、測試模式、`ORCH_JS` 同時送出、查證重送輪次、結果回報、畫面刷新 |
| `check_settings.py` | `fetch()`：讀系統開放規則、下次開放、目標月份與可排日期（唯讀） |
| `ucc_gui.py` | tkinter 介面；`--selftest` 給 CI 用 |
| `ucc_run.py` | 無介面版（雲端） |
| `ucc_booking.py` | 除錯：輸出網頁結構 |
| `.github/workflows/build-desktop.yml` | Windows 上用 PyInstaller 打包 `UCC-Booking.exe`（`--onefile --windowed --collect-all playwright`），自我測試後上傳 artifact |

### 5.1 核心送出程式（在內層 frame 執行）

```js
async ({plan, uid, pw, retryMs, deadline}) => {
  const log = (...a) => console.log('[UCC]' + JSON.stringify(a));   // Python 端監聽 console 更新畫面
  const call = (fn, ...args) => new Promise(res =>
    google.script.run
      .withSuccessHandler(r => res({ok: true, r}))
      .withFailureHandler(e => res({ok: false, e: String((e && e.message) || e)}))[fn](...args));
  let stopAll = false;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const one = async (day) => {                 // day = {date, cands:[{idx, location, shift, clinic}]}
    for (const c of day.cands) {
      let netTries = 0;
      while (!stopAll) {
        const t0 = performance.now();
        const r = await call('createBooking', day.date, c.location, c.shift, c.clinic, uid, pw);
        const ms = Math.round(performance.now() - t0);
        const msg = r.ok ? String((r.r && r.r.message) || '') : r.e;
        if (r.ok && r.r && r.r.success) { log('ok', c.idx, day.date, c.location, c.shift, c.clinic, ms, msg); return 'ok'; }
        if (/一天只能|已經有預約|已有預約|當日已|同一天/.test(msg)) { log('daydone', ...); return 'daydone'; }
        if (/已被|額滿|已滿|被預約|已有人/.test(msg)) { log('taken', ...); break; }
        if (/密碼|查無|帳號|員工編號|驗證失敗/.test(msg)) { stopAll = true; log('auth', ...); return 'auth'; }
        if (/未開放|尚未|開放時間|非開放|不在開放/.test(msg) && Date.now() < deadline) { await sleep(retryMs); continue; }
        if (netTries++ < 3) continue;          // 連線錯誤或不明失敗：同一格立即重送
        break;
      }
      if (stopAll) return 'stopped';
    }
    return 'none';
  };
  const res = await Promise.allSettled(plan.map(one));
  return res.map(x => x.status === 'fulfilled' ? x.value : String(x.reason));
}
```

`plan` 建法：依日期分組、日期由大到小；每天的候選 = 依清單順序，每筆展開成它的診別順序。

---

## 6. 踩過的坑（一定要看）

1. **開放日會被改**：寫死日期導致錯過整個月。所有排程、預設值都要從系統讀。
2. **找對 frame**：外層 → `userCodeAppPanel` → 內層 `…/blank`。`google.script.run` 在**內層**；用「哪個 frame 有 `google.script.run`」來找，不要用網址猜。
3. **Playwright 同步 API 不能用 `time.sleep` 等待頁面**：事件不會處理，frame 清單不更新。等待要用 `page.wait_for_timeout()`，或在迴圈中呼叫 `evaluate`。
4. **新加入白名單的主機要幾秒才生效**，第一次載入可能只看到 `about:blank`。
5. **HTTP 校時遇到 302 轉址**會轉去被擋的主機；用不跟隨轉址的 opener，直接讀 302 的 `Date`。
6. **原始碼比對只比 `<script>`**，整頁 HTML 每次都不同。
7. **Windows exe**：`--onefile` 執行時 `__file__` 在暫存資料夾，設定與紀錄要寫到 `sys.executable` 旁。
8. 寫模擬頁時別用 `id="closed"`（和 `window.closed` 衝突，整段 script 會壞）。
9. 雲端 Playwright 版本可能和預裝瀏覽器不符：用 `executable_path=/opt/pw-browsers/chromium`，不要 `playwright install`。
10. 點畫面模式要等右側面板換成新日期才點班，否則會點到上一天的班（快速模式不需要）。

---

## 7. 測試方法

### 7.1 模擬系統（必做）
內層頁面放一個假的 `google.script.run`，用 Proxy 讓 `withSuccessHandler/withFailureHandler` 回傳同一個 proxy，其他屬性回傳延遲 1.5 秒的伺服器函式。伺服器邏輯要模擬：開放時間前回「目前非開放預約時間」、帳密錯、一天一班、已被預約、備援需診1+診2 已滿、**第一次回「系統忙碌」**、**回報成功但沒存**、開放後 1.2 秒別人搶走某格。外面包兩層 iframe 模擬 GAS 結構。

### 7.2 驗收標準
| 情境 | 期望 |
|---|---|
| 3 天、診1 有兩天已被搶、伺服器 1.5 秒 | 3 天全成功、約 3 秒、改搶診2 |
| 伺服器回「尚未開放」 | 每 300ms 重試，開放後成功 |
| 第一次「系統忙碌」 | 同一格立即重送成功 |
| 回報成功但沒存 | 查證發現，第 2 輪重送成功 |
| 帳密錯 | 立即停止、不重送 |
| 真系統 `--dry-run` | ID 存在、日期可排、名稱正確、**沒有送出** |
| exe `--selftest`（Windows CI） | 讀到系統設定、exit 0 |

**不可以用真系統做正式送出測試**（會變成真的預約）。

---

## 8. 規範（不可違反）

- 不提早送出；不繞過「一人一天一班」；只替本人或已同意者登記本人要上的班。
- 帳密只在記憶體／當次執行；保存的網頁與網路紀錄要遮蔽密碼；不 commit。
- 不讀取、不輸出他人個資；分析一律匿名。
- 只呼叫 `getInitialData` 與 `createBooking`；不碰管理函式。
- 不要以大量請求灌爆系統：「未開放」重試間隔 ≥300ms、最多 2 分鐘；查證重送最多 3 輪。

---

## 9. 現況與待辦

**現況（2026-11-14）**
- 參考實作完成並推到分支；Windows exe 由 GitHub Actions 打包、自我測試通過。
- 雲端排程兩個已啟用（每日 16:48 檢查、每月 8–18 日 08:40 依開放日搶班）。
- 下次開放：**2026-12-13 09:00**（預約 2027 年 1 月），1 月日期系統尚未公布；**1 月願望清單尚未提供**。

**待辦**
1. 第一次正式送出後：記錄 `createBooking` 真實回應時間與所有錯誤訊息文字，修正 §2.2 的分類正則。
2. 實際 Windows 桌面操作驗證（目前只在 Linux＋CI 驗證過）。
3. 多帳號在同一視窗管理（每帳號本人同意）。
4. 內層頁面主機改變時自動提示更新白名單。

# UCC 支援護理人力預班 自動登記工具

自動開啟預班系統 → 選姓名 → 輸入密碼 → 登入 → 勾選指定 **日期 / 院區（林森、昆明、廣慈）/ 班別** → （可選）送出。

> 只用來登記你自己的班。密碼存在本機 `config.json`（已加入 .gitignore），也可以留空，執行時再輸入。

## 安裝（在自己的電腦）

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp config.example.json config.json
```

## 設定 `config.json`

| 欄位 | 說明 |
|---|---|
| `name` | 你的姓名（留空 → 執行時從下拉清單選） |
| `password` | 密碼（留空 → 執行時輸入，不會顯示） |
| `sites` | 要登記的院區，例如 `["林森","昆明"]` |
| `dates` | 日期，可寫區間：`["2026-10-03", "2026-10-10~2026-10-12"]` |
| `shifts` | 班別關鍵字（白班/小夜/大夜），留空 = 不限 |
| `open_time` | 開放時間，配合 `--wait` 使用 |
| `selectors` | 自動偵測失敗時手動填 CSS selector |

## 使用

```bash
# 1. 第一次先檢查網頁結構（輸出 inspect_report.txt + inspect.png）
python ucc_booking.py inspect

# 2. 登入並勾選，停在送出前讓你在瀏覽器確認（建議）
python ucc_booking.py book

# 3. 勾選後自動送出
python ucc_booking.py book --submit

# 4. 等到開放時間（open_time）才開始
python ucc_booking.py book --wait --submit

# 臨時覆寫設定
python ucc_booking.py book --name 王小明 --site 林森 --date 2026-10-03 --date 2026-10-17~2026-10-18
```

## 若自動偵測失敗

1. 執行 `inspect`，打開 `inspect_report.txt` 找姓名、密碼、登入、送出按鈕的 id。
2. 在 `config.json` 的 `selectors` 填入，例如 `"name": "#nameSelect"`、`"login_button": "#btnLogin"`。
3. 若班表的格子結構特殊，把 `inspect_report.txt` 提供給開發者調整比對規則。

出錯時程式會自動輸出 `inspect_report_error.txt` 方便排查。

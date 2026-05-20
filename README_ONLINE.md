# 擺攤收款紀錄：免費線上版

這個資料夾是雲端部署用版本，單機 App 不會被取代。

## 建議免費架構

- Render：放網站
- Supabase：放 PostgreSQL 資料庫
- 瀏覽器登入：使用共用帳號密碼

## 需要設定的環境變數

- `DATABASE_URL`：Supabase PostgreSQL 連線字串
- `APP_USERNAME`：登入帳號，預設建議 `admin`
- `APP_PASSWORD`：登入密碼，請自行設定

## 部署步驟摘要

1. 到 Supabase 建立免費專案。
2. 複製 PostgreSQL connection string，貼到 Render 的 `DATABASE_URL`。
3. 到 Render 建立 Python Web Service。
4. Root Directory 設為 `online`。
5. Build Command：`pip install -r requirements.txt`
6. Start Command：`gunicorn online_app:app`
7. 新增 `APP_USERNAME`、`APP_PASSWORD`、`DATABASE_URL` 三個環境變數。

第一次打開線上網址時，系統會自動建立資料表，並匯入 `seed_products.csv` 裡的商品清單。

## 免費版提醒

免費服務可能休眠或被暫停。活動結束後請到「每日結算」匯出 CSV 備份。

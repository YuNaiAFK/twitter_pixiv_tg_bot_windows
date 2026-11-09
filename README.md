# X／Twitter、Pixiv 與 e-hentai Telegram 機器人（Windows）

傳送作品連結即可取得圖片與影片；也可將本機圖片或壓縮檔整理成 R2 圖片與 Telegraph 頁面。

**Windows 為主專案，Android 為移植版本。** 兩者各自部署，共用功能同步維護。

[Windows 專案](https://github.com/YuNaiAFK/twitter_pixiv_tg_bot_windows) · [Android 專案](https://github.com/YuNaiAFK/twitter_pixiv_tg_bot_android) · [Android 最新版本](https://github.com/YuNaiAFK/twitter_pixiv_tg_bot_android/releases/latest)

## 導覽

- [快速開始](#快速開始)
- [功能與使用](#功能與使用)
- [常用指令與權限](#常用指令與權限)
- [部署、設定與維護](#部署設定與維護)
- [授權與致謝](#授權與致謝)

## 快速開始

需要 **Windows 與 Python 3.11+**。

1. 下載本專案，在專案目錄開啟 CMD，建立 Python 環境：

   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   py -m pip install --upgrade pip
   py -m pip install -r requirements.txt
   py -m pip install -U gallery-dl
   ```

2. 複製 `.env.example` 為 `.env`，填入 bot token 與管理員 ID：

   ```ini
   BOT_TOKEN=<向 @BotFather 取得>
   ADMIN_USER_IDS=<你的數字 ID>
   ```

3. 視需要設定 X／e-hentai cookie、Pixiv refresh token，以及 R2；啟動 `run.bat`，在 Telegram 傳送 `/start`。

[完整安裝、登入憑證與自架 Bot API 設定](docs/setup.md)

## 功能與使用

| 傳送內容 | 結果 | 使用範圍 |
| --- | --- | --- |
| X／Twitter 貼文連結 | 圖片／影片，圖片可組成相簿 | 私聊、已授權群組 |
| Pixiv 作品連結 | 原圖、影片與 GIF 一律以文件傳送 | 私聊、已授權群組 |
| e-hentai／exhentai 畫廊連結 | Telegraph 瀏覽、官方歸檔、重新生成 | 私聊、已授權群組 |
| ZIP／CBZ／7z／RAR 圖片壓縮檔 | 1280px R2 圖片與 Telegraph 頁面 | 僅 bot 管理員；私聊或群組指令 |
| 本機圖片、圖片文件 | 累積後統一製作 Telegraph | 僅 bot 管理員；僅私聊 |

X／Twitter 結果的「原始連結」為可點選文字，網址嵌入其中，不另顯示完整網址。

### 壓縮檔與圖片製作 Telegraph

**僅 bot 管理員可用。** 一般使用者的指令選單與說明不顯示製作指令；傳檔、輸入指令或操作相關按鈕均靜默忽略。一般連結解析權限不變。

1. **私聊**：傳送 ZIP／CBZ／7z／RAR 或圖片文件；多張可連續傳送。
2. **群組**：回覆 ZIP／CBZ／7z／RAR 訊息，輸入 `/publish@機器人名稱`。直接傳檔不會自動開始。
3. 在選單設定「作者連結」「E站連結」，以及「頁面文字（記憶）」「文字連結（記憶）」。群組內請回覆 bot 指定的提示輸入內容。
4. 按「上傳並製作」確認，或使用 `/publish`；不製作可按取消或使用 `/cancel_upload`。群組指令須加上 `@機器人名稱`。

每批作品使用同一則 bot 操作訊息更新接收、設定、進度與結果，完成時原地變成封面圖片。群組設定請回覆該操作訊息。

確認前只暫存檔案，**不會上傳 R2**。相同圖片依內容去重，直接沿用舊網址；失敗會通知並重試，三輪失敗後由發起者決定繼續或停止。

頁面順序為「標題 → 製作日期與自訂連結文字 → 按檔名自然排序的圖片」。完成訊息使用第一張圖片作為封面，附 `[ 即時預覽 ] / [ 作者 ] / [ E站 ]`；作者與 E站未填網址時為普通文字。

頁面文字與連結依使用者保存，私聊、群組及重啟後均可沿用，也能個別清除。標題使用第一個檔名；需要保留名稱與排序時，請以圖片文件或壓縮檔傳送。

[完整流程、記憶設定與檔案限制](docs/usage.md#本機檔案製作-telegraph)

### 管理員確認傳送到頻道

將 bot 設為頻道管理員，開啟「發布訊息」權限。管理員在**私聊解析 X／Twitter**後，會得到傳送頻道或不傳送的選項；**5 分鐘未操作預設不傳送**。

一般使用者照常解析，不顯示按鈕。新解析與快取重送都支援；可用 `/channels` 查詢，或以 `/channels @頻道名稱`、`/channels -100頻道ID` 手動登記。

[頻道設定與傳送規則](docs/usage.md#管理員確認傳送-xtwitter-結果到頻道)

### Pixiv 文件與 GIF

檔名為 `作品名稱_作品ID_p頁碼_畫師ID_畫師暱稱.副檔名`，頁碼由 `p0` 開始。Ugoira 會保留原始尺寸與全部影格轉成 GIF，再以文件傳送。

為讓 Telegram Desktop 顯示 `.gif` 檔名與下載按鈕，GIF 使用 MTProto；需設定 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH` 與 `BOT_TOKEN`。已上傳的 GIF 可沿用文件參照快取，避免重傳整檔。

[檔名、GIF 文件模式與快取說明](docs/usage.md#pixiv-gif-的桌面文件顯示)

## 常用指令與權限

| 指令 | 用途 | 權限 |
| --- | --- | --- |
| `/start`、`/help` | 使用說明 | 任何人 |
| `/id` | 查看數字 ID | 任何人 |
| `/publish` | 製作已接收檔案；群組回覆 ZIP／CBZ／7z／RAR 開始 | bot 管理員 |
| `/cancel_upload` | 取消本機檔案製作 | bot 管理員；該批發起者 |
| `/channels` | 查詢、登記或移除目標頻道 | bot 管理員；僅私聊 |
| `/grant`、`/revoke` | 授權群組／移除授權；帶 ID 管理封鎖名單 | bot 管理員；本群匿名管理員僅能無參數操作本群 |
| `/authorized`、`/blocked` | 查看授權與封鎖狀態 | bot 管理員 |
| `/promote`、`/demote` | 調整 bot 管理員 | bot 管理員 |
| `/lockgroup on`、`/lockgroup off` | 切換群組守門模式 | bot 管理員 |
| `/cookies`、`/status` | 憑證與執行狀態 | bot 管理員 |
| `/cache`、`/cache clear` | 查看／清除解析快取 | bot 管理員 |
| `/audit`、`/restart` | 套件檢查／重啟 | bot 管理員 |

私聊採封鎖名單，群組依群組授權與守門模式處理。群組指令請加 `@機器人名稱`，例如 `/grant@機器人名稱`。成功後只清除 bot 自己的處理提示，保留使用者原訊息。

[匿名管理員與授權說明](docs/usage.md#匿名群組管理員)

## 部署、設定與維護

| 需求 | 文件 |
| --- | --- |
| 安裝、`.env`、憑證、代理及本機 Bot API | [安裝與設定](docs/setup.md) |
| 檔案製作、Pixiv GIF、頻道、群組及匿名管理員 | [使用指南](docs/usage.md) |
| R2 去重與額度、快取、標籤、故障排除 | [維護與疑難排解](docs/maintenance.md) |
| 實作歷史與調查紀錄 | [NOTES.md](docs/NOTES.md) |

請勿公開 `.env`、cookies、`.mtproto/` 工作階段或執行時的個人設定檔。

## 授權與致謝

本專案未宣告整體程式碼授權條款。標籤翻譯資料為衍生作品，適用 **CC BY-NC-SA 3.0**；再利用時請一併確認上游與各相依專案的授權。

[完整授權說明](docs/maintenance.md#授權與免責) · [致謝](docs/maintenance.md#致謝)

### e-hentai 頁面與封面

Telegraph 的文字僅保留原標題與日文標題，不加入標籤或畫廊資料；圖片照常依序排列。Telegram 完成訊息使用實際第一頁大圖作為封面。舊快取頁面下次解析時會原地更新版型，網址不變。

原標題若使 Telegraph 建頁回傳 `PAGE_SAVE_FAILED`，會改以短網址建立後恢復原標題；文章中的原標題、日文標題及圖片不變。

### e-hentai 獨立頻道發布

管理員在私聊使用「Telegraph · 顯示圖」，頁面製作成功且顯示封面後，才會出現傳送至 EH 頻道的選項；5 分鐘未選擇預設不發布。歸檔、重新生成、取消、製作失敗及純文字備援不顯示發布選項，一般使用者也不顯示。

以 `/ehchannels @頻道名稱` 或 `/ehchannels -100頻道ID` 登記目標；`/ehchannels` 查詢，`/ehchannels remove -100頻道ID` 移除。bot 須有該頻道的發布權限。EH 目標清單、確認選項與 Twitter 的 `/channels` 完全分開，不會自動沿用 Twitter 頻道。

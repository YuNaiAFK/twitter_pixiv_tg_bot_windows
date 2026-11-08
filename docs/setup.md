# 安裝與設定（Windows）

[回到 README](../README.md)

Windows 版使用 Python 3.11 以上，以單一行程、polling 方式執行。

- [安裝](#安裝)
- [設定](#設定)
- [設定參考](#設定參考)
- [代理與網路](#代理與網路)
- [自架 Bot API 伺服器](#自架-bot-api-伺服器)

## 安裝

1. 從 <https://www.python.org/downloads/> 安裝 **Python 3.11+**（安裝時勾選加入 PATH）
2. 把 `.env.example` 複製成 `.env`，並把 `BOT_TOKEN` 設成新產生的 token
3. 在這個資料夾開啟 CMD：

   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   py -m pip install --upgrade pip
   py -m pip install -r requirements.txt
   py -m pip install -U gallery-dl
   ```

4. 需要成人限定或需登入的 Pixiv/X 內容時，設定登入 cookie（見[設定](setup.md#設定)）
5. 執行 `run.bat`，然後傳 `/start` 給機器人，確認它活著

**如果 pip 寫不進 venv。** 有些主機會拒絕寫入 `.venv\Lib\site-packages`，pip 在寫 wheel metadata
時就以 `PermissionError` 中斷。機器人還是能跑，只有 Pillow 受影響。改成「解包安裝」：

```bat
.venv\Scripts\python.exe install_pillow_offline.py
```

它會把對應的 wheel 解到 `vendor\`。`bot.py` 只在 Pillow 無法正常匯入時才把 `vendor\` 加進
`sys.path`，`pixiv_login.py` 對 `websockets` 也做一樣的事，所以真的用 pip 裝好時，
永遠是 pip 的版本優先。

## 設定

### 重要 `.env` 設定

```ini
# 必要
BOT_TOKEN=123456:ABC...
ADMIN_USER_IDS=123456789
# 授權
AUTH_FILE=authorized.json
AUTH_GROUP_LOCKED=0        # 1 = 啟動時就鎖住群組
BLOCKED_USER_IDS=          # 空白 = 所有人都能解析
# 上限
MAX_FILES=20
MAX_UPLOAD_MB=45
MAX_DOWNLOAD_SECONDS=180
MAX_CONCURRENT_DOWNLOADS=2
USER_COOLDOWN_SECONDS=10   # 只針對連結
REQUEST_STALE_SECONDS=1800 # 請求卡死時回收名額
# 顯示與連結
ATTACH_SOURCE_LINK=1
SOURCE_LABEL=原始連結
DELETE_STATUS_MESSAGE=1
SUPPORTED_SITES_NOTICE=目前只支援 X/Twitter、Pixiv 與 e-hentai 連結。
REPLY_UNSUPPORTED_LINKS=0  # 0 = 沉默
# 憑證
TWITTER_COOKIES=cookies/twitter.txt
PIXIV_REFRESH_TOKEN=
EHENTAI_COOKIES=cookies/ehentai.txt
EXPIRY_WARN_DAYS=14
# 快取與工作目錄
CACHE_FILE=parse_cache.json
CACHE_DIR=.cache
CACHE_STALE_SECONDS=1209600
CACHE_MAX_ENTRIES=300
WORK_DIR=D:/bot_tmp         # 空白則依序退回 .downloads\ 與系統暫存
# e-hentai
EHENTAI_TIMEOUT=60
EHENTAI_DOWNLOAD_TIMEOUT=900
EHENTAI_DOWNLOAD_WORKERS=6
EHENTAI_VIEWER_WORKERS=6
GALLERY_META_TTL_SECONDS=0  # 0 = 每次都重讀 tag／標題／評分
GALLERY_URL_TTL_SECONDS=7200
# Telegraph
TELEGRAPH_ALLOW_ADULT=1
TELEGRAPH_AUTHOR=e-hentai 解析
# 歸檔
ARCHIVE_CACHE=1
ARCHIVE_CACHE_MAX_BYTES=8589934592   # 8 GiB
ARCHIVE_DEADLINE_SECONDS=300
# /restart
RESTART_STARTUP_DELAY=6    # 替代行程等鎖的上限（秒）
RESTART_LOG_MAX_MB=2       # restart.log 超過就輪替
# 自架 Bot API（選用）
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_API_BASE=
TELEGRAM_LOCAL=
```

完整分類說明見[設定參考](setup.md#設定參考)。

### 登入憑證：`cookies/*.txt`

**cookie 檔要放在 `cookies/` 底下，並以 Netscape 格式存成 `.txt`。** 這個資料夾已被 gitignore，
憑證不會被提交；同一個檔案可以包含多個站點，e-hentai 的請求會依網域過濾後才送出。

這兩個站需要**不同種類**的憑證，這是整個設定過程中最常見的混淆來源：

| 站點 | 需要什麼 | 為什麼 |
| --- | --- | --- |
| X/Twitter | cookie：`auth_token` + `ct0` | 這類客戶端沒有可用的密碼登入 |
| Pixiv | OAuth **refresh token** | 作品頁走 Pixiv 的 App API，cookie 授權不了 |

**X/Twitter cookie。** 用平常的瀏覽器登入 X，以 *Get cookies.txt LOCALLY* 或 *cookies.txt* 這類擴充套件
匯出 **Netscape 格式**（`.txt`，不要 JSON），而且要分別在登入 `x.com` **和** `twitter.com` 的狀態下匯出。
存成 `cookies/twitter.txt`：

```ini
TWITTER_COOKIES=cookies/twitter.txt
```

**Pixiv refresh token。** `PHPSESSID` cookie 對 pixiv.net 自己的網頁端點有效，但對 gallery-dl 使用的
App API **無效**，所以需要 refresh token。授權碼綁在「發出請求的那個行程」的 PKCE `code_verifier` 上，
而那個 verifier 從不離開行程；把 code 複製到另一個行程**永遠**換不到 token，Pixiv 還會用與
「過期 code」相同的 `invalid_request` 回報。請用內建的輔助工具，它讓兩半在同一次執行裡完成：

```bat
.venv\Scripts\python.exe pixiv_login.py --auto
```

`--auto` 會啟動一個用完就丟的 Chrome（獨立設定檔，不會動到你自己的瀏覽器），自動從傳輸中讀到 code，
token 直接寫進 `.env`。偏好 gallery-dl 自己的流程，或 `--auto` 跑不起來時：

```bat
.venv\Scripts\python.exe pixiv_login.py          # 手動貼上
.venv\Scripts\gallery-dl.exe oauth:pixiv         # gallery-dl 的等價流程
```

兩條手動路徑都一樣：**登入前**先按 F12 → Network，登入後複製最後一個 `callback?state=...` 請求的
`code` 參數。code 在登入後 **30 秒**就過期。`accounts.pixiv.net/post-redirect?...code_challenge=...`
這種 URL 是流程的*起點*、不含 code，輔助工具會直接拒絕它並說明原因。
`cookies/pixiv.txt` 對 pixiv.net 的網頁請求仍然有用，`/cookies` 也會檢查它。

**e-hentai cookie。** 成人畫廊需要 session：檔案需要 `ipb_member_id` 與 `ipb_pass_hash`
（exhentai 另外需要 `sk` 與 `igneous`）。`/cookies` 與 `bot.py --check` 都會說缺哪一個。

```ini
EHENTAI_COOKIES=cookies/ehentai.txt
```

**憑證會過期，但三者行為差很多：**

| 憑證 | 有效期 | 過期後 |
| --- | --- | --- |
| X 的 `auth_token` + `ct0` | 約一年 | 所有 X 存取停止 |
| `PIXIV_REFRESH_TOKEN` | **不會過期**（除非被撤銷或改密碼）| — |
| `PHPSESSID` | 約一個月 | 只影響 pixiv.net 的 AJAX 端點 |

機器人會在距到期日少於 `EXPIRY_WARN_DAYS`（預設 14）天時於啟動時警告，管理員的 `/cookies` 隨時顯示
剩餘天數，所以故障不會毫無預警地出現。實測數據與「為什麼沒有自動抓 cookie 的工具」見
[docs/NOTES.md](../docs/NOTES.md)。

### 驗證設定

```bat
.venv\Scripts\python.exe bot.py --check
```

這會回報每個站點的真實狀態 —— 包括只有 cookie 時會說的「Pixiv: 缺少 refresh-token」。
管理員也可以在 Telegram 傳 `/cookies`。加上 `--live` 會對每個站點發出一次真實請求，
證明憑證真的能用，而不只是「還沒過期」：

```bat
.venv\Scripts\python.exe bot.py --check --live
```

```
線上驗證（會向站點發出一次真實請求）：
  [OK] X/Twitter：登入有效（注意：X 可能仍不返回推文內容）
  [OK] Pixiv：PHPSESSID 有效（關注 567，粉絲 2）
  [OK] Pixiv refresh-token：已設定（不過期）
```

> **Windows 上的代理是這裡的常見陷阱。** 這台機器走系統代理，而 `gallery-dl`、`httpx` 與 `urllib`
> 都不讀 Windows 登錄檔裡的代理設定。修法與量測見 [docs/NOTES.md](../docs/NOTES.md)；
> 簡單說：`HTTPS_PROXY` 設了就贏過登錄檔。詳見[代理與網路](setup.md#代理與網路)。

### 工作目錄與 gallery-dl 設定

下載會先暫存在「每次請求一個」的暫存目錄，結束後刪除。挑選順序是：有設 `WORK_DIR` 就用它，
否則用 `bot.py` 旁邊的 `.downloads\`，再否則用系統暫存資料夾。第一個能寫入的勝出，
所以唯讀或空間不足的位置只會降級，而不是讓每個請求都失敗。大型 Pixiv 作品請把 `WORK_DIR`
指到空間充足的磁碟。

`gallery-dl.conf` 會在啟動時產生在 `bot.py` 旁邊。它存在的理由是讓 gallery-dl 的 SQLite 快取落在
`.gallery-dl-cache\` 而不是 `%APPDATA%`：那個設定檔所在的位置若唯讀，gallery-dl 會以
*attempt to write a readonly database* 失敗，兩個站點就一起停擺。這個檔案也會帶上
`PIXIV_REFRESH_TOKEN`。兩個位置都可用 `GALLERY_DL_CACHE` 覆蓋，或用 `GALLERY_DL_CONFIG`
換成整份自訂設定。

## 設定參考

### 必要

| 鍵 | 說明 |
| --- | --- |
| `BOT_TOKEN` | 向 [@BotFather](https://t.me/BotFather) 取得 |
| `ADMIN_USER_IDS` | 數字 id，用逗號分隔。`/id` 取得 |

### 上傳與請求

| 鍵 | 預設 | 說明 |
| --- | --- | --- |
| `MAX_UPLOAD_MB` | `45` | 單檔上限。公開 API 的硬上限是 50 |
| `MAX_FILES` | `20` | 單次解析最多幾個檔案 |
| `MAX_DOWNLOAD_SECONDS` | `180` | 下載逾時 |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | 同時幾個解析 |
| `MAX_CONCURRENT_UPDATES` | `4` | PTB 同時處理幾個 update —— **`停止` 按鈕能不能及時生效就靠這個** |
| `USER_COOLDOWN_SECONDS` | `10` | 每人兩次**連結**之間的最短間隔 |
| `REQUEST_STALE_SECONDS` | `1800` | 卡死的請求何時被回收名額 |

### 快取

| 鍵 | 預設 | 說明 |
| --- | --- | --- |
| `CACHE_FILE` | `parse_cache.json` | 索引 |
| `CACHE_DIR` | `.cache` | 檔案 |
| `CACHE_STALE_SECONDS` | `1209600`（14 天）| 條目過期 |
| `CACHE_MAX_ENTRIES` | `300` | 條目上限 |
| `GALLERY_META_TTL_SECONDS` | **`0`** | **0 = 每次解析都重讀 tag／標題／評分** |
| `GALLERY_URL_TTL_SECONDS` | `7200`（2 小時）| 掃描到的圖片網址多久重用一次 |
| `GALLERY_CACHE` | `1` | 關掉會失去 Telegraph 頁面複用 |
| `ARCHIVE_CACHE` | `1` | 官方 ZIP 快取 |
| `ARCHIVE_CACHE_MAX_BYTES` | `8589934592`（8 GiB）| 歸檔快取上限 |

### e-hentai

| 鍵 | 預設 | 說明 |
| --- | --- | --- |
| `EHENTAI_COOKIES` | `cookies/ehentai.txt` | Netscape 格式 |
| `EHENTAI_TIMEOUT` | `60` | metadata 請求 |
| `EHENTAI_DOWNLOAD_TIMEOUT` | `900` | 下載每一頁 |
| `EHENTAI_VIEWER_WORKERS` | `6` | 掃描頁面時的並行數 |
| `EHENTAI_DOWNLOAD_WORKERS` | `6` | 下載頁面時的並行數 |
| `TELEGRAPH_ALLOW_ADULT` | `1` | 成人畫廊要不要建立頁面 |
| `TELEGRAPH_AUTHOR` | `e-hentai 解析` | 頁面上顯示的作者 |
| `ARCHIVE_DEADLINE_SECONDS` | `300` | 官方歸檔的整體時限 |

### R2（選用）

五個值**必須全部填寫**，否則 R2 不啟用。詳見 [R2 圖片代管](maintenance.md#r2-圖片代管)。

| 鍵 | 說明 |
| --- | --- |
| `R2_ACCOUNT_ID` | 端點固定是 `https://<id>.r2.cloudflarestorage.com` |
| `R2_ACCESS_KEY` / `R2_SECRET_KEY` | API token |
| `R2_BUCKET` | 桶名 |
| `R2_PUBLIC_BASE` | 匿名可讀的網域，必須是 `http://` 或 `https://` 開頭 |
| `R2_FREE_BYTES` / `R2_LIMIT_PERCENT` | 見下方額度說明 |
| `R2_UPLOAD_WORKERS` / `R2_UPLOAD_TIMEOUT` | 預設 6 個 worker、單張 60 秒 |

### 網路

| 鍵 | 預設 | 說明 |
| --- | --- | --- |
| `TELEGRAM_API_BASE` | 空白 | 自架伺服器：`http://127.0.0.1:8081` |
| `TELEGRAM_LOCAL` | 空白 | `1` = 伺服器以 `--local` 執行 |
| `TELEGRAM_MT_PROXY` | 空白 | TDLib 走代理時的上游 SOCKS5 |
| `TELEGRAM_SKIP_LOCAL_API` | 空白 | `1` = `run.bat` 跳過確保伺服器活著的步驟 |
| `WORK_DIR` | 空白 | 下載暫存位置 |
| `GALLERY_DL_CACHE` / `GALLERY_DL_CONFIG` | 空白 | 覆蓋 gallery-dl 的快取與設定位置 |

### 行為

| 鍵 | 預設 | 說明 |
| --- | --- | --- |
| `ATTACH_SOURCE_LINK` | `1` | 附上原始連結 |
| `SOURCE_LABEL` | `原始連結` | 那個前綴的文字 |
| `DELETE_STATUS_MESSAGE` | `1` | 完成後刪掉「正在解析……」那則 |
| `REPLY_UNSUPPORTED_LINKS` | `0` | `0` = 不支援的連結完全靜默 |
| `AUTH_GROUP_LOCKED` | `0` | `1` = 只允許授權過的群組 |
| `EXPIRY_WARN_DAYS` | `14` | 憑證還剩幾天開始警告 |

## 代理與網路

**Windows 上的代理是這裡最常見的陷阱，而且失敗方式每一種都會指錯方向。**

`httpx` 不讀 Windows 的系統代理，所以在這台機器上每個 e-hentai 請求都直接出去然後逾時，
而同一個網址在瀏覽器裡正常。`ehentai.http_client_kwargs()` 會回傳每個 client 都需要的關鍵字參數，
`bot.py` 與 `ehentai.py` 裡**全部九個** httpx client 都用它；有一項特徵化檢查會走過兩個檔案，
任何沒有用它的 `httpx.Client(...)` 都會讓檢查失敗。完整量測見 [docs/NOTES.md](../docs/NOTES.md)。

有效順序是：**環境變數贏過登錄檔。**

```
HTTPS_PROXY / HTTP_PROXY / ALL_PROXY  ->  有值就用
HKCU\...\Internet Settings            ->  否則讀這裡
```

**一個設定好但死掉的代理，比沒有代理更糟** —— 沒有代理會成功，關閉的埠讓每個請求失敗。
`ehentai.proxy_url()` 會先探測 loopback 代理，埠沒開就回退成直連，所以切換梯子模式不會再弄壞
一個能用的網路。遠端代理不探測 —— 那是刻意的選擇，短暫變慢不等於不存在。

> ### ⚠️ 「e-hentai 卡住」幾乎都是這個
>
> 症狀是每個請求都逾時，而同一個網址在瀏覽器裡正常。**先檢查代理，再懷疑站方。**

## 自架 Bot API 伺服器

公開的 Bot API 把機器人的上傳上限壓在 **50 MB**，而且只接受檔案位元組。
自架的 [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) 伺服器把上限拉到 **2000 MB**，
並且在以 `--local` 啟動時接受**本機路徑**而不是位元組：

| | 公開 API | 自架 |
| --- | --- | --- |
| 上傳 | 50 MB | 2000 MB |
| 以本機路徑上傳 | ❌ | ✅（加 `--local`）|
| 下載 | 20 MB | 無限制 |

這就是能讓 **`歸檔 · 原圖`**（148 MB 以上的壓縮檔）真的送達的關鍵。

### 取得 api_id 與 api_hash

這兩個**不是** bot token，而且只有你能建立 —— 它們識別的是「一個應用程式」，Telegram 把它綁在
你的帳號上。

1. 開啟 <https://my.telegram.org>，用**手機號碼**的國際格式登入（例如 `+886912345678`）。
   Telegram 會送登入碼 —— 到 **Telegram 應用程式**裡、來自 *Telegram* 的對話中找（不是簡訊）
2. 進入 **API development tools**（<https://my.telegram.org/apps>），沒有應用程式就填那張短表。
   沒有人會審核，但欄位**會被驗證**，而被拒絕的欄位只會跳出一個籠統的 `ERROR` 對話框，
   所以一次填對比一直重試划算：

   | 欄位 | 規則 | 範例 |
   | --- | --- | --- |
   | **App title** | 最多 64 字元；不能有 `@`、`#` 等符號；避開保留字 *telegram* | `shiroko9008parselinkbot` |
   | **Short name** | **只有小寫字母與數字**，5–32 字元，不能有空格或連字號 | `skr9008plbot` |
   | **URL** | 可以留空 | |
   | **Platform** | 選一個，例如 *Desktop* | |

   **short name 只能小寫**是最容易漏掉的規則：頁面上的提示只寫「英數字符、5–32 字元」，
   **沒提大小寫**，所以 `skr9008PLBot` 看起來合法卻會被拒絕；送出後頁面停在 *Saving…*，
   然後跳出 `my.telegram.org 顯示 ERROR`。那個對話框從不說原因（它把原始 XHR 物件交給 `alert()`），
   要看到真正的錯誤得在瀏覽器主控台重送一次 —— 做法記在 [docs/NOTES.md](../docs/NOTES.md)

3. 送出後會顯示 **App api_id** 與 **App api_hash**（32 個十六進位字元）。寫進 `.env`：

   ```ini
   TELEGRAM_API_ID=1234567
   TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
   ```

4. 檢查格式：`python run_local_api.py --verify-credentials`。**那個檢查只驗證格式** ——
   Telegram 沒有提供「不帶認證就確認 api_id/api_hash」的端點，所以唯一真正的測試是啟動伺服器，
   它會在啟動時認證，錯了就大聲失敗

**把兩個都當成機密。** 持有它們的人可以冒充你的應用程式，而 Telegram 可以限制憑證被濫用的帳號；
**不要**貼進聊天、issue 或 commit —— `.env` 已經在 `.gitignore` 裡。

### 用 Docker 安裝（建議，不需要編譯器）

Windows 沒有官方預先編譯的執行檔，自己編譯需要 C++17 編譯器、CMake、OpenSSL、zlib 與 gperf；
`aiogram/telegram-bot-api` 映像檔把這些全部省掉。先安裝並啟動 Docker Desktop
（Windows 11 上需要 WSL2 或 Hyper-V 後端，也就需要管理員權限），把憑證放進 `.env`，然後：

```sh
docker compose up -d
python run_local_api.py --check      # 確認容器在 8081 上回應
```

接著讓機器人指向它，並用 `run.bat` 重啟：

```ini
TELEGRAM_API_BASE=http://127.0.0.1:8081
TELEGRAM_LOCAL=1
```

`docker-compose.yml` 是提交進版控並加註解的。它設定的環境變數，就是映像檔自己的
`docker-entrypoint.sh` 會轉成參數的那些 —— 特別是：

```sh
append_flag_from_env "TELEGRAM_LOCAL" "--local"
```

所以 `TELEGRAM_LOCAL: "1"` 才會產生 `--local`。少了它，伺服器仍套用公開的 50 MB 上限並拒絕
本機路徑，整個切換等於白做。compose 檔裡 `TELEGRAM_API_ID` 與 `TELEGRAM_API_HASH` 使用 `:?`，
所以憑證少一個會讓 compose 停下來，而不是啟動一個不可能工作的伺服器。

不要 Docker、也不想編譯？把 runner 指向你編好或信任的執行檔
（編譯說明：<https://tdlib.github.io/telegram-bot-api/build.html>）：

```sh
python run_local_api.py --binary "C:\path\to\telegram-bot-api.exe"
```

### run.bat 會先確保伺服器活著

伺服器必須比機器人先起來，而以前沒有任何地方這樣說。少了它，機器人會默默退回公開 API，
上傳上限從 **2000 MB 掉回 50 MB** —— 能送出的東西差一個數量級，而這件事只以一行埋在啟動日誌裡
的訊息呈現。`run.bat` 現在會先呼叫 `run_local_api.py --ensure`，把缺的東西拉起來，
並等到伺服器真的回應為止：

```
檢查自架 Bot API 伺服器……
  啟動轉接（17900 -> 127.0.0.1:7897）……
  轉接埠 17900：已啟動
  Docker 引擎尚未啟動，正在開啟 Docker Desktop.exe……
  等待 Docker 引擎就緒（首次啟動可能需要一兩分鐘）……
  Docker 引擎：就緒
  容器：已在執行
  等待伺服器就緒……
  伺服器（port 8081）：就緒
```

它是**冪等**的：第二次執行會在三者都存在時什麼都不做，不到一秒。
必須有三個東西活著，而每一個壞掉時都會指向別的原因：

1. **Docker 引擎** —— 沒有它，compose 會在具名管線（`dockerDesktopLinuxEngine`）上失敗，
   讀起來像 Docker 安裝有問題，而不是「引擎沒開」。`--ensure` 會自己啟動 Docker Desktop
   並等最多四分鐘
2. **主機轉接** —— 沒有它，容器完全連不到 Telegram，因為代理只聽 loopback；
   TDLib 會把它回報成一般連線失敗。轉接以分離方式啟動，所以比啟動它的控制台活得久
   （實測：殺掉 `run.bat` 之後 17900 仍在監聽）
3. **伺服器** —— 沒有它就是 50 MB 上限

失敗**不是致命的**：機器人仍會對著公開 API 啟動，並以一則警告說明上限與「解釋原因的指令」。
擋住啟動比上傳上限小更糟。`TELEGRAM_SKIP_LOCAL_API=1` 會跳過整個步驟。

### 被過濾的網路：伺服器需要透過代理走 MTProto

這台機器的網路上，Telegram 的 MTProto 主機**部分被過濾**：直接連線時有些位址開、有些被擋，
而**容器直接連線時全部被擋**（Docker 被過濾得更兇）；兩者透過主機的 SOCKS5 就全部可通。
所以未經處理的映像檔起不來 —— 它會啟動、看起來健康，然後卡在 `getMe`、日誌一片空白，
很容易被誤認為 `api_id` 填錯。

`TDLib` **會忽略代理相關的環境變數**，而伺服器自己的 `--proxy` 只給 webhook 用，
所以那個映像檔再怎麼設定都救不回來。`Dockerfile.bot-api-proxy` 包住映像檔並加上唯一有效的做法：
`redsocks` 在本地監聽、把流量轉給上游 SOCKS5，再用 `iptables` NAT 規則把發往 Telegram 位址範圍
的流量整段導過去。TDLib 什麼都不知道，所以也沒有東西可以被它忽略；需要 `NET_ADMIN`，
也是唯一新增的能力。詳情與歷史（Windows bind mount、過期 volume）見 [docs/NOTES.md](../docs/NOTES.md)。

```ini
TELEGRAM_MT_PROXY=socks5://192.168.65.254:17900
```

**主機代理必須能從容器連到**，這是最常讓人卡住的地方。它只綁 `127.0.0.1`，容器用不到，
而 `host.docker.internal` 也沒用 —— 它解析到 Docker VM 的位址，那裡沒有東西在聽。
`host_proxy_relay.py` 負責搭橋（它是 **TCP 轉接，不是代理** —— 客戶端仍自己做 SOCKS5 握手 ——
而且沒有相依套件）：

```sh
python host_proxy_relay.py            # 0.0.0.0:17900 -> 127.0.0.1:7897
python host_proxy_relay.py --selftest # 驗證整條鏈路（在容器裡跑）
```

**還需要一條 inbound 防火牆規則**，因為 Windows 預設會擋掉來自容器的連線。少了它，
轉接在主機上完美運作，容器卻回報連不到：

```powershell
# 以管理員 PowerShell 執行一次：
New-NetFirewallRule -DisplayName "bot-api relay" -Direction Inbound `
  -Protocol TCP -LocalPort 17900 -Action Allow
```

**轉接必須跟機器人一起活著**：伺服器沒有它就連不到 Telegram，所以它屬於啟動流程，
而不是某個終端機視窗。診斷時要分三層、依序做：

```sh
# 1. 轉接在本機通嗎？   （主機）     -> SOCKS5 CONNECT ok
# 2. 容器連得到轉接嗎？ （容器）     -> --selftest 回報 CONNECT ok
# 3. 伺服器登入得了嗎？ （主機）     -> getMe 回 200
```

### `--local` 才是關鍵旗標

沒有它，伺服器仍套用公開的 50 MB 上限，切換等於沒換。`TELEGRAM_LOCAL=1` 是機器人自己對
「伺服器是那樣跑的」的聲明 —— 伺服器不會回報自己的模式，而**假設** `--local` 開著會讓每一次
檔案傳送都失敗，而不是優雅降級。機器人啟動時是**探測**而不是假設：它呼叫 `getMe`，
只有在真的有伺服器回應時才提高上限。所以連不到或設定錯誤的伺服器會讓公開上限繼續生效，
而 `/status` 會回報目前生效的上限與原因。

```ini
TELEGRAM_API_BASE=              # 空白 = 用 api.telegram.org
TELEGRAM_LOCAL=                 # 只有伺服器以 --local 執行時才設 1
```

`run_local_api.py` 是一層薄而可讀的包裝：有 Docker 時優先用容器（不需要編譯器），會讀 `.env`、
檢查憑證、探測 8081 是否已有東西在回應、印出指令列時遮蔽 `api_hash`，並回報缺什麼。
它**不**負責編譯伺服器。


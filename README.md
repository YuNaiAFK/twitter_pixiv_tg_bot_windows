# X/Twitter + Pixiv Telegram 機器人（Windows）

## 專案簡介

在 Telegram 裡貼上一則 **X（Twitter）**、**Pixiv** 或 **e-hentai** 的連結，機器人就把媒體送回同一個聊天室：

- **X/Twitter**：貼文中的圖片與影片，直接回傳到 Telegram。
- **Pixiv**：作品的**原始檔案**，以相簿形式送出——不壓縮、不重新編碼、不縮圖、不打包成 ZIP；多頁作品分成多個相簿。
- **e-hentai / exhentai**：畫廊的分類、語言、評分、頁數，以及依命名空間分組的完整標籤，再加上四個動作按鈕（Telegraph 頁面或 ZIP 歸檔）。

**只支援這三個站點。** 其他網站的連結永遠不會被下載或抓取，所以這個機器人不能被當成通用的連結抓取器。

機器人設計成**公開**的：任何在群組裡看得到它的人都能貼連結、拿到結果。管理機器人是另一回事——只有管理員能碰，而且管理權限刻意與「能不能貼連結」分開。

每個結果都會附上來源連結（`原始連結：<url>`），可用 `ATTACH_SOURCE_LINK=0` 關閉。解析成功時只送出結果，不附加任何說明；失敗時只回一行話，技術細節寫進日誌而不是聊天室。在 Windows 上以單一行程、polling 方式執行，需要 Python 3.11 以上。

### 訊息如何被路由
| 訊息內容 | 機器人做的事 |
| --- | --- |
| `x.com` / `twitter.com` 作品連結 | 下載媒體；圖片以相簿送出 |
| `pixiv.net` 作品連結 | 下載作品，以**原始檔案**相簿送出 |
| `e-hentai.org` / `exhentai.org` 畫廊連結 | 顯示畫廊資訊與標籤，附四個動作按鈕 |
| 其他網站的連結 | 預設**沉默**；`REPLY_UNSUPPORTED_LINKS=1` 時回覆 `SUPPORTED_SITES_NOTICE` |
| `t.me` 邀請／公開連結 | 說明它不會加入，也不會替這類連結分類 |
| 一則訊息含多個連結 | X/Twitter 或 Pixiv 作品連結優先，其次是畫廊連結 |

## 功能

- **只認 X/Twitter、Pixiv 與 e-hentai 連結**，其他站點一律不下載、不抓取。
- **Pixiv 以原始圖檔送出**，多頁作品變成相簿；X/Twitter 的圖片與影片原樣回傳。
- **e-hentai 畫廊變成 Telegraph 文章**：畫廊資料與每個標籤（依命名空間分組），以單一訊息送出，封面就是訊息本體。
- **Telegram 邀請連結**（`t.me/+...`、`t.me/joinchat/...`）會被單獨識別；公開的 `t.me` 連結會辨識但不會被誤標成群組——它可能指向群組、頻道、使用者或機器人。
- 連結冷卻、下載逾時、同時下載數上限、單檔大小上限，以及**一位使用者同時只能有一個請求**（只在真的有請求在跑時才拒絕）。
- 選用的使用者封鎖名單、各站登入 cookie、只有管理員能用的 `/cookies`、`/status`、`/restart`。
- **解析快取**（同一個連結不重新下載）、**平行下載頁面**、百分比進度條、可中止的「停止」按鈕。
- **歸檔改用 e-hentai 官方壓縮檔**、**標籤顯示繁體中文**、**Cloudflare R2 圖片代管**（選用）。
- 成功後自動清掉「正在解析…」提示與帶連結的原始訊息。

### Cloudflare R2 圖片代管與免費額度上限
e-hentai 內嵌的圖片是帶簽章、會過期的連結（`*.hath.network`），一旦過期，Telegraph 頁面上的**每一張圖同時**壞掉。`重新生成` 就是為了繞過這件事而做的：重新掃描、重新簽章、原地改寫頁面。把圖片複製一份到 **Cloudflare R2** 則根本沒有這個問題：物件存在多久，URL 就穩定多久——一個月前解析過的畫廊，也能在完全不碰 e-hentai 的情況下送出。

- **物件鍵是內容雜湊**（`galleries/ab/cd/<sha256>.jpg`，兩層前綴把物件分散到 65536 個前綴）。同張圖上傳兩次就是同一個物件，所以重新解析只補缺的；也因為 URL 不能從畫廊 id 猜到，知道一個路徑無法列舉桶裡還有什麼。Telegraph 是匿名抓圖的，URL 必須公開，雜湊是讓它「公開但不可搜尋」的手段。
- **`Cache-Control: public, max-age=31536000, immutable`**：物件不可變，沒有理由給更短的快取。
- 單張圖片上限 **32 MiB**；平行上傳預設 **6** 個 worker（`R2_UPLOAD_WORKERS`）、單張逾時 **60 秒**（`R2_UPLOAD_TIMEOUT`）。

**免費額度與上限。** Cloudflare 文件上的免費額度是每月 **10 GB** 儲存空間（程式以十進位計算：`10 * 1000**3` 位元組，因為 Cloudflare 以 GB 計費）。實際上傳會停在額度的 **95%**（預設即 **9.50 GB**），可用 `R2_FREE_BYTES` 與 `R2_LIMIT_PERCENT` 調整。留 5% 是必要的，不是保守：儲存按月、依物件存在與否計費，剛好頂到額度、一週後才被清掉的畫廊，那個月仍然超額。

- **上限在 `put` 之前檢查**（`已儲存 + 這張圖 <= 上限`），不是之後——物件一寫入就開始計費。**用量從桶子讀**（`ListObjectsV2`），不靠機器人自己記的數字：本機計數在有人從 Cloudflare 後台刪東西之後就失準，從此不是誤擋就是超額；而列出物件是 class B 操作，免費額度是每月一千萬次，問一下並不貴。
- 額度用完時**停止上傳、通知管理員、其餘圖片沿用原始網址**，結果上會出現「⚠️ R2 免費額度已滿，這次不改用 R2（已通知管理員）。」。這與「上傳失敗」是不同狀態：沒有東西壞掉，只是被刻意擋下。
- **R2 永遠不會讓機器人掛掉。** 沒設定、只設定一半、`boto3` 沒裝、上傳全失敗，任何一種都退回 e-hentai 自己的連結（`r2.py` 因此延後匯入 `boto3`）。已上傳的 R2 網址會依「畫廊 + 模式」記進解析快取，重按同一個按鈕不會再傳一次。

需要的五個設定值：`R2_ACCOUNT_ID`、`R2_ACCESS_KEY`、`R2_SECRET_KEY`、`R2_BUCKET`、`R2_PUBLIC_BASE`（匿名可讀的網域）。端點固定是 `https://<account_id>.r2.cloudflarestorage.com`；`R2_PUBLIC_BASE` 必須是 `http://` 或 `https://` 開頭，否則整組視為未設定。

### 解析快取：同一個連結不解析兩次
解析過的連結由快取供應，重貼一次不花下載、也不花請求。在 5 頁畫廊上實測：**1.93 秒 → 0.02 秒**，因為逐頁掃 viewer 的動作整個被跳過。兩種東西被快取，因為昂貴的工作不一樣：

| 種類 | 快取的內容 | 省下什麼 |
| --- | --- | --- |
| e-hentai | 畫廊資料，以及 viewer 掃描得到的圖片網址 | 掃描是每頁一個請求——53 頁畫廊約需一分鐘 |
| 媒體 | 下載下來的檔案本身（另存一份） | 完全不下載、也不跑 gallery-dl |

Pixiv 與 X 同理。實測：

```
第一次請求    2.09s   Pixiv 解析完成，已傳送原圖 2/2 個檔案。
第二次請求    0.01s   已從快取傳送 2/2 個檔案。
```

- **快取命中不受連結冷卻限制**：冷卻擋的是重複的*下載*工作，而快取命中沒有下載；套用冷卻只會讓一個已經在磁碟上的答案白等十秒。沒有任何快取時，冷卻照舊。
- **快取命中也會清理聊天室**，跟真的下載一樣：進度提示與連結訊息都被刪掉。少了這段清理，快取反而留下更多雜訊。
- 送出時使用**暫存目錄**，絕不直接用快取目錄：超大圖片會先寫一份縮圖，指向快取目錄就會覆蓋正在被快取的檔案。
- **失敗的結果刻意不快取**：失敗通常是暫時的，或是一組管理員後來修好的憑證；快取它會讓一次壞運氣變成永久。
- **刻意不使用 `gallery-dl --download-archive`**：它與「每次請求用全新的暫存目錄」衝突——archive 說「下載過了」，gallery-dl 於是跳過，而每個請求都要新目錄，結果是重貼時什麼都沒寫，機器人卻回報「沒有找到可傳送的圖片或影片」。重複使用上一次的解析結果是快取的工作；快取沒中時，重新下載才是正確行為。
- **快取的圖片網址還是會過期**：e-hentai 的圖片連結有時效，所以快取的掃描結果只能撐一陣子，過期就自己失效；`重新生成 Telegraph 頁面` 是重新整理這種頁面的正式做法，它會刻意忽略快取，並把新的網址寫回去。

```ini
CACHE_FILE=parse_cache.json
CACHE_DIR=.cache
CACHE_STALE_SECONDS=1209600   # 14 天
CACHE_MAX_ENTRIES=300
GALLERY_URL_TTL_SECONDS=7200  # 2 小時：快取的網址清單可信多久
```

快取在兩個維度都有上限，並優先清掉最舊的項目（連同它的檔案一起刪——只丟索引會漏出無人引用的磁碟空間）。損壞的快取檔會被丟棄而不是致命錯誤，因為快取只是最佳化，失去它只損失時間。**只存在一部分**的檔案集會被視為沒有快取，因為送出半個作品比重新下載更糟。`/cache` 顯示內容、命中／未命中次數與磁碟用量，`/cache clear` 清空，兩者都只有管理員能用。

### 為什麼 Pixiv 是以「檔案」而不是「照片」送出
Telegram 的照片路徑沒有辦法原樣送出一張圖：`sendPhoto` 會在伺服器端重新編碼並改名成 `.jpg`，所以「照片」永遠不是原檔。Pixiv 作品因此以**文件**送出，Telegram 會逐位元組原樣傳遞，原始檔名（`150653396_p0.jpg`）也保留下來。

- 圖片會以檔案而不是內嵌照片出現。點開來看，手機上可用「儲存到相簿」，桌機則直接下載。
- 多頁作品以最多 10 個為一組，所以 30 頁的作品會是三個相簿，而不是三十則訊息。
- 超過 `MAX_UPLOAD_MB` 的檔案會被回報並跳過，而不是悄悄縮小——重點是送出原檔，不是替代品。
- **X/Twitter 仍走一般照片路徑**，因為貼文本來就預期是壓縮過的預覽。

### 清理聊天室
請求成功後，機器人會刪掉它自己製造的雜訊：「正在解析…」提示，以及帶著連結的**原始訊息**。兩者都由 `.env` 控制，設成 `0` 就保留：

```ini
DELETE_STATUS_MESSAGE=1
DELETE_SOURCE_MESSAGE=1
```

**在群組裡刪掉別人的訊息，需要機器人是管理員並擁有「刪除訊息」權限。** 沒有這個權限時 Telegram 會拒絕刪除，機器人只記一筆警告，然後只刪掉自己的提示——請求本身仍然成功，解析結果不受影響。私人聊天不需要任何特殊權限。請求**失敗**時什麼都不刪：那時狀態訊息是使用者唯一的解釋，來源連結是他唯一的線索。

## 安裝 / 設定

1. 從 <https://www.python.org/downloads/> 安裝 **Python 3.11+**（安裝時勾選加入 PATH）。
2. 把 `.env.example` 複製成 `.env`，並把 `BOT_TOKEN` 設成新產生的 token。
3. 在這個資料夾開啟 CMD，執行：

   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   py -m pip install --upgrade pip
   py -m pip install -r requirements.txt
   py -m pip install -U gallery-dl
   ```

4. 需要成人限定或需登入的 Pixiv/X 內容時，設定登入 cookie（見下方 `cookies/*.txt`）。
5. 執行 `run.bat`，然後傳 `/start` 給機器人，確認它活著。

**如果 pip 寫不進 venv。** 有些主機會拒絕寫入 `.venv\Lib\site-packages`，pip 在寫 wheel metadata 時就以 `PermissionError` 中斷。機器人還是能跑，只有 Pillow 受影響。改成「解包安裝」：

```bat
.venv\Scripts\python.exe install_pillow_offline.py
```

它會把對應的 wheel 解到 `vendor\`。`bot.py` 只在 Pillow 無法正常匯入時才把 `vendor\` 加進 `sys.path`，`pixiv_login.py` 對 `websockets` 也做一樣的事，所以真的用 pip 裝好時，永遠是 pip 的版本優先。

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
DELETE_SOURCE_MESSAGE=1
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

### 登入憑證：`cookies/*.txt`
**cookie 檔要放在 `cookies/` 底下，並以 Netscape 格式存成 `.txt`。** 這個資料夾已被 gitignore，憑證不會被提交；同一個檔案可以包含多個站點，e-hentai 的請求會依網域過濾後才送出。

這兩個站需要**不同種類**的憑證，這是整個設定過程中最常見的混淆來源：

| 站點 | 需要什麼 | 為什麼 |
| --- | --- | --- |
| X/Twitter | cookie：`auth_token` + `ct0` | 這類客戶端沒有可用的密碼登入 |
| Pixiv | OAuth **refresh token** | 作品頁走 Pixiv 的 App API，cookie 授權不了 |

**X/Twitter cookie。** 用平常的瀏覽器登入 X，以 *Get cookies.txt LOCALLY* 或 *cookies.txt* 這類擴充套件匯出 **Netscape 格式**（`.txt`，不要 JSON），而且要分別在登入 `x.com` **和** `twitter.com` 的狀態下匯出。存成 `cookies/twitter.txt`，並讓 `.env` 指向它：

```ini
TWITTER_COOKIES=cookies/twitter.txt
```

**Pixiv refresh token。** `PHPSESSID` cookie 對 pixiv.net 自己的網頁端點有效，但對 gallery-dl 使用的 App API **無效**，所以需要 refresh token。授權碼綁在「發出請求的那個行程」的 PKCE `code_verifier` 上，而那個 verifier 從不離開行程；把 code 複製到另一個行程**永遠**換不到 token，Pixiv 還會用與「過期 code」相同的 `invalid_request` 回報。請用內建的輔助工具，它讓兩半在同一次執行裡完成：

```bat
.venv\Scripts\python.exe pixiv_login.py --auto
```

`--auto` 會啟動一個用完就丟的 Chrome（獨立設定檔，不會動到你自己的瀏覽器），自動從傳輸中讀到 code，token 直接寫進 `.env`。偏好 gallery-dl 自己的流程，或 `--auto` 跑不起來時：

```bat
.venv\Scripts\python.exe pixiv_login.py          # 手動貼上
.venv\Scripts\gallery-dl.exe oauth:pixiv         # gallery-dl 的等價流程
```

兩條手動路徑都一樣：**登入前**先按 F12 → Network，登入後複製最後一個 `callback?state=...` 請求的 `code` 參數。code 在登入後 **30 秒**就過期。`accounts.pixiv.net/post-redirect?...code_challenge=...` 這種 URL 是流程的*起點*、不含 code，輔助工具會直接拒絕它並說明原因。`cookies/pixiv.txt` 對 pixiv.net 的網頁請求仍然有用，`/cookies` 也會檢查它。

**e-hentai cookie。** 成人畫廊需要 session：檔案需要 `ipb_member_id` 與 `ipb_pass_hash`（exhentai 另外需要 `sk` 與 `igneous`）。`/cookies` 與 `bot.py --check` 都會說缺哪一個。

```ini
EHENTAI_COOKIES=cookies/ehentai.txt
```

**憑證會過期，但三者行為差很多**：X 的 `auth_token` + `ct0` 約一年，過期後所有 X 存取停止；Pixiv 的 `PIXIV_REFRESH_TOKEN` **不會過期**（除非被撤銷或改密碼）；`PHPSESSID` 約一個月，只影響 pixiv.net 的 AJAX 端點。機器人會在距到期日少於 `EXPIRY_WARN_DAYS`（預設 14）天時於啟動時警告，管理員的 `/cookies` 隨時顯示剩餘天數，所以故障不會毫無預警地出現。實測數據與「為什麼沒有自動抓 cookie 的工具」見 [docs/NOTES.md](docs/NOTES.md)。

### 驗證設定
```bat
.venv\Scripts\python.exe bot.py --check
```

這會回報每個站點的真實狀態——包括只有 cookie 時會說的「Pixiv: 缺少 refresh-token」。管理員也可以在 Telegram 傳 `/cookies`。加上 `--live` 會對每個站點發出一次真實請求，證明憑證真的能用，而不只是「還沒過期」：

```bat
.venv\Scripts\python.exe bot.py --check --live
```

```
線上驗證（會向站點發出一次真實請求）：
  [OK] X/Twitter：登入有效（注意：X 可能仍不返回推文內容）
  [OK] Pixiv：PHPSESSID 有效（關注 567，粉絲 2）
  [OK] Pixiv refresh-token：已設定（不過期）
```

> **Windows 上的代理是這裡的常見陷阱。** 這台機器走系統代理，而 `gallery-dl`、`httpx` 與 `urllib` 都不讀 Windows 登錄檔裡的代理設定。修法與量測見 [docs/NOTES.md](docs/NOTES.md)；簡單說：`HTTPS_PROXY` 設了就贏過登錄檔。

### 工作目錄與 gallery-dl 設定
下載會先暫存在「每次請求一個」的暫存目錄，結束後刪除。挑選順序是：有設 `WORK_DIR` 就用它，否則用 `bot.py` 旁邊的 `.downloads\`，再否則用系統暫存資料夾。第一個能寫入的勝出，所以唯讀或空間不足的位置只會降級，而不是讓每個請求都失敗。大型 Pixiv 作品請把 `WORK_DIR` 指到空間充足的磁碟。

`gallery-dl.conf` 會在啟動時產生在 `bot.py` 旁邊。它存在的理由是讓 gallery-dl 的 SQLite 快取落在 `.gallery-dl-cache\` 而不是 `%APPDATA%`：那個設定檔所在的位置若唯讀，gallery-dl 會以 *attempt to write a readonly database* 失敗，兩個站點就一起停擺。這個檔案也會帶上 `PIXIV_REFRESH_TOKEN`。兩個位置都可用 `GALLERY_DL_CACHE` 覆蓋，或用 `GALLERY_DL_CONFIG` 換成整份自訂設定。

## 使用方式

### 指令與權限
| 指令 | 誰能用 | 說明 |
| --- | --- | --- |
| `/start`、`/help` | 任何人 | 只有用法說明，不含部署細節 |
| `/id` | 任何人 | 顯示自己的 ID |
| `/authorized` | 管理員或授權使用者 | 目前的名單、模式，以及正在使用的檔案 |
| `/grant <ID>` | 管理員或授權使用者 | 私人聊天：解除封鎖某人。群組中不帶 ID：授權**該群組** |
| `/revoke <ID>` | 管理員或授權使用者 | 私人聊天：封鎖某人。群組中不帶 ID：移除**該群組**的授權 |
| `/blocked` | 管理員或授權使用者 | 列出被封鎖的使用者 |
| `/promote <ID>` | **僅管理員** | 把某人升為管理員 |
| `/demote <ID>` | **僅管理員** | 移除管理員權限 |
| `/lockgroup on\|off` | **僅管理員** | 群組模式的開關 |
| `/cookies` | **僅管理員** | 憑證檔、到期日、錯誤字串 |
| `/status` | **僅管理員** | 各項上限、授權狀態、執行期設定 |
| `/cache` | **僅管理員** | 解析快取內容；`/cache clear` 清空 |
| `/restart` | **僅管理員** | 用一份全新的行程取代正在跑的這一份 |

**解析對所有人開放。** 私人聊天用的是**封鎖名單**：名單為空代表任何人都能解析，把某人加進去只是擋掉他，不會給他原本沒有的東西。群組則刻意維持**允許名單**：公開群組裡的機器人應該只服務「被刻意加進去」的群組，而不是每一個剛好把它拉進去的群組。

| 名單 | 空白 | 有名單 |
| --- | --- | --- |
| **私人聊天**（封鎖名單） | 所有人都能解析 | 名單上的人被拒絕，其他人歡迎 |
| **群組**（允許名單） | 由 `AUTH_GROUP_LOCKED` 決定：全開或全關 | 只有名單上的群組 |

**管理員在兩種模式下都能通行**，所以名單寫錯也不會把操作者鎖在自己的機器人外面。私人聊天不受群組名單與群組模式影響，群組也不受封鎖名單影響——私人聊天沒有群組能替它發言，所以套用使用者規則。`/lock` 已經退役（封鎖名單不是一道閘門，沒有「模式」可選），但仍註冊著並會回答「它被什麼取代」，因為筆記裡還留著 `/lock` 的人一定會輸入它。

**授權整個群組：** 在群組裡打 `/grant`、不帶參數，那個群組就被授權了：其中每個成員都能解析，不必逐一授權。這是實務上的用法，因為聊天 ID 用手找很麻煩。

```
1. /lockgroup on          → 守門模式；目前沒有任何群組能解析
2. /grant   （在群組裡）  → 該群組成為唯一能解析的群組
3. 其他任何群組           → 仍然被拒絕
```

`/lockgroup on` 在群組名單為空時是**允許**的：它會先擋住所有群組，直到有人被授權為止，回覆也會這樣說。先守門、再到群組裡 `/grant`，是自然的順序；擋掉它反而會擋掉讓這個功能可用的那套流程。管理員隨時可以在私人聊天裡用 `/lockgroup off` 復原，所以沒有任何回不去的狀態。在群組裡 `/revoke` 會移除該群組，名單變空時回覆會提醒「所有群組又都歡迎了」。

三件值得知道的事：**在群組裡管理名單只有管理員能做**（群組授權是在群組裡決定的，如果任何授權使用者都能改，被授權群組裡的任何成員都能改名單）；**管理員到處通行**，包括不在名單上的群組；**私人聊天與群組的規則互不影響**。

**第一個管理員只能來自 `.env`**，因為那時沒有人能授權它：

```ini
ADMIN_USER_IDS=123456789
```

之後重啟一次，其餘都在 Telegram 裡用 `/promote` 完成——這就是這個介面的意義：改授權不該需要碰主機。傳 `/id` 可得知自己的數字 ID；**只接受數字 ID，永不接受 `@username`**，因為使用者名稱可以被改掉、被別人重新註冊，記在使用者名稱上的授權遲早會指向另一個人。**`ADMIN_USER_IDS` 空白不代表「所有人都是管理員」**，而是管理指令對所有人關閉並告訴擁有者怎麼開啟；在公開機器人上，相反的預設會把 `/cookies` 交給任何陌生人。

名單的變更寫進 `bot.py` 旁邊的 `authorized.json`，重啟後仍然有效，也包含群組聊天 ID（負數，超級群組是 `-100…` 的形式）。`.env` 提供起始值，檔案保存指令改過的部分。檔案損壞或讀不到時會退回 `.env` 的值並在日誌說明，而不是崩潰或默默放寬權限；改動名單的指令也會在聊天裡回報寫入失敗，所以「重啟後會消失的變更」不會看起來像有效的變更。這個檔案已被 gitignore（裡面是真實的使用者 ID）。管理指令不會出現在公開的 Telegram 指令選單裡，管理員與授權使用者則會拿到依自己聊天室範圍的選單。

### 按鈕
畫廊結果上的行內按鈕才是實際操作的地方：

| 按鈕 | 做什麼 |
| --- | --- |
| `Telegraph · 顯示圖` | 發佈一個嵌入站方 1280px 圖片的 Telegraph 頁面，什麼都不送進聊天室 |
| `Telegraph · 原圖` | 發佈嵌入全解析度圖片的 Telegraph 頁面 |
| `歸檔 · 1280` | 下載 1280px 版本，打包成一個 ZIP 送出 |
| `歸檔 · 原圖` | 下載全解析度版本，打包成一個 ZIP 送出 |
| `重新生成 Telegraph 頁面` | 重新取得簽章網址並原地更新頁面 |
| `停止` | 中止進行中的下載 |

按下按鈕之前什麼都不會下載，所以「只是好奇」的連結只花一次 metadata 請求。

### 在 Telegram 群組裡使用
- 把機器人加進群組。
- 如果機器人需要看到一般的群組訊息，用 `@BotFather` → `/setprivacy` → 選擇機器人 → `Disable`，然後把機器人移除再重新加入群組。
- 或者在隱私模式允許的範圍內，由使用者提及／回覆機器人。
- 除非真的需要管理權限，不要把它設成群組管理員。

**在隱私模式關閉的部署上（`can_read_all_group_messages: True`），Telegram 會送來每一則群組訊息**，包含完全沒有東西可解析的那些。兩條規則讓這件事不至於變成噪音：

- **沒有連結的訊息永不回覆**，無條件。它以前會在「看起來」被叫到時回覆說明文字——例如被 `@` 提及，或有人回覆它自己的訊息——結果被回報成「機器人被別人觸發」：有人回覆機器人先前貼的照片、寫了一句「如此巨乳！」，機器人就用使用說明回答。任何這類猜測都是對意圖的賭注，而猜錯比沉默更糟；`/help` 明確涵蓋了合法情境，而且不會誤觸。
- **授權檢查在找到連結之後才做。** 以前是先檢查，於是未授權使用者的每一句話都會收到「此機器人目前不允許你的帳號使用」，連「i send no link」這種話也一樣。這就是調整順序要修的誤判。

被拒絕的使用者永遠到不了快取、下載器或 Telegraph：閘門在它們全部之前，回歸測試也會斷言被拒絕的使用者根本不會觸發查詢。**被拒絕的群組則是完全沉默，而不是回一句拒絕**：拒絕訊息在私人聊天裡是有用的答案，在群組裡恰恰相反——它會在別人的對話裡對每一則連結出聲，還向沒在問它的人宣傳自己的存在。這個守衛是 `handle_message` 的第一件事，所以被拒絕的群組連冷卻提示、不支援連結提示、失敗訊息都到不了。

### 請求、冷卻與失敗回覆
一位使用者同時只能有**一個**請求在跑。第二個會被拒絕並說明現在的情況，第一個一結束就立刻接受。這取代了過去對所有東西（包含按鈕）一律套用的 10 秒冷卻：那個版本會在有人剛完成一個動作、馬上按下第二個時跳出「操作太快了」——明明沒有東西在跑，剩餘冷卻還在倒數——於是它拖慢了真正的工作，卻沒有增加任何保護。「距離上次請求多久」不是重點，「現在有沒有工作在跑」才是。連結保留自己的冷卻，因為貼連結會啟動真正的下載工作，重複張貼才是濫用情境；按鈕不受此限制。

```ini
USER_COOLDOWN_SECONDS=10   # 只針對連結
REQUEST_STALE_SECONDS=1800 # 請求沒釋放名額就卡死時，回收它
```

名額一定會被釋放（包含工作拋出例外），而卡死上限意味著任何當機都不會永久鎖住一位使用者。

**失敗回覆只有一行**，技術診斷——憑證狀態、`bot.py --check --live` 指令列、例外類型——**寫進日誌而不是聊天室**：

```
取不到連結內容，可能是平台限制，請稍後再試。

原始連結：https://x.com/...
```

以前這些會在使用者要求時附加在回覆裡，結果操作者在對話中間看到一整段 shell 指令。資訊沒有消失：它在主控台日誌與 `restart.log` 裡，`/cookies` 也能隨時回報憑證狀態。`@BotFather` 的隱私模式步驟刻意**不出現在任何聊天回覆裡**，因為那是部署細節。

**不支援的連結預設沉默**（`REPLY_UNSUPPORTED_LINKS=0`）：公開群組裡對每一則無法解析的連結都回話，只是在沒被邀請的對話裡製造噪音。想換成別的訊息，或想在看連結是真的不支援、還是被誤判時打開它：

```ini
SUPPORTED_SITES_NOTICE=目前只支援 X/Twitter、Pixiv 與 e-hentai 連結。
REPLY_UNSUPPORTED_LINKS=1
```

沉默之所以安全，是因為**分類照樣會跑**：不是貼文的 X 連結仍會在啟動下載器之前被辨識並丟掉，否則「沉默」會變成等三分鐘然後什麼都沒聽到。

### /restart
不需要碰主機就能重啟，這正是重點：否則改 `.env` 或換憑證檔都得去碰機器。它只給管理員用，因為它會讓機器人短暫離線。

1. 啟動 `bot.py` 的第二份副本，**分離**執行，不屬於啟動目前這份的控制台。副本會先等幾秒才開始 polling，免得跟它要取代的行程搶 Telegram 的 `getUpdates` 鎖。
2. 回覆新行程的 PID 與日誌路徑。
3. 短暫延遲後停止 polling，讓那則回覆先送達 Telegram，也讓進行中的更新跑完。

新副本會寫入 `bot.py` 旁邊的 `restart.log`，機器人沒回來就讀那個檔。啟動失敗會在聊天裡回報，而且**目前這個行程會繼續跑**——一場沒發生的重啟，不能看起來像發生過。

```ini
RESTART_STARTUP_DELAY=6      # 替代行程開始 polling 前等待的秒數（上限）
RESTART_LOG_MAX_MB=2         # restart.log 超過這個大小就輪替
```

實作上踩到的四個失敗模式（`stop_running()` 在錯的執行緒被呼叫、兩個 poller 搶同一個 token、子行程繼承 stdout 管線導致啟動器永遠等不到 EOF、`restart.log` 用系統代碼頁寫成亂碼），以及「重啟後沒有視窗」、「每次啟動都等六秒」兩個症狀，完整記錄在 [docs/NOTES.md](docs/NOTES.md)。

## e-hentai 畫廊 → Telegraph

貼上像 `https://e-hentai.org/g/3672745/82d9d6d00e/` 這樣的連結，機器人會回覆畫廊資料與依命名空間分組的每個標籤（Language、Artist、Group、Parody、Character、Female、Male 等等），加上上面那四個動作按鈕（`Telegraph · 顯示圖`、`Telegraph · 原圖`、`歸檔 · 1280`、`歸檔 · 原圖`）。按鈕按下之前什麼都不下載，所以只是好奇的連結只花一次 metadata 請求。

一次 HTTP GET 就能取回標題（英文與日文）、分類、語言、評分、頁數、大小、日期、收藏數、封面與完整標籤清單，約 **1.4 秒**。這是在 53 頁畫廊上實測各種替代方案之後選的：`gallery-dl -j` 要 **275 秒**（加上 `metadata: true` 也要 **265 秒**），而它的 `source: metadata` 模式雖然只要 1.4 秒，卻**沒有**分類名稱、沒有語言、標籤還是扁平的 `namespace:tag` 字串。完整量測表在 [docs/NOTES.md](docs/NOTES.md)。

解析用的正規表示式每一個都對真實的 HTML 驗證過，因為「看起來很明顯」的模式會默默比對到錯的東西：封面藏在行內 background style 裡（頁面第一個 `<img>` 是評分圖），而 `<div id="taglist">` 並沒有直接包含那些 `<tr>` 列。

### 一次請求一則訊息，封面就在裡面
畫廊封面會變成回覆的本體，結果是**一則訊息**：封面照片，畫廊資料、標籤與頁面連結都是它的說明文字。做法是**編輯機器人已經送出的那則訊息**，而不是再貼一則——把封面另外貼出去會讓每個回覆都變成兩則訊息，讀起來像機器人無緣無故重複自己。

圖片以**網址**引用，所以是 Telegram 自己去抓，機器人不為它下載任何位元組。它用的是 e-hentai 的靜態縮圖主機（`ehgt.org`），而不是頁面圖片那種有時效的 `hath.network` 連結，所以貼在聊天室裡的封面會一直有效。縮圖不見了不算錯誤：同一則訊息會退回純文字，請求照樣繼續——為了一張封面而拒絕送出整個畫廊，是錯的取捨。兩種情況下都會附上 `重新生成` 按鈕，因為 `edit_media` 換掉訊息本體時會把它弄掉。

**只有 X/Twitter 會帶來源連結，Pixiv 只送圖片。** 這是明確要求的，而且是每次呼叫的選項（`attach_link`）而不是全域設定，因為兩個平台的設定不同。連結放在第一張的說明文字，所以會顯示在圖片上方，而且只替第一張加說明：Telegram 不接受媒體群組裡每一張都有說明，而且本來就只需要一個連結。**位置由 Telegram 決定，不是我們**——相簿的說明文字是客戶端「對著媒體」畫的，早先把連結掛在後面幾張時，它會落在整組中間（比較與取捨見 NOTES）。

**Telegraph 頁面不會重複封面**：頁面是標題、資料、依命名空間分組的標籤，然後是頁面圖片。封面被略過——它已經在聊天訊息裡，放在頁面頂端只是花掉真正頁面需要的內容額度。

### 標籤顯示繁體中文
標籤名稱以繁體中文（台灣）呈現，旁邊保留原始英文，所以遇到不熟的翻譯也不會遺失資訊：

```
• 女性 / Female：眼罩（eyepatch）, 哥德蘿莉（gothic lolita）, 雙馬尾（twintails）
• 其他 / Other：差分集（variant set）
```

專有名稱——作者、社團、角色與原作名——沒有中文形式，所以直接顯示而不重複：`momozu komamochi`，不是 `momozu komamochi（momozu komamochi）`。

對照表在 `tag_translations.py`，是提交進版控的資料（約 380 筆），不是執行時抓的，所以畫一個畫廊永遠不需要第二個網路服務。要擴充就在那裡加一筆；不認得的標籤會原樣通過，而不是消失。e-hentai 會替某些標籤加上 wiki 註記（以前會顯示成 `japanese | definition revised please read the wiki`）——那是站方樣板而不是標籤的一部分，所以在顯示與翻譯之前就被移除。

### 進度與「停止」
兩個慢階段都會在執行中更新狀態訊息，附長條圖、百分比與頁數：

```
正在下載 5 頁圖片……
██░░░░░░░░ 20% (1/5)
（可按「停止」中止）
```

實測 5 頁下載，每完成一頁更新一次：

```
  5.5s  ██░░░░░░░░ 20% (1/5)
 11.7s  ████░░░░░░ 40% (2/5)
 15.6s  ██████░░░░ 60% (3/5)
 21.1s  ████████░░ 80% (4/5)
 27.2s  ██████████ 100% (5/5)
```

收集階段——每頁讀一個 viewer 頁面——有自己的進度條，因為在 53 頁的畫廊上光這一步就要約一分鐘。更新被節流成每 1.5 秒一次編輯，因為 Telegram 對訊息編輯有速率限制；最後狀態一定會寫入，所以訊息永遠不會停在 99%。

下載期間，進度訊息上會有**停止**按鈕。下載一個大畫廊的每一頁原圖要幾分鐘、搬幾百 MB，所以一定要能取消：

- 停止會殺掉 gallery-dl 子行程並回報「已停止下載」，不是失敗。（被殺的行程以非零結束，所以取消旗標要在結束碼之前檢查，否則刻意的停止會變成一個訊息空白的下載錯誤。）
- 在壓縮檔建好之前取消，就不會送出壓縮檔。打包*之後*才停止會留下不完整的壓縮檔，所以請在下載階段停止，而不是傳送途中。
- 註冊表以聊天室為鍵，所以在一個聊天室按停止不會殺掉另一個聊天室的下載；部分檔案留在該請求的暫存目錄裡，跟著它一起刪除。

停止有兩種機制，因為下載有兩種形狀：gallery-dl（子行程，殺掉）與平行抓取（**本行程內的執行緒**，沒有行程可殺，所以用旗標）。平行抓取是每個模式的正常路徑，包含 `歸檔`；worker 在每次請求前後各檢查一次旗標，停止之後才到的結果會被丟棄而不是寫入。

### 歸檔用 e-hentai 的官方壓縮檔
`歸檔` 不再自己建壓縮檔，而是向 e-hentai 的 archiver 要它自己發佈的 ZIP。這既更準確、也少做很多事：**完全不下載任何一頁**——一個 POST 取回壓縮檔網址，一個串流的 GET 送出檔案，檔名是 e-hentai 自己的（`(C106) [circle] title [中国翻訳]-1280x.zip`）。

| 步驟 | 請求 | 結果 |
| --- | --- | --- |
| 1 | `POST archiver.php`，帶 `dltype=org` 或 `res` | 一個說「檔案正在準備」的頁面 |
| 2 | GET 其中 `hath.network/archive/...` 的連結 | **HTML**，不是檔案——一個等待頁 |
| 3 | 同一網址加上 **`?start=1`** | ZIP，並由 `Content-Disposition` 命名 |

第 2 步是陷阱：那個網址回的是 HTML，把它當成檔案下載會得到 16 位元組的「檔案」。用範圍請求（`Range: bytes=0-3`）讀出真正的檔名與總大小，不必拉下整個壓縮檔。**同一頁上的 H@H 下載器刻意不用**——它需要一個正在跑的 H@H 客戶端，否則只會回答 *"Your H@H client appears to be offline."*。

| 選項 | `dltype` | 實測大小 | Telegram |
| --- | --- | --- | --- |
| `歸檔 · 原圖` | `org` | **148.2 MiB** | ❌ 超過 50 MB 的機器人上限 |
| `歸檔 · 1280` | `res` | **3.2 MiB** | ✅ |

大小會在嘗試前先回報，而且仍然會試著上傳，所以答案是 Telegram 給的，而不是本機自己拒絕：

```
官方歸檔 148.2 MB，超過 Telegram 的 45MB 建議上限，仍嘗試傳送……
```

上傳被拒絕時，回覆會說明並指向 `歸檔 · 1280`（降取樣版本，通常裝得下）。壓縮檔是**串流寫到磁碟**，絕不整份放進記憶體：原圖常常超過 100 MB，先讀進記憶體再交給 Telegram 既浪費又可能耗盡；同一個 `停止` 按鈕也能中止傳輸。官方檔名帶著畫廊標題，裡面會有 Windows 路徑不允許的字元（`:`、`?`、`|`），所以使用前會消毒：路徑分隔符被中和、保留裝置名（`CON`、`NUL`、`COM1`）被避開、過長的名字被縮短。

### ⚠️ Telegraph 圖片連結會過期
Telegraph 是靠**去抓圖片的網址**來嵌入圖片，而 e-hentai 提供的網址（`*.hath.network`）是**有時效、且綁定登入 session** 的。所以今天建好的 Telegraph 頁面，過幾天可能就圖片全壞。標籤與畫廊資料那幾段是靜態 HTML，永遠正確。

要永久保存就用歸檔動作（那些送的是真正的檔案），或者在設定好 R2 的情況下改用 R2——物件網址不會過期。要重建頁面，`重新生成 Telegraph 頁面` 按鈕會替同一個畫廊取得重新簽章的網址、發佈新頁面並原地取代舊訊息；按鈕依聊天室與使用者記住，所以一個人不能重建別人的頁面，更早的 session 留下來的過期按鈕會被拒絕，而不是用猜的。

**為什麼不乾脆把圖片永久代管**——實測結果：`catbox.moe` 回 `412 Invalid uploader`、`0x0.st` 已停用上傳、`uguu.se` 3 小時後刪檔、`tmpfiles.org` 1 小時。這裡沒有免費的永久圖片代管；如果你有付費或自架服務的 API 金鑰（imgbb、Cloudflare R2、自己的伺服器），可以把圖片傳上去並永久嵌入——本專案內建的 R2 支援就是這條路。

### 公開與成人畫廊
e-hentai 會用分類標記每個畫廊，例如 `Non-H`、`Manga`、`Doujinshi`。公開畫廊（`Non-H`、`Artbook`）不需要登入；成人畫廊需要 session，而沒有 session 時，站方的回答與「畫廊已被刪除」一模一樣——這就是為什麼機器人說「可能需要登入」，而不是說「找不到」。

```ini
EHENTAI_COOKIES=cookies/ehentai.txt
EHENTAI_TIMEOUT=60           # metadata 請求
EHENTAI_DOWNLOAD_TIMEOUT=900 # 下載每一頁
```

**Telegraph 與成人內容。** Telegraph 的條款禁止成人素材，所以替 H 畫廊建的頁面**可能被移除**。預設仍然會建，因為那是被要求的行為，但這個選擇是明確的：

```ini
TELEGRAPH_ALLOW_ADULT=1   # 1 = 永遠建頁面
                          # 0 = 成人畫廊只回文字資訊
```

每個頁面都用一個匿名 Telegraph 帳號，所以頁面彼此獨立：一個被移除，其他不受影響。

## 自助架設的 Bot API 伺服器

公開的 Bot API 把機器人的上傳上限壓在 **50 MB**，而且只接受檔案位元組。自架的 [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) 伺服器把上限拉到 **2000 MB**，並且在以 `--local` 啟動時接受**本機路徑**而不是位元組：

| | 公開 API | 自架 |
| --- | --- | --- |
| 上傳 | 50 MB | 2000 MB |
| 以本機路徑上傳 | ❌ | ✅（加 `--local`） |
| 下載 | 20 MB | 無限制 |

這就是能讓 **`歸檔 · 原圖`**（148 MB 以上的壓縮檔）真的送達的關鍵。

### 取得 api_id 與 api_hash
這兩個**不是** bot token，而且只有你能建立——它們識別的是「一個應用程式」，Telegram 把它綁在你的帳號上。

1. 開啟 <https://my.telegram.org>，用**手機號碼**的國際格式登入（例如 `+886912345678`）。Telegram 會送登入碼——到 **Telegram 應用程式**裡、來自 *Telegram* 的對話中找（不是簡訊）。
2. 進入 **API development tools**（<https://my.telegram.org/apps>），沒有應用程式就填那張短表。沒有人會審核，但欄位**會被驗證**，而被拒絕的欄位只會跳出一個籠統的 `ERROR` 對話框，所以一次填對比一直重試划算：

   | 欄位 | 規則 | 範例 |
   | --- | --- | --- |
   | **App title** | 最多 64 字元；不能有 `@`、`#` 等符號；避開保留字 *telegram* | `shiroko9008parselinkbot` |
   | **Short name** | **只有小寫字母與數字**，5–32 字元，不能有空格或連字號 | `skr9008plbot` |
   | **URL** | 可以留空 | |
   | **Platform** | 選一個，例如 *Desktop* | |

   **short name 只能小寫**是最容易漏掉的規則：頁面上的提示只寫「英數字、5–32 字元」，**沒提大小寫**，所以 `skr9008PLBot` 看起來合法卻會被拒絕；送出後頁面停在 *Saving…*，然後跳出 `my.telegram.org 顯示 ERROR`。那個對話框從不說原因（它把原始 XHR 物件交給 `alert()`），要看到真正的錯誤得在瀏覽器主控台重送一次——做法記在 [docs/NOTES.md](docs/NOTES.md)。

3. 送出後會顯示 **App api_id**（例如 `1234567`）與 **App api_hash**（32 個十六進位字元，例如 `0123456789abcdef0123456789abcdef`）。寫進 `.env`：

```ini
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
```

4. 檢查格式：`python run_local_api.py --verify-credentials`。**那個檢查只驗證格式**——Telegram 沒有提供「不帶認證就確認 api_id/api_hash」的端點，所以唯一真正的測試是啟動伺服器，它會在啟動時認證，錯了就大聲失敗：

```sh
docker compose up -d && docker compose logs -f
```

把兩個都當成機密。持有它們的人可以冒充你的應用程式，而 Telegram 可以限制憑證被濫用的帳號；**不要**貼進聊天、issue 或 commit——`.env` 已經在 `.gitignore` 裡。

### 用 Docker 安裝（建議，不需要編譯器）
Windows 沒有官方預先編譯的執行檔，自己編譯需要 C++17 編譯器、CMake、OpenSSL、zlib 與 gperf；`aiogram/telegram-bot-api` 映像檔把這些全部省掉。先安裝並啟動 Docker Desktop（Windows 11 上需要 WSL2 或 Hyper-V 後端，也就需要管理員權限），把憑證放進 `.env`，然後：

```sh
docker compose up -d
python run_local_api.py --check      # 確認容器在 8081 上回應
```

接著讓機器人指向它，並用 `run.bat` 重啟：

```ini
TELEGRAM_API_BASE=http://127.0.0.1:8081
TELEGRAM_LOCAL=1
```

`docker-compose.yml` 是提交進版控並加註解的。它設定的環境變數，就是映像檔自己的 `docker-entrypoint.sh` 會轉成參數的那些——特別是：

```sh
append_flag_from_env "TELEGRAM_LOCAL" "--local"
```

所以 `TELEGRAM_LOCAL: "1"` 才會產生 `--local`。少了它，伺服器仍套用公開的 50 MB 上限並拒絕本機路徑，整個切換等於白做。compose 檔裡 `TELEGRAM_API_ID` 與 `TELEGRAM_API_HASH` 使用 `:?`，所以憑證少一個會讓 compose 停下來，而不是啟動一個不可能工作的伺服器。

不要 Docker、也不想編譯？把 runner 指向你編好或信任的執行檔（編譯說明：<https://tdlib.github.io/telegram-bot-api/build.html>）：

```sh
python run_local_api.py --binary "C:\path\to\telegram-bot-api.exe"
```

### run.bat 會先確保伺服器活著
伺服器必須比機器人先起來，而以前沒有任何地方這樣說。少了它，機器人會默默退回公開 API，上傳上限從 **2000 MB 掉回 50 MB**——能送出的東西差一個數量級，而這件事只以一行埋在啟動日誌裡的訊息呈現。`run.bat` 現在會先呼叫 `run_local_api.py --ensure`，把缺的東西拉起來，並等到伺服器真的回應為止：

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

它是**冪等**的：第二次執行會在三者都存在時什麼都不做，不到一秒（「伺服器（port 8081）：已在執行，不需要啟動」「轉接埠 17900：已在執行」）。必須有三個東西活著，而每一個壞掉時都會指向別的原因：

1. **Docker 引擎**——沒有它，compose 會在具名管線（`dockerDesktopLinuxEngine`）上失敗，讀起來像 Docker 安裝有問題，而不是「引擎沒開」。`--ensure` 會自己啟動 Docker Desktop 並等最多四分鐘。
2. **主機轉接**——沒有它，容器完全連不到 Telegram，因為代理只聽 loopback；TDLib 會把它回報成一般連線失敗。轉接以分離方式啟動，所以比啟動它的控制台活得久（實測：殺掉 `run.bat` 之後 17900 仍在監聽）。
3. **伺服器**——沒有它就是 50 MB 上限。

失敗**不是致命的**：機器人仍會對著公開 API 啟動，並以一則警告說明上限與「解釋原因的指令」。擋住啟動比上傳上限小更糟。`TELEGRAM_SKIP_LOCAL_API=1` 會跳過整個步驟，給沒有 Docker、也不想要 Docker 的機器用。

### 被過濾的網路：伺服器需要透過代理走 MTProto
這台機器的網路上，Telegram 的 MTProto 主機**部分被過濾**：直接連線時有些位址開、有些被擋，而**容器直接連線時全部被擋**（Docker 被過濾得更兇）；兩者透過主機的 SOCKS5 就全部可通。所以未經處理的映像檔起不來——它會啟動、看起來健康，然後卡在 `getMe`、日誌一片空白，很容易被誤認為 `api_id` 填錯。

`TDLib` **會忽略代理相關的環境變數**，而伺服器自己的 `--proxy` 只給 webhook 用，所以那個映像檔再怎麼設定都救不回來。`Dockerfile.bot-api-proxy` 包住映像檔並加上唯一有效的做法：`redsocks` 在本地監聽、把流量轉給上游 SOCKS5，再用 `iptables` NAT 規則把發往 Telegram 位址範圍的流量整段導過去。TDLib 什麼都不知道，所以也沒有東西可以被它忽略；需要 `NET_ADMIN`，也是唯一新增的能力。詳情與歷史（Windows bind mount、過期 volume）見 [docs/NOTES.md](docs/NOTES.md)。

```ini
TELEGRAM_MT_PROXY=socks5://192.168.65.254:17900
```

**主機代理必須能從容器連到**，這是最常讓人卡住的地方。它只綁 `127.0.0.1`，容器用不到，而 `host.docker.internal` 也沒用——它解析到 Docker VM 的位址，那裡沒有東西在聽。`host_proxy_relay.py` 負責搭橋（它是 **TCP 轉接，不是代理**——客戶端仍自己做 SOCKS5 握手——而且沒有相依套件）：

```sh
python host_proxy_relay.py            # 0.0.0.0:17900 -> 127.0.0.1:7897
python host_proxy_relay.py --selftest # 驗證整條鏈路（在容器裡跑）
```

**還需要一條 inbound 防火牆規則**，因為 Windows 預設會擋掉來自容器的連線。少了它，轉接在主機上完美運作，容器卻回報連不到：

```powershell
# 以管理員 PowerShell 執行一次：
New-NetFirewallRule -DisplayName "bot-api relay" -Direction Inbound `
  -Protocol TCP -LocalPort 17900 -Action Allow
```

**轉接必須跟機器人一起活著**：伺服器沒有它就連不到 Telegram，所以它屬於啟動流程，而不是某個終端機視窗。診斷時要分三層、依序做：

```sh
# 1. 轉接在本機通嗎？   （主機）     -> SOCKS5 CONNECT ok
# 2. 容器連得到轉接嗎？ （容器）     -> --selftest 回報 CONNECT ok
# 3. 伺服器登入得了嗎？ （主機）     -> getMe 回 200
```

### `--local` 才是關鍵旗標
沒有它，伺服器仍套用公開的 50 MB 上限，切換等於沒換。`TELEGRAM_LOCAL=1` 是機器人自己對「伺服器是那樣跑的」的聲明——伺服器不會回報自己的模式，而**假設** `--local` 開著會讓每一次檔案傳送都失敗，而不是優雅降級。機器人啟動時是**探測**而不是假設：它呼叫 `getMe`，只有在真的有伺服器回應時才提高上限。所以連不到或設定錯誤的伺服器會讓公開上限繼續生效，而 `/status` 會回報目前生效的上限與原因。

```ini
TELEGRAM_API_BASE=              # 空白 = 用 api.telegram.org
TELEGRAM_LOCAL=                 # 只有伺服器以 --local 執行時才設 1
```

`run_local_api.py` 是一層薄而可讀的包裝：有 Docker 時優先用容器（不需要編譯器），會讀 `.env`、檢查憑證、探測 8081 是否已有東西在回應、印出指令列時遮蔽 `api_hash`，並回報缺什麼。它**不**負責編譯伺服器。

## 常見問題 / 已知限制

### X/Twitter 常常沒有結果
反覆驗證過，包含對一則長期存在的公開貼文（NASA 的推文），以及 X 自己未認證的 syndication 端點——後者回 HTTP 200 但內容是空的。cookie 沒問題：是 **X 對這類客戶端不給推文內容**。這是 X 端的限制，上游追蹤在 [mikf/gallery-dl#9267](https://github.com/mikf/gallery-dl/issues/9267)，不是這裡設定錯了。

- 用 `bot.py --check` 確認憑證；如果那裡已經顯示「已登入」，重匯 cookie 不會有幫助。
- 試 `python -m pip install -U gallery-dl`——上游的更新偶爾會繞過 X 的改動。
- Pixiv 不受影響。

機器人會明確回報這個情況，而不是聲稱登入失敗。

### e-hentai 好像「卡住」
**這是代理問題，不是 e-hentai 的錯。** `httpx` 不讀 Windows 的系統代理，所以在這台機器上每個 e-hentai 請求都直接出去然後逾時，而同一個網址在瀏覽器裡正常。`ehentai.http_client_kwargs()` 會回傳每個 client 都需要的關鍵字參數，`bot.py` 與 `ehentai.py` 裡**全部九個** httpx client 都用它；有一項特徵化檢查會走過兩個檔案，任何沒有用它的 `httpx.Client(...)` 都會讓檢查失敗。完整量測見 [docs/NOTES.md](docs/NOTES.md)。

### 上傳超過 50 MB
`歸檔 · 原圖` 常見是 148 MB 以上，公開 API 收不下。改用 `歸檔 · 1280`（約 3.2 MiB），或架自架 Bot API 伺服器把上限提到 2000 MB（見上一節）。

### 封面不見了，或 `重新生成` 失敗過
exhentai 畫廊的封面放在 `s.exhentai.org`，那個主機需要 session，而 Telegram 抓圖時不帶 cookie，所以會被拒絕；程式會把這個主機改寫成公開的 `ehgt.org`（路徑完全相同），未知主機則拒絕改寫。另一方面，`edit_message_to_cover` 會把結果訊息變成封面**照片**，而 Telegram 拒絕對媒體訊息做 `edit_message_text`；現在 `update_result_message()` 會看訊息本身：有 `message.photo` 就 `edit_caption`，否則 `edit_text`。

### 「Key missing, or incorrect key provided.」
e-hentai 有時候用 **HTTP 200** 加一段 39 位元組的內容回答。**狀態碼檢查看不到它，因為它是 200**；程式靠內容辨識，並回報為「存取被拒絕」，而不是「連結有誤或畫廊已被刪除」——後者會讓使用者去找一個不存在的問題。`EHENTAI_COOKIES` 對這種情況有用：當原因是 session 相關時，換一份新 cookie 可以解掉。重試沒有幫助，因為那是一個經過考慮的回答，不是斷線。

### Windows 每次都跳「開啟檔案 - 安全性警告」
雙擊 `run.bat`（或它的捷徑）會問「你要開啟此檔案嗎？」並標示「未知的發行者」。**原因不是 zone mark**（實測不存在），而是專案資料夾帶著可繼承的 **Low 完整性標籤**，Windows 因此把它們當成不可信。以**管理員** PowerShell 修一次：

```powershell
icacls "D:\TG_Bot\twitter_pixiv_tg_bot_windows" /setintegritylevel m /t /c
```

要還原就 `/setintegritylevel l /t /c`。沒有提權的嘗試只會改到一部分檔案（實測 3703 個中的 139 個），其餘回報 `Access is denied`，所以一定要提權。完整的調查（SmartScreen、AppLocker、`Zone.Identifier`、火絨安全軟體）在 [docs/NOTES.md](docs/NOTES.md)。

### `停止` 按鈕沒有反應過
PTB 預設**一次只處理一個 update**，而按鈕按一下就是一個 update。長時間下載佔住那個位置時，按鍵根本不會被派送，所以 `停止` 一直到下載結束、旗標被清掉之後才生效。現在用 `concurrent_updates(MAX_CONCURRENT_UPDATES)`，順帶解掉一個阻斷服務問題：一個 15 分鐘的請求以前會凍住**所有**其他使用者。重試的錯誤設計（等待 semaphore 反而讓同一個按鈕再次失效）記在 NOTES。

### 其他已知限制
- 公開的 `t.me/<name>` 網址無法只靠網址可靠地分類成群組、頻道、使用者或機器人。
- 機器人不會加入或驗證 Telegram 邀請連結。
- 只支援 X/Twitter 與 Pixiv 的作品連結，以及 e-hentai 的畫廊連結；其他站點不抓取。
- 站方改版或認證變更都可能影響 gallery-dl。
- 目前的範例用 polling 與暫存目錄，是單一行程的原型。
- 對外大規模使用之前，需要更強的濫用防護與監控。

### 安全性
- 在聊天裡曝露的 token 要立刻用 `@BotFather` 的 `/revoke` 換掉；新 token 只放 `.env`。
- 不要分享 `.env` 或 cookie。`.env` 與 `cookies/` 都被 gitignore，請保持這樣。
- 公開使用時把 `BLOCKED_USER_IDS` 留空；要限制就把 ID 填進去。`/id` 會顯示數字的使用者與聊天 ID。
- 對外公開之前先設好 `ADMIN_USER_IDS`；否則 `/cookies` 與 `/status` 就是不可用，而那正是安全的預設。
- 只會抓 X/Twitter、Pixiv 與 e-hentai 的網址，所以訪客無法用機器人去碰任意位址。
- 只下載或轉發你有權存取的內容；對外大規模使用前請先強化濫用防護。

### 指令列小工具
`bot.py` 啟動機器人；`bot.py --check` 印出設定、憑證、gallery-dl 與 Pillow 狀態；`bot.py --check --live` 再加上每站一次真實的認證請求；`bot.py --dry-run` 建好所有 handler 後結束、不連 Telegram；`bot.py --pixiv-token <url|code>` 從貼上的 callback URL 取出 OAuth `code`；`pixiv_login.py [--auto]` 完成 Pixiv OAuth 流程並把 refresh token 寫進 `.env`；`install_pillow_offline.py` 在 pip 裝不了時把 Pillow 裝進 `vendor/`；`test_bot_features.py` 是路由、憑證、送出與清理的自測。

## 致謝 / 授權

這個機器人站在一整排開源專案上：

| 專案／服務 | 用途 |
| --- | --- |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | Telegram Bot API 的 Python 封裝 |
| [gallery-dl](https://github.com/mikf/gallery-dl) | X/Twitter 與 Pixiv 的下載引擎 |
| [httpx](https://www.python-httpx.org/) | 所有對外 HTTP 請求 |
| [Pillow](https://python-pillow.org/) | 超大照片的縮圖處理 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 讀取 `.env` |
| [websockets](https://github.com/python-websockets/websockets) | `pixiv_login.py --auto` 讀 Chrome DevTools |
| [boto3](https://github.com/boto/boto3) | Cloudflare R2 的 S3 用戶端（選用） |
| [telegram-bot-api / TDLib](https://github.com/tdlib/telegram-bot-api) | 自架 Bot API 伺服器 |
| [Cloudflare R2](https://developers.cloudflare.com/r2/) | 不會過期的圖片代管（選用） |
| [e-hentai](https://e-hentai.org/)／[exhentai](https://exhentai.org/) | 畫廊資料、圖片與官方壓縮檔 |
| [Telegraph](https://telegra.ph/) | 畫廊頁面的發佈平台 |

另外感謝 [mikf/gallery-dl#9267](https://github.com/mikf/gallery-dl/issues/9267) 的追蹤紀錄，以及 `tag_translations.py` 裡那份標籤對照表（約 380 筆）。程式碼在 <https://github.com/YuNaiAFK/twitter_pixiv_tg_bot_windows>。

**授權：** 本儲存庫目前沒有 `LICENSE` 檔案，README 也沒有聲明授權條款；在再利用之前請先向作者確認。

**免責：** 請尊重各站的服務條款與著作權（下載權限見上面的安全性一節），成人內容的發佈——尤其是 Telegraph 頁面——可能違反平台政策而被移除。實作歷史、疑難雜症與安全稽核紀錄都在 [docs/NOTES.md](docs/NOTES.md)。

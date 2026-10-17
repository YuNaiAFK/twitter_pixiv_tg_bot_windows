# 技術筆記：實作歷史、疑難雜症與稽核紀錄

這份文件收錄從 [README](../README.md) 移出的深度技術紀錄：設計取捨、踩過的坑、量測數據、安全稽核，以及「為什麼最後是這樣寫」。**它不是使用說明**——要安裝、要指令、要設定，請看 README。

內容大致分成四類：

- **除錯考古**：重新啟動、代理、快取、停止按鈕、Windows 完整性標籤。
- **設計取捨**：來源連結放哪裡、為什麼不用 `--download-archive`、為什麼私有清單是封鎖名單。
- **安全事件**：四次稽核的結果、被劫持的機器人描述、必須輪替的 token。
- **基礎設施**：git 與推送、自架 Bot API 伺服器、被過濾網路上的 MTProto。

---

## 重新啟動（`/restart`）的實作與踩過的坑

`/restart` 的目的是**不需要碰主機就能重啟**：否則改 `.env` 或換憑證檔都得去碰機器。它只給管理員用，因為它會讓機器人短暫離線。

它做的事：

1. 啟動 `bot.py` 的第二份副本，**分離**執行，不屬於啟動目前這份的控制台。副本會先等幾秒才開始 polling，免得跟它要取代的行程搶 Telegram 的 `getUpdates` 鎖。
2. 回覆新行程的 PID 與日誌路徑。
3. 短暫延遲後停止 polling，讓那則回覆先送達 Telegram，也讓進行中的更新跑完。

新副本寫入 `bot.py` 旁邊的 `restart.log`。啟動失敗會在聊天裡回報，而且**目前這個行程會繼續跑**——一場沒發生的重啟，不能看起來像發生過。

```ini
RESTART_STARTUP_DELAY=6      # 替代行程開始 polling 前等待的秒數（上限）
RESTART_LOG_MAX_MB=2         # restart.log 超過這個大小就輪替
```

### 四個失敗模式，全部修掉，全部值得記住

- **`stop_running()` 在錯的執行緒被呼叫，所以什麼都沒做。** 第一版在工作執行緒上跑關閉流程，但 `Application.stop_running()` 的實作是 `asyncio.get_running_loop().stop()`——工作執行緒沒有 running loop，於是丟出 `RuntimeError`，舊行程繼續 polling。替代行程接著以 *Conflict: terminated by other getUpdates request* 死亡，機器人就**離線**了。現在這個請求用 `call_soon_threadsafe` 交回事件迴圈。例外也不再被默默吞掉，因為一個裸 `except` 正是這件事當初難以看見的唯一原因。
- **同一個 token 上兩個 poller 對新的那個是致命的。** Telegram 對第二個 `getUpdates` 回 *Conflict: terminated by other getUpdates request*。即使關閉流程修好了，替代行程仍可能在舊行程釋放鎖之前就開始 polling，所以替代行程現在**先等再開始 polling**（`RESTART_STARTUP_DELAY`）。這是把競爭條件移除，而不是賭新行程能撐過去。
- **替代行程繼承了父行程的 stdout 管線。** Windows 的預設是 `close_fds=False`，所以新行程握有每一個可繼承的控制代碼——包括啟動機器人的那個東西的 stdout 管線。任何擷取該管線的啟動器（`subprocess.run(..., capture_output=True)`、服務包裝、CI 步驟）就會永遠阻塞，等一個在機器人活著時不可能到來的 EOF。現在 spawn 會傳 `close_fds=True`。
- **`restart.log` 用機器的代碼頁寫入**，所以回覆叫操作者去讀的那個檔案是亂碼。現在 spawn 會強制 `PYTHONIOENCODING=utf-8`，而且檔案超過 `RESTART_LOG_MAX_MB` 就輪替，偶爾重啟的機器人才不會把它養成無限大。

### 為什麼用這組 Windows 旗標

`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` 而不是只用 `DETACHED_PROCESS`：完全沒有控制台時，新行程裡任何意外的 `print` 都會在二進位檔案控制代碼上失敗；`CREATE_NO_WINDOW` 則給它一個只是不顯示的控制台。

（後來這個組合又改過一次，見下面「重啟後沒有視窗」——`/restart` 的替代行程改成 `CREATE_NEW_CONSOLE`，因為操作者需要一個看得到的視窗。）

### 真實行程測試

`test_bot_features.py` 的第 1ab 節有一個真的行程測試：它用同一份 `schedule_restart` 啟動子行程，並要求子行程**自己結束**、有替代行程被 spawn、而且替代行程開始 polling 時沒有 `Conflict`。一場沒有留下任何機器人的重啟才是真正要命的失敗，而只有真的行程能抓到它。

### 症狀一：重啟後沒有視窗，而且每次啟動都等六秒

兩個症狀，**同一個根因**，所以寫在一起。

`RESTART_STARTUP_DELAY` 是文件化的 `.env` 值，而它同時被當成「我是不是替代行程？」的旗標。於是 `load_dotenv()` 替**每一次**啟動都設了它，結果：

1. **每一次**啟動都自認是替代行程，`run.bat` 在做事之前先印出 `等待 6.0 秒讓前一個程序結束後再開始輪詢……`；
2. 為第二個實例加的拒絕守衛被**跳過**，因為「替代行程」是允許繼續的——於是兩個視窗可以同時 poll 同一個 token。

改法是另外一個只有 spawner 會設的標記：

```
已啟動 → BOT_IS_REPLACEMENT=1 → /restart 的替代程序 → 等待（上限 6s）
run.bat  → 沒有這個變數        → 一般啟動        → 完全不等
```

**實測：一般啟動現在 1.81 秒就到「等待 Telegram 訊息」，那行六秒的訊息消失了。**

### 症狀一（續）：那個空白視窗

`/restart` 以前用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` spawn 替代行程——那是一個**完全沒有控制台**的行程，只寫 `restart.log`。畫面上看到的視窗是 `run.bat` 的，所以舊行程一結束，**那個視窗就關了，而且沒有東西接手**——機器人還在跑，但沒有視窗可以看。你看到的是標題為解譯器路徑、內容全空的控制台。

現在替代行程有自己的控制台：

```
CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP
```

所以重啟之後恰好只有**一個**視窗，而且就是活的那個。它的輸出進到那個視窗，也同時附加到 `restart.log`（子行程安裝了檔案 handler），因為重啟確認訊息就是叫操作者去讀那個檔。`CREATE_NO_WINDOW` 原本是為了「意外的 `print` 不能失敗」；`CREATE_NEW_CONSOLE` 同樣滿足這點，因為控制台反正存在。

### 症狀一（續）：第二個視窗現在會被拒絕

拿不到單一實例鎖的一般啟動會**停下來**，而不是繼續 polling：

```
已經有另一個機器人正在執行，這次啟動會結束。
請關閉另一個視窗後再試一次（或直接使用那一個）。
```

*替代行程*仍然會在沒有鎖的情況下繼續，因為拒絕它會讓機器人離線——那比可能發生的 Conflict 更糟。

### 症狀二：什麼都沒在跑，重啟也睡六秒

```
等待 6.0 秒讓前一個程序結束後再開始輪詢……
```

這行有真實的理由：被取代的行程需要一點時間釋放 Telegram 的 `getUpdates` 鎖，而同一個 token 上的第二個 poller 會得到 *Conflict: terminated by other getUpdates request*——那就是「重啟完成但沒有機器人在跑」的成因。但這個等待是**無條件**的，所以每次重啟都付滿六秒，**即使舊行程早已結束**（而那是常態）。

真正該問的問題是「上一個實例還在嗎？」，而作業系統就能回答，並在該行程死亡時**無論怎麼死**都釋放鎖：

| 上一個行程 | 舊 | 新 |
| --- | --- | --- |
| 已經不在了（常態） | 6.000s | **~0.00s** |
| 還在關閉中 | 6.000s | 只等必要的時間，上限仍是 6s |
| 被殺、沒機會清理 | 6.000s | 鎖仍由作業系統釋放 |

實測：在舊行程還在退出時啟動替代行程，**1.2 秒**就緒而不是固定 6 秒；沒有任何東西持有鎖時，**0.001 秒**就拿到。

`RESTART_STARTUP_DELAY` 現在是**上限，不是延遲**：它限制替代行程等鎖多久，預設仍是 6 秒，所以沒有任何行為比之前差。從 `run.bat` 的一般啟動根本不設它，也永遠不等。鎖另外在 `finally` 裡明確釋放，所以操作者停止機器人後立刻重啟，不必等作業系統回收舊行程。

---

## 版面配置：來源連結放在哪裡

**只有 X/Twitter 帶來源連結，Pixiv 只送圖片。** 這是明確要求的，而且是**每次呼叫**的選項（`attach_link`）而不是全域設定，因為兩個平台的設定不同。

X/Twitter 的連結是**第一張的說明文字**，所以會顯示在圖片上方：

```
[ 原始連結：https://x.com/pohitashi/status/... ]   ← 第 0 張的說明文字
[ 相簿：page0 · page1 · page2 ]
```

Pixiv 完全沒有說明文字：

```
[ 相簿：page0 · page1 · page2 ]
```

試過的做法與取捨：

| 做法 | 連結落在哪 | 成本 |
| --- | --- | --- |
| 第一張加說明文字 | **圖片上方** | 一則訊息 |
| 另送一則訊息 | 相簿上方 | **兩則**訊息 |
| 只用 url 按鈕 | 圖片下方 | 一則訊息 |
| 說明文字**加上**按鈕 | 上方一次、下方再一次 | 一則訊息，**連結出現兩次** |

**url 按鈕已經不再附加。** 它是在「連結只能是對著媒體畫的說明文字」的時期引入的，所以扮演「保證可觸及的副本」。當連結就在一則本身會通過清理的訊息之說明文字裡時，一個指向同一網址的按鈕就是**把連結印第二次**——被回報為多餘。`build_source_keyboard` 被保留並註明刻意未使用，因為那是那個鍵盤唯一存在的地方；如果哪天發現某個客戶端會把說明文字渲染到觸及不到的位置，重新接上應該是一行改動。**在沒有移除說明文字連結的情況下**重新接上，就會恢復重複。

**只有第一張有說明文字**：Telegram 不接受每一張都有說明文字的媒體群組，而且本來就只需要一個連結。**位置由 Telegram 決定，不是我們**——相簿的說明文字是客戶端*對著媒體*畫的，這就是早先把它掛在後面幾張時，連結會落在整組中間的原因。把它錨在第一張，是從這邊能做到的最好結果。說明文字只加在**第一個相簿**上，所以 30 頁的作品不會在三則訊息上都帶同一個連結。

**快取重播遵循同一套分割**：`send_cached_media` 傳 `attach_link=(platform != "pixiv")`，所以重貼一個快取的 Pixiv 連結不會長出原送沒有的連結。

**Telegraph 頁面不重複封面**：頁面是標題、資料、依命名空間分組的標籤，然後是頁面圖片。封面被略過——它已經在聊天訊息裡，放在頁面頂端只是花掉真正頁面需要的內容額度。

---

## 陷阱：相簿項目需要的是檔案物件，不是路徑

`send_media_album` 建構媒體群組時，每一項都必須傳一個**開啟的檔案物件**。`str` 會被 Telegram 讀成 URL 或 file_id，所以一個 Windows 路徑會變成 `file:///C:/...`，整個相簿就被拒絕：

```
BadRequest: Invalid file http url specified: unsupported url protocol
```

呼叫端接著退回「每頁一則訊息」，看起來完全像**相簿功能不存在**。一個只檢查元素型別名稱的測試完全看不見這件事，所以測試現在會斷言每個元素都是*已上傳的檔案*（帶內容的 `InputFile`）、帶真正的檔名、而且不是路徑或 `file://` URL。

這些串流是延遲開啟、在 `finally` 裡關閉的，所以中途失敗不會在呼叫端即將刪除的檔案上漏掉控制代碼。

## 陷阱：gallery-dl 的 `--` 終止符

`build_gallery_dl_command` 先寫選項、再寫 `-- <url>`。終止符能擋掉「以破折號開頭的 URL 被讀成選項」，但它同時意味著**它之後什麼都不能再加**。放在那裡的選項會被解析成第二個 URL：

```
[2/3] --download-archive
[gallery-dl][error] Unsupported URL '--download-archive'
```

那一個錯誤**同時弄壞了每一個 Pixiv 與 X 下載**，而憑證檢查仍回報一切健康——因為憑證真的健康，是**指令**壞了。回歸測試現在會斷言終止符恰好出現一次、而且它後面只有 URL，每個平台都驗。

同一組功能還有第二個、更糟的失敗：旗標放對位置之後，`--download-archive` 讓*重複*下載什麼都沒寫進全新的暫存目錄，所以一個原本可用的連結回報「沒有找到可傳送的圖片或影片」。它已被整個移除——見 README 的快取一節。

---

## 憑證到期與「為什麼沒有自動抓 cookie 的工具」

三個憑證的行為差很多。實測：

| 憑證 | 壽命 | 失效時會怎樣 |
| --- | --- | --- |
| X/Twitter `auth_token` + `ct0` | 約 1 年 | **所有 X 存取停止**，重匯 cookie |
| X/Twitter cookie（被撤銷時） | 立即 | 改密碼或登出時，X 會在伺服器端作廢 session |
| Pixiv `PIXIV_REFRESH_TOKEN` | **不會過期** | 沒事，除非被撤銷或改密碼 |
| Pixiv `PHPSESSID` | 約 1 個月 | 只有 pixiv.net 的 AJAX 端點（R-18 檢查）；作品下載照常 |

機器人在憑證距到期日少於 `EXPIRY_WARN_DAYS`（預設 14）天時於啟動時警告，管理員的 `/cookies` 永遠顯示剩餘天數，所以故障不會毫無預警地出現。

### 直接從瀏覽器讀 cookie 為什麼不可靠

從瀏覽器直接讀 cookie 是顯而易見的自動化，而它在這台機器上不可靠：

- Chrome 對它的 `Cookies` 資料庫持有排他鎖，所以 `gallery-dl --cookies-from-browser chrome` 在 **Chrome 執行中**會以 `PermissionError` 失敗——而先複製檔案也失敗，因為鎖是共享的。
- 關掉 Chrome 也解決不了：這個設定檔使用 Chrome 的 App-Bound Encryption（`Local State` 裡有 `app_bound_encrypted_key`），它把 cookie 金鑰綁在**瀏覽器行程**而不是 Windows 使用者上，所以即使檔案解鎖了，gallery-dl 也解不開那些值。

因此支援的工作流程是：被警告時重新匯出，再用 `--check --live` 確認。兩件事讓它很便宜：

1. X cookie 大約一年、Pixiv refresh token 永不過期——所以這是罕見任務，不是例行公事。
2. 只需要這兩個站點的 cookie。重跑匯出器並覆蓋 `cookies/twitter.txt` 就夠，其他都不用碰。

真的要自動化，可行的做法是**另一個瀏覽器設定檔**，只給這兩個站點用、機器人執行時保持關閉，並把 `GALLERY_DL_COOKIES` 指向它匯出的檔案。

cookie 與 token 會過期；下載開始失敗時，重新匯出 cookie 並重跑 OAuth 流程。`cookies/` 與 `gallery-dl.conf` 都被 gitignore，所以憑證永遠不會被提交。

---

## 版本控制：那次把 733 個測試弄丟的事故

這個專案是一個 git 儲存庫。**做大規模改動之前先 commit**——那不是儀式。一個手滑的清理指令曾經刪掉 `test_bot_features.py`（733 個測試），而當時沒有歷史、沒有 `.pyc`、沒有備份、沒有陰影複本可以救它，所以它就這樣沒了。

```sh
git status           # 有什麼還沒提交
git add -A
git commit -m "..."  # 早提交、常提交
git log --oneline    # 改了什麼
```

`.gitignore` 刻意排除所有機密與生成物，而且這件事是**驗證過的，不是假設的**：

| 被忽略 | 為什麼 |
| --- | --- |
| `.env` | bot token、api id/hash、refresh token |
| `cookies/` | Twitter、Pixiv 與 e-hentai 的 session cookie |
| `authorized.json` | 使用者、群組與管理員名單 |
| `parse_cache.json`、`.cache/` | 解析快取與它的檔案 |
| `restart.log*`、`bot_console.log` | 執行期日誌 |
| `gallery-dl.conf`、`.gallery-dl-cache/` | 由 `.env` 產生 |
| `.venv/`、`vendor/`、`__pycache__/` | 環境與開發用工具 |
| `bot-api-data/`、`telegram-bot-api*` | 自架伺服器的狀態與執行檔 |

`git check-ignore -v <path>` 可以確認任何單一路徑。被追蹤的檔案有十二個：四個 Python 模組、兩個 runner 腳本、`docker-compose.yml`、`run.bat`、`requirements.txt`、`README.md`、`.gitignore` 與 `.env.example`。

### 把更新推上 GitHub

remote 是 <https://github.com/YuNaiAFK/twitter_pixiv_tg_bot_windows>（公開），`main` 追蹤 `origin/main`。機器人有變更就推——那是異地副本，而它的缺席正是測試套件遺失後無法救回的原因。

```sh
git status                              # 看改了什麼
git add -A
git commit -m "what changed and why"
git push
```

**每一次會動到設定檔的推送之前，都要驗證排除清單。** 下面這行會列出真正會被發佈的東西，結果必須印出 **`SAFE`**：

```sh
git ls-files | findstr /I /R "^\.env$ cookies authorized parse_cache restart\.log"
```

用 `/R` 加錨定樣式是刻意的：單純的 `findstr /I "env"` 也會命中 `.env.example`，而那是一個佔位範本、**應該**被發佈。一個對安全檔案亂叫的檢查，是大家會學會忽略的檢查，那就失去意義了。

---

## 為什麼 push 失敗：代理調查

全域 git 設定指向 `http://127.0.0.1:10808`，一個**沒有在跑**的代理：

```
fatal: unable to access 'https://github.com/...': Failed to connect to github.com
port 443 via 127.0.0.1 after 2058 ms: Could not connect to server
```

清掉它是第一個修法，而在 GitHub 可以直連時有效。在這個網路上**不夠**：GitHub 也被過濾，清掉代理只是把失敗搬成 `Failed to connect to github.com port 443` 與 `Recv failure: Connection was reset`。

這台主機在 `127.0.0.1:7897` 跑一個 **SOCKS5** 代理（是驗證過它對 SOCKS5 問候的回應，不是假設），所以這個儲存庫把 git 指向它：

```sh
git config --local http.proxy "socks5h://127.0.0.1:7897"
git config --local https.proxy "socks5h://127.0.0.1:7897"
```

`socks5h` 裡的 `h` 是刻意的：它讓 **DNS 透過代理解析**，而那正是被過濾的網路需要的——`socks5://` 會在本機解析，然後可能連到一個反正被擋掉的位址。

兩個設定都是**這個儲存庫專屬**；全域的刻意不動，因為其他工具可能依賴它。如果在*另一個*儲存庫遇到同樣的連線錯誤，就在那裡套同樣兩行。

注意這個代理是 SOCKS5，不是 HTTP。在這裡設 `http://` 網址會產生 `Get "http://...": server gave HTTP response to HTTPS client` 或光禿禿的 `400 Bad Request`，是發現不匹配的一種很令人困惑的方式。

---

## 代理專題：三個客戶端都不讀 Windows 系統代理

這三個問題的共同形狀是：**同一個網址在瀏覽器裡正常，從程式碼裡卻失敗**。

### gallery-dl 忽略 Windows 系統代理，必須明確傳

Pixiv 下載花了 **115.7 秒然後失敗**，錯誤是 `ConnectTimeoutError: Connection to app-api.pixiv.net timed out`。同一道指令設了 `HTTPS_PROXY` 之後只花 **1.3 秒**。

原因是這台機器走**登錄檔裡的系統代理**（`HKCU\...\Internet Settings`，`ProxyEnable=1`，`ProxyServer=127.0.0.1:7897`），而兩個 HTTP 堆疊對它的態度不同：

| 客戶端 | 會讀系統代理嗎？ | 對 `app-api.pixiv.net` 的結果 |
| --- | --- | --- |
| `httpx`（`trust_env`） | 會 | 1.3 秒內 404 |
| 原始 TCP 連線 | 不適用 | **每次都逾時** |
| `gallery-dl` | **不會** | 三次 30 秒逾時，然後失敗 |

所以機器人必須自己傳代理。`gallery_dl_env()` 就是做這件事，而**兩個** gallery-dl 呼叫點都用它。

**「兩個」很重要**：一個是 `download_with_gallery_dl`（跑一個跑完就結束的子行程），另一個是 `download_gallery_media`（自己建 `Popen` 以便串流進度）。只修第一個對慢路徑完全沒有影響——實測的 115.7 秒絲毫未變——這值得記住，在因為「程式碼看起來對」就下結論說修好了之前。

環境裡明確的 `HTTPS_PROXY` 會贏過登錄檔，所以不用改登錄檔就能覆蓋或停用。

### httpx 忽略登錄檔代理，每個 e-hentai 請求都必須自己設

**這就是「e-hentai 卡住」的成因，而且不是 e-hentai 的錯。** httpx 不讀 Windows 系統代理，所以在這台機器上每個 e-hentai 請求都**直接**出去然後逾時，而同一個網址在瀏覽器裡正常。並排實測：

```
httpx，沒有明確代理   -> 每次都是 ConnectTimeout
httpx，proxy=...      -> HTTP 200
瀏覽器                -> 正常
```

瀏覽器會讀系統代理，httpx 不會。光這一個差異就解釋了整個症狀，而它被誤讀成「e-hentai 封鎖了這個 IP」很長一段時間——包括這裡曾經寫過「代理沒有幫助」的註記，那是**錯的**：代理根本沒有被用到。

`ehentai.proxy_url()` 先讀 `HTTPS_PROXY`／`ALL_PROXY`，再退回登錄檔（`HKCU\...\Internet Settings`），而 `ehentai.http_client_kwargs()` 回傳每個 client 都需要的那組關鍵字參數。**`bot.py` 與 `ehentai.py` 裡全部九個 httpx client 都用它**——畫廊抓取、viewer 掃描、頁面下載、壓縮檔探測、archiver POST，以及壓縮檔下載。有一項特徵化檢查會走過兩個檔案，任何沒有用它的 `httpx.Client(...)` 都會讓檢查失敗，因為一個裸 client 就足以讓自己卡住。

這個輔助函式回傳 `http://host:port` 形式的代理，即使那個埠也講 SOCKS5：httpx 要 `socks5://` 得額外裝 `socksio`，而 HTTP CONNECT 在這裡不需要那個相依。

### urllib 也不讀登錄檔代理

線上憑證檢查用的是 `urllib.request.urlopen`，它只認 `http_proxy` 環境變數——不認登錄檔。所以每次檢查都以 "URLError: handshake operation timed out" 失敗，並把**有效憑證回報成無法驗證**：實測 30.4 秒，X/Twitter 與 Pixiv 都「無法連線」，而同一組 cookie 下載完全正常。現在它用的是與其他東西相同的、帶代理的 httpx client，2.8 秒回報、三項檢查全部通過。

同樣的疏漏也在 `probe_telegram_limits`：它的 `httpx.get` 沒有代理，於是探測失敗、被回報成「使用公開 API」，並默默把上傳上限從 2000 MB 掉回 50 MB。

### 診斷這一類問題

上面每一個失敗，最初都被怪到遠端站點。真正解決每一個問題的檢查是**把我們自己的請求與一個已知可用的請求相比**——瀏覽器，或同一個呼叫但明確帶 `proxy=`。當一個站點在瀏覽器裡可以、從程式碼裡不行時，先懷疑客戶端的設定，再懷疑站點的政策。

---

## 安全性稽核

四項獨立的稽核跑過這個程式庫（機密、注入、授權、快取／檔案完整性）。這裡記錄結果，包含**沒有**修好的部分，以及唯一屬於操作者的動作。

### 必須做的事：輪替 bot token

bot token 曾被提交進這個**公開**儲存庫，共四處，全部引用同一個真實的啟動錯誤。token 已經輪替（`/revoke`），外洩的那一個確認死亡——用它呼叫 `getMe` 回 401。那次外洩不需要再做什么，但機器人的 **id** 現在永久公開，應該被當成公開資訊對待。

### 已修

| 嚴重度 | 問題 | 修法 |
| --- | --- | --- |
| HIGH | 快取索引名稱只用 `is_file()` 檢查就與 `CACHE_DIR` 串接，所以 `.cache/../.env` 會被接受——**機器人可能被誘導上傳自己的 token 檔**——而同一種串接也用在刪除 | `cache_file_path()` 驗證名稱形狀、拒絕絕對路徑、要求結果解析到快取目錄的直接子項；讀取與刪除兩條路徑都用它 |
| HIGH | 機器人可能把**當下**的 token 寫進日誌：httpx 會回顯包含它的請求 URL，而 `restart.log` 正是大家貼進 bug 報告的檔案 | `redact_secrets()`；用真實的外洩字串驗證過，也以形狀驗證，所以失敗請求之後才輪替的 token 仍會被攔到 |
| MEDIUM | 管理員名單透過 `/authorized` 洩露給每個授權使用者 | 非管理員看得到名單，但看不到管理員名單 |
| MEDIUM | 一份有效但含 `"admins": []` 的 `authorized.json` 讓機器人永久鎖死所有管理指令 | 檔案裡沒有管理員時，還原 `.env` 的 bootstrap 管理員 |
| MEDIUM | `stop` 按鈕執行時**沒有**任何授權檢查，而 `_running_downloads` 以聊天室為鍵——任何群組成員，不論是否授權，都能殺掉別人的下載 | 每個分支之前都檢查按下的人 |
| MEDIUM | 快取以 `if not target.exists()` 寫入，而名稱只由 URL 推導——在該名稱預先放一個檔案，它就會被當成該 URL 的媒體送出 | 透過暫存檔與 `os.replace` 原子寫入 |
| MEDIUM | 快取只以項目數設上限；實測 29 個檔案 101 MB，沒有位元組上限 | `CACHE_MAX_BYTES`，以最舊優先整項淘汰 |
| MEDIUM | 每個暫存目錄候選都被建立、卻只回傳其中一個，其餘漏掉——在 `%TEMP%` 實測 **2,027 個空目錄** | 候選改為延遲建立、被拒絕的刪除，並用 `sweep_stale_work_dirs()` 清掉已累積的 |
| MEDIUM | 非管理員可以讀到管理員名單；文件把封鎖名單描述成允許名單 | 見上；措辭與模式標籤已修正 |
| LOW | 壓縮檔網址只要**包含** `hath.network/archive/` 就接受，所以一個長得像的主機會被**帶著帳號 cookie**抓取 | 抓取之前先比對主機名稱是否為 `hath.network` |
| LOW | 每次重試都記下 `archiver.php?…&token=…`，一個活的 session token | `redact_url()` 記錄路徑時移除憑證參數 |
| LOW | `prune()` 沒有保護，一次維護失敗會讓一個已經送出的請求失敗 | 加上保護 |

### 已知且**未**修

- **壓縮檔的大小是從不可信的 `Content-Range` 標頭預先配置的。** 總傳輸時間現在有上限，所以這不能再無限拖延，但有惡意的標頭仍可能要求一筆很大的預先配置。
- `parse_cache.json` 與 `.cache/` 在預設的 Windows 設定檔 ACL 下**任何已認證的本機使用者都可寫**，而 `.env` 他們可讀。這就是快取那些發現之所以可觸及的原因。共用機器上：
  `icacls .env /inheritance:r /grant:r "%USERNAME%:(R,W)"`
- `api_hash` 以命令列參數傳給本機 Bot API 伺服器，所以任何本機使用者都能透過列出行程看到它。

### 檢查後確認乾淨

沒有指令注入的入口：只有兩個 `gallery-dl` 呼叫，都是 argv 陣列、`shell=False`、`--` 永遠在最後。第一方程式碼裡沒有 `eval`／`exec`／`pickle`／`yaml.load`。`sanitize_filename` 與 ZIP 項目名稱沒有路徑穿越，而機器人從不解壓它下載的壓縮檔。不同資源之間沒有快取鍵碰撞。沒有從非管理員通往管理員的路徑，而最後一個管理員不能被降級或撤銷。`admin_only` 在沒有設定管理員時是 fail-closed。沒有任何憑證檔被追蹤，`.gitignore` 涵蓋每一個；真實的 `api_hash` 與 `api_id` 從未進入任何 commit。

---

## 安全事件：機器人描述曾被劫持

機器人的 Telegram 描述被發現夾著一個推廣連結：

```
🔥 Best AI Porn here:
👉 https://evadast.com/telegram?start=ref_c3966075e4
🔞 🔥 Лучшее AI-порно здесь:
👉 https://evadast.com/telegram?start=ref_c3966075e4
```

兩種語言，加上一個 `ref_` 代碼，所以這是付費推薦，不是誤會。**這個儲存庫從來沒有設定過描述**——直接查 `getMyDescription`，文字就在那裡，而沒有任何程式路徑會寫它。那只可能是**持有 bot token 的人**寫的。

**把這件事當成憑證外洩，不是外觀問題。** 能設定描述的人，手上就是那組能讀更新、能冒用機器人發訊息的同一組 token。那段文字上線期間用過機器人的人，他們的連結與訊息可能都暴露給那一方。

處置順序：

1. **用 BotFather（`/revoke`）輪替 token**，把新的放進 `.env`。這才是真正的修法，下面都只是修症狀。
2. 檢查 BotFather 上還有什麼被改過——名稱、about 文字、頭像，以及任何連結的頻道。
3. 重啟機器人，它會重新發佈自己的個人檔案文字。

機器人現在**每次啟動**都會從 `BOT_DESCRIPTION`／`BOT_SHORT_DESCRIPTION`（兩者都可被 `.env` 覆蓋）設定 `set_my_description` 與 `set_my_short_description`。這讓個人檔案自我修復：被注入的描述只活到下一次重啟。設定失敗會被記錄並忽略，因為沒有描述只是外觀問題，而起不來不是。

**描述為什麼值得攻擊：** 它是 Telegram 在**使用者送出任何東西之前**就顯示的文字，所以是機器人觸及率最高的一段字。放在那裡的推薦連結，每一個訪客都會看到。

---

## 多出來的 python 視窗：venv launcher 與 base interpreter

被回報成「為什麼旁邊會多跳出一個 python 視窗？」。那不是機器人——是**代理轉接**，而原因是啟動它的解譯器。

`.venv\Scripts\python.exe` 是 venv 的**啟動器**，不是解譯器：它會把真正的解譯器當子行程重新執行。spawn 這一對就會在畫面上留下一個可見的空控制台，而 `CREATE_NO_WINDOW` 與 `DETACHED_PROCESS` 都壓不住它——兩個都先試過了：

```
venv stub        (python.exe): NEW windows = [(33140, '...\.venv\Scripts\python.exe')]
base interpreter (python.exe): NEW windows = []
```

`sys._base_executable` 才是真正的解譯器，啟動時不開視窗。現在兩個背景 spawn 都用它——`run_local_api.py` 裡的轉接，以及 `bot.py` 裡 `/restart` 的替代行程——透過 `detached_interpreter()`。

代價必須明確付出：base interpreter 不知道 venv，所以 `child_environment()` 把 venv 的 `site-packages` 加進 `PYTHONPATH`。沒有這個，子行程匯入不了 `telegram` 並立刻死亡：

```
prefix    C:\Python314
imports   ['httpx=FAIL', 'telegram=FAIL', 'PIL=FAIL', 'gallery_dl=FAIL']
```

`list_console_windows.py` 被保留，因為就是它解決了這件事：它列出每個可見的控制台視窗與它的擁有行程，所以「那個視窗是誰開的」是用跑一次回答的，不是用猜的。

---

## 封鎖名單：從允許名單改成封鎖名單

以前是允許名單：名單上有一個條目，就代表**只有**那些使用者能解析。那對一個「解析本來就對所有訪客開放」的機器人來說是錯的形狀。加進一個濫用者，就切掉了所有普通訪客——與意圖相反，而且是一個沒人會注意到、直到有人抱怨機器人壞了的錯誤。

```
以前   blocked = {5}   ->  允許使用者 5，其他所有人被拒絕
現在   blocked = {5}   ->  拒絕使用者 5，其他所有人允許
```

| 名單 | 空白 | 有名單 |
| --- | --- | --- |
| **私人聊天**（封鎖名單） | 所有人都能解析 | 名單上的人被拒絕，其他人歡迎 |
| **群組**（允許名單） | 由 `AUTH_GROUP_LOCKED` 決定：全開或全關 | 只有名單上的群組 |

群組刻意維持允許名單：公開群組裡的機器人應該服務「被刻意加進去」的群組，而不是每一個剛好包含它的群組。管理員到處通行，所以名單寫錯永遠不會把操作者鎖在自己的機器人外面。

### 指令

| 指令 | 私人聊天 | 群組 |
| --- | --- | --- |
| `/revoke <ID>` | **封鎖**該使用者 | 移除該群組的授權 |
| `/grant <ID>` | **解除封鎖**該使用者 | 授權該群組 |
| `/blocked` | 列出被封鎖的使用者 | — |
| `/lock` | 退役，回答它被什麼取代 | — |
| `/lockgroup on\|off` | — | 群組的守門模式 |

`/lock` 已經不是一個設定：封鎖名單不是閘門，所以沒有模式可選。它仍然註冊著，並回答「它被什麼取代」而不是沉默，因為筆記裡還留著 `/lock` 的操作者一定會輸入它。

### 這個改動暴露的安全漏洞

名單指令以前用 `list_manager_only`，那會放行管理員**與授權使用者**——在名單是「放人進來」的時期很合理，因為把權限傳下去就是重點。換成封鎖名單之後，同一條規則等於把控制權交給被它擋住的人：**被封鎖的使用者可以自己解除封鎖，還能封鎖別人。** 現在每個名單指令都是 `admin_only`，而 `user_can_manage_list` 與 `list_manager_only` 兩個都直接刪掉，而不是留著不用。

同樣的錯誤也在 `post_init` 裡：範圍化的指令選單替管理員**與** `AUTHORIZATION.allowed`（也就是封鎖名單）設定。被封鎖的使用者會在自己的 Telegram 選單裡看到操作者指令。他們用了會被拒絕，但**宣傳**它們仍然是錯的——而且那等於向被封鎖的使用者確認機器人在盯著他。

### 既有的 `authorized.json` 仍然可以載入

檔案裡的欄位仍然叫 `allowed`，所以不必遷移，另外有一個 `blocked` 鍵，存在時優先。因此一份舊的允許名單會被當成封鎖名單載入，**把上面列出的使用者封鎖掉**。這是刻意的方向：一小串 id 是濫用者的機率，遠高於它剛好是「所有被允許的人」的完整集合，而 `/blocked` 會明明白白顯示最後誰在名單上。`.env` 裡的 `ALLOWED_USER_IDS` 也以同樣方式讀取。

`AUTH_LOCKED` 現在直接被忽略——已經沒有閘門讓它設定。

## 被拒絕的群組以沉默回應，而不是回一句拒絕

授權拒絕在**私人**聊天裡是有用的答案：把機器人加進來的人看得到為什麼什麼都沒發生，也可以要求授權。在**群組**裡恰恰相反——機器人會對一個「被交代不要服務」的聊天室裡每一則連結出聲，那是在別人的對話裡製造噪音，還向沒在問它的人宣傳自己的存在。而成員也不能對這個拒絕做任何事。

所以拒絕由聊天類型決定：

| 聊天 | 答案 |
| --- | --- |
| 群組，未授權 | **沉默** |
| 超級群組，未授權 | **沉默** |
| 私人，未授權 | 此機器人目前不允許你的帳號使用。 |
| 已授權群組 | 檔案，如常 |

這個守衛是 `handle_message` 的第一件事，所以被拒絕的群組永遠到不了任何其他回覆——冷卻提示、不支援連結提示與失敗訊息對它全都不可達。按鈕回呼由同一條規則擋下並用 `show_alert`，那是一個只有按下的人看得到的彈窗；而被拒絕的群組從頭到尾就沒收到任何帶按鈕的訊息。

---

## 舊版 Pixiv 連結被服務到別的作品的檔案

從日誌回報：一個 `member_illust.php?...&illust_id=...` 連結以 `NotFoundError` 失敗，而操作者提示把原因怪到憑證上。那一行背後有三個獨立的錯。

### 1. 快取鍵丟掉了作品 id

`normalize_cache_key` 移除整個查詢字串，但 pixiv 的舊格式**只**在查詢字串裡帶 id：

```
https://www.pixiv.net/member_illust.php?mode=medium&illust_id=122010932
```

於是每個這種連結都正規化成 `https://www.pixiv.net/member_illust.php`——**一個快取項目被所有使用該路徑的作品共用**。可重現：解析過一個作品之後，同一路徑上第二個 `illust_id` 被服務了第一個的檔案，而且從未下載。

現在指名資源的參數（`illust_id`、`novel_id`、`id`）會被保留並排序，所以順序不會把同一個項目拆開。其他一律丟掉（`?s=20`、`&t=abc`），所以「同一則貼文不會被快取兩次」的正規化仍然有效：

```
...member_illust.php?mode=medium&illust_id=122010932 -> ...member_illust.php?illust_id=122010932
...member_illust.php?mode=medium&illust_id=150824857 -> ...member_illust.php?illust_id=150824857
https://x.com/u/status/123?s=20&t=abc               -> https://x.com/u/status/123
https://twitter.com/u/status/123                    -> https://x.com/u/status/123
```

### 2. 原因只在成功時被記錄

`download_with_gallery_dl` 在非零結束時**先**拋出例外，**才**記錄 gallery-dl 的輸出，所以 `LAST_DOWNLOAD_REASON` 恰好對那些值得解釋的執行保持空白——一個被刪除的作品以 4 結束並帶 `NotFoundError`。提示於是掉進它按平台的猜測：

```
classify_download_output(真實輸出) -> 'missing'     （辨識到了）
admin_hint('pixiv')                -> credentials   （沒有任何東西記錄下來）
```

一個追查已下架作品的操作者，被送去重跑一場從來不是問題的 OAuth 流程。現在輸出會在檢查結束碼之前就記錄：

```
作品不存在或連結已失效（gallery-dl 回報 not found）。這不是憑證問題。
```

### 3. 一次暫時失敗把刪除變成沉默

單一請求失敗時，`pixiv_work_state` 回 `unknown`，而 `unknown` 代表沉默。這是在一個「每兩次就有一次回 404」的作品上觀察到的。現在它會重試三次，所以一次掉線或一次被限流的 token 更新都不能掩蓋一個死連結。永遠無法解決的狀態仍然是 `unknown`，因為重試不能把「無法確認」變成指控。

三項都修完之後端到端：被回報的連結回答「這個 Pixiv 作品目前無法瀏覽：可能已被作者刪除，或設為非公開。」，而同一個舊路徑上一個**活的**作品正常下載，不再繼承那個被毒化的快取項目。

---

## Windows「開啟檔案 - 安全性警告」

雙擊 `run.bat`（或它的捷徑）會提示：

```
開啟檔案 - 安全性警告
你要開啟此檔案嗎？
發行者: 未知的發行者
來自網際網路的檔案可能有所幫助，但此檔案類型可能危害您的電腦。
```

**原因不是 zone mark。** 這台機器上驗證過它不存在：沒有 `Zone.Identifier` 資料流（用 `dir /r` 與 `FindFirstStreamW` API 檢查）、SmartScreen 關閉（`SmartScreenEnabled=off`、`EnableSmartScreen=0`）、Smart App Control 關閉（`VerifiedAndReputablePolicyState=0`）、沒有 AppLocker/WDAC 原則、D: 是本機 NTFS 磁碟，沒有 reparse point 或雲端同步。

### 真正的原因

專案資料夾帶著**可繼承的 Low 完整性標籤**：

```
D:\TG_Bot\twitter_pixiv_tg_bot_windows
  Mandatory Label\Low Mandatory Level:(OI)(CI)(NW)
```

`(OI)(CI)` 讓它繼承到每個檔案，所以全部 746+ 個檔案都是 `Low Mandatory Level`。Low 完整性是沙箱與瀏覽器寫入的等級，所以 Windows 把那些檔案當成不可信，並在執行前警告——**完全不需要 zone mark**。這是第二套、獨立的機制，這就是為什麼去找 zone mark 什麼都找不到。

用這個確認：

```
icacls "D:\TG_Bot\twitter_pixiv_tg_bot_windows"
```

### 修法

以**管理員** PowerShell 執行一次：

```
icacls "D:\TG_Bot\twitter_pixiv_tg_bot_windows" /setintegritylevel m /t /c
```

要還原：

```
icacls "D:\TG_Bot\twitter_pixiv_tg_bot_windows" /setintegritylevel l /t /c
```

沒有提權的嘗試只會改到一部分檔案（這裡是 3703 個中的 139 個），其餘回報 `Access is denied`，所以一定要提權。修完之後每個檔案都讀成 `Medium Mandatory Level`，而機器人的快取、歸檔快取與下載目錄都重新測試過可寫。

### 如果它又回來

專案被複製或重新解壓時，這個標籤可能回來。用上面的 `icacls` 指令檢查並重新套用。

還有一件值得檢查的事，因為它會產生非常類似的提示：這台機器裝了**火絨安全軟體**（Huorong Security），`HipsDaemon` 正在執行。它的 HIPS 模組會攔截腳本執行，並顯示自己的未知發行者警告。如果完整性標籤已經是 `Medium` 而提示還在，就把專案資料夾加進它的信任清單（防護中心 → 信任區 → 新增資料夾）。

---

## 私人 Pixiv 作品被誤報為壞連結

取代一個錯誤規則的窄規則：**已經不存在的作品要回報，其他一律沉默。**

在這之前，空的結果永遠會產生一則訊息，而對一個私人或年齡限制的作品，那則訊息錯了兩次：

```
沒有找到可傳送的圖片或影片。
取不到作品內容，連結可能已失效。
```

連結是好的。它需要一個這個機器人沒有的登入，那不是傳連結的人能處理的事——而「連結可能已失效」是在怪他。機器人還留下自己的狀態與來源訊息，所以一個什麼都沒解析出來的連結，反而比成功的連結製造**更多**雜訊。

### 未發佈的作品回 200，所以狀態碼什麼都沒說

用真實連結回報：`pixiv.net/artworks/150822697`。第一次嘗試說它可讀，因為 app API 回 **200**——它確實回 200，只是內容被清空：

```
unpublished   {"illust": {"id": 150822697, "title": "", "visible": false,
                          "total_view": 0, "total_bookmarks": 0}}      status 200
live          {"illust": {"id": 150824857, "title": "しろこ", "visible": true,
                          "total_view": 436, "total_bookmarks": 143}}   status 200
```

`visible` 才是訊號，不是狀態碼。標題與計數也是空的，但 `visible` 是直接說的，不是靠推論。所以規則變成：

| 作品 | 怎麼回答 | 結果 |
| --- | --- | --- |
| 活的 | 200，`visible: true` | 下載 |
| 年齡限制、憑證有效 | 200，`visible: true` | 下載 |
| **被作者下架** | **200，`visible: false`** | **回報** |
| 已刪除 | 404 | **回報** |
| 其他 | 500、逾時 | 沉默 |

通知用一句話涵蓋兩種原因，因為對訪客來說它們無法區分，而 gallery-dl 也分不出來：

```
這個 Pixiv 作品目前無法瀏覽：可能已被作者刪除，或設為非公開。
（若作者之後重新公開，重新傳送連結即可）
```

### gallery-dl 分不出「刪除」與「缺登入」

它對兩者回報同一個錯誤，因為 **web** API 兩種情況都回 404：

```
[pixiv][error] NotFoundError: Requested resource (gallery/image) could not be found
```

對照 **app** API，它分得出來：

```
app API，沒有憑證    400  login required
app API，有 token    200  作品
web API，沒有憑證    404  與刪除完全相同
```

所以 `pixiv_work_state()` 用設定的憑證去問 app API，由答案決定：

| 作品 | 狀態 | 使用者看到什麼 |
| --- | --- | --- |
| 可讀（已下載） | — | 檔案，沒有錯誤 |
| 私人，憑證有效 | `ok` | 什麼都沒有（下載成功） |
| 私人，憑證壞掉 | `unknown` | **什麼都沒有**——沉默 |
| 已下架或刪除 | `unavailable` | 這個 Pixiv 作品目前無法瀏覽… |

**`unknown` 刻意沉默。** 在機器人只是無法確認時，告訴別人他的連結死了，比什麼都不說更糟。

一個機器人*讀得到*的私人作品會正常下載，那正是請求要的：「私人画廊如果在登录的时候能解析出来则不需要提示错误」。

### 檢查時發現的兩個 bug

- **`classify_download_output` 認不出真正的錯誤。** 它找的是帶空格的 `"not found"`，而 gallery-dl 印的是 `NotFoundError:` 與 "could not be found"。兩者都不匹配，所以被刪除的作品被分類成 `""`，操作者提示就掉回對憑證的猜測——把操作者送去重跑一場從來不是問題的 OAuth 流程。現在兩種拼法都匹配。
- **`pixiv_illust_id` 要求五位數以上**，所以短的 id 默默變成 `""`，於是每一次刪除都被回報成沉默。

---

## 無法解析的連結改為沉默；Pixiv 頁面與 x.com 個人頁

兩件屬於一起的改動：無法解析的連結現在**完全不回覆**，而讓沉默變安全的那個分類被延伸到 Pixiv。

### 預設沉默

`REPLY_UNSUPPORTED_LINKS` 現在預設 **0**。機器人無法解析的連結不會收到任何訊息——不是通用的「只支援 X/Twitter、Pixiv 與 e-hentai」通知，也不是針對 X 的頁面說明。在公開群組裡，對每一則無法解析的連結都回一句話，是在一個沒被邀請的對話裡製造噪音。

說明字串被保留，因為 `REPLY_UNSUPPORTED_LINKS=1` 可以把它們叫回來，而那正是操作者在檢查「連結是真的不支援、還是只是被誤判」時要的。

**沉默之所以安全，只因為分類照樣會跑。** 非貼文的 X 連結仍會被偵測並在下載器啟動前丟掉；沒有這個，「沉默」會變成等**三分鐘**然後什麼都沒聽到。測試會斷言這個順序。

### Pixiv 有同樣的問題，症狀更糟

通知一安靜，把檢查延伸到 Pixiv 就不是選項了。用 gallery-dl 實測：

```
/artworks/150824857                2 files in 46.7s   -> 解析
/en/artworks/150824857             2 files in 31.0s   -> 解析
/member_illust.php?...illust_id=…  2 files in 20.8s   -> 解析
/users/12345                       0 files in  2.5s   -> 什麼都沒有
/search?word=cat                   TIMEOUT after 180s
/tags/cat                          TIMEOUT after 180s
/ranking.php                       TIMEOUT after 180s
/discover                          "Unsupported URL" in 0.6s
```

Pixiv 的使用者頁面**在通知關掉之前**就沒有回覆：下載器在那裡什麼都找不到，所以失敗訊息是唯一能解釋它的東西。更糟的是，它留下狀態訊息——實測一個什麼都解析不出來的連結留下兩則訊息。其他形狀則跑滿三分鐘。

`classify_link()` 現在分派到各平台的分類器，所以 handler 只有**一個**檢查點，而不是每站一個。只處理 X 正是 Pixiv 被漏掉的原因。兩個分類器都保持保守：任何沒有被正面辨識為頁面的東西都交給下載器，因為一個錯誤的「不支援」會拒絕一個可用的連結。

改完之後上面每一個形狀都在 **0.0 秒、沒有訊息**的情況下有回應，而 `/artworks/N`、`/en/artworks/N`、`member_illust.php?illust_id=N` 與 `/novel/show.php?id=N` 仍然會解析。

### 一個 x.com 個人頁被丟給下載器，花了 180 秒

以前每個 x.com URL 都直接進 gallery-dl，而只有一部分會導向媒體。實測：

```
/status/123          1 file in 3.9s   -> 解析
/i/status/123        1 file in 3.3s   -> 解析
/status/123/photo/1  1 file in 3.8s   -> 解析
/pohitashi          TIMEOUT after 180s
/pohitashi/media    TIMEOUT after 180s
```

個人頁就是時間軸，而 gallery-dl 把它當成「下載這個帳號發過的一切」。所以貼一個個人頁會讓使用者等**三分鐘**，然後讀到一句通用的「沒有找到可傳送的圖片或影片」——完全沒說到他送的那個頁面是什麼。

`classify_twitter_url()` 在下載任何東西之前辨識形狀，並回答那個頁面實際上是什麼：

```
這是 X（Twitter）的使用者主頁（@pohitashi），不是單一貼文連結。
主頁會包含該帳號的所有貼文，無法在這裡一次解析，請貼上單一貼文的網址。
```

**0.0 秒**回答，而不是 180 秒。其他非貼文形狀也一樣：個人頁分頁（媒體、含回覆的貼文、喜歡的內容、追蹤者…）、搜尋結果、主題標籤頁、清單、社群、廣播、`/i/user`、intent/share 連結，以及首頁、explore、設定、通知、訊息與法務頁面。

### 刻意保守

危險的方向是拒絕一個可用的連結，所以任何貼文形狀的東西都原樣通過——包括沒有實測過確切行為的形式。只有確定不是單一貼文的形狀才被歸類為其他，而 `tweet` 的判定會原封不動掉回正常路徑。

兩個容易弄錯的細節，都有測試釘住：

- **分頁是第二段路徑**（`/<user>/media`），不是第一段。比對第一段看起來很對，卻默默漏掉每一個分頁——它們掉進含糊的 catch-all，而不是指名那個分頁。
- **使用者可以取成頁面的名字。** `/media` 是一個真實的帳號名稱，`/search` 也是；只有 `/<user>/<tab>` 才是分頁。保留字先檢查，所以 `/search` 是搜尋頁、`/media` 是個人頁。

端到端驗證：貼文仍然下載它的媒體、pixiv 不受影響、每一個非貼文形狀都在 0.0 秒回答。

---

## Telegraph 頁面的圖片全壞：原圖網址需要 session

回報是「生成的链接点开后图片无法加载」。抓下頁面、匿名測試每一個內嵌圖片——這正是 Telegraph 自己的伺服器在做的事——就看出原因：

```
exhentai.org 上的 12 張圖   -> 0/3 可載入，content-type text/html（登入頁）
<node>.hath.network 的 12 張 -> 3/3 可載入，content-type image/jpeg
```

畫廊頁面對每一頁提供兩種網址，而它們不等價：

```
<img id="img" src="https://<node>.hath.network/h/...">          公開  - 200 image/webp
<a href="https://exhentai.org/fullimg/3778922/1/xxx/001.jpg">   需要 session - 302 到登入頁
```

第二種是**原圖**。`顯示圖` 嵌入第一種，所以能用；`原圖` 嵌入第二種，於是每一頁都壞，因為 Telegraph 自己抓每一張圖、不帶 cookie。所以 `原圖` 模式從來沒有產出過可用的頁面。

### 修法：解析原圖藏在後面的轉址

`/fullimg/` 回一個 302，目標在 `hath.network` 上——而**那個目標可以匿名抓取**（匿名實測：`200 image/jpeg, 6121400B`）。機器人本來就握有 session，所以它可以要那個轉址，然後嵌入公開網址：

```
exhentai.org/fullimg/...  --302-->  yqcohbpjgjmcamalcfry.hath.network/om/.../001.jpg
                                     ^ 可匿名載入
```

`resolve_public_image_urls()` 對整個畫廊做這件事，並保留長度與順序，所以一個失敗不會讓之後每一頁移位。發佈**與** `重新生成` 都用它，因為兩者嵌入同一份清單——只修前者會讓按鈕重建一個壞頁面。

真實 27 頁畫廊實測：20 張原圖在 17.7 秒內轉換完成，10 頁的集合則是 7.1 秒。`顯示圖` 模式什麼都不轉（本來就公開）也不花任何請求。

### 用 HEAD 驗證目標，不要用 ranged GET

確認轉址目標可用的明顯做法是範圍請求，而在這裡它是錯的——實測這個 CDN **忽略 Range 標頭**，對範圍請求回整個檔案：

```
解析轉址（只看標頭）    2.5s
對目標發 HEAD          1.4s
ranged GET bytes=0-1023 7.7s -> 而且回來了 6121400 位元組
```

所以驗證用 HEAD，不花什麼成本，也足以證明該網址有在服務。這不是外觀細節：這個功能的第一版用 ranged GET，會把畫廊裡每一張原圖都下載一遍，每張約 6 MB。

無法解析的頁面會保留原本的網址，並**以計數記錄**，而不是默默丟掉或默默壞掉，所以缺了幾張是知道的。

## `重新生成` 在封面載入後必定失敗

```
WARNING twitter_pixiv_bot: 更新頁面訊息失敗：There is no text in the message to edit
```

這是封面功能的第二個、獨立的後果。`edit_message_to_cover` 把結果訊息變成封面**照片**，而 Telegram 拒絕對媒體訊息做 `edit_message_text`。`重新生成` 的 handler 假設那是一則文字訊息，所以在任何封面已載入的畫廊上按它，就會記錄失敗並**另貼一份副本**，而不是原地更新。封面*沒有*載入的畫廊不受影響，這就是它看起來時有時無的原因。

照片的說明文字**可以**編輯，而且 Telegram 會把它顯示在圖片下方，所以封面能撐過更新，而「一次請求一則訊息」的規則也保住。`update_result_message()` 從訊息本身挑選呼叫：

```
message.photo 存在  -> edit_caption
否則                -> edit_text
```

這個檢查讀的是訊息物件，它從 update 帶著自己的媒體，所以它反映的是訊息**現在是什麼**，而不是 handler 被寫下時假設什麼。說明文字為空的照片會被拒絕（Telegram 要求媒體要有非空白的說明文字），呼叫端就另外送一則，因為第二則訊息勝過沉默。

兩個分支都檢查過同一個陷阱：封面只在 Telegraph 動作裡套用，而歸檔動作只編輯文字，兩者來自互斥的按鍵，所以誰都不會遇到對方轉換過的訊息。

## 每個 exhentai 封面都不見，而日誌只說 "Failed to get http url content"

```
WARNING twitter_pixiv_bot: 封面編輯失敗：https://s.exhentai.org/w/02/714/75349-4sjx86ns.webp
                            （Failed to get http url content）
```

程式碼聲稱封面是「e-hentai 的靜態縮圖（`ehgt.org`）」。並不是：exhentai 畫廊的封面由 **`s.exhentai.org`** 提供，而那個主機需要 session。Telegram 自己抓 `InputMediaPhoto(media=url)`，沒有 cookie、沒有 referer，所以被拒絕——而因為失敗的封面只是警告加一張缺圖，這件事在每個 exhentai 畫廊上一直默默失敗。

對日誌裡那個網址實測：

```
https://s.exhentai.org/w/02/714/75349-4sjx86ns.webp  -> 403   （匿名）
https://ehgt.org/w/02/714/75349-4sjx86ns.webp        -> 200   image/webp, 29.8 KB
```

**路徑逐位元組相同**——只有主機不同。這是用同一個畫廊透過兩個主機抓取確認的，也正是它讓修法成為「替換」而不是「猜測」：

```
exhentai.org  https://s.exhentai.org/w/02/240/36544-r7iw06st.webp
e-hentai.org  https://ehgt.org/w/02/240/36544-r7iw06st.webp
```

`public_thumbnail_url()` 把綁 session 的主機改寫成 `ehgt.org`，其他一律拒絕。兩個限制讓它誠實：

- **只改寫縮圖路徑。** 改寫是主機替換，安全的理由正是路徑原樣帶過去；非圖片的 URL 會被拒絕，而不是變成一個對無關主機的請求。
- **只改寫已知可抓的主機。** 未知主機會被拒絕，而不是送出去然後失敗，因為 Telegram 的錯誤既沒說主機也沒說原因——猜測要花一次往返，還產生一行沒人能處理的日誌。

改完之後在兩個真實畫廊上驗證：匿名請求回 `200 image/webp`，所以封面現在會顯示。

---

## 快取稽核：還有兩種「同一個資源被存兩次」

在歸檔快取的回報之後，程式庫裡每一個快取都被檢查同一類缺陷——**儲存與查詢對鍵的認知不一致**。又找到兩個真的。

### 主機別名各自成為快取項目

`normalize_cache_key` 把小寫化的 scheme 與主機正規化，並丟掉查詢、片段與結尾斜線，但它不知道有些主機是同一個站。這是對活站驗證的，不是假設：

```
https://twitter.com/...      -> 301 -> https://x.com/...          （同一則貼文）
https://pixiv.net/...        -> 301 -> https://www.pixiv.net/...  （同一件作品）
https://www.e-hentai.org/... -> 302 -> https://e-hentai.org/...   （同一個畫廊）
http://...                   -> 301 -> https://...                （同一個資源）
```

媒體快取除了這個函式之外沒有自己的正規化，所以一則貼文先經 `twitter.com`、再經 `x.com` 進來，就是**兩個項目、磁碟上兩份同樣的檔案**。一則推文的六種形式現在收斂成一個鍵：

```
https://x.com/u/status/1          \
https://twitter.com/u/status/1     |
https://www.x.com/u/status/1       |  全部 ->  https://x.com/u/status/1
http://twitter.com/u/status/1      |
https://twitter.com/u/status/1?s=20|
https://x.com/u/status/1/         /
```

別名表是明確列出的，不是規則推導的，因為各站的正規形式不同（pixiv 的是 `www` 那個），也因為**不在支援集合裡的主機絕不能被改名**。後綴比對會把 `notx.com` 併進 `x.com`；有一個測試釘住它不會。

### 早期儲存留下的孤兒檔案從未被回收

媒體快取把檔案命名成 `<url hash>_<index><suffix>`。`store()` 會取代整個項目，所以重新解析若產生不同的檔案集——頁數變少，或同樣的頁面換了格式——就會索引新名稱並拋棄舊的，而之後沒有任何東西刪掉它們。實測的快取沒有孤兒，所以還沒被咬到；但機制在那裡。

`orphaned_cache_files()` 把索引當成「什麼是合法的」的完整清單，並刪掉其餘的，在它們變得不可觸及時（寫入路徑）以及啟動時針對這個功能存在之前寫入的東西各做一次。它刻意窄，而測試斷言的是危險的方向：活的檔案、子目錄裡的歸檔快取，以及一個被丟進快取目錄的無關檔案，全部都活下來。

### 檢查後確認正常

- **`ParseCache.store`／`lookup`／`age` 全部經由 `normalize_cache_key`**，所以它們在結構上就一致；上面那些缺陷都在「餵給它什麼鍵」。
- **viewer URL 的 TTL 閘門在兩個邊界都正確**（7195 秒重用、7205 秒刷新），而 `GALLERY_URL_TTL_SECONDS`（2 小時）確實短於 `CACHE_STALE_SECONDS`（14 天）——簽章網址遠早於帶著它們的項目過期。
- **歸檔快取在解析之前就被查**，所以那 3 秒的往返也省了。
- **`_regenerate_cache`** 有上限（200）並以 `(chat, user)` 為鍵，所以一個人不能重新生成別人的頁面。
- **`_requests_in_flight`** 每位使用者限一個項目，並由 `end_request` 彈出。
- **畫廊項目不能被媒體項目覆蓋**：e-hentai 的網址永遠到不了媒體儲存，那只從 X/Pixiv 的 handler 被呼叫。

---

## 歸檔快取沒作用：一個畫廊被存了兩次

回報是「归档下载快取功能没有作用」。量測一次歸檔請求把時間花在哪裡，就先看出哪個快取重要，然後才看出它為什麼從沒命中。

**41 秒花在哪**（真實 27 頁畫廊，`歸檔 · 原圖`）：

| 階段 | 時間 | 哪個快取可以省掉 |
| --- | --- | --- |
| 讀畫廊資料 | 1.7s | — |
| viewer 網址掃描（27 頁） | 4.5s | 畫廊快取 |
| 解析壓縮檔連結 | 3.1s | 歸檔快取 |
| 下載 76.3 MiB | 31.9s | 歸檔快取 |
| **總計** | **41.2s** | |

### 畫廊快取沒命中，因為鍵包含了主機

e-hentai.org 與 exhentai.org 用同一個 `gid` 與 `token` 提供同一個畫廊，但快取項目存在「請求進來時的那個網址」底下。快取被發現把同一個畫廊**存了兩次**：

```
https://e-hentai.org/g/3778922/19330e2349   -> 27 個 display_urls
https://exhentai.org/g/3778922/19330e2349   ->  0 個 display_urls
```

所以送出一個 exhentai 連結、再按按鈕就是快取沒中，而按鈕寫入的項目也永遠不會滿足連結 handler。一個畫廊第一次是從哪個主機被看到，就只有那個項目會被重用——第二個是死重。

`gallery_cache_key()` 現在以 `gid`／`token` 為鍵，把同一個畫廊的六種網址變體（兩個主機、結尾斜線、`www.`、`?p=N`）收斂成一個項目：

```
https://exhentai.org/g/3778922/19330e2349/     \
https://e-hentai.org/g/3778922/19330e2349/      |
https://e-hentai.org/g/3778922/19330e2349       |  全部 ->
https://e-hentai.org/g/3778922/19330e2349/?p=2  |  https://e-hentai.org/g/3778922/19330e2349/
https://www.e-hentai.org/g/3778922/19330e2349/  |
https://exhentai.org/g/3778922/19330e2349/?p=3 /
```

### 壓縮檔本身從沒被保留

下載下來的壓縮檔根本沒有快取——每次請求都讓 e-hentai 重新打包並重新傳輸整個檔案。那是 41 秒裡的 35 秒。現在它被保存在 `.cache/archives/` 底下，以畫廊**與**變體（1280 與原圖是不同的檔案）為鍵，重複就是重送：

```
49.7s   ->   0 ms        （76.3 MiB，第二次請求）
```

檢查跑在 `resolve_official_archive` **之前**，不是之後：解析本身就是一次對 archiver 的網路往返，而它正是快取存在的目的之一。有測試釘住這個順序。

幾個重要的細節：

- **有自己的上限。** `ARCHIVE_CACHE_MAX_BYTES`（預設 8 GiB）與媒體快取的 2 GiB 分開，因為量級差好幾個數量級：300 件快取作品約 100 MB，而 300 個快取壓縮檔會是幾十 GB。最久未使用優先淘汰，所以你一直回頭看的畫廊會活下來。
- **可以用 `ARCHIVE_CACHE=0` 關掉**，不必改程式碼——那正是除錯時通常想要的。
- **原子寫入**（暫存檔加 rename），所以中途被殺不會留下一個之後看起來像完整壓縮檔的截斷檔案。0 位元組的項目永遠不會被送出。
- **重送失敗會退回下載。** 快取只是最佳化；它的任何失敗都不能讓使用者失去檔案。
- **在 `/cache` 裡顯示並可清除**，現在歸檔快取分開回報——它住在 `CACHE_DIR` 裡面，所以要從媒體的數字裡排除，否則會被算兩次。

冷路徑不變：一個畫廊的第一次請求仍然要付完整的 41 秒。

---

## 啟動期的兩個地雷：`logger` 未定義、空白 `.env`

在測試上面那個改動時發現的，而兩者都是同一個錯誤的兩種形式：**一條在開發期間從未走過的失敗路徑。**

### `logger` 定義在使用它的程式碼之後

`logger` 建在檔案幾百行之後，但 `Authorization.load()` 在 import 時就跑了，位置在它上面很多——而 `load()` 與 `save()` 都會從失敗路徑記警告。正常啟動時兩條路徑都不走，所以未定義的名稱從未出現。一走上去，機器人就在 import 時死亡：

```
NameError: name 'logger' is not defined
```

指向一個警告呼叫，而不是原因。現在日誌在**任何會記錄的東西之前**就設定好。**一個警告永遠不該能讓機器人起不來。**

### `os.getenv("X", default)` 對空值不會套用預設

`.env.example` 列出每一個選用鍵，而說明叫你複製它。它裡面每個選用值都是空的——而 `int("")` 會拋出：

```
ValueError: invalid literal for int() with base 10: ''
```

所以照著範本複製會做出一個起不來的機器人，錯誤還指向離原因很遠的一行。同樣的陷阱對路徑有更安靜的版本：`Path("")` 是當前目錄，所以空白的 `AUTH_FILE` 或 `CACHE_DIR` 會默默指向儲存庫資料夾，而不是預期的檔案。

現在兩者都由 `env_number()` 與 `env_text()` 處理，它們把缺少、空白或只有空白的值當成未設定。有一個測試把 `.env.example` 原封不動複製到臨時目錄、把**每一個**值清空，並斷言機器人仍然可以 import：

```
AUTH_FILE  = authorized.json
CACHE_FILE = parse_cache.json
CACHE_DIR  = .cache
MAX_FILES  = 20   DEADLINE = 300
```

寫那個測試時還抓到我自己的另外兩個錯：一個輔助函式引用了 `time`，而模組把它匯入成 `_time`，所以它拋 `NameError` 並默默回 `False`；還有一個機械性的正規表示式在改寫 `int(os.getenv(...))` 時跨過了 `try/except` 邊界，產生語法錯誤。之後那些地方是手工修的。

---

## 歸檔沒有總時間上限，而頁面圖片全進了記憶體

**同一條下載路徑上的兩個獨立問題。**

### 官方壓縮檔只有每次讀取的上限，沒有總量上限

唯一的限制是 300 秒的*每次讀取*逾時，而一個慢但穩定的傳輸永遠不會觸發它——所以一個停滯或被限速的下載，可以無上限地暫存一個任意大的檔案。現在整個傳輸有一個牆鐘期限 `ARCHIVE_DEADLINE_SECONDS`（預設 **300 秒／5 分鐘**），涵蓋探測、每一個範圍 worker 與單流退回。

兩個容易弄錯的細節：

- **一個期限，不是每條路徑一個。** 它在平行／單流的決策之前建立並交給兩者，所以退回單流不會重新開始一份預算、把等待變兩倍。
- **逾時是自己的例外型別。** 範圍請求也可能因為 HTTP 狀態拋 `EhentaiError`，而那一個*值得*重試。重用同一個型別會讓逾時被當成「失敗的範圍」重試，燒掉第二份完整預算，然後回報**下載損壞**而不是逾時。

每次讀取逾時抓不到真正要緊的情況，所以測試提供的是一個永遠滴水的串流，並斷言它被中止：

```
預先過期的期限 -> 256 KiB 之後 1.2 秒中止
預算 2.0s      -> 2.6 秒後中止
```

參考數字：真實的 76.3 MiB 原圖在這裡約 27 秒完成，所以 300 秒的預算大約是正常情況的十倍，只有真正停滯的傳輸會碰到它。

### 頁面圖片被整份讀進記憶體

`download_gallery_direct` 做了 `response.content` 再寫出位元組，而 `EHENTAI_DOWNLOAD_WORKERS` 同時在跑——好幾張完整圖片同時駐留，毫無理由，而且沒有任何東西限制單一過大的檔案。現在每一頁以 256 KiB 區塊串流寫到 `.part` 檔再改名就位，所以不管圖片多大，記憶體都是一個 worker 一個區塊。

單一 worker、64 MiB 的頁面實測：

```
尖峰追蹤記憶體：4.03 MiB（檔案大小的 6.3%）
```

而串流下來的檔案與來源逐位元組相同（透過本機伺服器以雜湊驗證）。rename 才是讓它安全的原因：寫到一半的檔案只以 `.part` 名字存在，所以中途失敗永遠不會看起來像一張完整的圖。

## 停止按鈕沒作用，原因是一行缺失

PTB 預設**一次處理一個 update**，而按鈕按一下就是一個 update。長時間下載佔住唯一的名額時，按鍵根本沒被派送，所以它無法在下載結束、旗標被清掉之前設定停止旗標。使用者收到的訊息是「目前沒有進行中的下載」——而下載還在跑。

改任何東西之前，先對 PTB 自己的 update processor 重現：

```
目前（一次一個）       下載期間 stop 被派送：False
concurrent_updates(4)  下載期間 stop 被派送：True
```

修法是 `concurrent_updates(MAX_CONCURRENT_UPDATES)`，它同時移除一個阻斷服務問題：一個 15 分鐘的請求以前會凍住**所有**其他使用者，不只按鈕。

### 第一次的 admission control 也是錯的，而量測顯示了它

光有併發會把每個請求的 worker 池相乘，而那些是照序列執行調的，所以重的畫廊工作由 `gallery_semaphore` 限流。第一版讓請求**等待**名額。六個工作提交後實測：

```
6 個工作排隊時 stop 被派送：False
尖峰同時重型工作：2（上限也是 2）
```

一個停在 semaphore 上的工作仍然在它的 handler 裡面，而 handler 在整個等待期間佔住一個 `MAX_CONCURRENT_UPDATES` 名額——所以每個名額都被一個什麼都不做的工作佔著，而 stop 要等到其中一個完成才會被派送。**同一個死掉的按鈕，換了一個觸發條件。** 現在程式碼不等待就取名額，並立刻拒絕第三個請求：

```
其他工作在跑／排隊時 stop 被派送：True
尖峰同時工作：2（上限 2）
已服務／已拒絕：2 / 4
```

「忙，請重試」勝過一個凍住的機器人，而且拒絕是立刻到達，不是幾分鐘後。

### 這件事的測試一開始也是錯的

最初的測試建了**自己的** `Application` 並套 `concurrent_updates`，所以從機器人裡刪掉那個呼叫，它們全部照樣通過——它們斷言的是測試自己的程式碼，不是機器人的。這就是 `build_application()` 存在的原因：它是 Application 被組起來的唯一地方，而測試走它。現在刪掉那一行會讓三項檢查失敗，包括那個按下 stop、並斷言執行中的工作看到旗標的端到端測試。

---

## viewer-link 正則被硬編碼成 e-hentai.org

`_VIEWER_LINK_RE` 只匹配 `https://e-hentai.org/s/...`，所以**每個 exhentai 畫廊的 viewer 連結都是零**，而那個功能在那裡以「讀不到畫廊的圖片清單。連結可能有誤，或畫廊已被刪除。」失敗——這句話會讓使用者去找一個不存在的壞連結，而頁面其實是好的：

```
e-hentai.org   https://e-hentai.org/s/...   -> 27 個匹配
exhentai.org   https://exhentai.org/s/...   ->  0 個匹配
```

現在樣式兩邊都吃。有測試釘住兩者，也釘住無關的主機不會被誤認。

## exhentai 每頁 20 張——而 20 剛好等於 `MAX_FILES`

exhentai 每頁顯示 20 張（「Showing 1 - 20 of 27 images」），而 e-hentai 一次回傳整個畫廊。上限被套在分頁迴圈**裡面**：

```python
for page_index in range(1, pages):
    if len(links) >= max_images:
        break
```

因為 `max_images` 就是 `MAX_FILES`、而 exhentai 的頁大小正好是 20，第一頁就填滿了預算，迴圈立刻中斷。**分頁程式碼一到埗就是死的**，一個 27 張的畫廊停在 20。上限現在套在分頁之後。

比上限大的畫廊也會被**回報**而不是默默截短——進度訊息的最後一次編輯會保留「（作品共 27 張，單次最多 20 張）」，這很重要，因為那才是使用者真正讀到的那份。以前截短是看不見的；要完整的集合請用 `歸檔`。

## e-hentai 的 39 位元組拒絕是真的回答，不是逾時

與代理問題分開，e-hentai 對某些請求回 **HTTP 200** 加一段 39 位元組的內容：

```
Key missing, or incorrect key provided.
```

**狀態碼檢查看不到它，因為它是 200。** 它由 `parse_gallery_page` 靠內容偵測，並回報為存取被拒絕，而不是「連結有誤或畫廊已被刪除」——後者會讓使用者去找一個不存在的問題。`EHENTAI_COOKIES` 對它有用：當原因是 session 相關時，一份新的 cookie 可以解除拒絕。

重試幫不上忙，因為那是一個經過考慮的回答，不是掉線。

## e-hentai 也會對突發流量節流

首頁可以回 200，而畫廊路徑逾時——那是真實的 TCP 層節流，也是 `request_with_retry` 存在的原因。**它的窗口是照實測行為調的，不是為了看起來合理：** 三次嘗試、短的固定延遲。早先一個五次、每次加倍的排程在測試期間打到約 120 個請求，然後主機開始直接拒絕畫廊路徑，所以更重的重試讓事情更糟而不是更好。

只有連線層的失敗會重試。HTTP 狀態是一個經過考慮的回答，重試它只會重複拒絕，同時讓使用者等。

## cookie 檔可能包含多個站點——要依網域過濾

Netscape cookie 檔不一定是單站的。這裡的那一份帶著 `.pixiv.net` 與 `.x.com` 的列，和 e-hentai 的放在一起，而讀取器把**每一個** cookie 送到**每一個** e-hentai 請求——一次多站洩漏。現在會對 `ehentai.COOKIE_DOMAINS` 過濾，用後綴比對，所以開頭帶點的列仍然匹配它自己的子網域。實測標頭從 1707 個字元降到 1041 個，外站的 cookie 不見了。

## `base_url` 必須以 `/bot` 結尾——token 是附加的，不是插入的

把機器人指向自架伺服器只要一行，而弄錯一點就會在 bootstrap 期間殺掉機器人：

```python
builder.base_url(telegram_base_url())   # 必須以 "/bot" 結尾
```

python-telegram-bot 這樣解析那個值：

```python
def _parse_base_url(value, token):
    ...
    return value + token          # 附加 token
```

它**不會**替換 `{token}` 佔位符，所以值本身必須已經以 `/bot` 結尾——這就是為什麼函式庫自己的預設是 `"https://api.telegram.org/bot"`。傳入裸的伺服器根路徑，會產生一個把 token 黏在埠號上的網址：

```
Bot.base_url = 'http://127.0.0.1:<REDACTED-TOKEN>'
httpx.InvalidURL: Invalid port: '<REDACTED-TOKEN>'
```

那個訊息完全指不到原因——它讀起來像格式錯誤的 token 或壞掉的埠，而不是少了 `/bot`。所以模組提供**兩個**輔助函式，而且不能互換：

| 輔助函式 | 形狀 | 給誰用 |
| --- | --- | --- |
| `telegram_server_root()` | `http://127.0.0.1:8081` | 自己組網址時 |
| `telegram_base_url()` | `http://127.0.0.1:8081/bot` | PTB 的 `base_url()` |

`telegram_base_url()` 在沒有設定本機伺服器時也會回 `""`，所以 `main()` 可以對它做真值測試並整個跳過 `base_url()`。有一項特徵化檢查釘住 PTB 的實際行為，所以某個版本若改成 `{token}` 佔位符，會在那裡就大聲失敗，而不是在下一次開機時。

---

## 過濾網路：自架伺服器需要透過代理走 MTProto

這台網路上 Telegram 的 MTProto 主機**部分被過濾**。實測：

| 來源 | 結果 |
| --- | --- |
| 主機，直連 | 有些位址開（`91.108.4.204`），有些被擋（`149.154.167.50`） |
| 容器，直連 | **全部**被擋——Docker 被過濾得更兇 |
| 兩者，經主機 SOCKS5 | **全部**開 |

所以未經處理的映像檔無法獨力登入。它會啟動、看起來健康，然後卡在 `getMe`、日誌一片空白——很容易被誤認為 `api_id` 填錯。`TDLib` **忽略代理相關的環境變數**，而伺服器自己的 `--proxy` 只給 webhook 用，所以那個映像檔的任何設定都修不好。

`Dockerfile.bot-api-proxy` 包住映像檔並加上唯一有效的做法：

| 元件 | 角色 |
| --- | --- |
| `redsocks` | 在本地監聽，把流量轉給上游 SOCKS5 代理 |
| `iptables` NAT 規則 | 把所有發往 Telegram 位址範圍的流量送到 redsocks |

TDLib 什麼都不知道，所以也沒有東西可以被它忽略。Telegram 的**位址範圍**是整段重導，而不是靠位址清單，因為 TDLib 在執行時抓取當前的資料中心位址、而且會持續發現更多——一份清單在默默失效之前都可用。需要 `NET_ADMIN`，也是唯一新增的能力。

```ini
TELEGRAM_MT_PROXY=socks5://192.168.65.254:17900
```

**主機代理必須能從容器連到**，這是最常讓人卡住的地方。它只綁 `127.0.0.1`，所以容器用不到，而 `host.docker.internal` 也沒用——它解析到 Docker VM 的位址，那裡沒有東西在聽。`host_proxy_relay.py` 搭這座橋：

```sh
python host_proxy_relay.py            # 0.0.0.0:17900 -> 127.0.0.1:7897
python host_proxy_relay.py --selftest # 驗證整條鏈路（在容器裡跑）
```

它是 **TCP 轉接，不是代理**——客戶端仍然自己做 SOCKS5 握手——而且沒有相依套件。

**需要一條 inbound 防火牆規則**，因為 Windows 預設會擋掉來自容器的連線。少了它，轉接在主機上完美運作，而容器回報它不可達，這是一組很令人困惑的事實：

```powershell
# 以管理員 PowerShell 執行一次：
New-NetFirewallRule -DisplayName "bot-api relay" -Direction Inbound `
  -Protocol TCP -LocalPort 17900 -Action Allow
```

**轉接必須在機器人活著時一直活著。** 伺服器沒有它就连不到 Telegram，所以它屬於啟動流程，而不是某個終端機。診斷要分三層、依序做：

```sh
# 1. 轉接在本機通嗎？   （主機）     -> SOCKS5 CONNECT ok
# 2. 容器連得到轉接嗎？ （容器）     -> --selftest 回報 CONNECT ok
# 3. 伺服器登入得了嗎？ （主機）     -> getMe 回 200
```

### 歷史註記：代理之前那個網路長什麼樣

下面兩個故障都是真的、也都修好了，但它們共用的*症狀*——一個健康卻卡在 `getMe` 的容器——與缺少代理時的症狀完全一樣。在懷疑它們之前，先檢查上面三層。

| 故障 | 影響 | 修法 |
| --- | --- | --- |
| 工作目錄是 **Windows bind mount**（9p/drvfs） | TDLib 的 binlog rename 失敗、`Signal: 6`、伺服器 crash-loop 並對所有請求回 502——與憑證錯誤無法區分 | 改用**具名 volume**，它是 ext4 |
| 一次失敗執行留下的過期 volume | TDLib 過不了初始化，日誌沉默 | 清掉 volume，不只是容器 |

### `--local` 才是關鍵旗標

沒有它，伺服器仍套用公開的 50 MB 上限，所以切換等於沒換。`TELEGRAM_LOCAL=1` 是機器人自己對「伺服器是那樣跑的」的聲明——伺服器不會回報自己的模式，而把 `--local` 當成開著會讓**每一次**檔案傳送都失敗，而不是優雅降級。

機器人啟動時是**探測**而不是假設：它呼叫 `getMe`，只有在真的有伺服器回應時才提高上限。所以連不到或設定錯誤的伺服器會讓公開上限繼續生效，而 `/status` 會回報目前生效的上限與原因。

```ini
TELEGRAM_API_BASE=              # 空白 = 用 api.telegram.org
TELEGRAM_LOCAL=                 # 只有伺服器以 --local 執行時才設 1
```

`run_local_api.py` 是一層薄而可讀的包裝。有 Docker 時優先用容器（不需要編譯器）；它讀 `.env`、檢查憑證、探測 8081 是否已有東西在回應、印出指令列時遮蔽 `api_hash`，並回報缺什麼。它**不**負責編譯伺服器。

### 加入 `--ensure` 時寫出的兩個 bug

- `subprocess.CREATE_DETACHED_PROCESS` 不存在——常數是 `DETACHED_PROCESS`，沒有 `CREATE_` 前綴。
- 上游代理埠是從 `TELEGRAM_MT_PROXY` 讀的，而它是 `socks5://192.168.65.254:17900`——**轉接自己的位址**，所以轉接被設定成把流量轉給自己、在同一埠上。上游是主機代理的 7897，來自機器人自己用的那個 SOCKS5 設定；`upstream_proxy_port()` 會讀它。

---

## 其他零散紀錄

- **`my.telegram.org` 的 `ERROR` 對話框從不說原因。** 頁面自己的 `createApp()` 在失敗時把原始 jQuery XHR 物件交給 `alert()`，所以伺服器真正的訊息被換成一個沒用的 `ERROR`——只有*成功*的 HTTP 回應才會把 body 拿去 alert。要看真正的理由，在 `/apps` 的瀏覽器主控台（F12 → Console）跑這段：

  ```js
  $.post('/apps/create', {hash:$('[name=hash]').val(),
    app_title:'shiroko9008parselinkbot', app_shortname:'skr9008plbot',
    app_url:'https://example.com', app_platform:'desktop', app_desc:''})
   .done(t => alert('OK: '+JSON.stringify(t)))
   .fail((x,s,e) => alert('HTTP '+x.status+' :: '+x.responseText));
  ```

  它會印出狀態與 body，而不是吞掉。解讀：

  | 結果 | 意思 |
  | --- | --- |
  | `OK: ...` | 建立了——重新載入 `/apps` 讀 `api_id` / `api_hash` |
  | `HTTP 401 :: Unauthorized` | session 或頁面的 `hash` 過期——重新載入 `/apps` 再試 |
  | `HTTP 429` 或 *too many tries* 的 body | 登入速率限制——**停止重試**，等幾小時或換網路 |
  | 任何其他 body 文字 | 某個欄位被拒；文字會說是哪一個 |

- **`run_local_api.py --verify-credentials` 只驗格式。** Telegram 沒有提供「不帶認證就確認 api_id/api_hash」的端點，所以唯一真正的測試是啟動伺服器，它會在啟動時認證並大聲失敗。

- **`R2_PUBLIC_BASE` 必須是網址。** 它不以 `http://` 或 `https://` 開頭時整組 R2 設定視為未設定並記錄警告；只設定一部分也一樣，因為半套設定會在畫廊下載完之後、第一次上傳時才失敗。

- **`r2.check_connection()` 用一次往返驗證憑證**，讓錯的金鑰被回報成它自己，而不是每一張圖各自失敗。

- **`bucket_usage()` 讀不到用量時拋 `R2Error`，不回傳猜測值。** 不知道用量的呼叫端必須自己決定怎麼辦；假設為零會讓檢查完全失效。

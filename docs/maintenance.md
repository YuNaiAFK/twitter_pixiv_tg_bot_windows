# 維護與疑難排解（Windows）

[回到 README](../README.md)

- [快取策略](#快取策略)
- [R2 圖片代管](#r2-圖片代管)
- [標籤翻譯](#標籤翻譯)
- [e-hentai 解析流程](#e-hentai-解析流程)
- [疑難排解](#疑難排解)
- [指令列小工具](#指令列小工具)
- [安全性](#安全性)
- [致謝](#致謝)
- [授權與免責](#授權與免責)

## 快取策略

**這裡只有一件事值得記住：便宜的事每次重做，昂貴的事才複用。**

| 資料 | 成本 | 策略 |
| --- | --- | --- |
| 標題、tag、評分 | **1 個請求** | **每次都重讀**（`GALLERY_META_TTL_SECONDS=0`）|
| 掃描到的圖片網址 | **每頁 1 個，53 頁 = 53 個** | 2 小時（`GALLERY_URL_TTL_SECONDS`）|
| Telegraph 頁面網址 | 建立一個新頁面 | 永久，直到條目過期 |
| R2 圖片網址 | 每張重新計算雜湊 | 永久，直到條目過期 |
| X／Pixiv 檔案 | 重新下載 | 14 天（`CACHE_STALE_SECONDS`）|
| 官方歸檔 ZIP | 幾十 MB 的下載 | 8 GiB 上限內 |

解析過的連結由快取供應，重貼一次不花下載、也不花請求。在 5 頁畫廊上實測：
**1.93 秒 → 0.02 秒**，因為逐頁掃 viewer 的動作整個被跳過。Pixiv 與 X 同理：

```
第一次請求    2.09s   Pixiv 解析完成，已傳送原圖 2/2 個檔案。
第二次請求    0.01s   已從快取傳送 2/2 個檔案。
```

> **這一層分開是有代價換來的。** 原本 `CACHE_STALE_SECONDS`（14 天）管整個 payload，
> 所以 e-hentai 上編輯過的 tag 要**兩週**才會出現。改成 6 小時的 metadata TTL 之後仍然不夠 ——
> 實測一本 47 分鐘前快取的本子（`命中畫廊快取（2853 秒前）`）照樣顯示舊標籤。
> **省下的是「一個請求」，而代價是一個錯誤的答案，那不是節省。**

其他規則：

- **快取命中不受連結冷卻限制**：冷卻擋的是重複的*下載*工作，而快取命中沒有下載；
  套用冷卻只會讓一個已經在磁碟上的答案白等十秒。沒有任何快取時，冷卻照舊
- **快取命中也會清理聊天室**：只清除 bot 的進度提示，保留使用者原始連結。
  少了這段清理，快取反而留下更多雜訊
- 送出時使用**暫存目錄**，絕不直接用快取目錄：超大圖片會先寫一份縮圖，
  指向快取目錄就會覆蓋正在被快取的檔案
- **失敗的結果刻意不快取**：失敗通常是暫時的，或是一組管理員後來修好的憑證；
  快取它會讓一次壞運氣變成永久
- **刻意不使用 `gallery-dl --download-archive`**：它與「每次請求用全新的暫存目錄」衝突 ——
  archive 說「下載過了」，gallery-dl 於是跳過，而每個請求都要新目錄，結果是重貼時什麼都沒寫，
  機器人卻回報「沒有找到可傳送的圖片或影片」。重複使用上一次的解析結果是快取的工作；
  快取沒中時，重新下載才是正確行為
- **快取的圖片網址還是會過期**：e-hentai 的圖片連結有時效，所以快取的掃描結果只能撐一陣子；
  `重新生成 Telegraph 頁面` 是重新整理這種頁面的正式做法，它會刻意忽略快取，並把新的網址寫回去
- 快取在兩個維度都有上限，並優先清掉最舊的項目（連同它的檔案一起刪 —— 只丟索引會漏出
  無人引用的磁碟空間）。損壞的快取檔會被丟棄而不是致命錯誤，因為快取只是最佳化，
  失去它只損失時間
- **只存在一部分**的檔案集會被視為沒有快取，因為送出半個作品比重新下載更糟
- `/cache` 顯示內容、命中／未命中次數與磁碟用量，`/cache clear` 清空，兩者都只有管理員能用

## R2 圖片代管

e-hentai 內嵌的圖片是帶簽章、會過期的連結（`*.hath.network`），一旦過期，Telegraph 頁面上的
**每一張圖同時**壞掉。`重新生成` 就是為了繞過這件事而做的：重新掃描、重新簽章、原地改寫頁面。

把圖片複製一份到 **Cloudflare R2** 則根本沒有這個問題：物件存在多久，URL 就穩定多久 ——
一個月前解析過的畫廊，也能在完全不碰 e-hentai 的情況下送出。

- **物件鍵是內容雜湊**（新物件使用 `galleries/ab/cd/<sha256>`，圖片格式由 `Content-Type` 指定）。
  同張圖上傳兩次就是同一個物件，所以重新解析只補缺的；也因為 URL 不能從畫廊 id 猜到，
  知道一個路徑無法列舉桶裡還有什麼。Telegraph 是匿名抓圖的，URL 必須公開，
  雜湊是讓它「公開但不可搜尋」的手段
- **上傳前先查詢重複檔案**：內容相同就沿用 R2 網址，即使檔名或副檔名不同也不重傳；
  舊版帶副檔名的物件同樣會被辨識。同一批或不同畫廊同時出現相同圖片時，也會共用物件。
  寫入使用 [R2 條件式上傳](https://developers.cloudflare.com/r2/api/s3/api/#conditional-operations)，
  防止其他程序同時建立相同檔案；若查詢失敗，則交給自動重試流程處理。
- **`Cache-Control: public, max-age=31536000, immutable`**：物件不可變，沒有理由給更短的快取
- 單張圖片上限 **32 MiB**；平行上傳預設 **6** 個 worker（`R2_UPLOAD_WORKERS`）、
  單張逾時 **60 秒**（`R2_UPLOAD_TIMEOUT`）

### 免費額度與上限

Cloudflare 文件上的免費額度是每月 **10 GB** 儲存空間（程式以十進位計算：`10 * 1000**3` 位元組，
因為 Cloudflare 以 GB 計費）。實際上傳會停在額度的 **95%**（預設即 **9.50 GB**），
可用 `R2_FREE_BYTES` 與 `R2_LIMIT_PERCENT` 調整。

**留 5% 是必要的，不是保守**：儲存按月、依物件存在與否計費，剛好頂到額度、
一週後才被清掉的畫廊，那個月仍然超額。

- **上限在 `put` 之前檢查**（`已儲存 + 這張圖 <= 上限`），不是之後 —— 物件一寫入就開始計費
- **用量從桶子讀**（`ListObjectsV2`），不靠機器人自己記的數字：本機計數在有人從 Cloudflare
  後台刪東西之後就失準，從此不是誤擋就是超額；而列出物件是 class B 操作，
  免費額度是每月一千萬次，問一下並不貴
- 額度用完時**停止上傳、通知管理員、其餘圖片沿用原始網址**，結果上會出現
  「⚠️ R2 免費額度已滿，這次不改用 R2（已通知管理員）。」。這與「上傳失敗」是不同狀態：
  沒有東西壞掉，只是被刻意擋下
- **暫時上傳失敗會自動重試**，每次只重傳失敗的圖片；連續失敗 3 次後會詢問發起解析的使用者。
  可選擇再重試 3 次，或停止本次 e-hentai 解析；停止後不會建立 Telegraph 頁面。15 分鐘未選擇時，
  也會停止本次解析。
- 沒設定 R2 時會沿用 e-hentai 原始網址；`boto3` 則延後匯入，未安裝不會影響機器人啟動。
  成功上傳的 R2 網址會依「畫廊 + 模式」記進解析快取，重按同一個按鈕不會再傳一次

需要的五個設定值：`R2_ACCOUNT_ID`、`R2_ACCESS_KEY`、`R2_SECRET_KEY`、`R2_BUCKET`、
`R2_PUBLIC_BASE`（匿名可讀的網域）。端點固定是 `https://<account_id>.r2.cloudflarestorage.com`；
`R2_PUBLIC_BASE` 必須是 `http://` 或 `https://` 開頭，否則整組視為未設定。

## 標籤翻譯

標籤名稱以繁體中文（台灣）呈現，旁邊保留原始英文，所以遇到不熟的翻譯也不會遺失資訊：

```
• 女性 / Female：眼罩（eyepatch）, 哥德蘿莉（gothic lolita）, 雙馬尾（twintails）
• 其他 / Other：差分集（variant set）
```

專有名稱 —— 作者、社團、角色與原作名 —— 沒有中文形式，所以直接顯示而不重複：
`momozu komamochi`，不是 `momozu komamochi（momozu komamochi）`。

**對照表有 14,324 筆**，在 `tag_translations.py`，由 `tools/build_tag_translations.py` 從
[EhTagTranslation/Database](https://github.com/EhTagTranslation/Database) 產生。
它是提交進版控的資料，不是執行時抓的，所以畫一個畫廊永遠不需要第二個網路服務。

**兩個轉換都是必要的：**

1. **簡體轉繁體用 `s2twp` 而不是 `s2t`。** `s2t` 只映射字符，所以「打印机」會變成「打印機」
   而不是「印表機」—— **字符轉換不是翻譯。** `s2twp` 連詞彙一起轉
2. **圖示標記會被剝掉。** 名稱欄可能是 `![贝合图标](https://...)贝合`，直接切分會把 img 標籤
   留在變成 hashtag 的字串中間

**artist 與 group 刻意不收。** 那兩者有 30,000 條，而它們的「翻譯」是筆名的羅馬拼音 ——
`endou hiroto` 變成「遠藤弘土」，沒有人會那樣搜尋，而且比原本的字串更長。

**同一個名字在不同命名空間有不同意思** —— `angel` 是女性的「天使」也是角色的「安吉爾」。
機器人只用值查表，所以只能留一個，而留的應該是**通用的描述性標籤**，不是剛好同名的角色名。
因此最特定的（character）先解析、最通用的（female）最後 —— **最後寫入的勝出。**

不認得的標籤會原樣通過，而不是消失。e-hentai 會替某些標籤加上 wiki 註記
（以前會顯示成 `japanese | definition revised please read the wiki`）——
那是站方樣板而不是標籤的一部分，所以在顯示與翻譯之前就被移除。

**更新字典：**

```bash
git clone --depth 1 https://github.com/EhTagTranslation/Database.git ../_ehtags
python tools/build_tag_translations.py
```

## e-hentai 解析流程

貼上像 `https://e-hentai.org/g/3672745/82d9d6d00e/` 這樣的連結，機器人會回覆畫廊資料與
依命名空間分組的每個標籤（Language、Artist、Group、Parody、Character、Female、Male 等等），
加上四個動作按鈕。

一次 HTTP GET 就能取回標題（英文與日文）、分類、語言、評分、頁數、大小、日期、收藏數、
封面與完整標籤清單，約 **1.4 秒**。這是在 53 頁畫廊上實測各種替代方案之後選的：
`gallery-dl -j` 要 **275 秒**（加上 `metadata: true` 也要 **265 秒**），而它的 `source: metadata`
模式雖然只要 1.4 秒，卻**沒有**分類名稱、沒有語言、標籤還是扁平的 `namespace:tag` 字串。
完整量測表在 [docs/NOTES.md](../docs/NOTES.md)。

解析用的正規表示式每一個都對真實的 HTML 驗證過，因為「看起來很明顯」的模式會默默比對到錯的東西：
封面藏在行內 background style 裡（頁面第一個 `<img>` 是評分圖），而 `<div id="taglist">`
並沒有直接包含那些 `<tr>` 列。

### 一次請求一則訊息，封面就在裡面

畫廊封面會變成回覆的本體，結果是**一則訊息**：封面照片，畫廊資料、標籤與頁面連結都是它的說明文字。
做法是**編輯機器人已經送出的那則訊息**，而不是再貼一則 —— 把封面另外貼出去會讓每個回覆都變成
兩則訊息，讀起來像機器人無緣無故重複自己。

圖片以**網址**引用，所以是 Telegram 自己去抓，機器人不為它下載任何位元組。它用的是 e-hentai
的靜態縮圖主機（`ehgt.org`），而不是頁面圖片那種有時效的 `hath.network` 連結，
所以貼在聊天室裡的封面會一直有效。縮圖不見了不算錯誤：同一則訊息會退回純文字，請求照樣繼續 ——
為了一張封面而拒絕送出整個畫廊，是錯的取捨。兩種情況下都會附上 `重新生成` 按鈕，
因為 `edit_media` 換掉訊息本體時會把它弄掉。

**只有 X/Twitter 會帶來源連結，Pixiv 只送圖片。** 這是明確要求的，而且是每次呼叫的選項
（`attach_link`）而不是全域設定，因為兩個平台的設定不同。連結放在第一張的說明文字，
所以會顯示在圖片上方，而且只替第一張加說明：Telegram 不接受媒體群組裡每一張都有說明，
而且本來就只需要一個連結。**位置由 Telegram 決定，不是我們** —— 相簿的說明文字是客戶端
「對著媒體」畫的，早先把連結掛在後面幾張時，它會落在整組中間。

**Telegraph 頁面不會重複封面**：頁面是標題、資料、依命名空間分組的標籤，然後是頁面圖片。
封面被略過 —— 它已經在聊天訊息裡，放在頁面頂端只是花掉真正頁面需要的內容額度。

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

收集階段 —— 每頁讀一個 viewer 頁面 —— 有自己的進度條，因為在 53 頁的畫廊上光這一步就要約一分鐘。
更新被節流成每 1.5 秒一次編輯，因為 Telegram 對訊息編輯有速率限制；最後狀態一定會寫入，
所以訊息永遠不會停在 99%。

下載期間，進度訊息上會有**停止**按鈕。下載一個大畫廊的每一頁原圖要幾分鐘、搬幾百 MB，
所以一定要能取消：

- 停止會殺掉 gallery-dl 子行程並回報「已停止下載」，不是失敗。（被殺的行程以非零結束，
  所以取消旗標要在結束碼之前檢查，否則刻意的停止會變成一個訊息空白的下載錯誤）
- 在壓縮檔建好之前取消，就不會送出壓縮檔。打包*之後*才停止會留下不完整的壓縮檔，
  所以請在下載階段停止，而不是傳送途中
- 註冊表以聊天室為鍵，所以在一個聊天室按停止不會殺掉另一個聊天室的下載；
  部分檔案留在該請求的暫存目錄裡，跟著它一起刪除

**停止有兩種機制，因為下載有兩種形狀：** gallery-dl（子行程，殺掉）與平行抓取
（**本行程內的執行緒**，沒有行程可殺，所以用旗標）。平行抓取是每個模式的正常路徑，包含 `歸檔`；
worker 在每次請求前後各檢查一次旗標，停止之後才到的結果會被丟棄而不是寫入。

### 歸檔用 e-hentai 的官方壓縮檔

`歸檔` 不再自己建壓縮檔，而是向 e-hentai 的 archiver 要它自己發佈的 ZIP。
這既更準確、也少做很多事：**完全不下載任何一頁** —— 一個 POST 取回壓縮檔網址，
一個串流的 GET 送出檔案，檔名是 e-hentai 自己的（`(C106) [circle] title [中国翻訳]-1280x.zip`）。

| 步驟 | 請求 | 結果 |
| --- | --- | --- |
| 1 | `POST archiver.php`，帶 `dltype=org` 或 `res` | 一個說「檔案正在準備」的頁面 |
| 2 | GET 其中 `hath.network/archive/...` 的連結 | **HTML**，不是檔案 —— 一個等待頁 |
| 3 | 同一網址加上 **`?start=1`** | ZIP，並由 `Content-Disposition` 命名 |

**第 2 步是陷阱**：那個網址回的是 HTML，把它當成檔案下載會得到 16 位元組的「檔案」。
用範圍請求（`Range: bytes=0-3`）讀出真正的檔名與總大小，不必拉下整個壓縮檔。
**同一頁上的 H@H 下載器刻意不用** —— 它需要一個正在跑的 H@H 客戶端，
否則只會回答 *"Your H@H client appears to be offline."*。

| 選項 | `dltype` | 實測大小 | Telegram |
| --- | --- | --- | --- |
| `歸檔 · 原圖` | `dltype=org` | **148.2 MiB** | ❌ 超過 50 MB 的機器人上限 |
| `歸檔 · 1280` | `dltype=res` | **3.2 MiB** | ✅ |

大小會在嘗試前先回報，而且仍然會試著上傳，所以答案是 Telegram 給的，而不是本機自己拒絕：

```
官方歸檔 148.2 MB，超過 Telegram 的 45MB 建議上限，仍嘗試傳送……
```

上傳被拒絕時，回覆會說明並指向 `歸檔 · 1280`（降取樣版本，通常裝得下）。
壓縮檔是**串流寫到磁碟**，絕不整份放進記憶體：原圖常常超過 100 MB，先讀進記憶體再交給 Telegram
既浪費又可能耗盡；同一個 `停止` 按鈕也能中止傳輸。官方檔名帶著畫廊標題，裡面會有 Windows
路徑不允許的字元（`:`、`?`、`|`），所以使用前會消毒：路徑分隔符被中和、保留裝置名
（`CON`、`NUL`、`COM1`）被避開、過長的名字被縮短。

### ⚠️ Telegraph 圖片連結會過期

Telegraph 是靠**去抓圖片的網址**來嵌入圖片，而 e-hentai 提供的網址（`*.hath.network`）是
**有時效、且綁定登入 session** 的。所以今天建好的 Telegraph 頁面，過幾天可能就圖片全壞。
標籤與畫廊資料那幾段是靜態 HTML，永遠正確。

要永久保存就用歸檔動作（那些送的是真正的檔案），或者在設定好 R2 的情況下改用 R2 ——
物件網址不會過期。要重建頁面，`重新生成 Telegraph 頁面` 按鈕會替同一個畫廊取得重新簽章的網址、
發佈新頁面並原地取代舊訊息；按鈕依聊天室與使用者記住，所以一個人不能重建別人的頁面，
更早的 session 留下來的過期按鈕會被拒絕，而不是用猜的。

**為什麼不乾脆把圖片永久代管** —— 實測結果：`catbox.moe` 回 `412 Invalid uploader`、
`0x0.st` 已停用上傳、`uguu.se` 3 小時後刪檔、`tmpfiles.org` 1 小時。
這裡沒有免費的永久圖片代管；如果你有付費或自架服務的 API 金鑰（imgbb、Cloudflare R2、自己的伺服器），
可以把圖片傳上去並永久嵌入 —— 本專案內建的 R2 支援就是這條路。

### 公開與成人畫廊

e-hentai 會用分類標記每個畫廊，例如 `Non-H`、`Manga`、`Doujinshi`。公開畫廊（`Non-H`、`Artbook`）
不需要登入；成人畫廊需要 session，而沒有 session 時，站方的回答與「畫廊已被刪除」一模一樣 ——
這就是為什麼機器人說「可能需要登入」，而不是說「找不到」。

**Telegraph 與成人內容。** Telegraph 的條款禁止成人素材，所以替 H 畫廊建的頁面**可能被移除**。
預設仍然會建，因為那是被要求的行為，但這個選擇是明確的：

```ini
TELEGRAPH_ALLOW_ADULT=1   # 1 = 永遠建頁面
                          # 0 = 成人畫廊只回文字資訊
```

每個頁面都用一個匿名 Telegraph 帳號，所以頁面彼此獨立：一個被移除，其他不受影響。

## 疑難排解

| 症狀 | 先檢查什麼 |
| --- | --- |
| **X/Twitter 沒有結果** | X 端限制，見下節。cookie 沒壞 |
| **e-hentai 好像「卡住」** | **代理**，不是站方 |
| **上傳超過 50 MB** | 改用 `歸檔 · 1280`，或架自架伺服器 |
| **封面不見了** | 見下節 —— 可能是 exhentai 主機或 `edit_media` |
| **tag 不更新** | 確認 `GALLERY_META_TTL_SECONDS=0` |
| **中文標籤不對或缺** | 更新字典（見[標籤翻譯](maintenance.md#標籤翻譯)）|
| **`停止` 按鈕沒反應** | `MAX_CONCURRENT_UPDATES` 有沒有設？ |
| **「Key missing, or incorrect key provided.」** | e-hentai 回 200 但拒絕，換 cookie |
| **每次跳「開啟檔案 - 安全性警告」** | 資料夾的完整性標籤 |
| **下載全部失敗在 `Connection refused`** | 梯子切成 TUN 了，把代理設定拿掉 |

### X/Twitter 常常沒有結果

反覆驗證過，包含對一則長期存在的公開貼文（NASA 的推文），以及 X 自己未認證的 syndication 端點
—— 後者回 HTTP 200 但內容是空的。cookie 沒問題：是 **X 對這類客戶端不給推文內容**。
這是 X 端的限制，上游追蹤在 [mikf/gallery-dl#9267](https://github.com/mikf/gallery-dl/issues/9267)，
不是這裡設定錯了。

- 用 `bot.py --check` 確認憑證；如果那裡已經顯示「已登入」，重匯 cookie 不會有幫助
- 試 `python -m pip install -U gallery-dl` —— 上游的更新偶爾會繞過 X 的改動
- Pixiv 不受影響

機器人會明確回報這個情況，而不是聲稱登入失敗。

### 上傳超過 50 MB

`歸檔 · 原圖` 常見是 148 MB 以上，公開 API 收不下。改用 `歸檔 · 1280`（約 3.2 MiB），
或架自架 Bot API 伺服器把上限提到 2000 MB（見[自架 Bot API 伺服器](setup.md#自架-bot-api-伺服器)）。

### 封面不見了，或 `重新生成` 失敗過

exhentai 畫廊的封面放在 `s.exhentai.org`，那個主機需要 session，而 Telegram 抓圖時不帶 cookie，
所以會被拒絕；程式會把這個主機改寫成公開的 `ehgt.org`（路徑完全相同），未知主機則拒絕改寫。
另一方面，`edit_message_to_cover` 會把結果訊息變成封面**照片**，而 Telegram 拒絕對媒體訊息做
`edit_message_text`；現在 `update_result_message()` 會看訊息本身：有 `message.photo` 就
`edit_caption`，否則 `edit_text`。

### 「Key missing, or incorrect key provided.」

e-hentai 有時候用 **HTTP 200** 加一段 39 位元組的內容回答。**狀態碼檢查看不到它，因為它是 200**；
程式靠內容辨識，並回報為「存取被拒絕」，而不是「連結有誤或畫廊已被刪除」——
後者會讓使用者去找一個不存在的問題。`EHENTAI_COOKIES` 對這種情況有用：當原因是 session 相關時，
換一份新 cookie 可以解掉。重試沒有幫助，因為那是一個經過考慮的回答，不是斷線。

### Windows 每次都跳「開啟檔案 - 安全性警告」

雙擊 `run.bat`（或它的捷徑）會問「你要開啟此檔案嗎？」並標示「未知的發行者」。
**原因不是 zone mark**（實測不存在），而是專案資料夾帶著可繼承的 **Low 完整性標籤**，
Windows 因此把它們當成不可信。以**管理員** PowerShell 修一次：

```powershell
icacls "D:\TG_Bot\twitter_pixiv_tg_bot_windows" /setintegritylevel m /t /c
```

要還原就 `/setintegritylevel l /t /c`。沒有提權的嘗試只會改到一部分檔案
（實測 3703 個中的 139 個），其餘回報 `Access is denied`，所以一定要提權。
完整的調查（SmartScreen、AppLocker、`Zone.Identifier`、火絨安全軟體）在 [docs/NOTES.md](../docs/NOTES.md)。

### `停止` 按鈕沒有反應過

PTB 預設**一次只處理一個 update**，而按鈕按一下就是一個 update。長時間下載佔住那個位置時，
按鍵根本不會被派送，所以 `停止` 一直到下載結束、旗標被清掉之後才生效。
現在用 `concurrent_updates(MAX_CONCURRENT_UPDATES)`，順帶解掉一個阻斷服務問題：
一個 15 分鐘的請求以前會凍住**所有**其他使用者。
重試的錯誤設計（等待 semaphore 反而讓同一個按鈕再次失效）記在 NOTES。

### 其他已知限制

- 公開的 `t.me/<name>` 網址無法只靠網址可靠地分類成群組、頻道、使用者或機器人
- 機器人不會加入或驗證 Telegram 邀請連結
- 只支援 X/Twitter 與 Pixiv 的作品連結，以及 e-hentai 的畫廊連結；其他站點不抓取
- 站方改版或認證變更都可能影響 gallery-dl
- 目前的範例用 polling 與暫存目錄，是單一行程的原型
- 對外大規模使用之前，需要更強的濫用防護與監控

## 指令列小工具

| 指令 | 做什麼 |
| --- | --- |
| `bot.py` | 啟動機器人 |
| `bot.py --check` | 印出設定、憑證、gallery-dl 與 Pillow 狀態 |
| `bot.py --check --live` | 再加上每站一次真實的認證請求 |
| `bot.py --dry-run` | 建好所有 handler 後結束、不連 Telegram |
| `bot.py --pixiv-token <url\|code>` | 從貼上的 callback URL 取出 OAuth `code` |
| `pixiv_login.py [--auto]` | 完成 Pixiv OAuth 流程並把 refresh token 寫進 `.env` |
| `install_pillow_offline.py` | 在 pip 裝不了時把 Pillow 裝進 `vendor/` |
| `python -m unittest test_r2_dedup test_local_uploads test_channel_publish -q` | R2 去重、本機製作與頻道確認的離線測試 |
| `run_local_api.py --ensure` | 確保 Docker 引擎、轉接與伺服器都活著 |
| `host_proxy_relay.py --selftest` | 驗證從容器到主機代理的整條鏈路 |

## 安全性

- 在聊天裡曝露的 token 要立刻用 `@BotFather` 的 `/revoke` 換掉；新 token 只放 `.env`
- 不要分享 `.env` 或 cookie。`.env` 與 `cookies/` 都被 gitignore，請保持這樣
- 公開使用時把 `BLOCKED_USER_IDS` 留空；要限制就把 ID 填進去。`/id` 會顯示數字的使用者與聊天 ID
- 對外公開之前先設好 `ADMIN_USER_IDS`；否則 `/cookies` 與 `/status` 就是不可用，而那正是安全的預設
- 只會抓 X/Twitter、Pixiv 與 e-hentai 的網址，所以訪客無法用機器人去碰任意位址
- 只下載或轉發你有權存取的內容；對外大規模使用前請先強化濫用防護
- **`/audit` 會把每一個套件拿去問 [OSV](https://osv.dev)**，回報有問題的套件與修復版本。
  查不到時它會明確說「這不代表沒有漏洞」，而不是回一句看起來很安心的「沒有發現漏洞」——
  那是這裡唯一真正要避免的錯誤

## 致謝

這個機器人站在一整排開源專案上：

| 專案／服務 | 用途 |
| --- | --- |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | Telegram Bot API 的 Python 封裝 |
| [gallery-dl](https://github.com/mikf/gallery-dl) | X/Twitter 與 Pixiv 的下載引擎 |
| [httpx](https://www.python-httpx.org/) | 所有對外 HTTP 請求 |
| [Pillow](https://python-pillow.org/) | 超大照片的縮圖處理 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 讀取 `.env` |
| [websockets](https://github.com/python-websockets/websockets) | `pixiv_login.py --auto` 讀 Chrome DevTools |
| [boto3](https://github.com/boto/boto3) | Cloudflare R2 的 S3 用戶端（選用）|
| [telegram-bot-api / TDLib](https://github.com/tdlib/telegram-bot-api) | 自架 Bot API 伺服器 |
| [Cloudflare R2](https://developers.cloudflare.com/r2/) | 不會過期的圖片代管（選用）|
| **[EhTagTranslation/Database](https://github.com/EhTagTranslation/Database)** | 標籤的中文對照表 |
| **[OpenCC](https://github.com/BYVoid/OpenCC)** | 簡體轉繁體（`s2twp`）|
| [e-hentai](https://e-hentai.org/)／[exhentai](https://exhentai.org/) | 畫廊資料、圖片與官方壓縮檔 |
| [Telegraph](https://telegra.ph/) | 畫廊頁面的發佈平台 |

另外感謝 [mikf/gallery-dl#9267](https://github.com/mikf/gallery-dl/issues/9267) 的追蹤紀錄。
程式碼在 <https://github.com/YuNaiAFK/twitter_pixiv_tg_bot_windows>。

> **`tag_translations.py` 是衍生作品，授權為 CC BY-NC-SA 3.0**，不是本專案自己的條款。
> 它由 `tools/build_tag_translations.py` 從 EhTagTranslation 資料庫產生（14,324 筆），
> 上游文本本身又是 EHWiki 標籤頁面的翻譯，以相同授權再散布。
>
> **非商業條款正是它與個人使用相容、與拿來收費不相容的原因。**

## 授權與免責

**授權：** 本儲存庫目前沒有 `LICENSE` 檔案，README 也沒有聲明授權條款；
在再利用之前請先向作者確認。唯一的例外是上面那段 `tag_translations.py` 的 CC BY-NC-SA 3.0。

**免責：** 請尊重各站的服務條款與著作權（下載權限見[安全性](maintenance.md#安全性)一節），
成人內容的發佈 —— 尤其是 Telegraph 頁面 —— 可能違反平台政策而被移除。

實作歷史、疑難雜症與安全稽核紀錄都在 [docs/NOTES.md](../docs/NOTES.md)。

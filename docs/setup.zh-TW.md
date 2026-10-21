# knee-mcp 安裝、設定與部署（詳細版）

> 首頁介紹見 [README.zh-TW.md](../README.zh-TW.md)。

讓 Claude 透過 MCP 查詢、檢視 Google Drive 指定資料夾裡的右膝 MRI／X 光影像。

- 圖片一律以 MCP **ImageContent**（`type: "image"`, `image/png`）回傳，單張 ≤ 1 MB，不會把 base64 當文字回傳。
- 回傳前一律遮蔽燒在影像上的個資（姓名、病歷號、生日、醫師名），遮完再 OCR 驗證；驗證不過就**不回傳**並回報錯誤。
- Google Drive 只用 `drive.readonly`，只從 `KNEE_ROOT_FOLDER_ID` 往下走，不會讀這個資料夾以外的檔案。
- Python + 官方 MCP Python SDK（2.x；v1 的 `FastMCP` 在 2.x 改名為 `MCPServer`），Streamable HTTP，必須驗證（Bearer token 或 OAuth）。

## 工具

| 工具 | 回傳 |
|---|---|
| `list_studies()` | 文字：study_id、日期、醫院、類型、序列數、張數，加上 issues（不符命名規則的檔名等） |
| `list_series(study)` | 文字：序列號、張數、張號範圍、切面（影像文字 OCR，如 `COR PD`、`Axial T2 FS`、`SAG PD`）、SL 範圍 |
| `get_contact_sheet(study, series, cols=5)` | 圖片：整個序列的縮圖，依張號排序，每格標 `I0013` 這類張號；附一行文字 |
| `get_slice(study, series, index, crop=None, zoom=1)` | 圖片＋一行文字（`2019-03-20_TSGH S7 I0013 \| COR PD \| SL 9.0 \| ST 3.0 \| size 512x512`） |
| `get_slices(study, series, start, end, crop=None)` | 最多 8 張圖片，每張後面接一行文字 |
| `compare(a, b)` | 圖片：左右並排，各標日期、醫院、序列、張號；`a`/`b` 為 `{study, series, index, crop?}` |

- `study` 可以用 `list_studies` 的 study_id（`2019-03-20_TSGH`），或只給日期（`2019-03-20`，當天只有一個檢查時）。
- `series` 接受 `7`、`"7"`、`"S7"`、`"S07"`，**S4 和 S04 視為同一序列**。
- **顯影劑：** 資料夾名稱或影像文字裡的 `+C`、`C+`、`Gd`、`post`、`contrast` 會保留成序列名稱的 `+C`；`list_series` 多一欄 contrast（有／無／?），`list_studies` 顯示該檢查有幾個顯影後序列，說明文字標「（顯影後）」。
- **左右膝：** 檔名裡的 `RK` 是右膝、`LK` 是左膝。右膝的 study_id 不變（`2019-03-20_TSGH`），左膝加 `_LK`（`2022-06-07_TSGH_LK`）。`list_studies` 有「side」欄，所有說明文字都標「右膝」或「左膝」。`compare` 比較左右膝時會提醒內外側方向相反，加 `mirror: true` 會把第二張左右翻轉。
- `crop` 為 `[x0, y0, x1, y1]`，以原圖像素計算（原圖尺寸見 `get_slice` 文字裡的 `size`）；`zoom` 範圍 0.25–4。

## 檔名規則

```
{YYYY-MM-DD}[_{醫院代碼}]_{RK|LK}[_{XR|MR|MRI|CT}][_S{序列號}]_{I{張號} | IMG_{張號}}.jpg
```

| 例子 | 解讀 |
|---|---|
| `2019-03-20_TSGH_RK_S4_I0008.jpg` | 2019-03-20、TSGH、序列 4、第 8 張 |
| `2024-12-03_VGHTPE_RK_S3_I0012.jpg` | 2024-12-03、VGHTPE、序列 3、第 12 張 |
| `2018-12-31_RK_XR_S01_I0001.jpg` | 沒有醫院代碼；`XR` 讓類型自動判為 X-ray；序列 1 |
| `2023-04-11_RK_IMG_0000.jpg` | 沒有醫院、沒有序列：序列取資料夾名稱（如 `序列-001`），沒有就當序列 1；張號可為 0000 |

- 醫院代碼可省略；省略時 study_id 只有日期（例如 `2018-12-31`）。
- **S4 和 S04 視為同一序列**，`S01` 就是序列 1。
- 不符合規則的檔名（例如 `img-00004-00008.jpg`）會用資料夾名稱推斷：日期取資料夾名稱裡的 `YYYY-MM-DD` 或 `YYYYMMDD`；醫院代碼取 3–8 個大寫英文字；序列取 `S5`、`SE5`、`Series 5` 或 `序列-005`；張號取檔名最後一組數字。推斷過的檔案會列在 `list_studies` 的 issues。
- **重複的副本資料夾請排除。** 例如 `日期待確認_匿名化MRI（原標記20251126）` 是 2019 MRI 的匿名副本，名稱裡的 20251126 會被誤認成一次 2025 年的檢查。設定 `KNEE_EXCLUDE_FOLDERS=匿名化` 後，名稱含這個字的資料夾整個略過，並在 issues 註明略過幾張。
- 檢查類型（MRI／X-ray）優先順序：`KNEE_STUDY_TYPES` → 檔名裡的 `XR`/`MRI` → 資料夾名稱 → `unknown`。
- 新增的檢查（例如 2026-12 的 MRI）照同樣規則放進根資料夾，最多 5 分鐘（`KNEE_LISTING_TTL`）就會出現；呼叫 `list_studies` 會立即重新整理。

## DICOM（醫院光碟，免改檔名）

醫院給的光碟通常是 DICOM。把整張光碟的資料夾原封不動上傳到根資料夾底下即可，**不必改檔名**：

- 會讀取的檔案：`.dcm`／`.dicom`、Drive 類型為 `application/dicom`，或沒有副檔名的檔案（光碟上常見的 `IM000001`）。`DICOMDIR` 會略過。
- 檢查日期、序列號、張號取自 DICOM 標籤 `StudyDate`、`SeriesNumber`、`InstanceNumber`；切面取 `SeriesDescription` 與 `ImageOrientationPatient`；SL／ST 取 `SliceLocation`／`SliceThickness`，比 OCR 準。
- 醫院代碼取資料夾名稱裡 3–8 個大寫英文字（例如資料夾取名 `2019-03-20_TSGH_光碟`）；沒有就只用日期當 study_id。
- 建立索引時只用 HTTP Range 讀每個檔案的前 256 KB 標頭，標頭資訊快取在 `<cache>/dicom-meta/`（只有上面列的非個資欄位）。
- **病患姓名、病歷號、生日、醫師、醫院名稱等標籤一律不讀出**，不會出現在任何回應、issues 或日誌；像素轉成 PNG 後同樣走 OCR 遮蔽與驗證，因為有些 DICOM 影像本身也燒有文字。
- 支援未壓縮、JPEG、JPEG-LS、JPEG 2000 壓縮（pylibjpeg）；MONOCHROME1 會自動反相；多影格 DICOM 只顯示第一格並列在 issues。無法解碼的檔案回報 `cannot decode this DICOM image`。
- 同一張影像若同時有 DICOM 與 JPEG，保留 DICOM。

## 離線前處理（prebuild）與 manifest

工具在請求當下**不下載、不做 OCR**，只讀事先處理好的結果：

```bash
python -m knee_mcp.prebuild
```

- 掃描 Drive 全部影像，平行下載（4 執行緒），遮罩後存成 PNG 到 `<cache>/redacted/`。
- 產生 `<cache>/manifest.json`：每張影像的 `study`、`series`、`index`、`plane`、`SL`、`ST`、`size`、`fileId`、`modifiedTime`、`masked_path`、`redaction_status`（`ok` / `withheld` / `todo`）。
- 增量處理：fileId + modifiedTime（與遮罩設定）沒變的影像會跳過；上傳新檢查後重跑即可。
- `list_series` 只彙整已處理的結果（多一欄 `prebuilt n/m`）；`get_slice`、`get_slices`、`compare`、`get_contact_sheet` 只讀快取 PNG 再 crop／zoom。
- 還沒處理的影像直接回 `not prebuilt yet`，同時在背景排入遮罩，不會卡住請求。
- crop 是對「已遮罩的整張圖」裁切，不會在裁切時重做任何檢查。

## 個資遮蔽（怎麼做、保證到什麼程度）

**只准塗黑兩類區域，影像中央的解剖區一律不塗：**

1. **(a) 醫院範本的邊緣文字帶**（`knee_mcp/templates.py`）。依實際影像量測：個資（病歷號在左上、姓名／醫院在右上）都在上方文字帶；下方只有技術參數，不塗。

   | 醫院 | 範本 |
   |---|---|
   | TSGH（2019） | 上方 0–19% 整條、最下方 95.5–100% 細條 |
   | VGHTPE（2024） | 上方 0–17% 整條、最下方 95.5–100% 細條 |

2. **(b) 第二道 OCR**（繁中＋英文，psm 11 與 psm 6、正負片）找出邊緣區（左右各 22%、上 20%、下 20%）內，**內容符合個資規則**的文字框，只塗那個框：
   - 中文姓名（2 字以上；Tesseract 把姓名拆成單字時，同一行相鄰的字會合併判斷）
   - 病歷號、身分證號（6 碼以上數字、`A123456789` 格式）
   - 日期／生日（`1975/04/12`、`19750412`、`12-APR-1975` 等）
   - 醫師姓名（`Dr` 後面的字）、`KNEE_PII_TERMS` 設定的字串
   - 方向字母（R/L/A/P/S/I）、序列名稱、數字參數、信心度 < 60 的辨識結果**一律不塗**
3. **解剖區內若偵測到疑似個資**（信心度 ≥ 70）：**不塗黑**，標記 `redaction_status = review` 並記錄框座標；工具回 `pending manual review`。由你確認：
   ```bash
   python -m knee_mcp.review list
   ```
   ```bash
   python -m knee_mcp.review approve 2019-03-20_TSGH S10 10
   ```
   （`approve` 原樣顯示；`mask` 把那些框塗黑後顯示）
4. 邊緣區的個資在遮蔽後若仍讀得到，該張**不回傳**（`withheld`）。
5. 每張的塗黑框記錄在 manifest 的 `mask_boxes`，`get_slice` 的說明文字附「masked boxes N」。
6. **人工檢查疊圖**：
   ```bash
   python -m knee_mcp.prebuild --debug-overlays /var/lib/knee-mcp/debug
   ```
   每張遮蔽後的影像，紅框標出塗黑處、黃框標出 review 的位置（黃框內容沒有塗黑，資料夾請保持私密）。

**切面與 SL：** 每張影像在塗黑前先 OCR 讀出切面、SL、ST。序列名稱改由同序列所有影像投票（出現在 30% 以上讀數的字才保留，例如 2019 S10 → `SAG PD FS`）；SL 依「同序列 SL 隨張號線性變化」修正漏掉小數點的讀數（`8423` → `84.23`），無法修正或讀不到的用趨勢線補，前面加 `~`。

**限制：** OCR 完全讀不到的文字無法偵測。第一次使用請用疊圖抽查，並建議設定 `KNEE_PII_TERMS`。

## 安裝

以下是 **Windows 11 本機**的安裝方式；長期對外服務建議看「部署在 Linux 伺服器」一節。指令都在 PowerShell 執行，工作目錄為 `C:\path	o\knee-mcp`。

### 1. Python 3.10+

```powershell
winget install --id Python.Python.3.12 -e
```

### 2. Tesseract OCR（Windows，UB Mannheim 版）

```powershell
winget install --id UB-Mannheim.TesseractOCR -e
```

- 預設安裝到 `C:\Program Files\Tesseract-OCR\`。安裝程式若有「Additional language data」選項，請勾選 **Chinese (Traditional)**。
- 確認版本與語言（需要看到 `eng` 和 `chi_tra`）：
  ```powershell
  & "C:\Program Files\Tesseract-OCR\tesseract.exe" --list-langs
  ```
- 如果沒有 `chi_tra`，從 [tessdata](https://github.com/tesseract-ocr/tessdata) 下載 `chi_tra.traineddata`，放進 `C:\Program Files\Tesseract-OCR\tessdata\`（需要系統管理員權限）。
- 如果 `tesseract` 不在 PATH，請在 `.env` 設定 `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`。

### 3. 建立虛擬環境並安裝套件

```powershell
python -m venv .venv
```

```powershell
.\.venv\Scripts\pip install -e ".[test]"
```

## 環境變數

複製 `.env.example` 成 `.env`（已在 `.gitignore`），或設定成系統環境變數。**不要把任何憑證放進 repo。**

| 變數 | 必要 | 說明 |
|---|---|---|
| `KNEE_ROOT_FOLDER_ID` | ✔ | Drive 根資料夾 ID（網址 `drive.google.com/drive/folders/<這段>`） |
| `GOOGLE_CLIENT_SECRETS` | 方案 A | Google OAuth 用戶端 JSON（Desktop app 類型） |
| `GOOGLE_TOKEN_FILE` | 方案 A | `google_login` 產生的 token 檔，預設 `<cache>/google-token.json` |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | 方案 B | 服務帳戶金鑰；要把根資料夾以「檢視者」分享給它 |
| `KNEE_CACHE_DIR` | | 快取目錄，預設 `./.cache`。只存遮蔽後的圖（`redacted/`）與 OAuth 狀態（`auth.json`） |
| `KNEE_KEEP_RAW` | | 預設 `0`：原圖從 Drive 下載到記憶體、遮蔽完即丟棄，**不寫入硬碟**。設 `1` 才會把未遮蔽原圖快取在 `<cache>/drive/`（含個資） |
| `KNEE_HOST` / `KNEE_PORT` | | 預設 `127.0.0.1` / `8765`；用 tunnel 時維持 127.0.0.1 |
| `KNEE_PUBLIC_URL` | 對外時 ✔ | 例如 `https://knee.example.com`（OAuth issuer 與 resource URL） |
| `KNEE_ALLOWED_HOSTS` | | 額外允許的 Host header（DNS rebinding 防護）；`KNEE_PUBLIC_URL` 的主機會自動加入 |
| `KNEE_BEARER_TOKENS` | 二擇一 ✔ | 逗號分隔的靜態 token，給 Claude Code / MCP Inspector；產生：`python -m knee_mcp --gen-token` |
| `KNEE_OWNER_PASSWORD` | 二擇一 ✔ | 啟用內建 OAuth 登入頁（claude.ai 連接器需要）；請用長密碼 |
| `KNEE_ALLOWED_REDIRECTS` | | OAuth 允許的 redirect URI 前綴；預設 claude.ai / claude.com / ChatGPT callback 與 localhost |
| `TESSERACT_CMD` | | tesseract.exe 路徑（不在 PATH 時） |
| `KNEE_OCR_LANGS` | | 預設 `eng`；建議 `eng+chi_tra` 以便偵測中文姓名 |
| `KNEE_PII_TERMS` | 建議 | 遮完後絕不能出現的字串（逗號分隔） |
| `KNEE_STUDY_TYPES` | | 例 `2018-12-31=X-ray,2019-03-20=MRI` |
| `KNEE_EXCLUDE_FOLDERS` | | 逗號分隔；資料夾名稱含其中任一字串就略過（例 `門診影像討論包,Claude標註圖`） |
| `KNEE_FOLDER_STUDIES` | | 強制指定某資料夾的 study_id，例 `匿名化MRI=2025-11-26_unconfirmed`：名稱含「匿名化MRI」的資料夾全部歸到該檢查；`序列-0000N` 子資料夾為序列，沒有序列號的子資料夾（如「重點影像」）成為序列 99，影像依檔名順序編號 |
| `KNEE_DUPLICATE_FOLDERS` | | 例 `匿名化MRI=2019-03-20_TSGH`：該資料夾是某次檢查的重複副本，整個排除，並在 `list_studies` 的 issues 註明「duplicate copy of study …」，避免被當成另一次檢查 |
| `KNEE_OVERRIDES` | | 覆寫檔路徑，預設 `<cache>/overrides.json`。給影像上沒有文字的序列指定切面、切片厚度、間距，並用 `no_text` 跳過 OCR 與遮罩；格式見 `docs/overrides.example.json` |
| `KNEE_MASKING` | | 預設 `1`。設 `0` 關閉遮罩：影像原樣回傳（含燒在影像上的個資），只做一次快速 OCR 讀切面／SL／ST，prebuild 約快 4 倍。**只適合自己的資料、自己使用**；`list_studies` 會標示 masking is DISABLED |
| `KNEE_LISTING_TTL` | | Drive 清單快取秒數，預設 300 |
| `KNEE_WORKERS` | | 平行處理數，預設 4 |
| `KNEE_TOOL_BUDGET` | | 工具願意等「還沒 prebuild 的影像」幾秒，預設 0：直接回 `not prebuilt yet`，並在背景排入遮蔽 |
| `KNEE_MASK_TEMPLATES` | | 覆寫各醫院的固定遮罩區域（JSON，比例座標），預設見 `knee_mcp/templates.py` |
| `KNEE_LOCAL_DIR` | 開發用 | 設定後改讀本機資料夾（同 Drive 結構），不連 Drive |

兩個都沒設（`KNEE_BEARER_TOKENS`、`KNEE_OWNER_PASSWORD`）時伺服器拒絕啟動。

## Google Drive OAuth 設定

**方案 A：用自己的 Google 帳號（OAuth）**

1. [Google Cloud Console](https://console.cloud.google.com/) → 建立專案 → 「API 和服務」→ 啟用 **Google Drive API**。
2. 「OAuth 同意畫面」：User type 選「外部」，加入範圍 `https://www.googleapis.com/auth/drive.readonly`，把自己加進「測試使用者」。
   - 注意：發布狀態為「測試中」時 refresh token 約 7 天失效。要長期使用請把應用程式「發布」（個人使用、未驗證也可，同意時會顯示未驗證警告）或改用方案 B。
3. 「憑證」→ 建立 OAuth 用戶端 ID → 應用程式類型 **電腦版應用程式** → 下載 JSON，放到 repo 以外的位置（例如 `C:\secure\knee-mcp\client_secret.json`），設定 `GOOGLE_CLIENT_SECRETS`。
4. 在有瀏覽器的電腦執行一次：
   ```powershell
   .\.venv\Scripts\python -m knee_mcp.google_login
   ```
   同意後會寫出 `GOOGLE_TOKEN_FILE`。若伺服器主機沒有瀏覽器，可在自己電腦執行後把 token 檔複製過去。

**方案 B：服務帳戶（伺服器長期執行較省事）**

1. 同一個專案 → 「服務帳戶」→ 建立 → 新增金鑰（JSON），設定 `GOOGLE_SERVICE_ACCOUNT_FILE`。
2. 在 Drive 把膝關節根資料夾**分享**給服務帳戶的 email，權限「檢視者」。服務帳戶只看得到被分享的這個資料夾。

## 啟動（Windows / PowerShell）

先把 `.env.example` 複製成 `.env` 並填好（見上方「環境變數」）：

```powershell
Copy-Item .env.example .env
```

```powershell
notepad .env
```

產生一組 Bearer token（貼到 `.env` 的 `KNEE_BEARER_TOKENS`）：

```powershell
.\.venv\Scripts\python -m knee_mcp --gen-token
```

第一次建議先預先下載並遮蔽全部影像（之後工具回應較快，也能先看出哪幾張被擋下）：

```powershell
.\.venv\Scripts\python -m knee_mcp --warm
```

前景啟動（關掉視窗就停止）：

```powershell
.\.venv\Scripts\python -m knee_mcp
```

- MCP 端點：`http://127.0.0.1:8765/mcp`（Streamable HTTP，stateless）；健康檢查：`http://127.0.0.1:8765/healthz`。
- 確認有在跑（應回 `ok`）：
  ```powershell
  Invoke-RestMethod http://127.0.0.1:8765/healthz
  ```

### 登入 Windows 後自動啟動（工作排程器）

`scripts\start.ps1` 會切到 repo 目錄、讀 `.env`、把日誌寫到 `knee-mcp.err.log`（伺服器日誌）與 `knee-mcp.out.log`。註冊成「登入時執行」的排程工作：

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "C:\path	o\knee-mcp\scripts\start.ps1"'
```

```powershell
Register-ScheduledTask -TaskName "knee-mcp" -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Settings (New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1))
```

手動啟動／停止：

```powershell
Start-ScheduledTask -TaskName "knee-mcp"
```

```powershell
Stop-ScheduledTask -TaskName "knee-mcp"
```

## 對外提供 HTTPS

伺服器只聽 `127.0.0.1:8765`，不需要開防火牆埠；由 tunnel 從外面連進來並提供 HTTPS。以下兩種方式擇一。

> ⚠️ **這台電腦關機、睡眠或休眠時，claude.ai 就連不到這個連接器**（Claude Code 也一樣）。要隨時可用，請讓電腦保持開機並關閉自動睡眠，例如接電源時永不睡眠／休眠：
>
> ```powershell
> powercfg /change standby-timeout-ac 0
> ```
>
> ```powershell
> powercfg /change hibernate-timeout-ac 0
> ```
>
> 筆電闔上螢幕時的行為要另外在「控制台 → 電源選項 → 選擇闔上螢幕時的行為」設定。

### 方案一：Cloudflare Tunnel（建議，子網域例如 `knee.example.com`）

前提：`example.com` 的 DNS 託管在 Cloudflare。

1. 安裝 cloudflared：
   ```powershell
   winget install --id Cloudflare.cloudflared -e
   ```
2. 登入 Cloudflare（會開瀏覽器，選擇 `example.com`）：
   ```powershell
   cloudflared tunnel login
   ```
3. 建立 tunnel 並綁定子網域（會自動建立 CNAME）：
   ```powershell
   cloudflared tunnel create knee-mcp
   ```
   ```powershell
   cloudflared tunnel route dns knee-mcp knee.example.com
   ```
4. 建立 `%USERPROFILE%\.cloudflared\config.yml`（`<TUNNEL-ID>` 換成上一步顯示的 ID；憑證 JSON 也在同一個資料夾）：
   ```yaml
   tunnel: <TUNNEL-ID>
   credentials-file: C:\Users\<你的帳號>\.cloudflared\<TUNNEL-ID>.json
   ingress:
     - hostname: knee.example.com
       service: http://127.0.0.1:8765
     - service: http_status:404
   ```
5. 測試執行（前景）：
   ```powershell
   cloudflared tunnel run knee-mcp
   ```
6. 常駐：用**系統管理員** PowerShell 安裝成 Windows 服務。服務以 LocalSystem 身分執行，讀的是 `C:\Windows\System32\config\systemprofile\.cloudflared\`，所以要先把 `config.yml` 和 `<TUNNEL-ID>.json` 複製過去，並把 `credentials-file` 改成新路徑，然後：
   ```powershell
   cloudflared service install
   ```
7. `.env` 設定 `KNEE_PUBLIC_URL=https://knee.example.com`，重新啟動 knee-mcp。
8. 驗證（應回 `ok`；不帶 token 打 `/mcp` 應回 401）：
   ```powershell
   Invoke-RestMethod https://knee.example.com/healthz
   ```

不要對這個子網域再套 Cloudflare Access 的登入頁，否則 claude.ai 的伺服器會被擋在外面；驗證已由本伺服器的 OAuth／Bearer token 負責。

### 方案二：Tailscale Funnel

不需要自己的網域，網址會是 `https://<這台電腦的機器名>.<tailnet>.ts.net`。

1. 安裝並登入 Tailscale（已安裝可略過）：
   ```powershell
   winget install --id Tailscale.Tailscale -e
   ```
2. 到 Tailscale 管理後台啟用 **MagicDNS** 和 **HTTPS Certificates**，並允許這台機器使用 Funnel（第一次執行下一步時，CLI 會給出啟用連結）。
3. 對外公開 8765 埠（`--bg` 在背景持續生效，重開機後仍保留）：
   ```powershell
   tailscale funnel --bg 8765
   ```
4. 查詢公開網址：
   ```powershell
   tailscale funnel status
   ```
5. `.env` 設定 `KNEE_PUBLIC_URL=https://<機器名>.<tailnet>.ts.net`，重新啟動 knee-mcp。
6. 關閉公開：
   ```powershell
   tailscale funnel --https=443 off
   ```

## 部署在 Linux 伺服器（Ubuntu 24.04）

以下是在一台 Ubuntu 24.04 VPS 上長期執行的做法（作者自己的部署就是這樣，網址以 `https://knee.example.com/mcp` 代替）。

| 項目 | 位置 |
|---|---|
| 程式 | `/opt/knee-mcp`（venv 在 `.venv`） |
| 設定與密鑰 | `/etc/knee-mcp/knee.env`（`root:knee-mcp`，權限 640） |
| 快取 | `/var/lib/knee-mcp`：只有遮蔽後的圖、`auth.json`、Google token；未遮蔽原圖不落地（`KNEE_KEEP_RAW=0`） |
| 服務 | systemd `knee-mcp.service`，以系統帳號 `knee-mcp` 執行，只聽 `127.0.0.1:8765`，記憶體上限 700 MB |
| 對外 | Cloudflare Tunnel 設定檔加一條 `knee.example.com → http://localhost:8765` |
| OCR | `apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-tra` |

VPS 只有 1 vCPU、2 GB RAM，所以 `KNEE_WORKERS=1`。第一次開一個 20 張的 contact sheet 可能要 1–2 分鐘，建議授權 Drive 後先跑一次 `--warm`（見下）。

### 給 VPS 授權 Google Drive

**方案 B（服務帳戶，建議）**：照上面「方案 B」建立服務帳戶、把根資料夾分享給它，然後：

```bash
scp service-account.json ubuntu@your-server:/tmp/sa.json
```

```bash
ssh ubuntu@your-server "sudo install -o root -g knee-mcp -m 640 /tmp/sa.json /etc/knee-mcp/service-account.json && rm /tmp/sa.json"
```

接著編輯 `/etc/knee-mcp/knee.env`：填 `KNEE_ROOT_FOLDER_ID=`，並取消 `GOOGLE_SERVICE_ACCOUNT_FILE=/etc/knee-mcp/service-account.json` 那行的註解。

**方案 A（自己的 Google 帳號）**：在自己電腦跑 `python -m knee_mcp.google_login` 產生 token，再放到 VPS：

```bash
scp google-token.json ubuntu@your-server:/tmp/gt.json
```

```bash
ssh ubuntu@your-server "sudo install -o knee-mcp -g knee-mcp -m 600 /tmp/gt.json /var/lib/knee-mcp/google-token.json && rm /tmp/gt.json"
```

改完設定後重新啟動，並預先遮蔽全部影像：

```bash
ssh ubuntu@your-server "sudo systemctl restart knee-mcp"
```

```bash
ssh ubuntu@your-server "sudo -u knee-mcp bash -c 'set -a; . /etc/knee-mcp/knee.env; cd /var/lib/knee-mcp; /opt/knee-mcp/.venv/bin/python -m knee_mcp --warm'"
```

### 取得擁有者密碼與 Bearer token

兩者是部署時隨機產生的，只存在 `/etc/knee-mcp/knee.env`：

```bash
ssh ubuntu@your-server "sudo grep -E '^KNEE_(OWNER_PASSWORD|BEARER_TOKENS)=' /etc/knee-mcp/knee.env"
```

要換新的：編輯該檔後執行 `sudo systemctl restart knee-mcp`。

### 更新程式

在本機 repo 目錄：

```bash
git ls-files -co --exclude-standard | tar -czf - -T - | ssh ubuntu@your-server "tar -xzf - -C /opt/knee-mcp && /opt/knee-mcp/.venv/bin/pip install -q /opt/knee-mcp && sudo systemctl restart knee-mcp"
```

日誌：

```bash
ssh ubuntu@your-server "journalctl -u knee-mcp -n 50 --no-pager"
```

## 加入 Claude / ChatGPT

### claude.ai 連接器（自訂連接器）

claude.ai 的自訂連接器支援 **OAuth（含 Dynamic Client Registration）** 或無驗證，**不支援直接填寫靜態 Bearer token**。所以本伺服器內建一個只給擁有者用的 OAuth 授權伺服器：

- 提供 `/.well-known/oauth-protected-resource`、`/.well-known/oauth-authorization-server`、`/register`（DCR）、`/authorize`（PKCE S256）、`/token`（含 refresh token 輪替）、`/revoke`。
- `/authorize` 會導到 `/login` 密碼頁，只有知道 `KNEE_OWNER_PASSWORD` 的人能核發 token；連續 5 次錯誤鎖 15 分鐘。
- DCR 只接受 `KNEE_ALLOWED_REDIRECTS` 內的 callback（預設 `https://claude.ai/api/mcp/auth_callback`、`https://claude.com/api/mcp/auth_callback`）。
- access token 1 小時、refresh token 30 天，皆以 SHA-256 雜湊存在 `<cache>/auth.json`。

步驟：

1. 設好 `KNEE_PUBLIC_URL`（必須 HTTPS）與 `KNEE_OWNER_PASSWORD`，啟動伺服器與 tunnel。
2. claude.ai → 設定 → 連接器 → 「新增自訂連接器」，URL 填 `https://knee.example.com/mcp`，進階設定留空（使用 DCR）。
3. 按「連線」→ 瀏覽器開到本伺服器的密碼頁 → 輸入擁有者密碼 → 自動回到 claude.ai。
4. 對話中開啟此連接器即可使用。要撤銷：刪除 `<cache>/auth.json` 中的 token 或整個檔案後重新啟動。

參考：[Building custom connectors via remote MCP servers](https://support.claude.com/en/articles/11503834)、[Connector authentication](https://claude.com/docs/connectors/building/authentication)。

### ChatGPT（開發人員模式連接器）

ChatGPT 的自訂 MCP 連接器同樣走 OAuth（Dynamic Client Registration + PKCE），用的是同一個登入頁與擁有者密碼。允許的 callback 預設已包含 `https://chatgpt.com/connector/oauth/…` 與舊版 `https://chatgpt.com/connector_platform_oauth_redirect`。

1. ChatGPT → 設定 → 應用程式與連接器 → 進階設定 → 開啟「開發人員模式」。
2. 建立連接器：MCP 伺服器 URL 填 `https://knee.example.com/mcp`，驗證方式選 **OAuth**，其餘留空（使用動態註冊）。
3. 連線時會開到本伺服器的密碼頁，輸入擁有者密碼即可。

注意：ChatGPT 是否會把工具回傳的圖片（ImageContent）顯示給模型看，**尚未實測**。參考：[OpenAI Apps SDK — Authentication](https://developers.openai.com/apps-sdk/build/auth)。

### Claude Code

```powershell
claude mcp add --transport http knee https://knee.example.com/mcp --header "Authorization: Bearer <KNEE_BEARER_TOKENS 其中一個>"
```

### MCP Inspector

```powershell
npx @modelcontextprotocol/inspector --cli https://knee.example.com/mcp --transport http --header "Authorization: Bearer <token>" --method tools/call --tool-name get_slice --tool-arg study=2019-03-20 --tool-arg series=S7 --tool-arg index=13
```

回應的 `content[0].type` 應為 `"image"`、`mimeType` 為 `"image/png"`。

## 測試

```powershell
.\.venv\Scripts\python -m pytest -q
```

測試使用 mock Drive 與合成影像（影像上燒入**假**的姓名、病歷號、生日、醫師名，含一張中文姓名），不需要任何憑證；完整跑一次約 1.5–2 分鐘（大部分是 OCR）。

## 安全備註

- 錯誤訊息只含 study／series／張號等資訊；未預期的例外只回 `internal error (ref xxxx)`，伺服器日誌只記例外類型。
- 原圖只存在 Google Drive；伺服器預設不把未遮蔽原圖寫入硬碟（`KNEE_KEEP_RAW=0`）。`google-token.json`、服務帳戶金鑰與 `auth.json` 是憑證，請確保只有自己能讀。
- 伺服器只綁 127.0.0.1，並開啟 DNS rebinding 防護（Host / Origin 白名單）。

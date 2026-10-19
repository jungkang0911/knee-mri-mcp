# 膝望：右膝 MRI 資料整理與私有 MCP

**版本 0.1.1，工程交付版；不是診斷工具，不是已完成驗收的正式上線服務。**

將本機 JPEG / DICOM 整理成可追溯資料集，並由官方 Python MCP SDK 的 `FastMCP` 透過 Streamable HTTP 回傳真正的 `ImageContent`。不引用醫院報告、不做病灶分類，也不修改原始資料。

## 交付與驗收狀態

請先看 [實際驗證紀錄](docs/DELIVERY_STATUS.md)。程式已完成；本次環境缺少可下載安裝的 MCP SDK／pydicom／Inspector，**所以不能聲稱通過 MCP Inspector、DICOM 執行或遠端部署驗收**。
本機只有先前對話的部分選片，並不是完整 `raw/`。程式包不包含你的原始醫療影像、去識別化候選影像、Notion 內容或任何 token。

## 核心原則

- `raw/` 只讀。輸出路徑不能位於 raw 內，拒絕 symlink，掃描前後重新比對 SHA-256；既有 dataset 不自動覆寫。
- 2019-03-20 TSGH 固定為 `pre_op`。匿名 20251126 是 2019 的副本集合，不建立新 study；無唯一匹配的副本留在 issues，不能偷偷合併。
- DICOM 優先處理，JPEG 不會僅因同日有 DICOM 就被刪除。必須是同一張的可靠配對，才標 `duplicate_of`。DICOM/JPEG 顯示尺寸或窗位不同而無法可靠配對時，由 `duplicate_overrides` 經人工作明確配對，否則拒絕碰撞。
- 圖片回傳為 `mcp.types.ImageContent(type="image", mimeType="image/png", data=...)`。**data 本來就按 MCP 規格使用 base64；禁止的是把 base64／整個 content 物件塞入 `TextContent.text`。**
- 全部工具都是唯讀。伺服器只掛載 `dataset/`，不掛載 raw、.private、病歷或醫院報告。無静態圖片網址、檔案下載路由或 HTTP 上傳路由。

## 1. 安裝（本機整理環境）

建議 Python 3.11/3.12；Windows 可在 WSL2 跑相同命令。

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
sudo apt-get update
sudo apt-get install -y tesseract-ocr tesseract-ocr-eng
python scripts/preflight.py
```

Windows 原生 PowerShell 啟用環境：`.\.venv\Scripts\Activate.ps1`，另安裝 Tesseract 並加入 PATH。OCR 使用 **eng + 數字**，不依赖網路 OCR。壓縮 DICOM 可能需要：

```bash
python -m pip install -e '.[compressed-dicom]'
```

SDK 固定相容範圍 `mcp[cli]>=1.28,<2`，因任務要求官方 `mcp.server.fastmcp.FastMCP` API；**不是另外安裝名為 fastmcp 的第三方套件**。SDK 官方文件指出 v2 有不相容變更，不能不加上限直接升級。實際安裝後請在你主機建立 lockfile／保存 `pip freeze` 再做安全更新。本交付不虛構一份未安裝驗證的鎖定檔。

## 2. 放置原始檔案

```text
raw/
  2019-03-20_TSGH/
    序列-006/ ... 序列-010/
    # 也會收錄其他實際存在的 MRI 序列，不把五個資料夾當成全部檢查
  2024-12-03_VGHTPE/
    右膝_橫切面_T2脂肪抑制FSE/
    矢狀面_PD+T2 FSE/
    矢狀面_PD+T2 FSE（序列008）/
    冠狀面_3D MERGE/
    冠狀面_T2脂肪抑制FSE/
  Anonymized_20251126/
    ...
  dicom/
    DICOMDIR
    ... 含 .dcm 或有 DICOM preamble 的無副檔名檔案 ...
```

先核對 `config/studies.yaml` 的資料夾匹配規則。2019 006–010 的 20 張是你提供的預期值；不把其他序列也強制填成 20。資料夾修改日期、匿名化日期一律不用來推測檢查日期。

匿名集合本身不夠重建來源，需要原始對應序列也在 raw 中。DICOMDIR 本身是索引而不是切片；程式遍歷 raw 下的影像檔（含無副檔名 DICOM），不追讀 raw 外的引用。無 preamble 又無 DICOM 副檔名的舊檔不使用 `force=True` 猜讀，應先在副本中補正命名／確認檔案格式，不修改原檔。

## 3. 建立資料集

```bash
knee-mri build --raw ./raw --out ./dataset --config config/studies.yaml --private ./.private
```

若 `dataset/` 已存在，使用新目錄如 `dataset-v2/`。程式不刪除舊資料，也沒有 `--force`。

```text
dataset/
  manifest.json
  validation_report.md
  review.json
  review.html
  studies/2019-03-20_TSGH/s07_COR_PD/2019-03-20_TSGH_RK_S07_COR_PD_I0013.png
  studies/2024-12-03_VGHTPE/s04_COR_T2_FS_FSE/2024-12-03_VGHTPE_RK_S04_COR_T2_FS_FSE_I0016.png
  studies/.../<same-image-stem>.npz          # 僅 DICOM：去識別化數值像素 + 永久遮罩，沒有 DICOM headers
  contact_sheets/<study>/<study>_RK_S<series>_<description>_CONTACT.png
  release.json                # 只有驗證與逐張人工覆核通過後才產生
.private/
  rename_map.csv             # 原始相對路徑／影格 → 新檔名；可能含個資，留本機
  provenance.json             # source_id -> raw 相對路徑；留本機，不部署
  ocr_cache/                  # 含原始 OCR 文字的私密快取，可能有個資，絕不可部署／分享
```

### 圖片命名（0.1.1）

`<study_id>_RK_S<原序列號>_<已擷取技術描述>_I<原張號>.png`

例如 `2019-03-20_TSGH_RK_S07_COR_PD_I0013.png`：日期／機構代碼、右膝、序列 7、冠狀面 PD、第 13 張。
`2024-12-03_VGHTPE_RK_S03_AX_T2_FS_FSE_I0012.png`：2024 檢查、右膝、序列 3、橫切 T2 脂肪抑制、第 12 張。
這些是命名範例，不代表對所有原圖的序列與病灶重新判讀。

- **只命名輸出的 PNG／NPZ／contact sheet，不更動任何 raw 原檔。** 檔名沒有姓名、病歷號或診斷；醫院僅使用 manifest 已配置的代碼。
- 保留原本的 Series 與 Image index，不因檔案排序或匿名片 0 起算而重新編號。缺片和 OCR 衝突仍列 issues，命名不能消除它們。
- 技術描述僅接受安全詞彙。`PD+T2`、MERGE 等權重不明時仍須覆核，不為改名猜成 T2；不在檔名加「水腫」「豆骨病變」「皺襞夾擠」等診斷。
- 已配對的匿名片只指到 **2019 原片的新名稱**，不產出 2025 檔名，不多存一張 PNG。
- `manifest.slices` 增加 `filename`、`slice_id`；MCP 的 metadata 也回傳兩者。`manifest.sources` 增加 `canonical_slice_id`、`output_filename`，讓重複片可追到同一張輸出。
- 原始檔名對照表為本機 **`.private/rename_map.csv`**。含原路徑，可能有個資，不得提交 GitHub 或部署到 MCP。原始逐位元追溯仍以 `provenance.json` 為準。
- 不在現有 dataset 內直接批次改名，以免弄壞 manifest／review／release 雜湊。請用新程式輸出到 `dataset-v2/`，重新覆核放行；舊資料集不動。MCP 仍可讀取舊版數字檔名資料集，會另外提供可辨識的 metadata 名稱。

### manifest 與數量

`manifest.slices` 只列唯一、成功建立的切片，包含要求的 study/date/hospital/pre_or_post_op、series、plane、weighting、fat_sat、n/N、ST、SL、window、sha256、duplicate_of 與 laterality_note，並加入 PNG 雜湊、尺寸、`filename`、`slice_id` 與可追溯 ID。

`manifest.sources` 列**每一個原始影像／DICOM frame**，含 duplicate_of、來源格式、配對數值、失敗／未解決狀態。重複片不另產生 PNG，但追溯紀錄不會消失。完整成功時：

```text
source_image_frames == len(manifest.slices) + duplicates
unresolved_or_failed == 0
```

多影格 DICOM 的「檔案數」與「切片数」本來不同；因此兩種統計分開。`sha256` 是來源檔案雜湊；`png_sha256` 是處理後 PNG 雜湊。

**隱私取捨：** manifest 的 `source_path` 使用 `source://<id>`，實際 raw 相對路徑在本機 `.private/provenance.json`，避免姓名藏在檔名中並隨 dataset 上傳。伺服器不回傳 source_path／來源檔名／原始 hash／OCR 原文。醫院僅保留你要求的機構代碼，不保存醫師姓名。

### OCR、未知參數與衝突

每張來源影像第一次 OCR 偵測文字框並擷取技術參數；遮罩後第二次 OCR 驗證。使用圖片尺寸自適應放大，但不對輸出 anatomy 做 AI 重繪。英文字月份日期如 `20-March-2019` 亦可辨識；生日不會被任意選作檢查日期。

OCR 與檔名的 Series/index 不一致會阻擋放行。未取得的值維持 null，不把資料夾名、別張片子的內容或報告猜成參數。`PD+T2` 這類雙權重名稱不自動選 PD/T2；需比對實際 echo/序列後，在 config 的 `overrides` 以 source_id 明確更正。`COR 3D MERGE` 權重不明時同樣保持待確認，不能強制改叫 T2。

DICOM 讀 StudyDate、Series/Instance、IOP/IPP、SliceThickness、SliceLocation、WindowCenter/Width 與 rescale，並處理 Enhanced per-frame/shared groups；不把 PatientName/PatientID/BirthDate/InstitutionName/ReferringPhysician/private tags 複製出去。非線性 VOI LUT 目前列為 blocking issue，不假裝 LINEAR window 與它等價。DICOM 未標總張數時，可統計實際檔案／frame 數，但這**不證明光碟沒有漏檔**。

## 4. 去識別化與人工放行

**OCR 沒讀到文字 ≠ 沒有個資。eng OCR 尤其不能保證偵測到中文姓名、院名。** 本工具因此預設封鎖所有未經人工逐張確認的資料，而不是把「零 OCR 結果」當成個資安全認證。

遮罩跟著 OCR 偵測的文字框位置，沒有固定四角矩形。會以自適應連通區粗略辨認影像主體，遇到疑似把骨內紋理誤認成字的框，先留待覆核，不盲目塗黑。這不是診斷用分割。會清除姓名、編號及技術文字；保留邊緣孤立、具合理信心的方向標記，包括 R/L 及 AR/PL 等斜切方向組合。**方向辨識本身仍需人核對；不能把姓名縮寫誤留成方向。** OCR 把骨內紋理誤讀成字時，遮罩也可能傷到 anatomy，這同樣是人工檢查的重點。

請在本機開啟 `dataset/review.html`，逐張原尺寸查看處理後影像及本機原檔對照。不能只看縮圖；不能上傳至公開靜態網站。確認 `validation_report.md` 無 error；如有殘留字，將該 source_id 的實際框填入 `extra_redaction_boxes`，重新輸出新版本。不應隨意降低 OCR 閾值來消除警告。若經人確認是骨內紋理而非文字，才能在 `verified_nontext_boxes` 記錄該 source_id 的具體誤識框；source_id 綁定來源內容，不使用全域免檢。

只有確認無個資、沒有遮掉診斷組織後，才手工把該切片 `review.json` 的：

```json
{
  "approved": true,
  "phi_free": true,
  "anatomy_preserved": true,
  "all_dicom_windows_checked": true
}
```

設為 true。最後一項只對 DICOM 必填；需檢查其數值像素在多種窗位下不會露出文字。不要改動 PNG 或 NPZ 雜湊；修改了影像必須重新建立並覆核。

```bash
knee-mri release --dataset ./dataset
knee-mri verify --dataset ./dataset
```

放行後，manifest、review、全部 PNG/contact sheet/NPZ 都由 SHA-256 綁定。伺服器啟動及每次讀圖會核對影像雜湊，資料被改過就拒絕服務。沒有批次無條件批准所有真實影像的指令。

PNG 是目前灰階像素的無損容器：**JPEG 轉成 PNG 不會恢復 JPEG 已損失的資訊。** DICOM 的顯示 PNG 為 8-bit；另存經 rescale 的數值像素用於重設窗位，不把 8-bit PNG 當作完整原始精度。去識別化可能移除被文字覆蓋的像素，不能把此副本取代醫院 DICOM。

## 5. 單機啟動 MCP

```bash
python scripts/setup_secrets.py
export MRI_TOKEN_FILE="$PWD/.secrets/mri_token"
export MRI_DATASET="$PWD/dataset"
export MRI_ALLOWED_HOSTS='127.0.0.1:8000,localhost:8000'
export MRI_ALLOWED_ORIGINS='http://localhost:6274,http://127.0.0.1:6274'
knee-mri-server
```

本機 endpoint 為 `http://127.0.0.1:8000/mcp`。**所有工具及 MCP 初始化都要求 Authorization: Bearer**，錯誤 401 不洩漏影像；token 不得放 query string 或路徑。預設 token 至少 32 個無空白字元，產生器使用更長的隨機值。token file 是優先方式，亦支援 `MRI_TOKEN` 環境變數，但不得 commit .env。

所有 PNG 原始 bytes 上限 1,000,000（base64 線上大小會約增為 4/3）。超過上限回傳明確錯誤，要求較小 crop，不暗中縮圖、降灰階、量化或改 JPEG。get_slices 最多 8 張，end 含在範圍內；有缺片就整批報錯。

|工具|回傳|
|---|---|
|list_studies()|TextContent|
|list_series(study_id)|TextContent|
|get_contact_sheet(study_id, series_no)|ImageContent，PNG|
|get_slice(study_id, series_no, index, crop=None, zoom=1, window=None)|ImageContent + 一行 TextContent metadata|
|get_slices(study_id, series_no, start, end)|最多 8 張 ImageContent，各張後附 metadata|
|compare(a, b)|左右日期標示 PNG ImageContent + 對照 metadata|

crop 是原圖像素 `[x0,y0,x1,y1)`，右／下端不包含；不接受超界、不自動 clamp。zoom 1–4，採最近鄰顯示放大，不增加影像資訊。compare 的 selector 是 `{study,series,index,crop?}`；不配準、不正規化，不暗示同編號代表同一解剖位置。

**左右定位不硬猜：** axial/coronal 皆有標準「畫面左＝病人右＝右膝外側」說明，但 JPEG 沒有定位核對時加註「尚未驗證是否翻轉」。DICOM IOP/IPP 優先；實際為斜切也保留其 metadata。確認前不把左／右標示當成已可靠定位。

## 6. HTTPS 部署到你的主機

本交付沒有登入或改動你的既有主機，也不推定 `aing.tw/mcp` 可以被覆蓋。請使用**獨立子網域**與新目錄，例如 `/opt/knee-mri-mcp`，不占用既有 STT MCP 路徑。

部署機只需要專案程式、已放行的 dataset、compose/Caddy 設定和**在該主機新產生**的 token。不傳 raw、.private、未放行資料。對敏感醫療資料應限制主機與 Notion／Claude 分享範圍；這不是醫療器材或法規認證。

```bash
cp .env.example .env
# 編輯 MRI_DOMAIN 為實際網域；MRI_UID/GID 為 dataset 和 token 檔案擁有者。
python scripts/setup_secrets.py
# 將經驗證放行的 dataset 安全複製到此目錄，再執行：
docker compose up -d --build mri
```

預設只把 app 映射到主機 loopback `127.0.0.1:48080`。已有 Nginx/Caddy 的主機請沿用該反向代理，設定新子網域指向這個 port，保留原始 Host/Authorization，關閉 response buffering 與快取。不要讓 Cloudflare 或反向代理快取圖片或 MCP 回應。

只有當該主機的 80/443 **沒有既有服務佔用**，且 DNS 已指到此主機時，才啟動附帶的 Caddy HTTPS：

```bash
docker compose --profile edge up -d --build
```

Caddy 會為你實際設定的網域申請憑證；不是交付時已取得 HTTPS 憑證。app 非 root、read-only rootfs、dataset bind mount read-only、移除 capabilities，且沒有 raw mount。token 檔在主機 chmod 600、父目錄 700；設定 MRI_UID/GID 讓容器能讀，不要把 token 改成公開可讀來避開權限錯誤。

驗證 header 用環境／檔案管理，不把 token 打進 URL 或分享截圖。輪替時在主機產生新 token、重啟 app，再更新客戶端，舊 token 即失效。建議代理／防火牆增加速率限制與連線上限。

## 7. 用 MCP Inspector 驗收（必做，不可用程式碼外觀取代）

```bash
pytest -q -ra
# 使用合成資料測 transport，不必先暴露你的 MRI：
python scripts/make_test_dataset.py --out synthetic-run
```

切换 MRI_DATASET 至 `synthetic-run/dataset` 並啟動 server，然後：

```bash
python scripts/check_inspector.py \
  --url http://127.0.0.1:8000/mcp \
  --token-file .secrets/mri_token \
  --study 2019-03-20_TSGH --series 7 --start 1 --end 2 \
  --out evidence/inspector-synthetic.json
```

腳本實際调用官方 Inspector CLI（安全修補 v1 發布線 `@v1-latest`），核對六工具、ImageContent 數量、mimeType、base64 可解碼、PNG magic、完整 PNG 解碼、每張 bytes 上限，以及 text 裡沒有 base64／序列化圖片結果。token 放權限 600 的臨時 Inspector config，結束後移除；不印出含圖片或憑證的 raw Inspector stdout。

GUI 手動驗收可執行 `npx @modelcontextprotocol/inspector@v1-latest`，選 Streamable HTTP，填 URL，新增 Authorization header；逐一呼叫六工具，確認有 image 區塊及圖片。Inspector UI 的本機安全 token 不能關掉，亦不要把 Inspector 對外公開。

正式 HTTPS 上線後，**再次對遠端 URL 跑腳本**，並補測：無 token=401、錯 token=401、超過 8 張拒絕、越界 crop 拒絕、JPEG 設 window 拒絕、raw/路徑不可讀、資料 hash 被更改則拒絕。以上尚未在你的主機完成。

## 8. 加入 claude.ai 連接器

Remote Connector 的請求由 Anthropic 雲端發出，不是從你的電腦，所以 localhost／只在 VPN 內的 URL 不能直接供 claude.ai 使用。

在 Claude 的 Customize（或 Settings）→ Connectors → Add custom connector，填入實際 `https://<你的 MRI 子網域>/mcp`。**本版使用靜態 Bearer，不是 OAuth server**：在支援 Request headers 的介面加入 header `Authorization`，完整值是 `Bearer <token>`。若介面另要求登入方式，選不啟用 OAuth 登入（No sign-in）；驗證仍由 Authorization header 執行，不是停用伺服器驗證。完成後在該對話開啟這個 connector，再先要求列出 studies / series / contact sheet。

**介面若沒有 Request headers，本 Bearer 版本不能保證直接連上該帳號的 claude.ai。** OAuth Client ID／Secret 欄不是貼 Bearer token 的地方。此時需要另接合規 OAuth 授權服務／OAuth gateway；本包沒有聲稱已實作或驗收 OAuth。不要為了連上而關閉驗證，也不要使用含 token 的網址。Claude Code 能設 header 不代表 claude.ai 的介面一定相同。

建議第一個提示：「使用右膝 MRI connector 列出所有 study 和 series；先看 contact sheet，再取指定切片。請分清圖上觀察、未知與推測，不引用先前報告，不把同張影像不同窗位當成病情進展。」

## 9. 限制與後續驗收

- 影像在幾次 MRI 之間不能僅按 I 編號對齊；層厚、方向、TE/TR、顯示窗不同，都不代表病情變化。
- 重複偵測以 OCR 遮罩加自適應主體區域計算 pHash，再以像素相關／誤差檢查；只在配對計算時排除最多 2% 大差異點（可能是燒錄文字／壓縮差），不改影像，pHash 距離≤4、相關係數≥0.995、正規化 RMSE≤0.015 才接受。相鄰切片相似不等於重複；不唯一或低品質重新編碼配對會被留作 issue，需要人工确认，而非刪掉資料。
- 本版沒有非線性 VOI LUT、顏色 MRI、無 preamble 舊式 DICOM、跨機自動影像配準、診斷 AI、醫療稽核認證或完整 OAuth。
- 單個 contact sheet 若因超大序列超過 1 MB，工具同樣拒絕，不私自降低灰階。可先調整本機縮圖 cell 的配置再重新覆核／放行；原始切片仍保留。

## 官方依據

- MCP SDK v1（本程式使用的 FastMCP API）：https://github.com/modelcontextprotocol/python-sdk/tree/v1.x
- SDK 主線與版本相容性：https://github.com/modelcontextprotocol/python-sdk
- MCP ImageContent schema：https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-06-18/schema.mdx
- Inspector CLI：https://github.com/modelcontextprotocol/inspector/tree/v1/main
- pydicom pixel data：https://pydicom.github.io/pydicom/stable/guides/user/working_with_pixel_data.html
- DICOM Clean Pixel Data：https://dicom.nema.org/Medical/dicom/2023a/output/chtml/part15/sect_E.3.html
- Claude 遠端 connector：https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- Claude connector Request headers 說明（官方語系頁）：https://support.claude.com/de/articles/11175166-erste-schritte-mit-benutzerdefinierten-connectoren-mit-remote-mcp

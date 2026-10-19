# 0.1.1 圖片命名更新｜2026-10-19

本次新增可獨立辨識的輸出檔名（日期／機構代碼／右膝／序列／技術描述／切片號）、contact sheet 名稱、manifest 與 MCP metadata 的 filename／slice_id，以及只在本機保存的 `.private/rename_map.csv`。

本輪合成資料與既有核心測試：**52 passed、2 skipped**；見 `evidence/naming-pytest-summary.txt` 與 `evidence/naming-pytest.xml`。新增測試包括同名防混淆、原張號保留、去重複指向同一新名稱、唯讀原始檔、新 PNG 完整解碼、DICOM 數值 sidecar 命名，以及舊数字檔名資料集相容。

兩項跳過仍是需要 mcp／pydicom 的實際整合測試。新增 DICOM sidecar 命名測試使用模擬解碼結果，**不是 DICOM 實檔驗收**。本輪沒有對完整 MRI 執行 OCR／去識別化，也沒有更名 Google Drive 原圖、重新上傳 Notion、推送 GitHub 或部署遠端伺服器。影像清理、Inspector、Claude 與 HTTPS 的先前未驗收狀態不變。

重新輸出請使用新目錄 `dataset-v2/`，不在已放行的 dataset 內直接改名；完成本輪重新覆核後才切換伺服器目錄。

---

## 0.1.0 歷史交付紀錄（以下保留當時實測結果）

# 本次交付與實際驗證狀態

版本：0.1.0。這是程式交付與測試紀錄，不是 MRI 診斷報告。

## 已完成

- 資料整理 CLI、study mapping、匿名副本來源對應、pHash + 像素檢查、source provenance 分離、OCR 文字框遮罩／覆核、PNG 輸出、逐序列 contact sheet、manifest、validation_report、人工 release gate。
- Python 官方 FastMCP 的六個工具程式；PNG 由 ImageContent 回傳，而非 base64 文字。
- 單一 Bearer token、Host/Origin、路徑／symlink、批次數量、crop/zoom、window、尺寸及資料完整性保護。
- Docker、Compose、Caddy、既有 Nginx 範例、token 產生器、README、MCP Inspector 六工具自動驗收腳本。

## 已執行的測試

最終 pytest 結果見 `evidence/pytest-summary.txt` 及 `evidence/pytest.xml`。測試涵蓋：

- Metadata 解析、英文字月份、DOB 不誤當檢查日期、檔名衝突、雙權重不猜測。
- 動態遮罩、方向／姓名縮寫區分、斜切方向 AR/PL、殘留文字不外洩。
- 重複與相鄰影像區分、唯讀 source hash、缺資料禁止放行。
- 影像讀取／crop／zoom／批次／compare／contact sheet、JPEG 不可換窗。
- DICOM 數值像素換窗時的遮罩保持（合成 NPZ；不是 pydicom 實檔驗收）。
- Bearer、Host、Origin、request size、無認證及猜檔案 URL 被拒絕。
- 變更 PNG／manifest 雜湊後拒絕服務；逐張人工放行條件。

這些核心測試不需要真正的 MCP 套件；**不得把它们當成 Inspector 或 Claude 端驗收。**

## 三張既有 JPEG 選片試跑

使用先前對話掛載的三份檔案建立本機唯讀樣本副本，沒有改原影像。

|項目|實際結果|
|---|---:|
|來源影像檔|3|
|唯一切片 PNG|2|
|匿名副本配對|1|
|輸出 contact sheet|2|
|來源前後 SHA-256|全部一致|
|阻擋錯誤|5|
|是否放行／外送|否|

已配對的匿名樣本對回 2019 原圖：pHash 距離 2、像素相關係數約 0.9978。這是影像來源相似度，不是病情、身分驗證或診斷機率；不得推定整個匿名資料夾都已逐檔核對。

五項 blocking issue：兩個序列張數不全（因為只取選片）、一處 OCR 與檔名衝突、兩張影像的後驗 OCR 有殘留／可疑文字。英文 OCR 會漏中文，也會把組織紋理誤當文字；本次沒有把它們宣告為「無個資」或放行。原始樣本與候選 PNG、OCR 原文都不包含在交付 ZIP。

## 尚未完成——不能寫成通過

|驗收項目|狀態與原因|
|---|---|
|你的完整 raw 全量處理|目前環境沒有你電腦的完整 ./raw，只有先前對話選片|
|pydicom 實際讀取與 Enhanced / 壓縮 DICOM|本環境未安裝 pydicom，無法下載套件；對應測試明確 skipped|
|官方 MCP SDK 執行、ImageContent on-wire|本環境未安裝 mcp，pip 因 DNS／網路失敗；對應測試 skipped|
|MCP Inspector 六工具驗收|已附可執行腳本；本機缺套件且 npm 無離線快取，未執行成功|
|HTTPS 部署到你的主機|沒有已确认的主機／部署通道／專用子網域，未登入、未上線|
|claude.ai 實際收到並能讀圖|必須在遠端部署、認證與 Inspector 通過後，由實際帳號連接測試；目前未驗證|

`evidence/inspector_status.json` 記錄未執行狀態，不提供假的 PASS JSON。容器啟動也不等於 HTTPS／Claude 驗收。

## 上線前必需的資料與條件

完整可存取的 raw／DICOM；正確的資料夾 mapping 和技術參數補正；逐張隱私／解剖覆核；具體主機與部署管道、DNS／TLS；Claude 帳號的 Request headers 或另建 OAuth 整合。不要透過聊天貼 token 或 SSH 私鑰。

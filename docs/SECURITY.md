# 安全邊界與已知限制

## 資料流
本機 raw（唯讀）→ 本機 OCR / DICOM metadata extraction → 本機候選 dataset → 驗證與逐张人工覆核 → release.json → 僅已放行 dataset 部署到只讀容器 → HTTPS + Bearer → MCP ImageContent → 授權的 Claude 對話。

`.private/ocr_cache` 可能包含姓名等原始 OCR 文字；`provenance.json` 可能包含具名資料夾。它們只留本機，不打包、不部署、不作 MCP tool 回傳。

## Fail-closed
- Unknown study／未唯一匹配的匿名副本不自行新增 study。
- OCR/檔名衝突、缺參數、缺片、重複 index、DICOM series number 碰撞、殘留文字，都禁止 release。
- 英文 OCR 無法認證中文去識別完整性。所有真實影像必須人工逐張、原解析度確認。
- 偵測到疑似解剖紋理的 OCR 框不盲目遮蓋。只有實際文字框或明確人工補框被清除；殘留／誤識仍列待解決。
- 來源仍可能含遠端 token／密碼？不傳遞任何 OCR 自由文字／DICOM 自由文字至 MCP metadata。
- source_path 為不透明引用，真實相對路徑本機留存。
- DICOM 像素數值在已遮區域被清空；每次 window / MONOCHROME1 inversion 後再次強制黑色，避免換窗復原已遮資料。
- get_slice 不讀 DICOM，不讀 raw，只讀已封存 PNG 或去識別 NPZ。
- 不自動去掉錯誤、不提供 force release、不把 Inspector skip 當 pass。

## 伺服器
- 固定六個唯讀工具，不提供 write/upload/shell/fetch-URL。
- Token 至少 32 字元；常數時間比較；不接受 query/path token。
- 拒絕不允許的 Host/Origin、多重 Authorization、越界 crop、超大 zoom、超過 8 張批次；單張 PNG 最大 1 MB。
- 一般工具錯誤為代碼，不回傳 traceback、完整路徑、raw 檔名或未篩選的來源描述。
- SDK/uvicorn request/error logs 關閉，避免例外包含敏感內容。正式主機仍應由代理提供不記 Authorization／request body／token 的最低限度狀態稽核。
- Docker dataset:ro，沒有 raw mount、沒有 file_server；loopback port 經反向代理提供 TLS。
- release.json 是本機完整性檢查，不是第三方簽章；可改部署檔案的人仍屬信任邊界。正式環境應限制主機 shell、Docker、備份及部署權限。
- Bearer 是單一持有人憑證，不是多使用者隔離，也不是已完成 OAuth。若需不同人有不同切片權限，須另做帳號／scope／授權服務，不應共用 token。

## 明確不聲稱
不聲稱 DICOM PS3.15 全部符合、不聲稱醫療去識別化保證、不聲稱 AI 能根據此資料確診、不聲稱患者影像已上線或已在 Claude 被看見。實際醫療判讀仍以專業醫師及完整原始 DICOM 為準。

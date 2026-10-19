# 圖片命名規則｜0.1.1

## 衍生副本的檔名

`<study_id>_RK_S<原序列號>_<已擷取技術描述>_I<原張號>.png`

- `2019-03-20_TSGH_RK_S07_COR_PD_I0013.png`
- `2024-12-03_VGHTPE_RK_S03_AX_T2_FS_FSE_I0012.png`
- `2019-03-20_TSGH_RK_S07_COR_PD_CONTACT.png`（縮圖總覽）

RK 表示本專案的右膝；S 是來源序列，I 是來源張號，至少四位補零。原本有序列／張號不更動，不以相鄰片推定未知資訊。這些名稱是格式範例，技術描述以實際擷取並覆核結果為準。

## 四個邊界

1. **raw 唯讀**：改的是輸出的副本，原始 JPEG、DICOM 和雜湊不變。
2. **不把猜測寫進名稱**：不含姓名、病歷號、醫師名或水腫／皺襞夾擠等诊斷；機構僅用已配置代碼。PD+T2 等未定權重仍阻擋放行，不能靠改名解決。
3. **匿名片不多算一次檢查**：已成功配對的匿名來源記錄指向同一 2019 切片的新 filename，不另建 2025 study，不多輸出一張。
4. **舊資料集不就地搬動**：重建到新目錄，避免 manifest、review、release 與檔案路徑不一致；舊數字檔名資料集仍能由原 manifest 讀取。

## 新舊檔名追溯

- `manifest.slices`：`filename`、`slice_id`、`path`。
- `manifest.sources`：每個來源含 `canonical_slice_id` 與 `output_filename`；未解決來源為 null。
- `.private/rename_map.csv`：來源相對路徑／影格號、新輸出路徑、duplicate_of 與狀態。**可能含個資，僅存本機，檔案權限 0600，不部署、不入 Git。** 為避免試算表公式注入，特殊開頭的原路徑會加前置單引號；原始精確路徑以 provenance.json 為準。
- MCP 圖片仍為 ImageContent；另一個 metadata TextContent 額外包含 filename 和 slice_id。ImageContent 本身沒有在本版新增 filename 屬性。

## 本次測試範圍

52 passed、2 skipped。新增命名測試皆用合成圖或模擬 metadata；沒有宣稱你的完整 MRI 已改名／去識別化。GitHub／遠端部署／MCP Inspector／pydicom 實檔讀取均未在本轮重新驗收。

## 重建命令

```bash
knee-mri build --raw ./raw --out ./dataset-v2 --config config/studies.yaml --private ./.private
```

逐張核對 `dataset-v2/review.html`、解決 `validation_report.md` 的 blocking issues，完成人工 review.json 後再 release。不可自動將真實醫療影像標為已覆核。

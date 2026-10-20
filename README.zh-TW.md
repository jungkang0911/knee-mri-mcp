# knee-mcp

**讓 Claude 或 ChatGPT 直接看你自己的膝關節 MRI／X 光影像**：影像放在 Google Drive 資料夾，燒在影像上的個資先遮蔽，再透過需要驗證的私人 MCP 伺服器提供給 AI。

[English →](README.md)

> ⚠️ knee-mcp 是給病友用的影像檢視工具，不是醫療器材。AI 對影像的說明不是診斷，請與醫師討論。

## 為什麼做這個

病友拿到的影像通常是醫院光碟，或一整個資料夾的 JPEG。想問 AI「我的膝蓋 2019 年和 2024 年差在哪裡？」，就得一張張手動上傳，每次對話重來一次，而且每張圖上都有姓名、病歷號、生日。knee-mcp 把資料夾變成一組 AI 可以自己操作的工具：列出檢查、整個序列的縮圖總覽、放大單張、兩次檢查左右並排比較。

這個專案是為一個真實案例做的（見[案例說明](docs/case-study.zh-TW.md)）：右膝，2018 年 X 光、2019 年術前 MRI、2023 年 X 光、2024 年追蹤 MRI，兩家醫院，共 438 張。

## 提供的工具

| 工具 | 回傳 |
|---|---|
| `list_studies()` | 找到的檢查：日期、醫院、類型、序列數、張數，以及命名問題 |
| `list_series(study)` | 每個序列的張數、切面／序列名稱（例如 `SAG PD FS`）、SL 範圍 |
| `get_contact_sheet(study, series)` | 一張圖：整個序列的縮圖，每格標張號 |
| `get_slice(study, series, index, crop, zoom)` | 一張圖＋一行說明（切面、SL、ST、塗黑框數） |
| `get_slices(study, series, start, end)` | 最多 8 張相鄰切片 |
| `compare(a, b)` | 兩張左右並排，各標日期與張號 |

- 圖片是真正的 MCP `ImageContent`（PNG），不會把 base64 當文字回傳；每次回應都控制在 MCP 單則訊息 1 MiB 以內。
- 支援 **claude.ai** 自訂連接器、**ChatGPT** 開發人員模式連接器（兩者都走 OAuth）、**Claude Code** 和 **MCP Inspector**（Bearer token）。

## 實際畫面

以下都是作者本人的膝蓋影像，經 knee-mcp 遮蔽後公開，已獲本人同意。

**`compare`**：2019 年術前 MRI 和 2024 年追蹤，同一切面左右並排：

![2019 與 2024 矢狀面比較](docs/images/compare-2019-2024.png)

**`get_contact_sheet`**：2024 年 MRI 冠狀面的相鄰切片，每格標張號，AI 可以再點名要哪一張：

![2024 冠狀面縮圖總覽](docs/images/contact-sheet-2024-cor.png)

**遮蔽**：一張 2019 年影像實際回傳的樣子，紅框是塗黑的區域（`prebuild --debug-overlays`）。上方文字帶原本是姓名、病歷號、生日；左下的技術參數保留可見：

![遮蔽示意](docs/images/masking-overlay.png)

## 運作方式

```
Google Drive 資料夾（drive.readonly，只讀一個資料夾）
        │  依簡單規則命名的 JPEG／PNG，或醫院光碟的 DICOM 原檔
        ▼
python -m knee_mcp.prebuild      ← 離線、增量處理（fileId + modifiedTime）
        │  1. 讀切面／SL／ST（OCR，或 DICOM 標籤）
        │  2. 遮蔽個資（醫院邊緣文字帶＋OCR 找姓名、證號、日期）
        │  3. 存遮蔽後的 PNG 和 manifest.json（原圖不寫入硬碟）
        ▼
MCP 伺服器（Streamable HTTP，OAuth 或 Bearer token）
        │  工具只讀事先處理好的結果：約 0.1–3 秒回應
        ▼
Claude／ChatGPT
```

- **檔案：** JPEG 依 `{YYYY-MM-DD}[_{醫院}]_RK_[XR_][S{序列}_]I{張號}.jpg` 命名；或醫院光碟的 DICOM 原封不動上傳（日期、序列、張號從 DICOM 標籤讀，病患標籤一律不讀出）。
- **遮蔽：** 只塗黑兩類區域：醫院範本定義的邊緣文字帶，以及邊緣區內「內容像個資」的文字框（中文姓名、病歷號／身分證號、日期、醫師姓名）。解剖區一律不塗；解剖區內若有疑似個資，改標記由你人工確認。用 `prebuild --debug-overlays` 可以逐張檢查塗黑了哪裡。
- **安全：** 單一擁有者。CLI 用 Bearer token；claude.ai／ChatGPT 用內建的小型 OAuth 伺服器（動態註冊＋PKCE＋密碼頁）。Drive 只用唯讀權限、只讀一個資料夾。

### 關於遮蔽，請務必讀

OCR 遮蔽不可能完美。在真實案例開發過程中，它曾**漏掉 X 光上「完整英文月份」格式的生日**（那時 X 光沒有邊緣範本），更早的版本也曾把脛骨塗黑。兩者都已修正並有測試，但**請先用除錯疊圖確認你醫院的版面**，並為你的醫院加一個範本（`knee_mcp/templates.py` 或 `KNEE_MASK_TEMPLATES`）。如果只給自己用，也可以關閉遮蔽（`KNEE_MASKING=0`）。

## 快速開始

需求：Python 3.10+、[Tesseract OCR](https://github.com/tesseract-ocr/tesseract)（含 `eng`、`chi_tra` 語言檔）。

```bash
git clone https://github.com/jungkang0911/knee-mri-mcp.git && cd knee-mri-mcp
python -m venv .venv && . .venv/bin/activate       # Windows：.\.venv\Scripts\Activate.ps1
pip install -e ".[test]"
cp .env.example .env                                # 填入下面的設定
python -m knee_mcp.google_login                     # 或改用 Google 服務帳戶
python -m knee_mcp.prebuild --debug-overlays ./debug
python -m knee_mcp                                  # 服務在 http://127.0.0.1:8765/mcp
```

最少需要的 `.env`：

```ini
KNEE_ROOT_FOLDER_ID=<Drive 資料夾 ID>
GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/service-account.json   # 或 GOOGLE_CLIENT_SECRETS + google_login
KNEE_PUBLIC_URL=https://knee.example.com
KNEE_BEARER_TOKENS=<python -m knee_mcp --gen-token 產生>
KNEE_OWNER_PASSWORD=<OAuth 登入頁用的長密碼>
KNEE_OCR_LANGS=eng+chi_tra
```

接著用 Cloudflare Tunnel 或 Tailscale Funnel 對外提供 HTTPS，把 `https://knee.example.com/mcp` 加成連接器。逐步說明（Google Cloud 服務帳戶、Windows 與 Ubuntu systemd 部署、所有設定、claude.ai／ChatGPT／Claude Code 連線）都在 [docs/setup.zh-TW.md](docs/setup.zh-TW.md)。

## 開發與測試

```bash
pytest -q        # 約 60 個測試：mock Drive、燒入假個資的合成影像、合成 DICOM、OAuth 流程
```

測試完全不碰真實資料：在合成膝蓋影像上燒入假姓名、假病歷號、假生日，確認 OCR 讀不回來、解剖區像素不變、每次回應都在一則 MCP 訊息內。

## 現況

目前用在一位病友自己的真實資料上。歡迎貢獻，特別是其他醫院的遮蔽範本，以及任何遮蔽漏掉的回報（請不要附真實影像）。

## 授權

[MIT](LICENSE)

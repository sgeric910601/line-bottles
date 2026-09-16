# LINE on Bottles

*[English](README.en.md)*

在 Ubuntu 上執行 Windows 版 LINE,跑在 Flatpak Bottles + Wine 之中,並用一台專屬的巢狀 X server(Xephyr `:99`)把它與主桌面隔開。

這個 repo 收的是**外殼層** —— 啟動流程、systemd 服務、輸入法橋接、版本切換與安裝腳本。LINE 本體與你的聊天記錄留在 Wine prefix 裡,不進版控。

## 為什麼要這麼複雜

直接用 Bottles 跑 LINE 會遇到幾個問題:視窗管理混亂、會冒出黑視窗、中文打不出來。把它關進專屬的 Xephyr 再配一個 openbox,這些問題就被侷限在那個視窗裡,不會污染主桌面。

代價是多了兩層隔離,衍生出三件必須額外處理的事:

| 問題 | 解法 |
|---|---|
| Flatpak 沙箱看不到 host 的 `/tmp/.X11-unix/X99` | `socat` 把它橋接成 TCP `127.0.0.1:6099` |
| fcitx5 只服務主顯示器,`:99` 上沒有輸入法 | 為 `:99` 另開一個隔離的 fcitx5 實例 |
| LINE 會拒載沒有簽章的 Wine 系統 DLL | 用本機自簽憑證把 `system32`/`syswow64` 簽過一遍 |

## 快速開始

```bash
git clone <此 repo> ~/line-bottles
cd ~/line-bottles
./install.sh --dry-run      # 先看會做什麼
./install.sh
```

bottle 已經存在時會自動跳過建立,只安裝外殼層。要從零開始的話,把 LINE 官方安裝檔放到 `~/Downloads/LineInst.exe`(或用 `LINE_INSTALLER=` 指定路徑)。

## 常用指令

```bash
line-bottles                  # 啟動(等同點選單裡的 LINE)
line-bottles --stop           # 關閉 LINE 與殘留的黑視窗
line-bottles --recover        # 卡住時強制重啟整套環境
line-bottles --status         # 輸出 JSON 狀態

line-version status           # 目前版本、佈局、自動更新狀態
line-version list             # 已安裝的版本
line-version use 26.4.2.3957  # 切換版本
line-version pin              # 鎖定版本(停用 LINE 自動更新)
```

版本切換的細節見 [docs/versions.md](docs/versions.md)。

## 架構速覽

```
GNOME 桌面 (:1)
└── Xephyr :99                    一台 1280x800 的巢狀 X server
    ├── openbox                   視窗管理
    ├── fcitx5(獨立實例)         注音輸入,私有 dbus + 隔離設定
    └── Wine / LINE.exe           經 socat TCP 橋接連入
```

五個 systemd user unit 由 `line-session.service` 統籌,啟動與關閉順序**不是**彼此的反序 —— 理由寫在 `lib/session.py` 的 `START_ORDER` / `STOP_ORDER` 旁邊,以及 [docs/architecture.md](docs/architecture.md)。

## 已知限制

- **不能拖放檔案。** Xephyr 是獨立的 X server,而 X11 的拖放(XDND)靠同一個 X server 內的 client message,跨 server 沒有這個機制。拖出、拖入都不行。唯一根治法是拆掉 Xephyr,但那會把當初要解決的視窗管理問題帶回來。
- **畫面偏慢。** `LIBGL_ALWAYS_SOFTWARE=true` 讓所有 GL 走 llvmpipe(CPU 軟體渲染)。這是目前流暢度的上限。
- **檔案選擇器只看得到 `~/Downloads` 與 `~/Pictures`。** Flatpak 沙箱刻意只開這兩個目錄;要傳其他位置的檔案得先複製過去。

## 出問題時

先看 [docs/troubleshooting.md](docs/troubleshooting.md)。那份文件除了症狀對照表,還記錄了**已經證實無效的排查方向** —— 這些是實際查過、被日誌推翻的假設,照著重試只會浪費時間。

## 檔案配置

| 路徑 | 內容 |
|---|---|
| `bin/*.in` | 啟動器範本,`install.sh` 會代入實際路徑 |
| `lib/` | `session.py`(主控)、`x11.py`(ctypes Xlib)、`version.py`、`peversion.py` |
| `systemd/` | 5 個 user unit |
| `bottle/` | 建立 bottle、簽署 DLL、憑證設定 |
| `docs/` | 架構、疑難排解、版本管理 |

`lib/` 下的程式只用 Python 標準函式庫,沒有第三方相依。

## 授權

MIT,見 [LICENSE](LICENSE)。本專案只打包外圍基礎設施,不轉散布 LINE、Wine、Bottles 或 DXVK 本身。

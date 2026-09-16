# 版本管理

## 佈局

LINE 原本的結構是 `bin/current`(實體目錄)加上 `bin/old`(上一版)。`line-version migrate` 會把它改成:

```
bin/
├── current -> versions/26.4.2.3957     符號連結,指向使用中的版本
├── versions/
│   ├── 26.4.2.3957/                    各版本約 135 MB
│   └── 26.3.1.2841/
├── LineLauncher.exe
└── LineUpdater.exe
```

切換版本只是重指一條符號連結,幾秒完成。

**`Data/` 不在 `bin/` 底下,各版本共用。** 聊天記錄、登入狀態、設定都在那裡,切換版本不會影響它們 —— 但也代表**降版有風險**:新版可能已經把 `Data/` 升級成舊版讀不懂的格式。降版前先備份 `Data/`。

## 指令

```bash
line-version status           # 目前版本、佈局、自動更新狀態
line-version list             # 列出已安裝版本,標示使用中
line-version migrate          # 一次性:轉成符號連結佈局
line-version use <版本>       # 切換
line-version snapshot [名稱]  # 把目前版本另存一份
line-version pin / unpin      # 鎖定 / 解除鎖定版本
line-version prune            # 刪除與目前版本重複的 bin/old
```

切換與快照都需要 LINE **未執行**,工具會自己檢查並擋下來。

## 版本號怎麼來的

`lib/peversion.py` 直接解析 `LINE.exe` 的 PE 版本資源(`VS_FIXEDFILEINFO`)。這台機器沒有 `pefile` 或 `exiftool`,而這裡只需要一個欄位,不值得為它引入新相依 —— 與 `x11.py` 一樣只用標準函式庫。

```bash
python3 lib/peversion.py <任何 PE 檔>
```

## 與自動更新的互動

LINE 透過 `LineUpdater.exe` 自動更新,更新時會把 `bin/current` 換掉。這跟「鎖定某個版本」是直接衝突的。

`line-version pin` 把 `LineUpdater.exe` 改名為 `LineUpdater.exe.disabled`,LINE 找不到它就不會更新。`unpin` 改回來。這個做法完全可逆,也不用改登錄檔。

預設**不鎖定** —— 自動更新會帶來安全性修正。建議的用法是:想留住某個版本時先 `snapshot`,再視需要 `pin`。

```bash
line-version snapshot      # 先把目前版本留一份
line-version pin           # 再鎖定
```

## 取得舊版安裝檔

LINE 官方只提供最新版。要保留舊版,唯一可靠的來源是**你自己在更新前做的快照** —— 這也是為什麼 `snapshot` 值得在每次更新前跑一次。

`bin/old` 是 LINE 自己保留的上一版,`migrate` 會在版本不同時自動把它一併收錄;若與目前版本相同(單純的重複資料),會提示你用 `prune` 移除以省下 135 MB。

## 風險與退路

符號連結的做法依賴兩件事,首次使用時請確認:

1. **Wine 能正常跟隨 `bin/current` 這條符號連結** —— 切換後啟動一次 LINE 確認即可
2. **未鎖定時,`LineUpdater` 更新後 `current` 是否仍是符號連結** —— 有可能被換成實體目錄

若第 2 點發生,`line-version status` 會顯示「尚未遷移」,再跑一次 `migrate` 即可恢復。若第 1 點不成立(Wine 不吃符號連結),退路是改用「改名切換」:把要用的版本目錄改名為 `current`,原本的改回版本號。

# 架構

## 四層結構

```
GNOME 桌面 (:1)
│
└── Xephyr :99                1280x800,-ac(關閉存取控制)、-noreset
    │                          在 GNOME 上就是一個標題為 LINE 的普通視窗
    ├── openbox --sm-disable   視窗管理
    ├── fcitx5(獨立實例)      DISPLAY=:99、私有 dbus、隔離設定目錄
    └── Wine / LINE.exe        DISPLAY=127.0.0.1:99(經 socat)
```

## 五個 systemd user unit

| Unit | 做什麼 | 關鍵設定 |
|---|---|---|
| `line-session` | 總控。跑 `session.py --session`,負責相依檢查、依序啟動其餘四個、監控存活、結束時清理 | `ExecStopPost=--cleanup`、`TimeoutStopSec=35` |
| `line-xephyr` | 巢狀 X server | `SuccessExitStatus=1`(關視窗時離開碼為 1,不算失敗) |
| `line-fcitx5` | `:99` 專屬輸入法 | `BindsTo=line-xephyr`、**`Before=line-window-manager`** |
| `line-window-manager` | openbox | `TimeoutStopSec=5` |
| `line-x11-bridge` | socat TCP 橋接 | `SuccessExitStatus=143`(SIGTERM) |

這些 unit **都沒有被 enable**,也不該 enable —— 它們只由 `line-session` 觸發。原本 `line-xephyr` 與 `line-x11-bridge` 寫了 `WantedBy=default.target`,但從未生效,已移除以免誤導。

## 為什麼需要 socat 橋接

LINE 跑在 Flatpak 沙箱裡。Flatpak 的 `--socket=x11` 只會把**主** X server 的 socket 掛進沙箱,`/tmp/.X11-unix/X99` 看不到。

解法是 `socat TCP4-LISTEN:6099,bind=127.0.0.1 → UNIX-CONNECT:/tmp/.X11-unix/X99`,LINE 那端用 `DISPLAY=127.0.0.1:99` 走 TCP 連進來(`session.py` 裡設定)。Xephyr 開了 `-ac`,所以不需要 X 授權檔。

## 為什麼 `:99` 需要自己的 fcitx5

一個 fcitx5 實例只能服務一個 X display —— XIM 前端 `libxim.so` 相依 `xcb` addon,而 xcb 只會連 `$DISPLAY`。主實例在 `:1`,`:99` 上因此完全沒有 XIM server,LINE 就打不出中文。

第二個實例靠四重隔離避免與主實例互相干擾(歷史上兩個 fcitx5 同時連同一個 display 造成過「一半注音一半英文」):

| 面向 | 做法 |
|---|---|
| X display | 固定 `DISPLAY=:99`,永不接觸主顯示器 |
| DBus | `dbus-run-session` 給私有 session bus,不競爭 `org.fcitx.Fcitx5` |
| 設定檔 | 獨立 `XDG_CONFIG_HOME`;fcitx5 結束時會回寫設定,隔離後汙染不到 `~/.config/fcitx5` |
| 生命週期 | `BindsTo=line-xephyr`,Xephyr 一停它就停 |

`ExecStartPre` 每次啟動會把主實例的 `config`/`profile` 複製到隔離目錄當種子,所以 LINE 裡的輸入法行為會跟著你的主設定走。

注音個人詞庫 `~/.chewing/chewing.sqlite3` **兩個實例共用**(sqlite 有檔案鎖,並行寫入安全),所以在 LINE 裡學到的詞會進主詞庫。

## 啟動與關閉順序

兩者不是彼此的反序,各有理由。定義集中在 `lib/session.py` 的 `START_ORDER` / `STOP_ORDER`。

**啟動**:`xephyr → fcitx5 → window-manager → x11-bridge`

fcitx5 必須在 openbox 之前。openbox 啟動時會嘗試開啟輸入法,晚了就只會在日誌留下 `Failed to open an Input Method`。

**關閉**:`window-manager → x11-bridge → fcitx5 → xephyr`

openbox 必須在 fcitx5 之前結束。openbox 用 `XRegisterIMInstantiateCallback` 註冊回呼,XIM server 晚點出現它也會回頭接上;若 fcitx5 先死,openbox 會卡在 XIM 拆連線的同步等待上,SIGTERM 叫不動,60 秒後才被 SIGKILL —— 期間舊的 Xephyr 一直佔著 `:99`,下一次啟動就會失敗。

這個順序由 `line-fcitx5.service` 的 **`Before=line-window-manager.service`** 保證。在 systemd 裡 `Before=` 同時定義啟動與關閉順序(關閉時反向),一行就涵蓋兩個方向。`cleanup()` 的 `systemctl stop A B C D` 是**平行**執行的,沒有這行就沒有順序保證。

## 存活偵測

`session()` 的監控迴圈每秒檢查一次,連續 5 秒判定為「使用者關掉了 LINE」才收掉整個工作階段。

判定條件是 `s.get('windows') or bottle_pids()`。**不能只看視窗** —— `x11.py` 的 `inspect` 只認標題剛好等於 `"LINE"` 的視窗,而圖片檢視器的視窗標題是寄件者名稱。檢視器全螢幕蓋住主視窗時會回報零視窗,只看視窗就會在使用者看圖看到一半時自我了斷。

## 視窗滿版

LINE 啟動時會先開一個載入視窗(約 604x426),約 8 秒後才建立真正的主視窗,**兩者 window id 不同**。只在「就緒」當下放大會打到載入視窗。

`fill_screen()` 因此在監控迴圈裡持續執行,對每個沒處理過的主視窗各送一次 EWMH `_NET_WM_STATE` + `MAXIMIZED_VERT/HORZ`。已處理過的記在 `MAXIMIZED` 集合裡,所以使用者之後自行調整的大小不會被覆蓋回去。

## 檔案進出

Wine prefix 內的 `users/steamuser/{Downloads,Pictures}` 是指向 host 家目錄的符號連結,配合 Flatpak 的 `--filesystem=xdg-download --filesystem=xdg-pictures`,LINE 存檔就會直接落在你的家目錄。

`users/steamuser` 本身是 Bottles 建立的、指向以主機使用者命名的目錄的符號連結,所以程式碼一律走 `steamuser` 這條路徑,不寫死使用者名稱。

沒有被授權的目錄 LINE 完全看不到 —— 這是刻意的,LINE 是閉源的 Windows 程式。

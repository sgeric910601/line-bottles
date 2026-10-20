# 架構

## 四層結構

```
GNOME 桌面 (:1)
│
├── 外框視窗(frame.py)        在 GNOME 上是標題為 LINE 的普通視窗;關閉 = 最小化
│   └── Xephyr :99             以 -parent 嵌在外框裡,目前固定 1280x800;-ac、-noreset
│       ├── openbox --sm-disable   視窗管理
│       ├── fcitx5(獨立實例)      DISPLAY=:99、私有 dbus、隔離設定目錄
│       └── Wine / LINE.exe        DISPLAY=127.0.0.1:99(經 socat)
│
└── 剪貼簿橋接                 唯一同時連著 :1 與 :99 的元件
        ↕                      XFixes 監聽所有權變動 + xclip 搬運內容
```

除了剪貼簿橋接之外,所有元件都只當其中一邊的用戶端(外框與 Xephyr 是 `:1` 的用戶端,
Xephyr 同時是 `:99` 本身)。`line-clipboard` 與 `line-xephyr` 都不設 `DISPLAY`,
主機那端從 systemd user manager 繼承(GNOME 登入時匯入)。

## 六個 systemd user unit

| Unit | 做什麼 | 關鍵設定 |
|---|---|---|
| `line-session` | 總控。跑 `session.py --session`,負責相依檢查、依序啟動其餘五個、監控存活、結束時清理 | `ExecStopPost=--cleanup`、`TimeoutStopSec=35` |
| `line-xephyr` | 外框視窗 + 巢狀 X server。主程序是 `frame.py`,Xephyr 是它的子行程 | `SuccessExitStatus=1`(登出時兩者都以 1 結束,不算失敗);不設 `DISPLAY` |
| `line-fcitx5` | `:99` 專屬輸入法 | `BindsTo=line-xephyr`、**`Before=line-window-manager`** |
| `line-window-manager` | openbox | `TimeoutStopSec=5` |
| `line-x11-bridge` | socat TCP 橋接 | `SuccessExitStatus=143`(SIGTERM) |
| `line-clipboard` | 主機 ↔ `:99` 的剪貼簿橋接 | **不設 `DISPLAY`**(要同時連兩邊);選用,起不來不影響 LINE |

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

## 外框視窗:為什麼標題列的 X 是最小化

Xephyr 不處理 `WM_DELETE_WINDOW`,也沒在 `WM_PROTOCOLS` 宣告它。按標題列的 X 時 mutter 只能
`XKillClient`,Xephyr 的連線一斷就 `exit(1)`,整組服務跟著停 —— 2026-10-20 一天內就這樣誤關了四次。
Xephyr 沒有任何選項能改變這件事。

`lib/frame.py` 在主機上自己建一個頂層視窗,用 `Xephyr -parent <外框>` 把畫面嵌進去,自己宣告並
接下 `WM_DELETE_WINDOW`,收到就 `XIconifyWindow`。嵌入後有幾件事 Xephyr 不會自己做,由外框補上:

| 面向 | 做法 |
|---|---|
| 鍵盤焦點 | `WM_HINTS` input=True + `WM_TAKE_FOCUS`。收到 `WM_TAKE_FOCUS` 就用 WM 給的時間戳把焦點交給 Xephyr 的子視窗;外框被直接設成焦點(`x11.py focus`)時、子視窗剛映射時也轉。焦點若停在外框,指標不在畫面上時鍵盤事件會送到外框,Xephyr 收不到 |
| 大小 | **目前固定 1280x800**(`WM_NORMAL_HINTS` 最小=最大,不給拖、不給最大化),原因見下方「改大小的限制」。同步機制仍保留:外框的 `ConfigureNotify` 停 0.2 秒沒再變,才 `XResizeWindow` 子視窗,Xephyr 收到後自己做 RandR;再等 Xephyr 調好,用 `XRRGetScreenResources` 讓 `:99` 補發 CRTC／輸出變更通知給 Wine |
| 啟動順序 | 先設好 `WM_CLASS` 等屬性再映射,等到 `MapNotify` 再 `XSync`,才啟動 Xephyr。少了同步,Xephyr 偶爾查不到外框、以 1 結束,而 1 被當成正常 |
| 生命週期 | 外框是主程序、Xephyr 是子行程(另設 `PR_SET_PDEATHSIG`)。SIGTERM → 收掉 Xephyr(3 秒後強制)→ 外框以 0 結束;Xephyr 自己結束 → 外框以同樣的結束碼或訊號結束 |

其他決定:

- **`-no-host-grab`**:Xephyr 預設在放開 Ctrl+Shift 時抓住鍵盤與滑鼠,嵌入後沒有標題可以提示,
  Alt+Tab 會莫名失效。`-title` 也拿掉了,它會在抓取時把字寫到子視窗上。
- **不宣告 `_NET_WM_PING`**:mutter 送出關閉後會檢查程式是否還活著。
- **`WM_CLASS` instance 是 `line-bottles`**(`x11.FRAME_CLASS`),`.desktop` 的 `StartupWMClass` 靠它
  對上 Dock 圖示;剪貼簿的焦點閘門與 `session.focus()` 都用 `x11.find_frame()` 找它。
- **真正結束 LINE**:Dock 圖示右鍵「結束 LINE」(`line-bottles --stop`),或在 LINE 裡登出。
  Alt+F4、概覽畫面的關閉、Dock 的「關閉視窗」都會送 `WM_DELETE_WINDOW`,所以也都變成最小化。

### 改大小的限制(2026-10-20,未解決)

外框原本可以拖曳,但 LINE 沒辦法可靠地跟著變:

1. Xephyr 改大小只發 `RRScreenChangeNotify`;Wine 11.0 只聽 CRTC／輸出變更
   (`xrandr14_device_change_handler`),一直以為螢幕還是舊大小。已解:外框調整後
   用 `XRRGetScreenResources` 讓 `:99` 補發這兩種通知(`frame.py` 的 `announce()`)。
2. LINE 放大後大小等於螢幕,Wine 會把它標成 `_NET_WM_STATE_FULLSCREEN`。螢幕改變之後,
   Wine 對這種視窗會用舊解析度渲染再等比拉伸、置中(上下黑條,文字變糊);取消全螢幕後
   LINE 能用實際大小重排,但高度被卡在一個不對的值(實測 969,螢幕是 1219)。**未解**。

研究出解法之前外框固定大小;`session.fill_screen()` 在 `:99` 畫面改變時重新放大、
`frame.py` 的大小同步與 `announce()` 都保留,之後研究直接沿用。

和正式的 LINE 並存的測試外框:`DISPLAY=:1 line-bottles --frame :98 line-bottles-test`,
標題會是 `LINE (line-bottles-test)`。測鍵盤時要注意它是黑畫面,看不到打的字,用
`DISPLAY=:98 xinput test-xi2 --root` 看按鍵有沒有進去。

## 為什麼剪貼簿要另外橋接

X11 的選取區(selection)是**綁在單一 X server 上**的協定:擁有者是某個視窗,
要資料的人向那個視窗發 `SelectionRequest`,資料透過同一個 server 的屬性傳遞。
`:1` 與 `:99` 是兩個獨立的 server,彼此沒有任何共同的視窗,所以 CLIPBOARD 天生不相通。

這跟 socat 那個橋接是**不同層次**的問題。socat 解決的是「LINE 連不到 `:99` 這個 server」;
剪貼簿要解決的是「`:1` 與 `:99` 是兩個 server」。就算 LINE 直接跑在主機上也不需要 socat,
但只要有 Xephyr,剪貼簿就一定要橋。

`lib/clipboard.py` 同時開兩個 display 連線:

| 面向 | 做法 |
|---|---|
| 變更偵測 | XFixes 的 `XFixesSelectSelectionInput`,兩個 X 連線的 fd 一起 `select()`,不輪詢 |
| 搬運 | 交給 `xclip`。INCR 分段傳輸、TARGETS 協商、`image/png` 這三件事自己寫最容易出隱性 bug |
| 格式 | 依序挑 `image/png → image/bmp → image/jpeg → UTF8_STRING → text/plain;charset=utf-8 → STRING`,**原樣轉送不做轉換**(Wine 的 winex11.drv 對圖片不一定給 png) |
| 迴圈防護 | 記住自己上次裝在每一端的內容雜湊;所有權變動若來自自己就不再彈回去 |
| 隱私 | 日誌只記位元組數與 target 名稱,**絕不記錄內容** |
| Wine 喚醒 | 每 0.5 秒戳一下 Wine 的剪貼簿視窗,見下方「Wine 11.0 的剪貼簿請求不會自己送出」 |
| 生命週期 | `BindsTo=line-xephyr` |

### 方向不對稱是刻意的

**LINE → 主機一律即時。** 從 LINE 複製出來的東西沒有外洩風險。

**主機 → LINE 只在 LINE 視窗取得焦點時才推入。** 若做成全自動,你在密碼管理員按下複製的那一刻,
密碼就會出現在 LINE 讀得到的 X server 裡 —— 而 LINE 是閉源的 Windows 程式。
焦點偵測靠監聽 `:1` root 的 `_NET_ACTIVE_WINDOW`,跟外框視窗(`x11.find_frame()`)的 id 比對。

代價是你必須先點進 LINE 視窗才貼得到,但那本來就是貼上前的動作。

### Xlib 配 select 的陷阱

主迴圈在 `select()` 之前一定要再檢查一次 `XPending`。Xlib 的任何往返呼叫(例如查
`_NET_ACTIVE_WINDOW`)都會順手把已經到達的事件讀進**內部佇列**,fd 上就沒有資料了,
`select()` 於是空等滿一秒才回來。實測這個差別是**剪貼簿延遲 1.5 秒 vs 0.05 秒**。

### Wine 11.0 的剪貼簿請求不會自己送出

症狀是兩個方向一起壞:LINE 裡複製的東西不會出現在主機;等你在外面複製、點回 LINE,
橋接把外面的內容推進 `:99`,Wine 就在 20 ms 內把剪貼簿搶回去,換上 LINE 上次複製的舊內容,
橋接再把舊內容送回主機,蓋掉你剛複製的東西。搶回之前 Wine 完全不讀新內容。

原因在 Wine 的 `dlls/winex11.drv/clipboard.c`:`acquire_selection()` 呼叫 `XSetSelectionOwner`
之後**沒有 `XFlush`**。這個請求卡在 Xlib 的送出緩衝區,要等那條 X 連線收到任何事件時才會順便送出 ——
所以才會是「外面一動,LINE 的舊內容就冒出來」。上游 Wine 已在同一個函式補上 `XFlush( display );`,
但 ValveSoftware/wine 的 `proton_11.0` 分支(`kron4ek-wine-proton-11.0-1` 的來源)還沒有。

做法:橋接每 0.5 秒對 Wine 剪貼簿執行緒的 `import_window` 送一個 `_LINE_CLIPBOARD_FLUSH`
ClientMessage。那個視窗在 Wine 裡沒有對應的 hwnd,Wine 收到後直接忽略,唯一的作用就是讓連線醒來、
把緩衝區送出去。2026-10-20 實測:送出後 36 ms,Wine 就交出了 LINE 剛複製的內容。

找視窗靠特徵,不靠寫死的 id:root 底下 1x1+0+0、未映射、沒有屬性、事件遮罩**恰好只有**
`PropertyChangeMask`(Wine 在 `clipboard_init` 為了 INCR 這樣建它)。每條 Wine 連線都還有一個只選
StructureNotify 的 1x1 視窗,遮罩不同所以不會認錯。xclip 持有選取區的視窗長得一模一樣,
因此一律排除**目前的 CLIPBOARD 擁有者** —— Wine 的 `import_window` 只負責讀,永遠不會是擁有者。
剪貼簿管理員是每個 Wine 桌面一個(在 `explorer.exe` 裡),當天 LINE 連通話、播放器子程式都開著,
`:99` 上 93 個視窗裡也只有它一個符合。符合的超過 4 個時日誌會記一筆警告。
注意遮罩是所有用戶端在該視窗上選的事件**聯集**:若別的程式也對它選了事件就會認不出來。

限制:

- 啟用 Wine 的虛擬桌面時,`import_window` 會變成桌面視窗的子視窗,特徵對不上,
  `clipboard.log` 就不會出現「找到 Wine 剪貼簿視窗」。
- 在 LINE 複製後 0.5 秒內就切出去、在外面複製、再切回來,仍可能被舊內容蓋掉。

**何時可以拿掉**:執行器換成 `acquire_selection()` 已經有 `XFlush` 的版本之後。
驗證法:暫時拿掉 `loop()` 裡的 `self.wake_wine()`,開著 LINE 重啟橋接,在 LINE 複製,
主機馬上貼得出來就不再需要。

## 啟動與關閉順序

兩者不是彼此的反序,各有理由。定義集中在 `lib/session.py` 的 `START_ORDER` / `STOP_ORDER`。

**啟動**:`xephyr → fcitx5 → window-manager → x11-bridge → clipboard`

fcitx5 必須在 openbox 之前。openbox 啟動時會嘗試開啟輸入法,晚了就只會在日誌留下 `Failed to open an Input Method`。

剪貼簿橋接排在最後,因為它只依賴 Xephyr,而且列在 `session.py` 的 `OPTIONAL` 裡 ——
它起不來只會在日誌留一筆警告,不會讓整個工作階段失敗。LINE 少了剪貼簿照樣能用。

**關閉**:`window-manager → x11-bridge → fcitx5 → clipboard → xephyr`

openbox 必須在 fcitx5 之前結束。openbox 用 `XRegisterIMInstantiateCallback` 註冊回呼,XIM server 晚點出現它也會回頭接上;若 fcitx5 先死,openbox 會卡在 XIM 拆連線的同步等待上,SIGTERM 叫不動,60 秒後才被 SIGKILL —— 期間舊的 Xephyr 一直佔著 `:99`,下一次啟動就會失敗。

這個順序由 `line-fcitx5.service` 的 **`Before=line-window-manager.service`** 保證。在 systemd 裡 `Before=` 同時定義啟動與關閉順序(關閉時反向),一行就涵蓋兩個方向。`cleanup()` 的 `systemctl stop A B C D` 是**平行**執行的,沒有這行就沒有順序保證。

## 啟動前的殘留清理

`preflight()` 的最後一步是 `clear_stale()`:掃掉上一輪沒收乾淨的 Wine 行程。

這不是預防性的潔癖 —— 2026-10-08 有一次 LINE 自動更新留下死鎖的 `wineserver`,卡了兩天,
期間 LINE 完全打不開。同一個 prefix 只能有一個 `wineserver`,殭屍不走,新的 LINE 就全部
掛在它身上空等。詳見 [troubleshooting.md](troubleshooting.md)「自動更新後整個打不開」。

兩件事必須照著做,否則這段程式反而會製造問題:

**偵測用 `WINEPREFIX` 環境變數,不要比對指令列字串。** `bottle_pids(line_only=False)` 已經
這樣做了。改成比對路徑字串會抓到「指令列剛好提到這個路徑」的無關行程 —— 實測會連執行
清理的那個 shell 自己都列進去。

**不要用 `bottles-cli stop` 當清理手段。** 它會卡在同一個殭屍上,每呼叫一次就多留一個卡住
的行程。只能直接下訊號,而且 SIGTERM 對死鎖的 `wineserver` 無效,一定要能升級到 SIGKILL。

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

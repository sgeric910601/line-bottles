# 疑難排解

## 症狀對照表

| 症狀 | 成因 | 修法 |
|---|---|---|
| 圖片點開是**全黑視窗** | `QSG_RHI_BACKEND=d3d11` 與 `QT_QUICK_BACKEND=software` 互相矛盾 | 設 `QSG_RHI_BACKEND=opengl`,且**不要**設 `QT_QUICK_BACKEND` |
| 圖片點開**完全沒反應**、連視窗都沒有 | 只移除了 `QT_QUICK_BACKEND` 卻留著 d3d11 → 連 swapchain 都不建 | 同上,改用 `opengl` |
| 看圖看到一半**整個 LINE 關掉** | 存活偵測只看標題為 `"LINE"` 的視窗,檢視器標題是寄件者名稱 | `session.py` 判定條件改為 `s.get('windows') or bottle_pids()` |
| LINE 視窗**沒有滿版** | maximize 打在 8 秒後就消失的載入視窗上 | `fill_screen()` 在監控迴圈裡對每個新主視窗各做一次 |
| LINE 裡**打不出中文** | `:99` 上沒有 XIM server | 啟動 `line-fcitx5.service`;驗證:`DISPLAY=:99 xprop -root XIM_SERVERS` 應含 `@server=fcitx` |
| 關閉後**下次啟動失敗** | openbox 卡在 XIM 拆連線,60 秒後才被 SIGKILL,舊 Xephyr 佔著 `:99` | `line-fcitx5.service` 需有 `Before=line-window-manager.service` |
| **存檔後找不到檔案** | 檔案存進了 Wine prefix 內的目錄,家目錄看不到 | prefix 內 `users/steamuser/{Downloads,Pictures}` 要是指向 host 的符號連結 |
| LINE **根本起不來** | Wine 系統 DLL 沒有簽章,被 LINE 拒載 | 執行 `bottle/sign-dlls.sh` |
| `fcitx5-fix` 報告**實例數 ≠ 1** | 它把 `:99` 的實例也算進去了 | 用 cgroup 排除:`grep -qs line-fcitx5 /proc/<pid>/cgroup` |
| **貼不進 LINE**,但從 LINE 複製得出來 | 正常行為:主機 → LINE 只在 LINE 視窗有焦點時才推入 | 先點一下 LINE 視窗再按 Ctrl+V |
| 剪貼簿**完全不通** | `line-clipboard.service` 沒起來,或 `xclip` 沒裝 | `systemctl --user status line-clipboard`;`sudo apt install xclip` |
| 剪貼簿**只有文字能過,圖片不行** | 來源那端沒有提供任何支援的圖片格式 | 查來源實際提供什麼:見下方「剪貼簿查不通時」 |
| 剪貼簿**慢一拍**(約一秒) | `select()` 前沒有重新檢查 `XPending` | 見 `docs/architecture.md`「Xlib 配 select 的陷阱」 |
| 關掉 LINE 後**剛複製的內容不見了** | X11 的選取區由擁有者行程持有,橋接一結束就沒有擁有者 | 不是 bug,見下方「剪貼簿的固有限制」 |
| **點了沒反應**、Xephyr 裡一個視窗都沒有 | 上一輪留下死鎖的 `wineserver`,新的 LINE 全掛在它身上 | `preflight()` 的 `clear_stale()` 會自動處理;手動做法見下方「自動更新後整個打不開」 |
| `line-bottles --stop` **也沒用** | `bottles-cli stop` 自己也卡在同一個殭屍上 | 同上 —— 別再試 `--stop`,直接下訊號 |

## 已證實無效,別再重試

以下是實際查過、被日誌推翻的假設。重走這些路只會浪費時間。

**「DXVK / Vulkan 在 Xephyr 上建不起來」** —— 錯。DXVK 日誌顯示裝置與 swapchain 都建立成功,零錯誤:

```
D3D11InternalCreateDevice: Using feature level D3D_FEATURE_LEVEL_11_0
Presenter: Actual swapchain properties:
  Buffer size:  602x762
  Image count:  4
```

問題從來不是圖形堆疊不能用,而是**沒有東西被畫進那個 swapchain**。

**「Xephyr 的 GLX 撐不住 llvmpipe 而崩潰」** —— 錯。`wine-debug.log` 結尾確實是 `X connection to 127.0.0.1:99 broken (explicit kill or server shutdown)`,看起來非常像 X server 崩潰。但 journal 顯示那是**正常的關閉流程**:所有服務收到 SIGTERM(signal 15)、依序乾淨停止。

**這是本專案最重要的一課:只看 Wine 日誌會查錯方向,必須交叉比對 `journalctl --user -u line-*`。**

**「COM 類別未註冊造成的」** —— 無關。`{c2f03a33-…}` 與 `{d9b3211d-…}` 出現在 `commdlg` / `IShellBrowser` / `#32770` 的上下文裡,那是**存檔對話框**,不是圖片檢視器。

**「decoder 相關錯誤」** —— 無關。日誌裡 22 次 "decoder" 全是 `cryptasn:CryptDecodeObjectEx`,那是 **Authenticode 憑證的 ASN.1 解碼**,與影像解碼無關。

**「Xephyr 應該會自己代理剪貼簿」** —— 不會。Xephyr 只是一個 X server 的實作,
它不是 X client,不參與任何 selection 協商。`autocutsel` 那類工具也幫不上忙 ——
它同步的是**同一個 display 內**的 PRIMARY 與 CLIPBOARD,不跨 server。

**「`virtual_desktop` 設定有影響」** —— 沒有生效。`bottle.yml` 裡雖有 `virtual_desktop: true`、`virtual_desktop_res: 1920x1080`,但 Wine 登錄檔中沒有 `Explorer\Desktops` 項目,實際上沒有啟用。

## 自動更新後整個打不開

2026-10-08 實際發生過,值得記下來:LINE 自動更新到 26.5.0.3975 之後留下一個**死鎖的
`wineserver`,卡了兩天九小時**。症狀是點 LINE 完全沒反應 —— 服務全部 active、Xephyr 開著,
但裡面一個視窗都沒有。

成因:**同一個 Wine prefix 只能有一個 `wineserver`。** 殭屍沒走,之後每次啟動的 `LINE.exe`
都連上它然後卡在 `anon_pipe_read`,永遠等不到回應。

最難纏的是 **`line-bottles --stop` 救不了**:它呼叫的 `bottles-cli stop -b Line` 會卡在
同一個殭屍上,每試一次就多留一個卡住的清理行程(那天累積了六個,各吃 75 MB)。
**清理工具自己也壞掉了。**

`wineserver -k` 也沒用 —— 新開的 `flatpak run` 是另一個沙箱,`/tmp` 是獨立的 tmpfs,
找不到原本那組的 socket。

### 現在會自動處理

`preflight()` 的 `clear_stale()` 在每次啟動前會掃一次殘留:SIGTERM → 等 6 秒 → 還在就
SIGKILL。那個死鎖的 `wineserver` 實測對 SIGTERM 毫無反應,**必須 SIGKILL 才倒**。

包裝用的 `bwrap` / `bottles-cli` 不必另外處理:實測 `wineserver` 一死它們就自己收
(41 個行程在 SIGTERM 之後只剩 3 個)。

### 手動清(萬一自動清也失效)

```bash
# 看有哪些行程還掛在這個 prefix 上
python3 -c "
import sys; sys.path.insert(0, '$HOME/.local/lib/line-bottles')
import session
for p in session.bottle_pids(line_only=False):
    print(p, open(f'/proc/{p}/cmdline','rb').read().decode('utf-8','replace').replace(chr(0),' '))
"
```

**偵測一定要用 `WINEPREFIX` 環境變數比對(`bottle_pids` 就是這樣做的),不要比對指令列字串**
—— 後者會把「指令列剛好提到這個路徑」的無關行程一起抓進來,包括清理腳本自己。
這是實測踩到的:一行 `grep` 就把執行它的那個 shell 列進了待殺清單。

## 剪貼簿查不通時

先確認橋接活著、旗標立著:

```bash
systemctl --user status line-clipboard.service
xprop -display :99 -root _LINE_CLIPBOARD_READY      # 應為 CARDINAL = 1
tail ~/.local/state/line-bottles/clipboard.log      # 只有位元組數與格式,沒有內容
```

再看兩端各自實際提供什麼格式 —— **這是圖片貼不過去時唯一有用的一條線索**:

```bash
xclip -display :99 -selection clipboard -t TARGETS -o    # LINE 那端提供什麼
xclip -display :1  -selection clipboard -t TARGETS -o    # 主機那端提供什麼
```

橋接只會轉送 `lib/clipboard.py` 的 `PREFERRED` 清單裡有的格式,而且**原樣轉送不做轉換**。
若 LINE 給的是清單外的格式(例如只有 `CF_DIB` 而沒有任何 `image/*`),那就是沒東西可搬。
要支援就把那個格式加進 `PREFERRED`,而不是去加轉檔工具。

## 剪貼簿的固有限制

**關掉 LINE 之後,剛從 LINE 複製的內容會從主機剪貼簿消失。** X11 的選取區沒有「存放區」,
內容由擁有者**行程**持有;橋接一結束就沒有擁有者,剪貼簿就空了。

```bash
xprop -display :1 -root CLIPBOARD_MANAGER   # 沒有輸出 = 這台機器沒有剪貼簿管理員
```

裝了剪貼簿管理員(GNOME 擴充、`clipman`、CopyQ 等)的系統會由它接手,內容就留得住。
沒裝的話:**先貼出來再關 LINE**。這是 X11 的設計,不是橋接的缺陷。

**兩個方向的信任不對稱,代價要知道在哪。**

`:99` 本身沒有任何存取保護:`line-xephyr.service` 用 `-ac`(關閉存取控制),socat 又把它開在
`127.0.0.1:6099`。所以**推進去的主機剪貼簿,任何本機程序都讀得到**。這不是橋接造成的
(鍵盤輸入本來就在同一個 server 裡),但流進去的內容是新的 —— 焦點閘門正是擋這個用的:
只在你點進 LINE 的那一刻才推,在別處複製的密碼不會憑空跑進去。

反方向沒有閘門,而且**「複製」不一定是你按的**。Windows 程式隨時可以自己呼叫
`SetClipboardData`,所以 LINE 那端有辦法在你沒複製任何東西的時候,把內容放上你的主機剪貼簿。
橋接之前這只留在 `:99`,現在會出來。真正的風險是**之後貼進終端機** —— 貼上前先看一眼,
或先貼到文字編輯器。要整個關掉這個方向就停掉 `line-clipboard.service`,其餘功能不受影響。

**拖放仍然不行,而且和剪貼簿是兩回事。** 剪貼簿可以橋是因為內容能由第三者代持;
XDND 則是拖曳過程中在**同一個 X server 內**用 client message 即時協商,
跨 server 沒有對應的東西可以轉送。

## 蒐證方法

預設情況下 `WINEDEBUG=-all` 關掉所有 Wine 訊息,而 `session.py` 把子行程的輸出丟進 `DEVNULL` —— 兩者都要暫時改掉才看得到東西。

**開啟(暫時):**

1. `bottle.yml` 的 `Environment_Variables` 改成:
   ```yaml
   WINEDEBUG: err+all
   DXVK_LOG_LEVEL: info
   DXVK_LOG_PATH: /home/<你>/…/bottles/Line
   ```
2. `session.py` 裡啟動 LINE 的 `sp.Popen(...)`,把 `stdout=sp.DEVNULL, stderr=sp.DEVNULL` 改成寫入檔案
3. 重啟 LINE、重現問題

**要看什麼:**

```bash
# Wine 的錯誤(去重後通常只有幾種)
grep -oE 'err:[a-z0-9_]+:[A-Za-z_0-9]+ ?.*' wine-debug.log | sort | uniq -c | sort -rn

# DXVK 是否真的建立了 swapchain —— 這是「黑畫面」與「沒視窗」的分界
grep -c 'Presenter' *_d3d11.log

# 交叉比對:到底是崩潰還是正常關閉
journalctl --user -u line-xephyr -u line-session --since "10 min ago" -o short-precise
```

**查完務必改回** `WINEDEBUG: -all`、移除 DXVK 日誌設定、`session.py` 改回 `DEVNULL`。留著會明顯拖慢效能。

## 觀察執行中的狀態

```bash
line-bottles --status | python3 -m json.tool     # 階段、視窗、行程

# :99 上實際有哪些可見視窗(找出檢視器視窗、確認尺寸與狀態)
DISPLAY=:99 xwininfo -root -tree
DISPLAY=:99 xprop -id <視窗id> _NET_WM_STATE

# 確認兩個 fcitx5 實例各自的歸屬
for p in $(pgrep -x fcitx5); do
    echo "$p: $(grep -qs line-fcitx5 /proc/$p/cgroup && echo 'LINE(:99)' || echo '主實例')"
done

# 剪貼簿目前的擁有者(0x0 = 沒有人持有,剪貼簿是空的)
python3 - <<'EOF'
import ctypes as c
X = c.CDLL('libX11.so.6')
X.XOpenDisplay.argtypes = [c.c_char_p]; X.XOpenDisplay.restype = c.c_void_p
X.XInternAtom.argtypes = [c.c_void_p, c.c_char_p, c.c_int]; X.XInternAtom.restype = c.c_ulong
X.XGetSelectionOwner.argtypes = [c.c_void_p, c.c_ulong]; X.XGetSelectionOwner.restype = c.c_ulong
for d in (b':1', b':99'):
    dpy = X.XOpenDisplay(d)
    print(d.decode(), hex(X.XGetSelectionOwner(dpy, X.XInternAtom(dpy, b'CLIPBOARD', 0))))
EOF
```

注意 `xprop -id <w> _NET_WM_PID` 回報的是 **Wine 內部的 Windows PID**,不是 Linux PID,拿去 `ps` 查會得到完全無關的行程。

## 效能

畫面偏慢是 `LIBGL_ALWAYS_SOFTWARE=true` 的直接後果 —— 所有 GL 都走 llvmpipe(CPU 軟體渲染)。

這個設定是為了讓畫面能在 Xephyr 上正確顯示而存在的。要拿掉它得當成一次獨立實驗來做,並且準備好隨時改回:圖片顯示剛修好的時候不建議同時動它,否則出問題會分不清是誰造成的。

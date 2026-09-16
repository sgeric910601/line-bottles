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

**「`virtual_desktop` 設定有影響」** —— 沒有生效。`bottle.yml` 裡雖有 `virtual_desktop: true`、`virtual_desktop_res: 1920x1080`,但 Wine 登錄檔中沒有 `Explorer\Desktops` 項目,實際上沒有啟用。

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
```

注意 `xprop -id <w> _NET_WM_PID` 回報的是 **Wine 內部的 Windows PID**,不是 Linux PID,拿去 `ps` 查會得到完全無關的行程。

## 效能

畫面偏慢是 `LIBGL_ALWAYS_SOFTWARE=true` 的直接後果 —— 所有 GL 都走 llvmpipe(CPU 軟體渲染)。

這個設定是為了讓畫面能在 Xephyr 上正確顯示而存在的。要拿掉它得當成一次獨立實驗來做,並且準備好隨時改回:圖片顯示剛修好的時候不建議同時動它,否則出問題會分不清是誰造成的。

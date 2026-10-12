#!/usr/bin/python3
"""在主機顯示器與 LINE 專用的 Xephyr :99 之間橋接 CLIPBOARD。

X11 的選取區綁在單一 X server 上，兩個 server 之間本來就不相通。這支常駐
程式同時連著兩邊，用 XFixes 監聽所有權變動，再用 xclip 搬運內容。

隱私：日誌只記錄位元組數與 target 名稱，絕不記錄剪貼簿內容。
"""
import ctypes as c
import hashlib
import logging
import os
import select as poller
import shutil
import subprocess as sp
import sys

import x11
from x11 import D, I, U, X, atom, prop

LINE_DISPLAY = ':99'
READY_PROPERTY = '_LINE_CLIPBOARD_READY'
XA_CARDINAL = 6
LIMIT = 32 * 1024 * 1024
TIMEOUT = 5
XFIXES_OWNER_MASK = 1

# 依序挑第一個來源有提供的格式，原樣轉送不做轉換。
# Wine 的 winex11.drv 對圖片不一定給 image/png，image/bmp 也要收得下來。
PREFERRED = ('image/png', 'image/bmp', 'image/jpeg',
             'UTF8_STRING', 'text/plain;charset=utf-8', 'STRING')

XF = c.CDLL('libXfixes.so.3')
XF.XFixesQueryExtension.argtypes = [D, c.POINTER(I), c.POINTER(I)]
XF.XFixesQueryExtension.restype = I
XF.XFixesSelectSelectionInput.argtypes = [D, U, U, c.c_ulong]

x11.declare('XConnectionNumber', [D])
x11.declare('XChangeProperty', [D, U, U, U, I, I, c.c_void_p, I])
x11.declare('XDeleteProperty', [D, U, U])

STOP = False


# ── 純函式：可單獨測試，不碰 X ────────────────────────────────────────

def pick_target(available):
    """從來源提供的 TARGETS 裡挑最豐富、且兩邊都吃得下的格式。"""
    have = set(available)
    for target in PREFERRED:
        if target in have:
            return target
    return None


def accept(data, installed):
    """判斷讀到的內容要不要轉送，回傳 (雜湊或 None, 理由)。

    installed 是我們自己上次裝在「來源那一端」的內容雜湊。相同就代表這次的
    所有權變動是我們自己造成的，不能再彈回去，否則兩邊會無限互推。
    """
    if not data:
        return None, 'empty'
    if len(data) > LIMIT:
        return None, 'too-large'
    digest = hashlib.sha256(data).hexdigest()
    if digest == installed:
        return None, 'echo'
    return digest, 'ok'


# ── xclip 包裝：INCR 分段與 TARGETS 協商都交給它 ──────────────────────

def run(args, data=None, capture=True):
    # 寫入時一定要 DEVNULL：xclip -i 會 fork 出背景行程持有選取區，
    # 若 stdout 是管線，communicate() 會等不到 EOF 而卡到逾時。
    try:
        return sp.run(args, input=data, timeout=TIMEOUT, stderr=sp.DEVNULL,
                      stdout=sp.PIPE if capture else sp.DEVNULL)
    except (sp.TimeoutExpired, OSError):
        return sp.CompletedProcess(args, 124, b'')


def xclip(display, *args, data=None, capture=True):
    return run(['xclip', '-display', display, '-selection', 'clipboard', *args],
               data, capture)


def targets(display):
    result = xclip(display, '-t', 'TARGETS', '-o')
    if result.returncode or not result.stdout:
        return []
    text = result.stdout.decode('ascii', 'replace')
    return [line.strip() for line in text.splitlines() if line.strip()]


def read_selection(display, target):
    result = xclip(display, '-t', target, '-o')
    return b'' if result.returncode else result.stdout


def write_selection(display, target, data):
    return xclip(display, '-t', target, '-i', data=data, capture=False).returncode == 0


# ── 橋接本體 ──────────────────────────────────────────────────────────

class Bridge:
    def __init__(self, host, line=LINE_DISPLAY):
        self.host, self.line = host, line
        self.hd = self.open(host)
        self.ld = self.open(line)
        self.host_root = X.XDefaultRootWindow(self.hd)
        self.line_root = X.XDefaultRootWindow(self.ld)
        self.host_fixes = self.watch(self.hd, self.host_root)
        self.line_fixes = self.watch(self.ld, self.line_root)
        # 主機這端還要知道哪個視窗在前景，才判斷得出焦點是否在 LINE 上。
        X.XSelectInput(self.hd, self.host_root, 1 << 22)  # PropertyChangeMask
        X.XFlush(self.hd)
        self.xephyr = 0
        self.focused = False
        # 我們自己裝在各端的內容雜湊，用來擋掉自己造成的所有權變動。
        self.installed = {host: None, line: None}

    @staticmethod
    def open(display):
        d = X.XOpenDisplay(display.encode())
        if not d:
            raise SystemExit('無法連上顯示器 ' + display)
        return d

    @staticmethod
    def watch(d, root):
        base, error = I(), I()
        if not XF.XFixesQueryExtension(d, c.byref(base), c.byref(error)):
            raise SystemExit('這個 X server 沒有 XFIXES 擴充，無法監聽剪貼簿')
        XF.XFixesSelectSelectionInput(d, root, atom(d, 'CLIPBOARD'), XFIXES_OWNER_MASK)
        X.XFlush(d)
        return base.value  # XFixesSelectionNotify 就是 event base + 0

    def ready(self, value=True):
        """在 :99 的 root 上立一個旗標，給 session.py 當就緒訊號。"""
        name = atom(self.ld, READY_PROPERTY)
        if value:
            flag = U(1)
            X.XChangeProperty(self.ld, self.line_root, name, XA_CARDINAL, 32, 0,
                              c.byref(flag), 1)
        else:
            X.XDeleteProperty(self.ld, self.line_root, name)
        X.XFlush(self.ld)

    def find_xephyr(self):
        """在主機顯示器上找出 LINE 的那個 Xephyr 視窗。

        Xephyr 以 -title LINE 啟動，同時比對 WM_CLASS 與標題，
        避免誤判其他同時開著的 Xephyr。
        """
        for w in prop(self.hd, self.host_root, '_NET_CLIENT_LIST') or []:
            if b'Xephyr' not in (prop(self.hd, w, 'WM_CLASS') or b''):
                continue
            name = prop(self.hd, w, '_NET_WM_NAME') or prop(self.hd, w, 'WM_NAME') or b''
            if name.decode('utf-8', 'replace').strip('\0') == 'LINE':
                return w
        return 0

    def refresh_focus(self):
        # Xephyr 視窗可能比這支程式晚一點才映射上去，沒找到就下次再找。
        if not self.xephyr:
            self.xephyr = self.find_xephyr()
        active = (prop(self.hd, self.host_root, '_NET_ACTIVE_WINDOW') or [0])[0]
        focused = bool(self.xephyr) and active == self.xephyr
        gained, self.focused = focused and not self.focused, focused
        if gained:
            # 切進 LINE 的當下才把主機剪貼簿送進去：在別處複製的東西
            # （密碼、金鑰）不會憑空流進 LINE 讀得到的 X server。
            self.sync(self.host, self.line)

    def sync(self, source, destination):
        target = pick_target(targets(source))
        if not target:
            return
        data = read_selection(source, target)
        digest, reason = accept(data, self.installed[source])
        if not digest:
            if reason == 'too-large':
                logging.info('略過 %s → %s：%d bytes 超過上限', source, destination, len(data))
            return
        if write_selection(destination, target, data):
            self.installed[destination] = digest
            logging.info('%s → %s  %s  %d bytes', source, destination, target, len(data))
        else:
            logging.warning('寫入 %s 失敗（%s）', destination, target)

    def drain(self):
        event = x11.Event()
        for display, d, base in ((self.host, self.hd, self.host_fixes),
                                 (self.line, self.ld, self.line_fixes)):
            while X.XPending(d):
                X.XNextEvent(d, c.byref(event))
                if event.type != base:
                    continue  # 主機 root 的其他屬性變動，交給下面的 refresh_focus
                if display == self.line:
                    self.sync(self.line, self.host)   # LINE → 主機一律即時
                elif self.focused:
                    self.sync(self.host, self.line)

    def loop(self):
        fds = [X.XConnectionNumber(self.hd), X.XConnectionNumber(self.ld)]
        self.refresh_focus()
        self.ready()
        logging.info('剪貼簿橋接就緒：%s ↔ %s', self.host, self.line)
        while not STOP:
            self.drain()
            # 逾時一秒同時當保險：就算漏接 PropertyNotify，焦點狀態最多慢一秒。
            self.refresh_focus()
            # Xlib 配 select 的經典陷阱：refresh_focus 這類往返會把已到達的事件
            # 一併讀進 Xlib 的內部佇列，fd 上就沒東西了，select 會空等滿一秒。
            # 佇列裡還有貨就直接回去 drain，別讓剪貼簿慢一拍。
            if X.XPending(self.hd) or X.XPending(self.ld):
                continue
            try:
                poller.select(fds, [], [], 1)
            except OSError:
                break
        self.ready(False)
        logging.info('剪貼簿橋接結束')


def main(argv):
    global STOP
    import signal
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__('STOP', True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__('STOP', True))
    if not shutil.which('xclip'):
        raise SystemExit('缺少 xclip，剪貼簿橋接無法運作（sudo apt install xclip）')
    host = argv[0] if argv else os.environ.get('DISPLAY', '')
    if not host:
        raise SystemExit('找不到主機顯示器：DISPLAY 未設定')
    line = argv[1] if len(argv) > 1 else LINE_DISPLAY
    if host == line:
        raise SystemExit('主機顯示器與 LINE 顯示器相同，不需要橋接')
    Bridge(host, line).loop()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    main(sys.argv[1:])

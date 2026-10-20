#!/usr/bin/python3
"""在主機顯示器與 LINE 專用的 Xephyr :99 之間橋接 CLIPBOARD。

X11 的選取區綁在單一 X server 上，兩個 server 之間本來就不相通。這支常駐
程式同時連著兩邊，用 XFixes 監聽所有權變動，再用 xclip 搬運內容。

隱私：日誌只記錄位元組數與 target 名稱，絕不記錄剪貼簿內容。

另外每 0.5 秒戳一下 Wine 的剪貼簿視窗，讓 Wine 11.0 卡在送出緩衝區的
「接手剪貼簿」請求送出去，原因見 docs/architecture.md。
"""
import ctypes as c
import hashlib
import logging
import os
import select as poller
import shutil
import subprocess as sp
import sys
import time
from typing import NamedTuple

import x11
from x11 import D, I, U, X, atom, prop

LINE_DISPLAY = ':99'
READY_PROPERTY = '_LINE_CLIPBOARD_READY'
XA_CARDINAL = 6
LIMIT = 32 * 1024 * 1024
TIMEOUT = 5
XFIXES_OWNER_MASK = 1
PROPERTY_CHANGE_MASK = 1 << 22
IS_UNMAPPED = 0
INPUT_OUTPUT = 1

# Wine 剪貼簿喚醒的節奏。間隔就是「LINE 裡複製後，最慢多久才出現在主機」。
FLUSH_MESSAGE = '_LINE_CLIPBOARD_FLUSH'
WAKE_INTERVAL = 0.5
WAKE_CAP = 4           # 符合特徵的視窗正常只有一個，多於這個數一定是認錯了
RESCAN_INTERVAL = 5    # 找不到時重掃的間隔；橋接比 LINE 早啟動，一開始一定找不到
SELECT_CEILING = 1.0   # 同時也是焦點偵測的保險：漏接 PropertyNotify 最多慢這麼久

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
x11.declare('XQueryTree', [D, U, c.POINTER(U), c.POINTER(U), c.POINTER(D), c.POINTER(c.c_uint)])
x11.declare('XListProperties', [D, U, c.POINTER(I)], D)
x11.declare('XGetSelectionOwner', [D, U], U)


class WindowAttributes(c.Structure):
    """Xlib.h 的 XWindowAttributes。指標用 c_void_p、XID 用 c_ulong，對齊交給 ctypes。"""
    _fields_ = [('x', I), ('y', I), ('width', I), ('height', I), ('border_width', I),
                ('depth', I), ('visual', D), ('root', U), ('window_class', I),
                ('bit_gravity', I), ('win_gravity', I), ('backing_store', I),
                ('backing_planes', U), ('backing_pixel', U), ('save_under', I),
                ('colormap', U), ('map_installed', I), ('map_state', I),
                ('all_event_masks', c.c_long), ('your_event_mask', c.c_long),
                ('do_not_propagate_mask', c.c_long), ('override_redirect', I),
                ('screen', D)]


x11.declare('XGetWindowAttributes', [D, U, c.POINTER(WindowAttributes)])

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


class WindowInfo(NamedTuple):
    """判斷是不是 Wine 剪貼簿視窗所需的特徵。"""
    width: int
    height: int
    x: int
    y: int
    border: int
    input_output: bool
    mapped: bool
    override_redirect: bool
    event_mask: int
    property_count: int


def is_wine_clipboard_window(info):
    """Wine 在 clipboard_init 建立的 import_window：root 底下 1x1、未映射、沒有屬性，
    事件遮罩恰好只有 PropertyChangeMask（為了 INCR 分段傳輸）。

    每條 Wine 連線都有一個只選 StructureNotify 的 1x1 視窗，只有剪貼簿執行緒多這一個。
    xclip 持有選取區用的視窗也長這樣，那個交給 wake_targets 用「它是擁有者」排除。
    event_mask 是所有用戶端在這個視窗上選的事件聯集：若別的程式也對它選了事件，
    這裡會認不出來（日誌就不會出現「找到 Wine 剪貼簿視窗」）。
    """
    return (info.width == 1 and info.height == 1 and info.x == 0 and info.y == 0
            and info.border == 0 and info.input_output and not info.mapped
            and not info.override_redirect
            and info.event_mask == PROPERTY_CHANGE_MASK and info.property_count == 0)


def wake_targets(candidates, owner, cap=WAKE_CAP):
    """要戳的視窗：排除目前的 CLIPBOARD 擁有者，最多 cap 個。

    Wine 的 import_window 只負責讀，永遠不會是擁有者；是擁有者的必定是 xclip 那類。
    """
    return [w for w in candidates if w != owner][:cap]


def select_timeout(now, next_wake, ceiling=SELECT_CEILING):
    """select() 最多等到下一次喚醒。主機事件不斷時固定逾時會一直被重設而漂移。"""
    return max(0, min(ceiling, next_wake - now))


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


# ── Wine 剪貼簿視窗：讀 X 上的視窗特徵 ───────────────────────────────

def root_children(d, root):
    children, count = D(), c.c_uint()
    if not X.XQueryTree(d, root, c.byref(U()), c.byref(U()), c.byref(children),
                        c.byref(count)):
        return []
    try:
        return list(c.cast(children, c.POINTER(U))[:count.value])
    finally:
        if children:
            X.XFree(children)


def window_info(d, w):
    """讀出判斷所需的特徵；視窗已不存在時回傳 None。

    XGetWindowAttributes 是往返請求，視窗不存在時回傳 0（錯誤由 x11 的處理器吞掉），
    所以可以拿來可靠地驗證快取的 id。XListProperties 又是一次往返，
    不是 1x1 的視窗不可能是候選，就不查了，property_count 記 0。
    """
    attrs = WindowAttributes()
    if not X.XGetWindowAttributes(d, w, c.byref(attrs)):
        return None
    properties = 0
    if attrs.width == 1 and attrs.height == 1:
        count = I()
        atoms = X.XListProperties(d, w, c.byref(count))
        if atoms:
            X.XFree(atoms)
        properties = count.value
    return WindowInfo(width=attrs.width, height=attrs.height, x=attrs.x, y=attrs.y,
                      border=attrs.border_width,
                      input_output=attrs.window_class == INPUT_OUTPUT,
                      mapped=attrs.map_state != IS_UNMAPPED,
                      override_redirect=bool(attrs.override_redirect),
                      event_mask=attrs.all_event_masks, property_count=properties)


def is_candidate(d, w):
    info = window_info(d, w)
    return info is not None and is_wine_clipboard_window(info)


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
        X.XSelectInput(self.hd, self.host_root, PROPERTY_CHANGE_MASK)
        X.XFlush(self.hd)
        self.xephyr = 0
        self.focused = False
        # 我們自己裝在各端的內容雜湊，用來擋掉自己造成的所有權變動。
        self.installed = {host: None, line: None}
        self.line_clipboard = atom(self.ld, 'CLIPBOARD')
        self.wine = []           # 已找到的 Wine 剪貼簿視窗
        self.last_scan = None    # 上次掃描的時間（單調時鐘），None 表示要立刻掃
        self.next_wake = 0.0

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

    def refresh_wine(self, now):
        """確認手上的 Wine 剪貼簿視窗還有效；全部失效或還沒找到時，有節流地重掃。

        驗證用完整特徵而不只是「視窗還在」：LINE 重開後 Xephyr 會重用連線的 id，
        舊 id 可能已經變成別人的視窗。還有活著的就不重掃：剪貼簿管理員是每個
        Wine 桌面一個（在 explorer.exe 裡），LINE 開了通話、播放器等子程式也只有一個。
        """
        alive = [w for w in self.wine if is_candidate(self.ld, w)]
        if alive:
            self.wine = alive
            return
        if self.wine:
            logging.info('Wine 剪貼簿視窗已消失（LINE 可能重開了），重新尋找')
            self.wine, self.last_scan = [], None
        if self.last_scan is not None and now - self.last_scan < RESCAN_INTERVAL:
            return
        self.last_scan = now
        owner = X.XGetSelectionOwner(self.ld, self.line_clipboard)
        found = [w for w in root_children(self.ld, self.line_root)
                 if w != owner and is_candidate(self.ld, w)]
        if found:
            logging.info('找到 Wine 剪貼簿視窗：%s', ' '.join(hex(w) for w in found))
            if len(found) > WAKE_CAP:
                logging.warning('符合特徵的視窗多達 %d 個，只會戳前 %d 個；'
                                'Wine 版本可能變了，請重新確認特徵', len(found), WAKE_CAP)
            self.wine = found

    def wake_wine(self):
        """戳一下 Wine 的剪貼簿視窗，讓它卡在送出緩衝區的請求送出去。

        Wine 11.0 的 acquire_selection() 呼叫 XSetSelectionOwner 後沒有 XFlush，
        LINE 複製的內容要等那條連線收到任何 X 事件才會真正接手剪貼簿。
        這個 ClientMessage 的類型 Wine 不認得，會直接忽略，唯一的作用就是那次送出。
        """
        if not self.wine:
            return
        owner = X.XGetSelectionOwner(self.ld, self.line_clipboard)
        for w in wake_targets(self.wine, owner):
            x11.message(self.ld, w, w, FLUSH_MESSAGE, [], 0)

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
            # select 的逾時同時當保險：就算漏接 PropertyNotify，焦點狀態最多慢一秒。
            self.refresh_focus()
            now = time.monotonic()
            if now >= self.next_wake:
                self.refresh_wine(now)
                self.wake_wine()
                self.next_wake = now + WAKE_INTERVAL
            # Xlib 配 select 的經典陷阱：refresh_focus 這類往返會把已到達的事件
            # 一併讀進 Xlib 的內部佇列，fd 上就沒東西了，select 會空等到逾時。
            # 佇列裡還有貨就直接回去 drain，別讓剪貼簿慢一拍。
            if X.XPending(self.hd) or X.XPending(self.ld):
                continue
            try:
                poller.select(fds, [], [], select_timeout(time.monotonic(), self.next_wake))
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

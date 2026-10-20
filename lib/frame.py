#!/usr/bin/python3
"""LINE 的外框視窗：把 Xephyr 嵌進來，讓標題列的關閉變成最小化。

Xephyr 不處理 WM_DELETE_WINDOW，也不宣告支援它；按標題列的 X 時 mutter 只能切斷
它的連線，整組 LINE 服務跟著結束。這支程式在主機顯示器上建立頂層視窗，以
`Xephyr -parent` 把畫面嵌進來，自己接下關閉訊息改成最小化，並把鍵盤焦點交給
Xephyr 的子視窗（嵌入後它不會自己拿）。外框目前固定 1280x800；讓子視窗跟著外框
改變大小的機制保留待用，原因見下方 FIXED_SIZE。
細節見 docs/architecture.md「外框視窗」。
"""
import ctypes as c
import functools
import logging
import os
import select as poller
import signal
import socket
import subprocess as sp
import sys
import time

import x11
from x11 import I, U, X, atom

XEPHYR = '/usr/bin/Xephyr'
NAME = 'LINE'
# 外框固定大小：Wine 11.0 在 :99 改過大小之後，對「跟螢幕一樣大」的 LINE 視窗
# 會用舊解析度渲染再拉伸（變糊、上下黑條），或卡住高度。研究出解法之前先不給拖，
# 見 docs/architecture.md「外框視窗」。下面跟著大小同步的機制仍保留：WM 若還是
# 改了外框大小（例如螢幕太小放不下），畫面至少不會被切掉。
FIXED_SIZE = (1280, 800)
INITIAL_SIZE = FIXED_SIZE
RESIZE_SETTLE = 0.2   # 拖曳改變大小時，停下這麼久才調整，免得 LINE 一路重新排版
STOP_GRACE = 3        # 結束時等 Xephyr 自己收掉的秒數，逾時就強制結束
MAP_TIMEOUT = 3
SELECT_CEILING = 1.0
ANNOUNCE_DELAY = 0.3  # 調整 Xephyr 大小後，隔這麼久再去確認它調好了沒、通知 Wine
ANNOUNCE_TRIES = 10
STARTUP_GRACE = 2     # Xephyr 在這之內就以非零碼結束，算啟動失敗而不是登出
STARTUP_FAILED = 3    # 不能用 1：服務設定把 1 當成登出那種正常收場
FRAME_FAILED = 2      # 外框自己出錯

# Xlib.h / X.h 的常數
FOCUS_IN, CREATE_NOTIFY, DESTROY_NOTIFY = 9, 16, 17
UNMAP_NOTIFY, MAP_NOTIFY, CONFIGURE_NOTIFY, CLIENT_MESSAGE = 18, 19, 22, 33
STRUCTURE_NOTIFY_MASK = 1 << 17
SUBSTRUCTURE_NOTIFY_MASK = 1 << 19
FOCUS_CHANGE_MASK = 1 << 21
NOTIFY_NORMAL = 0
REVERT_TO_PARENT = 2
CURRENT_TIME = 0
XA_ATOM, XA_CARDINAL, XA_STRING, XA_WM_HINTS, XA_WM_SIZE_HINTS = 4, 6, 31, 35, 41  # Xatom.h
INPUT_HINT, STATE_HINT, NORMAL_STATE = 1, 2, 1
P_MIN_SIZE, P_MAX_SIZE = 1 << 4, 1 << 5

LIBC = c.CDLL('libc.so.6', use_errno=True)
LIBC.prctl.argtypes = [c.c_int, c.c_ulong]
PR_SET_PDEATHSIG = 1


# ── 純函式：可單獨測試，不碰 X ────────────────────────────────────────

def xephyr_argv(nested, frame_id):
    """大小取自外框，所以不帶 -screen／-resizeable；不帶 -title，Ctrl+Shift 抓取時
    它會把字寫到子視窗上。-no-host-grab：嵌入後 Ctrl+Shift 鎖住鍵盤滑鼠時連提示都看不到。
    """
    return [XEPHYR, nested, '-parent', str(frame_id), '-ac', '-br', '-noreset',
            '-no-host-grab']


def client_message_action(message_type, data0, atoms):
    if message_type != atoms['WM_PROTOCOLS']:
        return None
    if data0 == atoms['WM_DELETE_WINDOW']:
        return 'minimize'
    if data0 == atoms['WM_TAKE_FOCUS']:
        return 'take-focus'
    return None


def should_redirect_focus(frame, child, focused, mode):
    """焦點停在外框本身就要轉給 Xephyr 的子視窗，否則指標不在畫面上時打的字會不見。
    鍵盤被抓取中（選單、拖曳）的焦點變化不動。"""
    return bool(child) and focused == frame and mode == NOTIFY_NORMAL


def next_resize(pending, applied, changed_at, now, settle=RESIZE_SETTLE):
    """回傳 (現在該套用的大小, 還要等幾秒)；兩者都是 None 表示沒事要做。"""
    if pending is None or pending == applied:
        return None, None
    waited = now - changed_at
    if waited + 1e-6 >= settle:   # 浮點誤差（10.2 - 10.0 < 0.2）不該再多等一輪
        return pending, None
    return None, settle - waited


@functools.lru_cache(maxsize=None)
def xrandr():
    """libXrandr 只有通知 Wine 時才用到；在這裡才載入，缺了不會讓外框起不來。"""
    lib = c.CDLL('libXrandr.so.2')
    lib.XRRGetScreenResources.argtypes = [c.c_void_p, U]
    lib.XRRGetScreenResources.restype = c.c_void_p
    lib.XRRFreeScreenResources.argtypes = [c.c_void_p]
    return lib


def size_hints(size):
    """WM_NORMAL_HINTS（XSizeHints 的 18 個 long）：最小＝最大＝size。"""
    hints = [0] * 18
    hints[0] = P_MIN_SIZE | P_MAX_SIZE
    hints[5:9] = [*size, *size]
    return hints


def announce_action(current, wanted, tries, max_tries=ANNOUNCE_TRIES):
    """調整大小後要不要通知 Wine：'announce'（:99 已是新大小）、'wait'（Xephyr 還在調）
    或 'give-up'。太早查詢時 X server 沒有變化可報，等於白做。"""
    if current == wanted:
        return 'announce'
    if tries + 1 >= max_tries:
        return 'give-up'
    return 'wait'


def next_timeout(now, deadlines, ceiling=SELECT_CEILING):
    """select 最多等到最近的一個期限；沒有期限就等 ceiling。"""
    pending = [d for d in deadlines if d is not None]
    if not pending:
        return ceiling
    return max(0, min(ceiling, min(pending) - now))


def exit_result(returncode, ran_for, startup=STARTUP_GRACE):
    """Xephyr 的 returncode 換成外框自己的 (結束碼, 訊號)；負數代表被訊號結束。

    剛啟動就以非零碼結束（:99 被佔用、查不到外框）是故障，回報 STARTUP_FAILED；
    否則照原樣，登出時的 1 才會和以前一樣被當成正常收場。
    """
    if returncode < 0:
        return None, -returncode
    if returncode and ran_for < startup:
        return STARTUP_FAILED, None
    return returncode, None


# ── 外框 ──────────────────────────────────────────────────────────────

def die_with_parent(parent):
    """在 Xephyr 子行程裡執行：外框意外消失時由核心送 SIGTERM（systemd 之外執行時的保險）。
    外框若在 fork 與 prctl 之間就死了，這個設定已經來不及，直接結束。"""
    LIBC.prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM))
    if os.getppid() != parent:
        os._exit(1)


def set_property(d, w, name, kind, values):
    """bytes 以 format 8 寫入，整數清單以 format 32（Xlib 用 long）寫入。"""
    if isinstance(values, bytes):
        data, fmt, count = c.create_string_buffer(values, len(values)), 8, len(values)
    else:
        data, fmt, count = (c.c_long * len(values))(*values), 32, len(values)
    X.XChangeProperty(d, w, atom(d, name), kind, fmt, 0, data, count)


class Frame:
    def __init__(self, host, nested, instance, title=NAME):
        self.d = X.XOpenDisplay(host.encode())
        if not self.d:
            raise RuntimeError('無法連上顯示器 ' + host)
        self.nested = nested
        self.root = X.XDefaultRootWindow(self.d)
        self.screen = X.XDefaultScreen(self.d)
        self.atoms = {n: atom(self.d, n)
                      for n in ('WM_PROTOCOLS', 'WM_DELETE_WINDOW', 'WM_TAKE_FOCUS')}
        self.window = self.create(instance, title)
        self.child, self.child_mapped = 0, False
        self.applied, self.pending, self.changed_at = INITIAL_SIZE, None, 0.0
        self.announce_at, self.announce_size, self.announce_tries = None, None, 0
        self.xephyr, self.started = None, 0.0
        self.stopping = False

    def create(self, instance, title):
        # 預設 visual 與深度：Xephyr 的子視窗用 CopyFromParent，ARGB 外框會讓它 BadMatch。
        black = X.XBlackPixel(self.d, self.screen)
        w = X.XCreateSimpleWindow(self.d, self.root, 0, 0, *INITIAL_SIZE, 0, black, black)
        d = self.d
        # 映射之前就設好：GNOME 一看到視窗就用 WM_CLASS 對 .desktop 的圖示。
        set_property(d, w, 'WM_CLASS', XA_STRING, f'{instance}\0{NAME}\0'.encode())
        set_property(d, w, 'WM_NAME', XA_STRING, title.encode())
        set_property(d, w, '_NET_WM_NAME', atom(d, 'UTF8_STRING'), title.encode())
        set_property(d, w, 'WM_CLIENT_MACHINE', XA_STRING, socket.gethostname().encode())
        set_property(d, w, '_NET_WM_PID', XA_CARDINAL, [os.getpid()])
        # 刻意不宣告 _NET_WM_PING：關閉後 mutter 會來確認程式是否還活著。
        set_property(d, w, 'WM_PROTOCOLS', XA_ATOM,
                     [self.atoms['WM_DELETE_WINDOW'], self.atoms['WM_TAKE_FOCUS']])
        set_property(d, w, 'WM_HINTS', XA_WM_HINTS,
                     [INPUT_HINT | STATE_HINT, 1, NORMAL_STATE, 0, 0, 0, 0, 0, 0])
        set_property(d, w, 'WM_NORMAL_HINTS', XA_WM_SIZE_HINTS, size_hints(FIXED_SIZE))
        X.XSelectInput(d, w, STRUCTURE_NOTIFY_MASK | SUBSTRUCTURE_NOTIFY_MASK
                       | FOCUS_CHANGE_MASK)
        return w

    def map(self):
        """映射後等它真的出現，再同步一次才能啟動 Xephyr：不然 Xephyr 有時查不到外框，
        印出「-parent window does not exist」以 1 結束，而服務設定把 1 當成正常。"""
        X.XMapWindow(self.d, self.window)
        X.XFlush(self.d)
        event, deadline = x11.Event(), time.monotonic() + MAP_TIMEOUT
        while time.monotonic() < deadline:
            while X.XPending(self.d):
                X.XNextEvent(self.d, c.byref(event))
                self.handle(event)
                if event.type == MAP_NOTIFY and event.map.window == self.window:
                    X.XSync(self.d, 0)
                    return
            time.sleep(.02)
        logging.warning('外框 %d 秒內沒有出現，照樣啟動 Xephyr', MAP_TIMEOUT)
        X.XSync(self.d, 0)

    def handle(self, e):
        if e.type == CLIENT_MESSAGE and e.client.window == self.window:
            action = client_message_action(e.client.message_type, e.client.data.l[0],
                                           self.atoms)
            if action == 'minimize':
                logging.info('收到關閉要求，改成最小化')
                X.XIconifyWindow(self.d, self.window, self.screen)
                X.XFlush(self.d)
            elif action == 'take-focus':
                self.give_focus(e.client.data.l[1])
        elif e.type == CONFIGURE_NOTIFY and e.configure.window == self.window:
            size = (e.configure.width, e.configure.height)
            if size != self.pending:
                self.pending, self.changed_at = size, time.monotonic()
        elif e.type == CREATE_NOTIFY and e.create.parent == self.window and not self.child:
            # Xephyr 第一個建立的就是它的畫面。記下它實際的大小：外框若在 Xephyr 讀完
            # 大小之後才被調整，next_resize 會看出差異補調。
            self.child = e.create.window
            self.applied = (e.create.width, e.create.height)
        elif e.type == MAP_NOTIFY and e.map.window == self.child:
            self.child_mapped = True
            self.redirect_if_focused(NOTIFY_NORMAL)
        elif e.type == UNMAP_NOTIFY and e.map.window == self.child:
            self.child_mapped = False
        elif e.type == DESTROY_NOTIFY and e.destroy.window == self.child:
            self.child, self.child_mapped = 0, False
        elif e.type == FOCUS_IN and e.focus.window == self.window:
            self.redirect_if_focused(e.focus.mode)

    def give_focus(self, timestamp):
        """WM_TAKE_FOCUS：用 WM 給的時間戳，之後 Alt+Tab 走了也不會被搶回來。"""
        target = self.child if self.child_mapped else self.window
        X.XSetInputFocus(self.d, target, REVERT_TO_PARENT, timestamp)
        X.XFlush(self.d)

    def redirect_if_focused(self, mode):
        """x11.py 的 focus 直接對外框 XSetInputFocus，不經 WM_TAKE_FOCUS，所以這裡也要接。"""
        if not self.child_mapped:
            return
        focused, revert = U(), I()
        X.XGetInputFocus(self.d, c.byref(focused), c.byref(revert))
        if should_redirect_focus(self.window, self.child, focused.value, mode):
            X.XSetInputFocus(self.d, self.child, REVERT_TO_PARENT, CURRENT_TIME)
            X.XFlush(self.d)

    def resize(self, size):
        """還沒有子視窗時什麼都不記：子視窗建立時才知道它實際用了多大。"""
        if not self.child:
            return
        X.XResizeWindow(self.d, self.child, *size)
        X.XFlush(self.d)
        logging.info('Xephyr 畫面調整為 %dx%d', *size)
        self.applied = size
        self.announce_at = time.monotonic() + ANNOUNCE_DELAY
        self.announce_size, self.announce_tries = size, 0

    def announce(self, now):
        """讓 Wine 知道 :99 的大小變了，LINE 才會跟著放大。

        Xephyr 改大小只發 RRScreenChangeNotify，Wine 11.0 卻只聽 CRTC／輸出變更
        （xrandr14_device_change_handler），所以一直以為螢幕還是舊大小。
        XRRGetScreenResources 會讓 X server 重新探測輸出，補發這兩種通知；
        但要等 Xephyr 真的調完才有變化可報，所以先確認 :99 已經是新大小。
        """
        d = X.XOpenDisplay(self.nested.encode())
        if not d:
            self.announce_at = None
            return
        try:
            screen = X.XDefaultScreen(d)
            current = (X.XDisplayWidth(d, screen), X.XDisplayHeight(d, screen))
            action = announce_action(current, self.announce_size, self.announce_tries)
            if action == 'announce':
                self.announce_at = None
                try:
                    lib = xrandr()
                except OSError:
                    logging.warning('找不到 libXrandr，無法通知 Wine 螢幕大小變了')
                    return
                resources = lib.XRRGetScreenResources(d, X.XDefaultRootWindow(d))
                if resources:
                    lib.XRRFreeScreenResources(resources)
            elif action == 'wait':
                self.announce_tries += 1
                self.announce_at = now + ANNOUNCE_DELAY
            else:
                logging.warning('Xephyr 一直沒調成 %dx%d，放棄通知 Wine', *self.announce_size)
                self.announce_at = None
        finally:
            X.XCloseDisplay(d)

    def spawn(self):
        argv = xephyr_argv(self.nested, self.window)
        logging.info('啟動 %s', ' '.join(argv))
        # 單執行緒，preexec_fn 是安全的。
        self.xephyr = sp.Popen(argv, preexec_fn=functools.partial(die_with_parent, os.getpid()))
        self.started = time.monotonic()

    def request_stop(self, *_):
        self.stopping = True

    def run(self):
        """回傳 (結束碼, 訊號)，交給 main 決定怎麼結束自己。"""
        wake_r, wake_w = os.pipe()
        for fd in (wake_r, wake_w):
            os.set_blocking(fd, False)
        # 訊號處理器只立旗標；wakeup fd 讓等待中的 select 立刻醒來處理。
        signal.set_wakeup_fd(wake_w, warn_on_full_buffer=False)
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGCHLD, lambda *_: None)
        # 不論哪一步出錯，外框視窗與 Xephyr 都要收乾淨，不能只靠 PDEATHSIG。
        try:
            self.map()
            self.applied = x11.geometry(self.d, self.window)
            self.spawn()
            self.loop([X.XConnectionNumber(self.d), wake_r], wake_r)
            return self.result()
        finally:
            self.cleanup()

    def loop(self, fds, wake_r):
        event = x11.Event()
        while not self.stopping and self.xephyr.poll() is None:
            while X.XPending(self.d):
                X.XNextEvent(self.d, c.byref(event))
                self.handle(event)
            now = time.monotonic()
            if self.announce_at is not None and now >= self.announce_at:
                self.announce(now)
            # 子視窗出現之前沒東西可調；不先擋掉的話 resize 什麼都不做，迴圈會空轉。
            size, wait = (next_resize(self.pending, self.applied, self.changed_at, now)
                          if self.child else (None, None))
            if size:
                self.resize(size)
                continue
            # 與剪貼簿橋接同一個陷阱：往返呼叫會把事件先讀進 Xlib 佇列，select 看不到。
            if X.XPending(self.d):
                continue
            resize_at = None if wait is None else now + wait
            timeout = next_timeout(time.monotonic(), [resize_at, self.announce_at])
            ready, _, _ = poller.select(fds, [], [], timeout)
            if wake_r in ready:
                try:
                    while os.read(wake_r, 64):
                        pass
                except BlockingIOError:
                    pass

    def result(self):
        if self.stopping:
            return 0, None
        code, ran_for = self.xephyr.returncode, time.monotonic() - self.started
        result = exit_result(code, ran_for)
        if result[0] == STARTUP_FAILED:
            logging.error('Xephyr 啟動 %.1f 秒就結束（%s），視為啟動失敗', ran_for, code)
        else:
            logging.info('Xephyr 結束（%s），外框跟著收掉', code)
        return result

    def cleanup(self):
        try:
            if self.xephyr and self.xephyr.poll() is None:
                self.xephyr.terminate()
                try:
                    self.xephyr.wait(STOP_GRACE)
                except sp.TimeoutExpired:
                    self.xephyr.kill()
                    self.xephyr.wait()
        finally:
            X.XDestroyWindow(self.d, self.window)
            X.XCloseDisplay(self.d)


def main(argv):
    """line-bottles --frame [巢狀 display] [WM_CLASS instance]；主機顯示器取自 DISPLAY。

    後兩個參數只給測試用：換一個 display 與 instance 就能和正式的 LINE 並存。
    """
    host = os.environ.get('DISPLAY', '')
    if not host:
        # 不用 SystemExit('…')：它以 1 結束，而 1 被服務設定當成正常。
        logging.error('找不到主機顯示器：DISPLAY 未設定')
        return sys.exit(FRAME_FAILED)
    nested = argv[0] if argv else ':99'
    instance = argv[1] if len(argv) > 1 else x11.FRAME_CLASS
    # 測試用的外框在標題標出 instance，免得和正式的 LINE 視窗搞混。
    title = NAME if instance == x11.FRAME_CLASS else f'{NAME} ({instance})'
    try:
        code, sig = Frame(host, nested, instance, title).run()
    except Exception:
        # 未捕捉的例外會以 1 結束，而 1 被服務設定當成正常；改用別的碼才看得出是故障。
        logging.exception('外框出錯')
        return sys.exit(FRAME_FAILED)
    if sig:
        # Xephyr 被訊號結束：外框用同一個訊號結束，systemd 看到的就和以前一樣。
        # SIGKILL／SIGSTOP 不能設處理器（會丟 OSError），本來就是預設行為，照送即可。
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (OSError, ValueError):
            pass
        os.kill(os.getpid(), sig)
    sys.exit(code)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    main(sys.argv[1:])

#!/usr/bin/python3
"""Short-lived X11 probes: never collect text typed into LINE."""
import ctypes as c
import json
import sys
import time

X = c.CDLL('libX11.so.6')
D = c.c_void_p
U = c.c_ulong
I = c.c_int

def declare(name, args, result=I):
    f = getattr(X, name)
    f.argtypes, f.restype = args, result
    return f

declare('XOpenDisplay', [c.c_char_p], D)
declare('XDefaultRootWindow', [D], U)
declare('XInternAtom', [D, c.c_char_p, I], U)
declare('XGetWindowProperty', [D, U, U, c.c_long, c.c_long, I, U,
    c.POINTER(U), c.POINTER(I), c.POINTER(U), c.POINTER(U), c.POINTER(D)])
declare('XFree', [D])
declare('XGetGeometry', [D, U, c.POINTER(U), c.POINTER(I), c.POINTER(I),
    c.POINTER(c.c_uint), c.POINTER(c.c_uint), c.POINTER(c.c_uint), c.POINTER(c.c_uint)])
declare('XGetInputFocus', [D, c.POINTER(U), c.POINTER(I)])
declare('XSetInputFocus', [D, U, I, U])
declare('XMapRaised', [D, U])
declare('XFlush', [D])
declare('XSync', [D, I])
declare('XSelectInput', [D, U, c.c_long])
declare('XPending', [D])
declare('XCloseDisplay', [D])
declare('XConnectionNumber', [D])
declare('XChangeProperty', [D, U, U, U, I, I, c.c_void_p, I])
declare('XQueryTree', [D, U, c.POINTER(U), c.POINTER(U), c.POINTER(D), c.POINTER(c.c_uint)])
declare('XDefaultScreen', [D])
declare('XDisplayWidth', [D, I])
declare('XDisplayHeight', [D, I])
declare('XBlackPixel', [D, I], U)
declare('XCreateSimpleWindow', [D, U, I, I, c.c_uint, c.c_uint, c.c_uint, U, U], U)
declare('XMapWindow', [D, U])
declare('XIconifyWindow', [D, U, I])
declare('XResizeWindow', [D, U, c.c_uint, c.c_uint])
declare('XDestroyWindow', [D, U])
ERROR = c.CFUNCTYPE(I, D, D)
error_handler = ERROR(lambda display, event: 0)
X.XSetErrorHandler.argtypes = [ERROR]
X.XSetErrorHandler(error_handler)

class Data(c.Union):
    _fields_ = [('b', c.c_char * 20), ('s', c.c_short * 10), ('l', c.c_long * 5)]

class Client(c.Structure):
    _fields_ = [('type', I), ('serial', U), ('send_event', I), ('display', D),
        ('window', U), ('message_type', U), ('format', I), ('data', Data)]

class Configure(c.Structure):
    _fields_ = [('type', I), ('serial', U), ('send_event', I), ('display', D),
        ('event', U), ('window', U), ('x', I), ('y', I), ('width', I), ('height', I),
        ('border_width', I), ('above', U), ('override_redirect', I)]

class FocusChange(c.Structure):
    _fields_ = [('type', I), ('serial', U), ('send_event', I), ('display', D),
        ('window', U), ('mode', I), ('detail', I)]

class CreateWindow(c.Structure):
    _fields_ = [('type', I), ('serial', U), ('send_event', I), ('display', D),
        ('parent', U), ('window', U), ('x', I), ('y', I), ('width', I), ('height', I),
        ('border_width', I), ('override_redirect', I)]

class Map(c.Structure):
    _fields_ = [('type', I), ('serial', U), ('send_event', I), ('display', D),
        ('event', U), ('window', U), ('override_redirect', I)]

class DestroyWindow(c.Structure):
    _fields_ = [('type', I), ('serial', U), ('send_event', I), ('display', D),
        ('event', U), ('window', U)]

class Event(c.Union):
    _fields_ = [('type', I), ('client', Client), ('configure', Configure),
        ('focus', FocusChange), ('create', CreateWindow), ('map', Map),
        ('destroy', DestroyWindow), ('pad', c.c_long * 24)]

declare('XSendEvent', [D, U, I, c.c_long, c.POINTER(Event)])
declare('XNextEvent', [D, c.POINTER(Event)])

def atom(d, name):
    return X.XInternAtom(d, name.encode(), 0)

def prop(d, w, name):
    kind, fmt, count, remaining, ptr = U(), I(), U(), U(), D()
    rc = X.XGetWindowProperty(d, w, atom(d, name), 0, 4096, 0, 0,
        c.byref(kind), c.byref(fmt), c.byref(count), c.byref(remaining), c.byref(ptr))
    if rc or not ptr:
        return None
    try:
        if fmt.value == 32:
            return list(c.cast(ptr, c.POINTER(U))[:count.value])
        if fmt.value == 8:
            return c.string_at(ptr, count.value)
    finally:
        X.XFree(ptr)

def geometry(d, w):
    root, x, y = U(), I(), I()
    width, height, border, depth = [c.c_uint() for _ in range(4)]
    if not X.XGetGeometry(d, w, c.byref(root), c.byref(x), c.byref(y),
        c.byref(width), c.byref(height), c.byref(border), c.byref(depth)):
        return 0, 0
    return width.value, height.value

def message(d, dest, window, name, values, mask):
    event = Event()
    event.client.type = 33
    event.client.display = d
    event.client.window = window
    event.client.message_type = atom(d, name)
    event.client.format = 32
    for index, value in enumerate(values):
        event.client.data.l[index] = value
    X.XSendEvent(d, dest, 0, mask, c.byref(event))
    X.XFlush(d)

# frame.py 建立的外框視窗用這個 WM_CLASS instance；Dock 也靠它對上 .desktop 的圖示。
FRAME_CLASS = 'line-bottles'

def is_frame_class(wm_class, instance=FRAME_CLASS):
    return bool(wm_class) and wm_class.split(b'\0')[0] == instance.encode()

def find_frame(d, root, instance=FRAME_CLASS):
    """主機顯示器上 LINE 的外框視窗；找不到回傳 0。"""
    for w in prop(d, root, '_NET_CLIENT_LIST') or []:
        if is_frame_class(prop(d, w, 'WM_CLASS'), instance):
            return w
    return 0

def activate(d, root, w):
    """拉到最前面並給焦點；最小化的視窗也會被還原（_NET_ACTIVE_WINDOW 來源 2 = pager）。"""
    X.XMapRaised(d, w)
    message(d, root, w, '_NET_ACTIVE_WINDOW', [2, 0, 0], (1 << 20) | (1 << 19))
    X.XSetInputFocus(d, w, 2, 0)
    X.XSync(d, 0)

def main():
    action = sys.argv[1]
    d = X.XOpenDisplay(sys.argv[2].encode())
    if not d:
        raise SystemExit(2)
    root = X.XDefaultRootWindow(d)
    if action == 'inspect':
        windows = []
        for w in prop(d, root, '_NET_CLIENT_LIST') or []:
            # Restrict to LINE's own main/login/loading windows, not message text.
            name = prop(d, w, '_NET_WM_NAME') or prop(d, w, 'WM_NAME') or b''
            if name.decode('utf-8', 'replace').strip('\0') != 'LINE':
                continue
            width, height = geometry(d, w)
            state = prop(d, w, 'WM_STATE') or [0]
            if state[0] not in (1, 3):
                continue
            windows.append(dict(id=w, width=width, height=height,
                iconic=state[0] == 3, main=width >= 250 and height >= 200,
                ping=atom(d, '_NET_WM_PING') in (prop(d, w, 'WM_PROTOCOLS') or [])))
        focus, revert = U(), I()
        X.XGetInputFocus(d, c.byref(focus), c.byref(revert))
        screen = X.XDefaultScreen(d)
        print(json.dumps(dict(wm=bool(prop(d, root, '_NET_SUPPORTING_WM_CHECK')),
            windows=windows, focus=focus.value,
            screen=[X.XDisplayWidth(d, screen), X.XDisplayHeight(d, screen)])))
    elif action == 'focus':
        activate(d, root, int(sys.argv[3]))
    elif action == 'focus-frame':
        # 外框拿到焦點後會自己再轉給 Xephyr 的子視窗。第三個參數只給測試用的外框。
        w = find_frame(d, root, *sys.argv[3:4])
        if w:
            activate(d, root, w)
    elif action == 'maximize':
        w = int(sys.argv[3])
        # EWMH _NET_WM_STATE：data[0] 1 = add，兩個 MAXIMIZED 狀態一起送即滿版，
        # data[3] 2 = 來源為 pager。openbox 的 _NET_SUPPORTED 有列這兩個原子。
        message(d, root, w, '_NET_WM_STATE',
                [1, atom(d, '_NET_WM_STATE_MAXIMIZED_VERT'),
                 atom(d, '_NET_WM_STATE_MAXIMIZED_HORZ'), 2, 0],
                (1 << 20) | (1 << 19))
        X.XSync(d, 0)
    elif action == 'ping':
        w = int(sys.argv[3])
        ping = atom(d, '_NET_WM_PING')
        if ping not in (prop(d, w, 'WM_PROTOCOLS') or []):
            print('unsupported')
        else:
            X.XSelectInput(d, root, (1 << 19))
            stamp = int(time.monotonic() * 1000) & 0xffffffff
            message(d, w, w, 'WM_PROTOCOLS', [ping, stamp, w], 0)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                while X.XPending(d):
                    e = Event()
                    X.XNextEvent(d, c.byref(e))
                    if (e.type == 33 and e.client.data.l[0] == ping
                            and e.client.data.l[1] == stamp and e.client.data.l[2] == w):
                        print('ok')
                        X.XCloseDisplay(d)
                        return
                time.sleep(.05)
            print('timeout')
    elif action == 'close':
        w = int(sys.argv[3])
        message(d, w, w, 'WM_PROTOCOLS', [atom(d, 'WM_DELETE_WINDOW'), 0], 0)
    elif action == 'minimize':
        w = int(sys.argv[3])
        message(d, root, w, 'WM_CHANGE_STATE', [3], (1 << 20) | (1 << 19))
    elif action == 'close-wm':
        w = int(sys.argv[3])
        message(d, root, w, '_NET_CLOSE_WINDOW', [0, 2], (1 << 20) | (1 << 19))
    X.XCloseDisplay(d)

if __name__ == '__main__':
    main()

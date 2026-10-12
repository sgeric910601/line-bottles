#!/usr/bin/python3
"""Manage exactly the Line bottle and its dedicated desktop session."""
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess as sp
import sys
import tempfile
import time

BASE = Path.home()
STATE = BASE / '.local/state/line-bottles'
PREFIX = BASE / '.var/app/com.usebottles.bottles/data/bottles/bottles/Line'
# Bottles 會建立 users/steamuser 指向以主機使用者命名的目錄，用它可避免寫死使用者名稱。
LINE = PREFIX / 'drive_c/users/steamuser/AppData/Local/LINE'
HERE = Path(__file__).parent
UNIT = 'line-session.service'

# 啟動與關閉順序不是彼此的反序，兩者都有各自的理由，集中定義在這裡：
#  - fcitx5 要在 openbox 之前啟動，openbox 開場才連得上輸入法。
#  - 關閉時 openbox 要在 fcitx5 之前結束，否則它會卡在 XIM 拆連線的同步等待上
#    （見 line-fcitx5.service 的 Before=line-window-manager.service）。
#  - Xephyr 一律最後才停，其餘服務都 BindsTo 它。
#  - 剪貼簿橋接排在最後啟動：它只依賴 Xephyr，而且是選用的，
#    不該讓它的就緒等待擋住前面那些真正必要的服務。
START_ORDER = ('line-xephyr.service', 'line-fcitx5.service',
               'line-window-manager.service', 'line-x11-bridge.service',
               'line-clipboard.service')
STOP_ORDER = ('line-window-manager.service', 'line-x11-bridge.service',
              'line-fcitx5.service', 'line-clipboard.service',
              'line-xephyr.service')
SERVICES = list(STOP_ORDER)
# 這些服務起不來只記警告：LINE 少了它們照樣能用，不值得讓整個工作階段失敗。
OPTIONAL = frozenset({'line-clipboard.service'})
CLI = ['flatpak', 'run', '--command=bottles-cli', 'com.usebottles.bottles']
WIN_EXE = r'C:\users\steamuser\AppData\Local\LINE\bin\current\LINE.exe'
STOP = False

def command(args, seconds=5, env=None):
    try:
        return sp.run(args, text=True, stdout=sp.PIPE, stderr=sp.DEVNULL,
                      timeout=seconds, env=env)
    except (sp.TimeoutExpired, OSError):
        return sp.CompletedProcess(args, 124, '')

def system(*args, seconds=15):
    return command(['systemctl', '--user', *args], seconds)

def active(name):
    return system('is-active', '--quiet', name, seconds=3).returncode == 0

def notify(text, error=False):
    command(['notify-send', '--app-name=LINE', 'LINE', text], 3)
    if error:
        sp.Popen(['zenity', '--error', '--title=LINE 啟動失敗', '--text=' + text],
                 stdout=sp.DEVNULL, stderr=sp.DEVNULL, start_new_session=True)

def xprobe(action='inspect', *args, display=':99'):
    return command(['/usr/bin/python3', str(HERE / 'x11.py'), action, display,
                    *map(str, args)], 7)

def snapshot():
    r = xprobe()
    try:
        return json.loads(r.stdout) if r.returncode == 0 else {}
    except ValueError:
        return {}

def main_window(s):
    return next((w for w in s.get('windows', []) if w['main']), None)

def bottle_pids(line_only=True):
    result = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            if line_only and p.joinpath('comm').read_text().strip().lower() != 'line.exe':
                continue
            env = p.joinpath('environ').read_bytes().split(b'\0')
            if ('WINEPREFIX=' + str(PREFIX)).encode() in env:
                result.append(int(p.name))
        except (OSError, PermissionError):
            pass
    return result

def set_status(value):
    temporary = STATE / 'status.tmp'
    temporary.write_text(value)
    temporary.replace(STATE / 'status')
    logging.info('state=%s', value)

def read_status():
    try:
        return (STATE / 'status').read_text().strip()
    except OSError:
        return ''

def cleanup():
    logging.info('Stopping Line bottle and dedicated display services')
    if bottle_pids(line_only=False):
        command(CLI + ['stop', '-b', 'Line'], 15)
    system('stop', *SERVICES, seconds=12)

def clear_stale():
    """啟動前清掉上一輪留下的 Wine 行程。

    2026-10-08 實際發生過：LINE 自動更新到 26.5.0.3975 之後留下一個死鎖的
    wineserver，卡了兩天九小時。同一個 prefix 只能有一個 wineserver，之後每次
    啟動的 LINE 都掛到那個殭屍上等待，Xephyr 裡一個視窗都不會出現。

    不能改用 `bottles-cli stop`：它自己也會卡在同一個殭屍上，每試一次就多留一個
    卡住的清理行程（那天累積了六個）。唯一有效的是直接下訊號。

    偵測沿用 bottle_pids 的 WINEPREFIX 比對。不要改成比對指令列字串 —— 那會連
    「指令列剛好提到這個路徑」的無關行程（包括清理腳本自己）一起抓進來。
    包裝用的 bwrap / bottles-cli 不必另外處理：wineserver 一死它們就會自己收。
    """
    stale = bottle_pids(line_only=False)
    if not stale:
        return
    logging.warning('發現 %d 個上一輪殘留的 Wine 行程，清理中', len(stale))
    for sig, label in ((signal.SIGTERM, '正常結束'), (signal.SIGKILL, '強制結束')):
        for pid in stale:
            try:
                os.kill(pid, sig)
            except OSError:
                pass  # 自己先走了，正是我們要的結果
        if wait_for(lambda: not bottle_pids(line_only=False), 6):
            logging.info('殘留行程已清除（%s）', label)
            notify('已清掉上次殘留的程序，正在重新啟動 LINE。')
            return
        stale = bottle_pids(line_only=False)
    raise RuntimeError('有 Wine 程序清不掉，請登出再登入，或重新開機。')

def preflight():
    for executable in ['flatpak', 'systemctl', 'xdpyinfo', 'openbox', 'Xephyr', 'socat']:
        if not shutil.which(executable):
            raise RuntimeError('缺少必要元件：' + executable)
    if command(['flatpak', 'info', '--user', 'com.usebottles.bottles'], 8).returncode:
        raise RuntimeError('找不到 Bottles Flatpak。')
    if not (LINE / 'bin/current/LINE.exe').is_file():
        raise RuntimeError('找不到已安裝的 LINE 執行檔。')
    config = (PREFIX / 'bottle.yml').read_text()
    runner = re.search(r'^Runner:\s*(.+)$', config, re.M)
    if not runner or not (PREFIX.parent.parent / 'runners' / runner[1].strip()).is_dir():
        raise RuntimeError('找不到 Line 酒瓶使用的 Wine 執行器。')
    # 擺在最後：先確認環境本身沒問題，再動手清上一輪的殘留。
    clear_stale()

def prepare_ini():
    path = LINE / 'Data/LINE.ini'
    raw = path.read_bytes() if path.exists() else b'[global]\r\n'
    # This is a local private backup, never included in diagnostic output.
    backup = STATE / 'LINE.ini.before-launch'
    backup.write_bytes(raw)
    backup.chmod(0o600)
    text = raw.decode('utf-8-sig')
    lines = text.splitlines()
    output, inside, found = [], False, False
    for line in lines:
        if line.strip().startswith('['):
            inside = line.strip() == '[global]'
            output.append(line)
            if inside:
                output.append('windowstore_app_dbfile_checked=true')
                found = True
        elif inside and line.split('=', 1)[0].strip() == 'windowstore_app_dbfile_checked':
            continue
        else:
            output.append(line)
    if not found:
        output = ['[global]', 'windowstore_app_dbfile_checked=true', *output]
    payload = ('\r\n'.join(output) + '\r\n').encode('utf-8')
    fd, temporary = tempfile.mkstemp(prefix='.line-ini-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(payload)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def wait_for(test, seconds):
    deadline = time.monotonic() + seconds
    while not STOP and time.monotonic() < deadline:
        if test():
            return True
        time.sleep(.3)
    return False

def start_environment():
    for service in START_ORDER:
        optional = service in OPTIONAL
        if system('start', service).returncode:
            if optional:
                logging.warning('選用服務啟動失敗，略過：%s', service)
                continue
            raise RuntimeError('無法啟動 ' + service)
        if service == 'line-xephyr.service':
            test = lambda: command(['xdpyinfo', '-display', ':99'], 2).returncode == 0
        elif service == 'line-fcitx5.service':
            test = lambda: '@server=fcitx' in command(
                ['xprop', '-display', ':99', '-root', 'XIM_SERVERS'], 2).stdout
        elif service == 'line-window-manager.service':
            test = lambda: bool(snapshot().get('wm'))
        elif service == 'line-clipboard.service':
            # 橋接接上兩邊之後才會立這個旗標，與 fcitx5 看 XIM_SERVERS 同理。
            test = lambda: '_LINE_CLIPBOARD_READY(CARDINAL)' in command(
                ['xprop', '-display', ':99', '-root', '_LINE_CLIPBOARD_READY'], 2).stdout
        else:
            test = lambda: command(['xdpyinfo', '-display', '127.0.0.1:99'], 2).returncode == 0
        if not wait_for(test, 8):
            if optional:
                logging.warning('選用服務未就緒，略過：%s', service)
                continue
            raise RuntimeError('服務未能就緒：' + service)

def responsive(window):
    if not window.get('ping'):
        return True  # Unsupported does not imply a hung application.
    for _ in range(3):
        result = xprobe('ping', window['id'])
        if result.stdout.strip() in ('ok', 'unsupported'):
            return True
        if not active('line-xephyr.service'):
            return False
    return False

def focus(window):
    xprobe('focus', window['id'])
    # Bring the outer GNOME window forward too; only target Xephyr titled LINE.
    r = command(['xwininfo', '-root', '-tree'], 3)
    m = re.search(r'(0x[0-9a-f]+) "LINE": \("Xephyr" "Xephyr"\)', r.stdout)
    if m:
        xprobe('focus', int(m[1], 16), display=os.environ.get('DISPLAY', ':0'))

MAXIMIZED = set()

def fill_screen(windows):
    """把還沒處理過的主視窗放大到填滿 Xephyr。

    LINE 會先開一個載入視窗、稍後才建立真正的主視窗，兩者 id 不同。
    只在就緒當下放大會打到前者，真正的主視窗永遠不會被放大。
    因此每個主視窗各處理一次即可；已處理過的不再碰，使用者之後
    自己調整的大小得以保留。
    """
    for w in windows:
        if w.get('main') and not w.get('iconic') and w['id'] not in MAXIMIZED:
            MAXIMIZED.add(w['id'])
            xprobe('maximize', w['id'])

def session():
    global STOP
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__('STOP', True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__('STOP', True))
    set_status('starting')
    try:
        preflight()
        # Two attempts total, never an unlimited restart loop.
        for attempt in range(2):
            if STOP:
                return
            if attempt:
                notify('啟動未完成，正在修復並重試一次。')
                cleanup()
            try:
                start_environment()
            except RuntimeError:
                if attempt == 0 and not STOP:
                    logging.warning('Environment startup failed; retrying once')
                    continue
                raise
            prepare_ini()
            env = dict(os.environ, DISPLAY='127.0.0.1:99')
            sp.Popen(CLI + ['run', '-b', 'Line', '-e', WIN_EXE, '--', 'run'],
                     env=env, stdout=sp.DEVNULL, stderr=sp.DEVNULL)
            started = time.monotonic()
            window = None
            while not STOP and time.monotonic() - started < 60:
                # Closing the outer window is an explicit end, not a retry trigger.
                if not active('line-xephyr.service'):
                    set_status('closed')
                    return
                window = main_window(snapshot())
                if window and responsive(window):
                    break
                window = None
                time.sleep(.5)
            if STOP:
                return
            if window:
                fill_screen([window])
                focus(window)
                set_status('ready')
                break
        else:
            raise RuntimeError('LINE 在兩次啟動後仍未就緒。請查看 ~/.local/state/line-bottles/session.log。')

        missing_since = None
        while not STOP:
            if not active('line-xephyr.service'):
                break
            s = snapshot()
            # 圖片／影片檢視器全螢幕時會蓋住主視窗，而它的標題是寄件者名稱而非
            # "LINE"，x11.py 的 inspect 會過濾掉，snapshot() 因此回報零視窗。
            # 只看視窗會讓「看圖看到一半整個工作階段被關掉」。以 LINE.exe 是否
            # 還活著為準：使用者真的關掉 LINE 時該行程會消失，偵測依然正確。
            if s.get('windows') or bottle_pids():
                # Iconic/minimized windows and LINE loading windows keep the session alive.
                missing_since = None
                # 真正的主視窗可能在載入視窗之後才出現，在這裡補放大。
                fill_screen(s.get('windows', []))
            else:
                missing_since = missing_since or time.monotonic()
                if time.monotonic() - missing_since >= 5:
                    break
            time.sleep(1)
        set_status('closed')
    except Exception as exc:
        set_status('failed')
        logging.error('%s', exc)
        notify(str(exc), error=True)
        raise SystemExit(1)

def launch(action):
    with (STATE / 'action.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            notify('LINE 正在檢查或啟動，請稍候。')
            return
        system('import-environment', 'DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS')
        if action == '--stop':
            system('stop', UNIT, seconds=40)
            cleanup()
            return
        if active(UNIT) and action != '--recover':
            if read_status() == 'starting':
                notify('LINE 正在啟動，請稍候。')
                return
            s = snapshot()
            w = main_window(s)
            services_ready = all(active(service) for service in SERVICES)
            bridge_ready = services_ready and command(
                ['xdpyinfo', '-display', '127.0.0.1:99'], 2).returncode == 0
            if bridge_ready and s.get('wm') and w and responsive(w):
                focus(w)
                return
            notify('LINE 未正常回應，正在重新啟動。')
        elif not active(UNIT) and action != '--recover':
            # An older/manual instance has no lifecycle supervisor; migrate it once.
            if bottle_pids():
                notify('正在將 LINE 切換至新的桌面啟動流程。')
        system('stop', UNIT, seconds=40)
        cleanup()
        try:
            preflight()
        except Exception as exc:
            notify(str(exc), error=True)
            return
        system('reset-failed', UNIT, *SERVICES)
        set_status('starting')
        if system('start', UNIT).returncode:
            notify('無法啟動 LINE 工作階段。', error=True)
        else:
            notify('正在檢查環境並開啟 LINE…')

def main():
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True)
    action = sys.argv[1] if len(sys.argv) > 1 else '--launch'
    # 橋接是獨立的長時間常駐行程，日誌另開一份，免得與工作階段的紀錄交錯難讀。
    handler = RotatingFileHandler(
        STATE / ('clipboard.log' if action == '--clipboard' else 'session.log'),
        maxBytes=1024 * 1024, backupCount=4, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format='%(asctime)s %(message)s')
    if action == '--session':
        session()
    elif action == '--clipboard':
        import clipboard
        clipboard.main(sys.argv[2:])
    elif action == '--cleanup':
        cleanup()
    elif action == '--status':
        s = snapshot()
        print(json.dumps(dict(session=active(UNIT), phase=read_status(),
            services={u: active(u) for u in SERVICES}, line_pids=bottle_pids(), **s)))
    elif action in ('--launch', '--recover', '--stop'):
        launch(action)
    else:
        raise SystemExit('Usage: line-bottles [--launch|--recover|--stop|--status]')

if __name__ == '__main__':
    main()

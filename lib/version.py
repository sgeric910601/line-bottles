#!/usr/bin/python3
"""在多個已安裝的 LINE 版本之間切換。

LINE 原本的佈局是 bin/current（實體目錄）與 bin/old（上一版）。本工具把它改成：

    bin/versions/<版本號>/     各版本的實體目錄
    bin/current -> versions/…  符號連結，指向使用中的版本

這樣切換版本只是重指一條符號連結，且 Data/（聊天記錄、登入狀態）完全不受影響
──它在 bin/ 之外，各版本共用。
"""
import hashlib
import os
import shutil
import subprocess as sp
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from peversion import file_version                      # noqa: E402

PREFIX = Path.home() / '.var/app/com.usebottles.bottles/data/bottles/bottles/Line'
BIN = PREFIX / 'drive_c/users/steamuser/AppData/Local/LINE/bin'
CURRENT = BIN / 'current'
VERSIONS = BIN / 'versions'
UPDATER = BIN / 'LineUpdater.exe'
UPDATER_OFF = BIN / 'LineUpdater.exe.disabled'

RED, GRN, YEL, DIM, RST = '\033[31m', '\033[32m', '\033[33m', '\033[2m', '\033[0m'


def die(message):
    raise SystemExit(f'{RED}✗ {message}{RST}')


def ok(message):
    print(f'{GRN}✓ {message}{RST}')


def info(message):
    print(f'{YEL}→ {message}{RST}')


def line_running():
    """LINE.exe 是否正在這個 prefix 裡執行。切換版本前必須確認它沒在跑。"""
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.joinpath('comm').read_text().strip().lower() != 'line.exe':
                continue
            if (b'WINEPREFIX=' + str(PREFIX).encode()) in entry.joinpath('environ').read_bytes().split(b'\0'):
                return True
        except (OSError, PermissionError):
            pass
    return False


def require_stopped():
    if line_running():
        die('LINE 正在執行，請先關閉（line-bottles --stop）再切換版本。')


def detect_version(directory):
    return file_version(directory / 'LINE.exe') or 'unknown'


def content_hash(directory):
    """整個目錄的內容雜湊。

    只比對 LINE.exe 的版本號並不足以判斷兩份是否相同：實測過 bin/old 與
    bin/current 的 LINE.exe 位元組完全相同，但其餘檔案有差異。要刪除或判定
    重複，必須看整個目錄。
    """
    digest = hashlib.md5()
    for path in sorted(p for p in directory.rglob('*') if p.is_file()):
        digest.update(str(path.relative_to(directory)).encode())
        digest.update(hashlib.md5(path.read_bytes()).digest())
    return digest.hexdigest()


def installed():
    """回傳 [(版本名, 目錄)]，依版本名排序。"""
    if not VERSIONS.is_dir():
        return []
    return sorted(((d.name, d) for d in VERSIONS.iterdir() if d.is_dir()),
                  key=lambda pair: pair[0])


def active_name():
    """目前 current 指向哪個版本；不是符號連結則回傳 None（代表尚未遷移）。"""
    if CURRENT.is_symlink():
        return Path(os.readlink(CURRENT)).name
    return None


def migrate():
    """一次性：把實體的 bin/current 轉成 versions/<版本>/ + 符號連結。"""
    if CURRENT.is_symlink():
        ok('已經是符號連結佈局，無需遷移。')
        return
    if not CURRENT.is_dir():
        die(f'找不到 {CURRENT}')
    require_stopped()

    version = detect_version(CURRENT)
    target = VERSIONS / version
    if target.exists():
        die(f'{target} 已存在，請先處理後再遷移。')

    info(f'偵測到目前版本 {version}，開始遷移…')
    VERSIONS.mkdir(parents=True, exist_ok=True)
    shutil.move(str(CURRENT), str(target))
    CURRENT.symlink_to(Path('versions') / version)
    ok(f'bin/current -> versions/{version}')

    old = BIN / 'old'
    if old.is_dir():
        old_version = detect_version(old)
        info('比對 bin/old 的內容…')
        if old_version == version and content_hash(old) == content_hash(target):
            print(f'{DIM}  bin/old 與 {version} 內容完全相同（純重複資料，'
                  f'可用 line-version prune 移除）{RST}')
        else:
            # LINE.exe 版本號可能相同但其餘檔案有差異，這種情況要另取名字保留。
            name = old_version if old_version != version else f'{version}-old'
            info(f'把 bin/old 收進 versions/{name}')
            shutil.move(str(old), str(VERSIONS / name))
            ok(f'已收錄 {name}')


def cmd_list():
    entries = installed()
    if not entries:
        print('尚未遷移，請先執行：line-version migrate')
        return
    active = active_name()
    for name, directory in entries:
        mark = f'{GRN}● 使用中{RST}' if name == active else f'{DIM}○{RST}      '
        size = sum(f.stat().st_size for f in directory.rglob('*') if f.is_file()) // (1024 * 1024)
        print(f'  {mark}  {name:<20} {DIM}{size} MB{RST}')


def cmd_use(name):
    if not name:
        die('用法：line-version use <版本>')
    target = VERSIONS / name
    if not target.is_dir():
        die(f'找不到版本 {name}，可用 line-version list 查看。')
    if not (target / 'LINE.exe').is_file():
        die(f'{target} 裡沒有 LINE.exe，可能不是完整的版本目錄。')
    require_stopped()

    if CURRENT.is_symlink() or CURRENT.exists():
        if not CURRENT.is_symlink():
            die(f'{CURRENT} 是實體目錄，請先執行 line-version migrate。')
        CURRENT.unlink()
    CURRENT.symlink_to(Path('versions') / name)
    ok(f'已切換到 {name}')


def cmd_snapshot(label):
    """把目前 current 的內容另存成一個版本（用於 LINE 自動更新前後留底）。"""
    source = CURRENT.resolve()
    if not source.is_dir():
        die(f'找不到 {CURRENT}')
    name = label or detect_version(source)
    target = VERSIONS / name
    if target.exists():
        die(f'版本 {name} 已存在。')
    require_stopped()
    VERSIONS.mkdir(parents=True, exist_ok=True)
    info(f'複製 {source.name} → versions/{name}（約 135 MB，需要一點時間）…')
    shutil.copytree(source, target)
    ok(f'已建立快照 {name}')


def cmd_pin(enable):
    if enable:
        if UPDATER_OFF.exists():
            ok('已經是鎖定狀態。')
            return
        if not UPDATER.exists():
            die(f'找不到 {UPDATER.name}')
        require_stopped()
        UPDATER.rename(UPDATER_OFF)
        ok('已鎖定版本：LineUpdater.exe 已停用，LINE 不會自動更新。')
    else:
        if not UPDATER_OFF.exists():
            ok('本來就沒有鎖定。')
            return
        require_stopped()
        UPDATER_OFF.rename(UPDATER)
        ok('已解除鎖定：LINE 會恢復自動更新。')


def cmd_status():
    active = active_name()
    if active is None:
        print(f'  佈局    : {YEL}尚未遷移{RST}（bin/current 仍是實體目錄）')
        if CURRENT.is_dir():
            print(f'  版本    : {detect_version(CURRENT)}')
    else:
        print(f'  佈局    : 符號連結')
        print(f'  使用中  : {GRN}{active}{RST}')
        print(f'  已安裝  : {len(installed())} 個版本')
    pinned = UPDATER_OFF.exists()
    print(f'  自動更新: {(RED + "已停用（版本鎖定中）") if pinned else (GRN + "啟用")}{RST}')
    print(f'  LINE    : {"執行中" if line_running() else "未執行"}')


def cmd_prune():
    """移除 bin/old —— 但只在它的內容與某個已收錄版本完全相同時。"""
    old = BIN / 'old'
    if not old.is_dir():
        ok('沒有 bin/old，無需清理。')
        return
    require_stopped()

    info('比對 bin/old 與已收錄的版本…')
    old_hash = content_hash(old)
    duplicate_of = next((name for name, directory in installed()
                         if content_hash(directory) == old_hash), None)
    if duplicate_of is None:
        die('bin/old 的內容與任何已收錄版本都不同，刪掉就永遠拿不回來了。\n'
            '請改用 line-version migrate 把它收進 versions/。')

    size = sum(f.stat().st_size for f in old.rglob('*') if f.is_file()) // (1024 * 1024)
    print(f'  bin/old 與 versions/{duplicate_of} 內容完全相同（{size} MB）')
    if input('  確定刪除？[y/N] ').strip().lower() != 'y':
        print('已取消。')
        return
    shutil.rmtree(old)
    ok(f'已刪除 bin/old，釋出 {size} MB')


USAGE = """用法：line-version <指令>

  status            顯示目前版本、佈局與自動更新狀態
  list              列出已安裝的版本
  migrate           一次性：把 bin/current 轉成符號連結佈局
  use <版本>        切換到指定版本
  snapshot [名稱]   把目前版本另存一份（名稱預設為偵測到的版本號）
  pin               停用 LINE 自動更新，鎖定目前版本
  unpin             恢復自動更新
  prune             刪除與目前版本重複的 bin/old
"""


def main():
    args = sys.argv[1:]
    action = args[0] if args else 'status'
    argument = args[1] if len(args) > 1 else None
    if not PREFIX.is_dir():
        die(f'找不到 Line bottle：{PREFIX}')
    handlers = {
        'status': cmd_status,
        'list': cmd_list,
        'migrate': migrate,
        'use': lambda: cmd_use(argument),
        'snapshot': lambda: cmd_snapshot(argument),
        'pin': lambda: cmd_pin(True),
        'unpin': lambda: cmd_pin(False),
        'prune': cmd_prune,
    }
    if action in ('-h', '--help', 'help') or action not in handlers:
        raise SystemExit(USAGE)
    handlers[action]()


if __name__ == '__main__':
    main()

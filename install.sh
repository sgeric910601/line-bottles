#!/usr/bin/env bash
# 安裝 LINE-on-Bottles。可重複執行（冪等）。
#
#   ./install.sh              完整安裝（bottle 已存在時自動跳過建立）
#   ./install.sh --dry-run    只顯示會做什麼，不改動任何東西
#   ./install.sh --shell-only 只安裝外殼層（腳本／服務／啟動器），不碰 bottle
set -euo pipefail

REPO="$(dirname "$(readlink -f "$0")")"
BINDIR="$HOME/.local/bin"
LIBDIR="$HOME/.local/lib/line-bottles"
UNITDIR="$HOME/.config/systemd/user"
APPDIR="$HOME/.local/share/applications"
ICONDIR="$HOME/.local/share/icons/hicolor/256x256/apps"
STATEDIR="$HOME/.local/state/line-bottles"
BOTTLE="$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/Line"
APT_PACKAGES=(xserver-xephyr openbox socat x11-utils zenity osslsigncode xclip libxrandr2)

DRY=0; SHELL_ONLY=0
for argument in "$@"; do
    case "$argument" in
        --dry-run)    DRY=1 ;;
        --shell-only) SHELL_ONLY=1 ;;
        -h|--help)    sed -n '2,8p' "$0"; exit 0 ;;
        *)            echo "未知參數：$argument" >&2; exit 2 ;;
    esac
done

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; BLD=$'\e[1m'; RST=$'\e[0m'
die()   { echo "${RED}✗ $*${RST}" >&2; exit 1; }
ok()    { echo "${GRN}✓ $*${RST}"; }
info()  { echo "${YEL}→ $*${RST}"; }
step()  { echo; echo "${BLD}── $* ──${RST}"; }
run()   { if [ "$DRY" = 1 ]; then echo "    [dry-run] $*"; else "$@"; fi; }

# ── 0. preflight ──────────────────────────────────────────────────────
step "0. 環境檢查"
[ "$(id -u)" -ne 0 ] || die "請用一般使用者身分執行，不要用 sudo。"
[ -w "$HOME" ] || die "家目錄不可寫。"
command -v flatpak >/dev/null || die "需要 flatpak（sudo apt install flatpak）"
ok "以 $(whoami) 身分執行，家目錄可寫"
[ "$DRY" = 1 ] && info "dry-run 模式：不會有任何改動"

# ── 1. 系統套件 ───────────────────────────────────────────────────────
step "1. 系統套件"
missing=()
for package in "${APT_PACKAGES[@]}"; do
    dpkg -s "$package" >/dev/null 2>&1 || missing+=("$package")
done
if [ ${#missing[@]} -eq 0 ]; then
    ok "已齊全：${APT_PACKAGES[*]}"
else
    info "缺少：${missing[*]}"
    run sudo apt-get install -y "${missing[@]}"
fi

# ── 2. Bottles ────────────────────────────────────────────────────────
step "2. Flatpak Bottles"
if flatpak info --user com.usebottles.bottles >/dev/null 2>&1; then
    ok "已安裝（$(flatpak info --user com.usebottles.bottles 2>/dev/null | awk '/Version:/{print $2}')）"
else
    info "安裝 Bottles…"
    run flatpak install --user -y flathub com.usebottles.bottles
fi

# ── 3. bottle 本體 ────────────────────────────────────────────────────
step "3. Line bottle"
if [ "$SHELL_ONLY" = 1 ]; then
    info "--shell-only：跳過"
elif [ -d "$BOTTLE" ]; then
    ok "已存在，跳過建立：$BOTTLE"
    info "（要重建請先自行改名或刪除該目錄）"
else
    info "bottle 不存在，開始建立（會下載數 GB 並開出 LINE 安裝精靈）"
    run "$REPO/bottle/create-bottle.sh"
fi

# ── 4. bottle 設定 ────────────────────────────────────────────────────
step "4. bottle 環境變數"
if [ "$SHELL_ONLY" = 1 ] || [ ! -f "$BOTTLE/bottle.yml" ]; then
    info "跳過（無 bottle）"
else
    # QSG_RHI_BACKEND 必須是 opengl：設成 d3d11 會讓圖片檢視器變成全黑視窗。
    if [ "$DRY" = 1 ]; then
        echo "    [dry-run] 套用 4 個環境變數到 bottle.yml"
    else
        python3 - "$BOTTLE/bottle.yml" <<'PY'
import sys
WANT = {'LIBGL_ALWAYS_SOFTWARE': "'true'", 'QSG_RHI_BACKEND': 'opengl',
        'WINEDEBUG': '-all', '__GLX_VENDOR_LIBRARY_NAME': 'mesa'}
path = sys.argv[1]
lines = open(path, encoding='utf-8').read().splitlines(True)
out, inside, seen, changed = [], False, set(), False
for line in lines:
    if line.startswith('Environment_Variables:'):
        inside = True; out.append(line); continue
    if inside and line[:1] not in (' ', '\t'):
        for key, value in WANT.items():
            if key not in seen:
                out.append(f'    {key}: {value}\n'); changed = True
        inside = False
    if inside and ':' in line:
        key = line.split(':', 1)[0].strip()
        if key in WANT:
            seen.add(key)
            new = f'    {key}: {WANT[key]}\n'
            if new != line: changed = True
            out.append(new); continue
    out.append(line)
if inside:
    for key, value in WANT.items():
        if key not in seen:
            out.append(f'    {key}: {value}\n'); changed = True
if changed:
    open(path, 'w', encoding='utf-8').writelines(out)
    print('  已更新環境變數')
else:
    print('  環境變數已正確')
PY
    fi
    ok "環境變數就緒"
fi

# ── 5. 主機整合 ───────────────────────────────────────────────────────
step "5. 主機整合（檔案存取與圖示）"
info "Flatpak 檔案權限"
run flatpak override --user \
    --filesystem=xdg-download --filesystem=xdg-pictures \
    --filesystem=xdg-data/applications com.usebottles.bottles

if [ -d "$BOTTLE" ]; then
    WINE_USER="$BOTTLE/drive_c/users/steamuser"
    for pair in "Downloads:$HOME/Downloads" "Pictures:$HOME/Pictures"; do
        name="${pair%%:*}"; target="${pair#*:}"
        link="$WINE_USER/$name"
        if [ -L "$link" ]; then
            ok "$name 已連到 $(readlink "$link")"
        elif [ -d "$link" ] && [ -n "$(ls -A "$link" 2>/dev/null)" ]; then
            info "$name 內有檔案，未自動改為連結（請手動搬移後再執行）"
        else
            run rmdir "$link" 2>/dev/null || true
            run mkdir -p "$target"
            run ln -sfn "$target" "$link"
            ok "$name → $target"
        fi
    done

    # 圖示原本只存在於 bottle 內，刪 bottle 就會掉圖示；複製一份到標準路徑。
    SRC_ICON="$BOTTLE/icons/LineLauncher.png"
    if [ -f "$SRC_ICON" ]; then
        run mkdir -p "$ICONDIR"
        run cp -f "$SRC_ICON" "$ICONDIR/line-bottles.png"
        ok "圖示已複製到 $ICONDIR/line-bottles.png"
    fi
fi

# ── 6. 外殼層 ─────────────────────────────────────────────────────────
step "6. 外殼層（腳本、服務、啟動器）"
run mkdir -p "$BINDIR" "$LIBDIR" "$UNITDIR" "$APPDIR" "$STATEDIR"

for module in session.py x11.py version.py peversion.py clipboard.py frame.py; do
    run install -m 644 "$REPO/lib/$module" "$LIBDIR/$module"
done
ok "lib → $LIBDIR"

for tool in line-bottles line-version; do
    if [ "$DRY" = 1 ]; then
        echo "    [dry-run] 產生 $BINDIR/$tool"
    else
        sed "s|@LIBDIR@|$LIBDIR|g" "$REPO/bin/$tool.in" > "$BINDIR/$tool"
        chmod 755 "$BINDIR/$tool"
    fi
done
ok "bin → $BINDIR/{line-bottles,line-version}"

for unit in "$REPO"/systemd/*.service; do
    run install -m 644 "$unit" "$UNITDIR/$(basename "$unit")"
done
run systemctl --user daemon-reload
ok "systemd unit → $UNITDIR"

DESKTOP="$APPDIR/bottles-Line-LineLauncher.desktop"
if [ "$DRY" = 1 ]; then
    echo "    [dry-run] 產生 $DESKTOP"
else
    sed -e "s|@HOME@|$HOME|g" \
        -e "s|^Icon=.*|Icon=$ICONDIR/line-bottles.png|" \
        "$REPO/desktop/line.desktop.in" > "$DESKTOP"
    chmod 644 "$DESKTOP"
fi
ok "啟動器 → $DESKTOP"

# 桌面上的第二份副本改成符號連結，避免兩份各自漂移。
if [ -e "$HOME/Desktop/LINE.desktop" ] && [ ! -L "$HOME/Desktop/LINE.desktop" ]; then
    info "桌面捷徑目前是獨立副本，改為指向同一份"
    run ln -sfn "$DESKTOP" "$HOME/Desktop/LINE.desktop"
fi

# ── 7. 驗收 ───────────────────────────────────────────────────────────
step "7. 驗收"
if [ "$DRY" = 1 ]; then
    info "dry-run 結束，未做任何改動。"
    exit 0
fi
fail=0
for path in "$BINDIR/line-bottles" "$BINDIR/line-version" "$LIBDIR/session.py" "$DESKTOP"; do
    [ -e "$path" ] && ok "$(basename "$path")" || { echo "${RED}✗ 缺少 $path${RST}"; fail=1; }
done
for unit in "$REPO"/systemd/*.service; do
    name="$(basename "$unit")"
    systemctl --user cat "$name" >/dev/null 2>&1 && ok "$name" || { echo "${RED}✗ $name 未載入${RST}"; fail=1; }
done
echo
[ "$fail" -eq 0 ] || die "安裝未完全成功，請檢視上方錯誤。"
ok "安裝完成"
echo
echo "  啟動 LINE      ：從應用程式選單點「LINE」，或執行 line-bottles"
echo "  版本管理       ：line-version status / list / use <版本>"
echo "  疑難排解       ：$REPO/docs/troubleshooting.md"

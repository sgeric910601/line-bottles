#!/usr/bin/env bash
# 移除 LINE-on-Bottles 的外殼層。
#
# 預設「不」刪除 bottle，因為那裡面有你的聊天記錄與登入狀態。
# 要連 bottle 一起刪，明確加上 --purge-bottle（會再次確認）。
set -euo pipefail

BINDIR="$HOME/.local/bin"
LIBDIR="$HOME/.local/lib/line-bottles"
UNITDIR="$HOME/.config/systemd/user"
APPDIR="$HOME/.local/share/applications"
STATEDIR="$HOME/.local/state/line-bottles"
CERTDIR="$HOME/.local/share/line-wine-cert"
BOTTLE="$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/Line"

PURGE=0
for argument in "$@"; do
    case "$argument" in
        --purge-bottle) PURGE=1 ;;
        -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
        *) echo "未知參數：$argument" >&2; exit 2 ;;
    esac
done

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
ok()   { echo "${GRN}✓ $*${RST}"; }
info() { echo "${YEL}→ $*${RST}"; }

info "停止工作階段…"
"$BINDIR/line-bottles" --stop >/dev/null 2>&1 || true
systemctl --user stop line-session.service line-window-manager.service \
    line-x11-bridge.service line-fcitx5.service line-xephyr.service 2>/dev/null || true

info "移除服務與啟動器…"
rm -f "$UNITDIR"/line-{session,xephyr,x11-bridge,window-manager,fcitx5}.service
systemctl --user daemon-reload
rm -f "$APPDIR/bottles-Line-LineLauncher.desktop"
[ -L "$HOME/Desktop/LINE.desktop" ] && rm -f "$HOME/Desktop/LINE.desktop"
rm -f "$BINDIR/line-bottles" "$BINDIR/line-version"
rm -rf "$LIBDIR"
ok "外殼層已移除"

echo
echo "以下項目${YEL}保留${RST}（含你的資料，需要時請自行刪除）："
echo "  聊天記錄與登入狀態 : $BOTTLE"
echo "  執行期狀態與備份   : $STATEDIR"
echo "  簽章憑證與私鑰     : $CERTDIR"

if [ "$PURGE" = 1 ]; then
    echo
    echo "${RED}--purge-bottle：即將刪除整個 bottle，包含所有聊天記錄與登入狀態。${RST}"
    echo "${RED}此動作無法復原。${RST}"
    read -r -p "請輸入 DELETE 以確認：" answer
    if [ "$answer" = "DELETE" ]; then
        rm -rf "$BOTTLE"
        ok "bottle 已刪除"
    else
        info "未確認，bottle 保留。"
    fi
fi

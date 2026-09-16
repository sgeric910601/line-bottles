#!/usr/bin/env bash
# 建立 Line bottle 並安裝 LINE 本體。
#
# 只有在 bottle 尚未存在時才需要執行；install.sh 會自動判斷。
# 需要 LINE 官方安裝檔（LineInst.exe），路徑由 $LINE_INSTALLER 指定。
set -euo pipefail

BOTTLE_NAME="${BOTTLE_NAME:-Line}"
BOTTLES_DATA="$HOME/.var/app/com.usebottles.bottles/data/bottles"
BOTTLE="$BOTTLES_DATA/bottles/$BOTTLE_NAME"
LINE_INSTALLER="${LINE_INSTALLER:-$HOME/Downloads/LineInst.exe}"
CLI=(flatpak run --command=bottles-cli com.usebottles.bottles)

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
die()  { echo "${RED}✗ $*${RST}" >&2; exit 1; }
ok()   { echo "${GRN}✓ $*${RST}"; }
info() { echo "${YEL}→ $*${RST}"; }

# 這些版本是實測可用的組合，升級前請先備份整個 bottle。
RUNNER="${RUNNER:-kron4ek-wine-proton-11.0-1-amd64}"
DEPENDENCIES=(arial32 times32 courie32 mono gecko cjkfonts d3dcompiler_47 vcredist2022)

[ -d "$BOTTLE" ] && die "bottle 已存在：$BOTTLE（要重建請先自行改名或刪除）"
[ -f "$LINE_INSTALLER" ] || die "找不到 LINE 安裝檔：$LINE_INSTALLER
請從 LINE 官網下載，或用 LINE_INSTALLER=/路徑/LineInst.exe 指定。"

info "建立 bottle「$BOTTLE_NAME」(runner: $RUNNER)…"
"${CLI[@]}" new --bottle-name "$BOTTLE_NAME" --environment application --runner "$RUNNER" \
    || die "bottle 建立失敗（可用 ${CLI[*]} --help 檢查 Bottles 版本的參數差異）"
[ -d "$BOTTLE" ] || die "bottle 建立後找不到目錄：$BOTTLE"
ok "bottle 已建立"

info "安裝相依元件（字型、mono、gecko、vcredist…，會下載數百 MB）…"
for dependency in "${DEPENDENCIES[@]}"; do
    printf '  - %s\n' "$dependency"
    "${CLI[@]}" add-dependency --bottle "$BOTTLE_NAME" --dependency "$dependency" \
        || die "相依元件安裝失敗：$dependency"
done
ok "相依元件完成"

# 簽章必須在 LINE 安裝「之前」完成：LINE 啟動時就會檢查 DLL 簽章。
info "簽署 Wine 系統 DLL…"
BOTTLE="$BOTTLE" "$(dirname "$(readlink -f "$0")")/sign-dlls.sh" || die "DLL 簽署失敗"

info "執行 LINE 安裝程式（會開出安裝精靈視窗，請依指示完成）…"
"${CLI[@]}" run --bottle "$BOTTLE_NAME" --executable "$LINE_INSTALLER" || true

LINE_EXE="$BOTTLE/drive_c/users/steamuser/AppData/Local/LINE/bin/current/LINE.exe"
[ -f "$LINE_EXE" ] || die "安裝後找不到 LINE.exe，請確認安裝精靈有跑完。
預期位置：$LINE_EXE"
ok "LINE 安裝完成：$(python3 "$(dirname "$(readlink -f "$0")")/../lib/peversion.py" "$LINE_EXE" 2>/dev/null || echo '版本未知')"

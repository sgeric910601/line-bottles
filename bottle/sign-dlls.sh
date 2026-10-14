#!/usr/bin/env bash
# 簽署 Wine prefix 內的系統 DLL。
#
# 為什麼需要這一步：LINE 會檢查它載入的 DLL 是否帶有數位簽章，而 Wine 內建的系統
# DLL 沒有簽章，因此 LINE 會拒絕載入而根本起不來。這裡用一張只在本機產生、只在
# 這個 prefix 內使用的自簽憑證，把 system32 / syswow64 的 DLL 簽過一遍。
#
# 已經帶有簽章的檔案會跳過，所以本腳本可以重複執行。
set -euo pipefail

BOTTLE="${BOTTLE:-$HOME/.var/app/com.usebottles.bottles/data/bottles/bottles/Line}"
CERT_DIR="${CERT_DIR:-$HOME/.local/share/line-wine-cert}"
CONF="${CONF:-$(dirname "$(readlink -f "$0")")/codesign.cnf}"

RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RST=$'\e[0m'
die()  { echo "${RED}✗ $*${RST}" >&2; exit 1; }
ok()   { echo "${GRN}✓ $*${RST}"; }
info() { echo "${YEL}→ $*${RST}"; }

command -v osslsigncode >/dev/null || die "缺少 osslsigncode（sudo apt install osslsigncode）"
command -v openssl      >/dev/null || die "缺少 openssl"
[ -f "$CONF" ] || die "找不到憑證設定檔：$CONF"

WINDOWS="$BOTTLE/drive_c/windows"
[ -d "$WINDOWS/system32" ] || die "找不到 Wine prefix 的 system32：$WINDOWS/system32"

CRT="$CERT_DIR/codesign.crt"
KEY="$CERT_DIR/codesign.key"

# ── 憑證：不存在才產生，私鑰權限鎖 600 ────────────────────────────────
if [ -f "$CRT" ] && [ -f "$KEY" ]; then
    ok "沿用既有憑證：$CRT"
else
    info "產生本機自簽憑證（10 年效期）…"
    mkdir -p "$CERT_DIR"; chmod 700 "$CERT_DIR"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -config "$CONF" -keyout "$KEY" -out "$CRT" 2>/dev/null \
        || die "憑證產生失敗"
    chmod 600 "$KEY" "$CRT"
    ok "已產生：$(openssl x509 -in "$CRT" -noout -subject | sed 's/^subject=//')"
fi

# ── 簽署 ──────────────────────────────────────────────────────────────
TMP="$(mktemp -d /tmp/line-dll-sign.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
signed=0; skipped=0; failed=0

for directory in "$WINDOWS/system32" "$WINDOWS/syswow64"; do
    [ -d "$directory" ] || continue
    while IFS= read -r -d '' dll; do
        # osslsigncode verify 的離開碼完全不能拿來判斷：沒有簽章回傳非 0，
        # 帶著自簽憑證（不被系統信任）也回傳非 0。而在 pipefail 下整條管線會跟著
        # 它失敗，於是 `if ! …` 恆成立，每個檔案都被當成已簽章跳過 —— 腳本等於
        # 什麼都沒做（實測：新簽 0／已有簽章 1257）。只認輸出文字，並用 || true 擋 set -e。
        report="$(osslsigncode verify -in "$dll" 2>&1 || true)"
        if [[ "$report" != *"No signature found"* ]]; then
            skipped=$((skipped + 1)); continue
        fi
        if osslsigncode sign -h sha256 -certs "$CRT" -key "$KEY" \
            -n "Local Wine system DLL" -in "$dll" -out "$TMP/signed.dll" >/dev/null 2>&1; then
            mv "$TMP/signed.dll" "$dll"
            signed=$((signed + 1))
            (( signed % 100 == 0 )) && printf '  已簽署 %d 個…\n' "$signed"
        else
            failed=$((failed + 1)); printf '%s  簽署失敗：%s%s\n' "$RED" "$dll" "$RST" >&2
        fi
    done < <(find "$directory" -maxdepth 1 -type f -iname '*.dll' -print0)
done

printf '完成：新簽 %d／已有簽章 %d／失敗 %d\n' "$signed" "$skipped" "$failed"
[ "$failed" -eq 0 ] || die "有 $failed 個檔案簽署失敗"
ok "DLL 簽署完成"

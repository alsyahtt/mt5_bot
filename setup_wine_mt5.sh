#!/usr/bin/env bash
#
# Setup jembatan Python <-> MetaTrader 5 di macOS, memakai Wine bawaan
# aplikasi "MetaTrader 5 for Mac" resmi dari MetaQuotes (gratis, signed).
#
#   ./setup_wine_mt5.sh mt5      # cek / pandu instalasi MetaTrader 5.app
#   ./setup_wine_mt5.sh python   # pasang Python Windows ke prefix MT5
#   ./setup_wine_mt5.sh deps     # pasang MetaTrader5 + rpyc di Python tsb
#   ./setup_wine_mt5.sh check    # ringkasan status
#   ./setup_wine_mt5.sh terminal # buka GUI MT5 (untuk login akun demo)
#   ./setup_wine_mt5.sh status   # cek jembatan mt5linux nyala atau tidak
#   ./setup_wine_mt5.sh stop     # hentikan jembatan mt5linux
#   ./setup_wine_mt5.sh server   # jalankan jembatan mt5linux
#   ./setup_wine_mt5.sh all      # mt5 -> python -> deps -> check
#
set -euo pipefail

MT5_APP="${MT5_APP:-/Applications/MetaTrader 5.app}"
WINE_BIN="$MT5_APP/Contents/SharedSupport/wine/bin"
WINE="$WINE_BIN/wine"

export WINEPREFIX="${WINEPREFIX:-$HOME/Library/Application Support/net.metaquotes.wine.metatrader5}"
export WINEDEBUG="${WINEDEBUG:-fixme-all,err-ole}"
export PATH="$WINE_BIN:$PATH"

PY_VER="${PY_VER:-3.11.9}"
# Wine bawaan MetaQuotes murni 64-bit (tanpa WoW64), sedangkan bootstrapper
# installer resmi Python itu 32-bit. Karena itu dipakai paket "embeddable"
# yang tinggal di-unzip, lalu pip dipasang lewat get-pip.py.
PY_URL="https://www.python.org/ftp/python/${PY_VER}/python-${PY_VER}-embed-amd64.zip"
GETPIP_URL="https://bootstrap.pypa.io/get-pip.py"
PKG_URL="https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/MetaTrader5.dmg"
CACHE="$HOME/.cache/mt5-wine"

WIN_PY="$WINEPREFIX/drive_c/Python311/python.exe"
WIN_TERMINAL="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"

RPC_HOST="${MT5_RPC_HOST:-localhost}"
RPC_PORT="${MT5_RPC_PORT:-18812}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31mxx\033[0m %s\n' "$*" >&2; exit 1; }

need_app() {
    [ -x "$WINE" ] || die "Wine bawaan MT5 tidak ada. Jalankan dulu: ./setup_wine_mt5.sh mt5"
}

need_prefix() {
    [ -d "$WINEPREFIX/drive_c" ] || die \
"Wine prefix belum terbentuk di:
  $WINEPREFIX
Buka MetaTrader 5 sekali (make terminal) supaya prefix-nya dibuat, lalu ulangi."
}

fetch() {  # fetch <url> <dest>
    [ -f "$2" ] && { log "Sudah ada: $(basename "$2")"; return 0; }
    mkdir -p "$(dirname "$2")"
    log "Download $(basename "$2")"
    curl -fL --progress-bar -o "$2.part" "$1"
    mv "$2.part" "$2"
}

step_mt5() {
    if [ -x "$WINE" ]; then
        log "MetaTrader 5.app sudah terpasang."
        return 0
    fi
    fetch "$PKG_URL" "$CACHE/MetaTrader5.pkg.zip"
    if [ ! -f "$CACHE/MetaTrader 5.pkg" ]; then
        log "Ekstrak installer"
        (cd "$CACHE" && unzip -q -o MetaTrader5.pkg.zip -x '._*')
    fi
    cat <<MSG

Installer butuh hak administrator, jadi jalankan sendiri salah satu berikut:

  sudo installer -pkg "$CACHE/MetaTrader 5.pkg" -target /

atau lewat GUI:

  open "$CACHE/MetaTrader 5.pkg"

Catatan: postinstall MetaQuotes otomatis membuka MT5 dan tab mql5.com.
Setelah selesai, lanjutkan dengan: ./setup_wine_mt5.sh python

MSG
    die "MetaTrader 5.app belum ada."
}

step_python() {
    need_app; need_prefix
    if [ -f "$WIN_PY" ]; then log "Python Windows sudah terpasang."; return 0; fi

    fetch "$PY_URL" "$CACHE/python-${PY_VER}-embed-amd64.zip"
    local target="$WINEPREFIX/drive_c/Python311"
    log "Ekstrak Python $PY_VER (embeddable) ke C:\\Python311"
    mkdir -p "$target"
    unzip -q -o "$CACHE/python-${PY_VER}-embed-amd64.zip" -d "$target"
    [ -f "$WIN_PY" ] || die "python.exe tidak ditemukan setelah ekstrak."

    # Paket embeddable mematikan site-packages secara default; hidupkan lagi
    # supaya pip dan library pihak ketiga terbaca.
    local pth; pth="$(find "$target" -maxdepth 1 -name 'python*._pth' | head -1)"
    if [ -n "$pth" ]; then
        log "Aktifkan site-packages di $(basename "$pth")"
        printf '%s\n' "$(basename "${pth%._pth}").zip" "." 'Lib\site-packages' 'import site' > "$pth"
    fi

    fetch "$GETPIP_URL" "$CACHE/get-pip.py"
    log "Pasang pip"
    "$WINE" "$WIN_PY" "$CACHE/get-pip.py" --no-warn-script-location
    "$WINE" "$WIN_PY" -m pip --version || die "pip gagal terpasang."
    log "Python Windows siap."
}

step_deps() {
    need_app; need_prefix
    [ -f "$WIN_PY" ] || die "Python Windows belum ada. Jalankan: ./setup_wine_mt5.sh python"
    # rpyc di-pin: versi sisi Wine dan sisi macOS harus sama persis, kalau tidak
    # koneksi rpyc gagal. Pasangannya ada di requirements.txt -- ubah keduanya.
    log "Pasang MetaTrader5 + rpyc==6.0.2 + plumbum di Python Wine"
    "$WINE" "$WIN_PY" -m pip install MetaTrader5 'rpyc==6.0.2' plumbum
    log "Dependency siap."
}

step_check() {
    printf '%-24s %s\n' "MetaTrader 5.app"  "$([ -x "$WINE" ] && echo OK || echo 'TIDAK ADA')"
    printf '%-24s %s\n' "Wine prefix"       "$([ -d "$WINEPREFIX/drive_c" ] && echo OK || echo 'BELUM DIBUAT')"
    printf '%-24s %s\n' "  lokasi"          "$WINEPREFIX"
    printf '%-24s %s\n' "terminal64.exe"    "$([ -f "$WIN_TERMINAL" ] && echo OK || echo 'TIDAK ADA')"
    printf '%-24s %s\n' "python.exe (Wine)" "$([ -f "$WIN_PY" ] && echo OK || echo 'TIDAK ADA')"
    printf '%-24s %s\n' "terminal jalan?"   "$(pgrep -qf 'terminal64.exe' && echo YA || echo TIDAK)"
    if [ -x "$WINE" ] && [ -f "$WIN_PY" ]; then
        printf '%-24s ' "modul MetaTrader5"
        "$WINE" "$WIN_PY" -c "import MetaTrader5 as m; print('OK', m.__version__)" 2>/dev/null \
            || echo "TIDAK ADA"
    fi
}

step_terminal() {
    [ -d "$MT5_APP" ] || die "MetaTrader 5.app belum terpasang."
    log "Buka MT5. Di sana login akun demo, lalu aktifkan:"
    log "  Tools > Options > Expert Advisors > Allow Algo Trading"
    open -a "$MT5_APP"
}

step_status() {
    printf '%-24s %s\n' "port ${RPC_PORT}" \
        "$(nc -z "$RPC_HOST" "$RPC_PORT" 2>/dev/null && echo 'LISTEN' || echo 'tertutup')"

    local pid; pid="$(pgrep -f 'm mt5linux' | head -1)"
    printf '%-24s %s\n' "proses mt5linux" "${pid:-tidak ada}"

    local venv_py; venv_py="$(cd "$(dirname "$0")" && pwd)/.venv/bin/python"
    [ -x "$venv_py" ] || { warn "Virtualenv belum ada, round-trip dilewati."; return 0; }

    # Port terbuka belum tentu server sehat. Uji betulan: eval di sisi Wine
    # lalu pastikan modul MetaTrader5 kebaca dari sana.
    printf '%-24s ' "round-trip rpyc"
    "$venv_py" - "$RPC_HOST" "$RPC_PORT" <<'PY'
import sys, rpyc
host, port = sys.argv[1], int(sys.argv[2])
try:
    conn = rpyc.classic.connect(host, port)
    conn._config["sync_request_timeout"] = 15
    assert conn.eval("1+1") == 2
    ver = conn.modules.MetaTrader5.__version__
    print("OK (MetaTrader5 %s di sisi Wine)" % ver)
except Exception as exc:
    print("GAGAL: %s: %s" % (type(exc).__name__, exc))
    sys.exit(1)
PY
}

step_stop() {
    local pid; pid="$(pgrep -f "m mt5linux" | head -1)"
    if [ -n "$pid" ]; then
        log "Hentikan server mt5linux (pid $pid)"
        kill "$pid" 2>/dev/null || true
    fi
    # Server rpyc sebenarnya adalah python.exe di dalam Wine; kalau initialize()
    # menggantung, proses ini tidak ikut mati dan port tetap terpakai.
    pkill -f 'Python311\\python.exe' 2>/dev/null || true
    pkill -f 'Python311/python.exe' 2>/dev/null || true
    /bin/sleep 2
    nc -z "$RPC_HOST" "$RPC_PORT" 2>/dev/null \
        && warn "Port ${RPC_PORT} masih terpakai." \
        || log "Server berhenti."
}

step_server() {
    need_app; need_prefix
    [ -f "$WIN_PY" ] || die "Python Windows belum ada. Jalankan: ./setup_wine_mt5.sh python"
    local venv_py; venv_py="$(cd "$(dirname "$0")" && pwd)/.venv/bin/python"
    [ -x "$venv_py" ] || die "Virtualenv belum ada. Jalankan: make install"
    pgrep -qf 'terminal64.exe' || warn "Terminal MT5 belum jalan. Buka dulu: make terminal"
    log "Server mt5linux di ${RPC_HOST}:${RPC_PORT} (Ctrl-C untuk berhenti)"
    exec "$venv_py" -m mt5linux --host "$RPC_HOST" -p "$RPC_PORT" \
        -w "$WINE" -s "$CACHE/server" "$WIN_PY"
}

case "${1:-all}" in
    mt5)      step_mt5 ;;
    python)   step_python ;;
    deps)     step_deps ;;
    check)    step_check ;;
    terminal) step_terminal ;;
    status)   step_status ;;
    stop)     step_stop ;;
    server)   step_server ;;
    all)      step_mt5; step_python; step_deps; step_check ;;
    *)        die "Langkah tidak dikenal: $1" ;;
esac

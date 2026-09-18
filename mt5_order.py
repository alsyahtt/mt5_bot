#!/usr/bin/env python3
"""Kirim order ke MetaTrader 5.

Dipakai lewat CLI, backend mengikuti MT5_BACKEND di .env (lihat mt5_client.py).
Daftar contoh perintah ada di CONTOH di bawah.

Tiap order selalu lewat mt5.order_check() dulu; kalau check gagal, order tidak
dikirim. Tambahkan --dry-run untuk berhenti setelah check.

Pengaman (lihat .env.example): akun REAL ditolak kecuali MT5_ALLOW_LIVE=yes,
dan volume di atas MT5_MAX_LOT selalu ditolak.
"""

CONTOH = """contoh:
    python mt5_order.py quote  EURUSD
    python mt5_order.py buy    EURUSD 0.10 --sl-points 200 --tp-points 400
    python mt5_order.py sell   EURUSD 0.10 --sl 1.0950 --tp 1.0850
    python mt5_order.py buy-limit  EURUSD 0.10 --price 1.0750
    python mt5_order.py sell-stop  EURUSD 0.10 --price 1.0700
    python mt5_order.py positions
    python mt5_order.py close 557416535
    python mt5_order.py close-all --symbol EURUSD
    python mt5_order.py modify 557416535 --sl 1.0800
    python mt5_order.py orders
    python mt5_order.py cancel 557416600
"""

import argparse
import os
import sys
from decimal import ROUND_DOWN, Decimal
from typing import Any, Dict, List, Optional, Tuple

from mt5_client import Config, connect, load_env


# --------------------------------------------------------------------------
# Arti kode balasan server (retcode). Sumber: dokumentasi MQL5.
# --------------------------------------------------------------------------

RETCODE_TEXT = {
    10004: "REQUOTE - harga sudah berubah, broker menawarkan harga baru",
    10006: "REJECT - permintaan ditolak broker",
    10007: "CANCEL - dibatalkan trader",
    10008: "PLACED - order pending berhasil dipasang",
    10009: "DONE - permintaan selesai dieksekusi",
    10010: "DONE_PARTIAL - hanya sebagian volume yang terisi",
    10011: "ERROR - kesalahan saat memproses permintaan",
    10012: "TIMEOUT - permintaan kedaluwarsa",
    10013: "INVALID - permintaan tidak valid (ada field salah/kurang)",
    10014: "INVALID_VOLUME - volume tidak valid (cek volume_min/step/max)",
    10015: "INVALID_PRICE - harga tidak valid",
    10016: "INVALID_STOPS - SL/TP terlalu dekat harga atau di sisi yang salah",
    10017: "TRADE_DISABLED - trading dimatikan untuk akun ini",
    10018: "MARKET_CLOSED - pasar sedang tutup",
    10019: "NO_MONEY - dana tidak cukup untuk margin",
    10020: "PRICE_CHANGED - harga berubah, naikkan --deviation",
    10021: "PRICE_OFF - tidak ada quote untuk diproses",
    10022: "INVALID_EXPIRATION - tanggal kedaluwarsa order tidak valid",
    10023: "ORDER_CHANGED - state order sudah berubah",
    10024: "TOO_MANY_REQUESTS - permintaan terlalu sering",
    10025: "NO_CHANGES - tidak ada yang berubah dari permintaan",
    10026: "SERVER_DISABLES_AT - autotrading dimatikan di sisi server",
    10027: "CLIENT_DISABLES_AT - autotrading dimatikan di terminal (tombol AutoTrading)",
    10028: "LOCKED - permintaan dikunci untuk diproses",
    10029: "FROZEN - order/posisi sedang di zona freeze, tidak bisa diubah",
    10030: "INVALID_FILL - tipe filling tidak didukung simbol ini",
    10031: "CONNECTION - tidak ada koneksi ke server trading",
    10032: "ONLY_REAL - operasi hanya untuk akun live",
    10033: "LIMIT_ORDERS - jumlah order pending sudah mentok limit",
    10034: "LIMIT_VOLUME - total volume untuk simbol ini sudah mentok limit",
    10035: "INVALID_ORDER - tipe order tidak benar",
    10036: "POSITION_CLOSED - posisi sudah tertutup",
    10038: "INVALID_CLOSE_VOLUME - volume close lebih besar dari volume posisi",
    10039: "CLOSE_ORDER_EXIST - sudah ada order close untuk posisi ini",
    10040: "LIMIT_POSITIONS - jumlah posisi terbuka sudah mentok limit",
    10041: "REJECT_CANCEL - aktivasi order pending ditolak, order dibatalkan",
    10042: "LONG_ONLY - simbol ini hanya menerima posisi buy",
    10043: "SHORT_ONLY - simbol ini hanya menerima posisi sell",
    10044: "CLOSE_ONLY - simbol ini hanya menerima penutupan posisi",
    10045: "FIFO_CLOSE - akun FIFO, posisi harus ditutup urut dari yang terlama",
    10046: "HEDGE_PROHIBITED - akun ini melarang posisi berlawanan arah",
}

# retcode yang berarti "aman dilanjutkan". order_check() sukses mengembalikan 0,
# sebagian broker mengembalikan 10009.
OK_CHECK = (0, 10009)
OK_SEND = (10008, 10009, 10010)

def leg_default() -> int:
    """Berapa posisi dibuka perintah scale ketika hanya satu TP yang diisi.
    Dengan beberapa TP, jumlahnya sudah jelas dari panjang daftarnya dan nilai
    ini tidak dipakai. Dibaca saat dipanggil, bukan saat modul diimpor, supaya
    .env yang dimuat belakangan tetap berlaku."""
    mentah = os.getenv("MT5_SCALE_LEGS", "3")
    try:
        return int(mentah)
    except ValueError:
        raise SystemExit("MT5_SCALE_LEGS harus bilangan bulat, bukan %r." % mentah)


def retcode_text(code: int) -> str:
    return RETCODE_TEXT.get(int(code), "kode tidak dikenal")


# --------------------------------------------------------------------------
# Konfigurasi khusus trading
# --------------------------------------------------------------------------

def _env_flag(name: str, default: str = "no") -> bool:
    return os.getenv(name, default).strip().lower() in ("yes", "y", "true", "1", "on")


class TradeConfig:
    def __init__(self) -> None:
        self.deviation = int(os.getenv("MT5_DEVIATION", "20"))
        self.magic = int(os.getenv("MT5_MAGIC", "990215"))
        self.max_lot = float(os.getenv("MT5_MAX_LOT", "1.0"))
        self.allow_live = _env_flag("MT5_ALLOW_LIVE")
        self.comment = os.getenv("MT5_COMMENT", "mt5_order.py")


# --------------------------------------------------------------------------
# Pembulatan volume & harga ke aturan simbol
#
# Broker menolak volume yang bukan kelipatan volume_step dan harga yang
# desimalnya lebih banyak dari `digits`. Float mentah (0.1+0.2) gampang meleset,
# jadi pembulatan step lewat Decimal.
# --------------------------------------------------------------------------

def _decimals(value: float) -> int:
    exp = Decimal(str(value)).normalize().as_tuple().exponent
    return max(0, -int(exp))


def normalize_volume(volume: float, info: Any) -> float:
    vmin = float(info.volume_min)
    vmax = float(info.volume_max)
    step = float(info.volume_step) or 0.01

    if volume < vmin:
        raise SystemExit(
            "Volume %.8g di bawah minimum simbol (%.8g)." % (volume, vmin))
    if volume > vmax:
        raise SystemExit(
            "Volume %.8g di atas maksimum simbol (%.8g)." % (volume, vmax))

    # Dibulatkan ke BAWAH dengan sengaja: pembulatan ke atas berarti diam-diam
    # membuka posisi lebih besar dari yang diminta.
    steps = (Decimal(str(volume)) / Decimal(str(step))).to_integral_value(ROUND_DOWN)
    snapped = float(Decimal(str(step)) * steps)
    hasil = round(snapped, _decimals(step))
    if hasil < vmin:
        raise SystemExit(
            "Volume %.8g dibulatkan ke bawah jadi %.8g, di bawah minimum %.8g."
            % (volume, hasil, vmin))
    return hasil


def normalize_price(price: float, info: Any) -> float:
    return round(float(price), int(info.digits))


# --------------------------------------------------------------------------
# Info simbol & harga
# --------------------------------------------------------------------------

def prepare_symbol(mt5: Any, symbol: str) -> Any:
    """Pastikan simbol ada dan tampil di Market Watch, kembalikan symbol_info."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise SystemExit(
            "Simbol %r tidak dikenal broker ini.\n"
            "Cek ejaannya di Market Watch (sering ada sufiks: EURUSD.m, EURUSDmicro)."
            % symbol)
    if not info.visible:
        # Simbol yang belum tampil di Market Watch tidak bisa ditradingkan.
        if not mt5.symbol_select(symbol, True):
            raise SystemExit("Gagal menampilkan %s di Market Watch." % symbol)
        info = mt5.symbol_info(symbol)
    return info


def current_tick(mt5: Any, symbol: str) -> Any:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or (not tick.bid and not tick.ask):
        raise SystemExit(
            "Belum ada quote untuk %s. Pasar mungkin tutup atau simbol belum "
            "sinkron di terminal." % symbol)
    return tick


# --------------------------------------------------------------------------
# Pengaman
# --------------------------------------------------------------------------

def guard_account(mt5: Any, tcfg: TradeConfig, assume_yes: bool) -> Any:
    """Tolak trading kalau terminal/akun tidak mengizinkan, atau akun REAL
    dipakai tanpa izin eksplisit."""
    terminal = mt5.terminal_info()
    if terminal is not None and not terminal.trade_allowed:
        raise SystemExit(
            "Terminal menolak order otomatis.\n"
            "Nyalakan tombol AutoTrading di toolbar MetaTrader 5, lalu ulangi.")

    account = mt5.account_info()
    if account is None:
        raise SystemExit("Tidak bisa membaca account_info().")
    if not account.trade_allowed:
        raise SystemExit(
            "Akun %s tidak diizinkan trading oleh broker (read-only/investor "
            "password?)." % account.login)

    mode = int(account.trade_mode)
    label = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(mode, str(mode))
    if mode != mt5.ACCOUNT_TRADE_MODE_REAL:
        print("Akun       : %s %s (%s) - mode %s" % (
            account.login, account.name, account.company, label))
        return account

    # --- mulai sini: akun uang sungguhan ---
    if not tcfg.allow_live:
        raise SystemExit(
            "Akun %s adalah akun REAL (uang sungguhan) dan order dihentikan.\n"
            "Kalau memang disengaja, set MT5_ALLOW_LIVE=yes di .env." % account.login)

    print("*** AKUN REAL - UANG SUNGGUHAN ***")
    print("Akun       : %s %s (%s)" % (account.login, account.name, account.company))
    print("Balance    : %.2f %s" % (account.balance, account.currency))
    if assume_yes:
        return account
    if not sys.stdin.isatty():
        raise SystemExit(
            "Akun REAL butuh konfirmasi, tapi stdin bukan terminal. "
            "Tambahkan --yes kalau memang disengaja.")
    if input('Ketik "YA" untuk lanjut: ').strip() != "YA":
        raise SystemExit("Dibatalkan.")
    return account


def guard_volume(volume: float, tcfg: TradeConfig) -> None:
    if volume > tcfg.max_lot:
        raise SystemExit(
            "Volume %.8g melewati batas MT5_MAX_LOT=%.8g.\n"
            "Batas ini pengaman salah ketik. Naikkan di .env kalau memang perlu."
            % (volume, tcfg.max_lot))


def guard_symbol_direction(mt5: Any, info: Any, is_buy: bool) -> None:
    mode = int(info.trade_mode)
    if mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
        raise SystemExit("Trading untuk %s sedang dinonaktifkan broker." % info.name)
    if mode == mt5.SYMBOL_TRADE_MODE_CLOSEONLY:
        raise SystemExit("%s sedang close-only, tidak bisa buka posisi baru." % info.name)
    if mode == mt5.SYMBOL_TRADE_MODE_LONGONLY and not is_buy:
        raise SystemExit("%s hanya menerima posisi buy (long-only)." % info.name)
    if mode == mt5.SYMBOL_TRADE_MODE_SHORTONLY and is_buy:
        raise SystemExit("%s hanya menerima posisi sell (short-only)." % info.name)


# --------------------------------------------------------------------------
# SL / TP
#
# Broker menolak SL/TP yang lebih dekat dari trade_stops_level poin dari harga.
# --------------------------------------------------------------------------

def resolve_sltp(
    info: Any,
    entry: float,
    is_buy: bool,
    sl: Optional[float],
    tp: Optional[float],
    sl_points: Optional[int],
    tp_points: Optional[int],
) -> Tuple[float, float]:
    point = float(info.point)
    sign = 1.0 if is_buy else -1.0

    if sl_points is not None:
        sl = entry - sign * sl_points * point
    if tp_points is not None:
        tp = entry + sign * tp_points * point

    sl = normalize_price(sl, info) if sl else 0.0
    tp = normalize_price(tp, info) if tp else 0.0

    # Arah harus benar: SL selalu di sisi rugi, TP di sisi untung.
    if sl:
        if is_buy and sl >= entry:
            raise SystemExit("Untuk buy, SL (%s) harus di BAWAH harga masuk (%s)." % (sl, entry))
        if not is_buy and sl <= entry:
            raise SystemExit("Untuk sell, SL (%s) harus di ATAS harga masuk (%s)." % (sl, entry))
    if tp:
        if is_buy and tp <= entry:
            raise SystemExit("Untuk buy, TP (%s) harus di ATAS harga masuk (%s)." % (tp, entry))
        if not is_buy and tp >= entry:
            raise SystemExit("Untuk sell, TP (%s) harus di BAWAH harga masuk (%s)." % (tp, entry))

    stops = int(getattr(info, "trade_stops_level", 0) or 0)
    if stops:
        jarak = stops * point
        for nama, nilai in (("SL", sl), ("TP", tp)):
            if nilai and abs(entry - nilai) < jarak:
                raise SystemExit(
                    "%s terlalu dekat: broker minta minimal %d poin (%.*f) dari harga %s."
                    % (nama, stops, int(info.digits), jarak, entry))
    return sl, tp


# --------------------------------------------------------------------------
# Pengiriman order
# --------------------------------------------------------------------------

def filling_candidates(mt5: Any, info: Any, pending: bool) -> List[int]:
    """Urutan tipe filling yang akan dicoba.

    symbol_info.filling_mode adalah bitmask: bit0=FOK, bit1=IOC. Nilainya tidak
    selalu akurat di semua broker, jadi sisanya tetap dicoba sebagai cadangan
    dan order_check() yang memutuskan.
    """
    mask = int(getattr(info, "filling_mode", 0) or 0)
    urutan: List[int] = []
    if pending:
        urutan.append(mt5.ORDER_FILLING_RETURN)
    if mask & 1:
        urutan.append(mt5.ORDER_FILLING_FOK)
    if mask & 2:
        urutan.append(mt5.ORDER_FILLING_IOC)
    for f in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN):
        if f not in urutan:
            urutan.append(f)
    return urutan


def order_check(mt5: Any, request: Dict[str, Any]) -> Any:
    """Panggil mt5.order_check() dengan melewati wrapper mt5linux yang rusak.

    mt5linux 0.1.9 merakit pemanggilannya sebagai

        mt5.order_check(*({...},),**{})

    dan bentuk itu ditolak paket MetaTrader5 dengan error -2 "Unnamed arguments
    not allowed", jadi order_check() selalu mengembalikan None lewat backend rpc.
    order_send() di paket yang sama memakai bentuk langsung dan tidak kena.

    Di sini permintaannya dikirim ulang lewat koneksi rpyc di balik wrapper,
    dalam bentuk satu argumen posisional yang memang diterima. Backend local
    (paket MetaTrader5 resmi) tidak lewat sini sama sekali.
    """
    conn = getattr(mt5, "_MetaTrader5__conn", None)
    if conn is None:
        return mt5.order_check(request)
    # repr() dipakai supaya nilai string ter-escape dengan benar; ini mekanisme
    # yang sama dengan yang dipakai mt5linux sendiri untuk order_send().
    return conn.eval("mt5.order_check(%r)" % (request,))


def _print_check(check: Any) -> None:
    margin = getattr(check, "margin", None)
    if margin is not None:
        print("  margin dipakai : %.2f" % margin)
        print("  margin sisa    : %.2f" % getattr(check, "margin_free", 0.0))
        level = getattr(check, "margin_level", 0.0)
        if level:
            print("  margin level   : %.2f%%" % level)


def submit(mt5: Any, request: Dict[str, Any], info: Any, pending: bool,
           dry_run: bool, pakai_filling: bool = True,
           check_wajib: bool = True) -> Optional[Any]:
    """order_check() dulu (sekalian mencari tipe filling yang diterima),
    baru order_send().

    pakai_filling=False untuk permintaan yang tidak punya sisi eksekusi
    (ubah SL/TP, hapus order pending) - di situ type_filling tidak berarti dan
    sebagian broker malah menolak permintaannya.

    check_wajib=False untuk operasi yang hanya mengurangi eksposur (batalkan
    pending): kalau pre-check bermasalah, order tetap dikirim dan check-nya
    cuma jadi peringatan.
    """
    terakhir = None
    if pakai_filling:
        for filling in filling_candidates(mt5, info, pending):
            percobaan = dict(request, type_filling=int(filling))
            check = order_check(mt5, percobaan)
            if check is None:
                code, message = mt5.last_error()
                raise SystemExit("order_check() gagal [%s] %s" % (code, message))
            terakhir = check
            if int(check.retcode) in OK_CHECK:
                request = percobaan
                break
            # Hanya INVALID_FILL yang layak dicoba ulang dengan filling lain.
            if int(check.retcode) != 10030:
                break
    else:
        terakhir = order_check(mt5, request)
        if terakhir is None:
            code, message = mt5.last_error()
            raise SystemExit("order_check() gagal [%s] %s" % (code, message))

    lolos = terakhir is not None and int(terakhir.retcode) in OK_CHECK
    if not lolos:
        code = int(terakhir.retcode) if terakhir is not None else -1
        pesan = getattr(terakhir, "comment", "") if terakhir is not None else ""
        teks = "[%d] %s\n  komentar broker: %s" % (
            code, retcode_text(code), pesan or "-")
        if check_wajib:
            raise SystemExit("Pre-check ditolak " + teks)
        print("Pre-check  : dilewati, broker menolak check " + teks)
    else:
        print("Pre-check  : OK")
        _print_check(terakhir)

    if dry_run:
        print("\n--dry-run: order TIDAK dikirim.")
        return None

    hasil = mt5.order_send(request)
    if hasil is None:
        code, message = mt5.last_error()
        raise SystemExit("order_send() tidak mengembalikan hasil [%s] %s" % (code, message))

    code = int(hasil.retcode)
    if code not in OK_SEND:
        raise SystemExit(
            "Order DITOLAK [%d] %s\n  komentar broker: %s" % (
                code, retcode_text(code), getattr(hasil, "comment", "") or "-"))

    print("\nOrder OK   : [%d] %s" % (code, retcode_text(code)))
    if getattr(hasil, "order", 0):
        print("  ticket order : %s" % hasil.order)
    if getattr(hasil, "deal", 0):
        print("  ticket deal  : %s" % hasil.deal)
    if getattr(hasil, "volume", 0):
        print("  volume terisi: %s" % hasil.volume)
    if getattr(hasil, "price", 0):
        print("  harga        : %s" % hasil.price)
    if code == 10010:
        print("  CATATAN: hanya sebagian volume yang terisi.")
    return hasil


# --------------------------------------------------------------------------
# Perintah: market order
# --------------------------------------------------------------------------

MARKET_TYPES = {"buy": "ORDER_TYPE_BUY", "sell": "ORDER_TYPE_SELL"}

PENDING_TYPES = {
    "buy-limit": "ORDER_TYPE_BUY_LIMIT",
    "sell-limit": "ORDER_TYPE_SELL_LIMIT",
    "buy-stop": "ORDER_TYPE_BUY_STOP",
    "sell-stop": "ORDER_TYPE_SELL_STOP",
}


def cmd_market(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    is_buy = args.command == "buy"
    info = prepare_symbol(mt5, args.symbol)
    guard_symbol_direction(mt5, info, is_buy)

    volume = normalize_volume(args.volume, info)
    guard_volume(volume, tcfg)

    tick = current_tick(mt5, args.symbol)
    harga = normalize_price(float(tick.ask if is_buy else tick.bid), info)
    sl, tp = resolve_sltp(info, harga, is_buy, args.sl, args.tp,
                          args.sl_points, args.tp_points)

    print("Order      : %s %s %.8g lot @ %s (pasar)" % (
        args.command.upper(), args.symbol, volume, harga))
    if volume != args.volume:
        print("  volume dibulatkan TURUN dari %.8g (kelipatan step %s)" % (
            args.volume, info.volume_step))
    print("  SL / TP      : %s / %s" % (sl or "-", tp or "-"))
    print("  spread       : %s poin, deviation %s poin" % (info.spread, args.deviation))

    request = {
        "action": int(mt5.TRADE_ACTION_DEAL),
        "symbol": str(args.symbol),
        "volume": float(volume),
        "type": int(getattr(mt5, MARKET_TYPES[args.command])),
        "price": float(harga),
        "sl": float(sl),
        "tp": float(tp),
        "deviation": int(args.deviation),
        "magic": int(tcfg.magic),
        "comment": str(args.comment)[:31],
        "type_time": int(mt5.ORDER_TIME_GTC),
    }
    submit(mt5, request, info, pending=False, dry_run=args.dry_run)
    return 0


def cmd_pending(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    is_buy = args.command.startswith("buy")
    info = prepare_symbol(mt5, args.symbol)
    guard_symbol_direction(mt5, info, is_buy)

    volume = normalize_volume(args.volume, info)
    guard_volume(volume, tcfg)

    harga = normalize_price(args.price, info)
    tick = current_tick(mt5, args.symbol)
    pasar = float(tick.ask if is_buy else tick.bid)

    # Limit dipasang lebih baik dari harga sekarang, stop lebih buruk. Kebalik =
    # order langsung tereksekusi / ditolak, jadi dicegat di sini.
    limit = args.command.endswith("limit")
    if limit and ((is_buy and harga >= pasar) or (not is_buy and harga <= pasar)):
        raise SystemExit(
            "%s butuh harga %s harga pasar (%s), diberikan %s." % (
                args.command, "di bawah" if is_buy else "di atas", pasar, harga))
    if not limit and ((is_buy and harga <= pasar) or (not is_buy and harga >= pasar)):
        raise SystemExit(
            "%s butuh harga %s harga pasar (%s), diberikan %s." % (
                args.command, "di atas" if is_buy else "di bawah", pasar, harga))

    sl, tp = resolve_sltp(info, harga, is_buy, args.sl, args.tp,
                          args.sl_points, args.tp_points)

    print("Order      : %s %s %.8g lot @ %s (pending, pasar %s)" % (
        args.command.upper(), args.symbol, volume, harga, pasar))
    print("  SL / TP      : %s / %s" % (sl or "-", tp or "-"))

    request = {
        "action": int(mt5.TRADE_ACTION_PENDING),
        "symbol": str(args.symbol),
        "volume": float(volume),
        "type": int(getattr(mt5, PENDING_TYPES[args.command])),
        "price": float(harga),
        "sl": float(sl),
        "tp": float(tp),
        "magic": int(tcfg.magic),
        "comment": str(args.comment)[:31],
        "type_time": int(mt5.ORDER_TIME_GTC),
    }
    submit(mt5, request, info, pending=True, dry_run=args.dry_run)
    return 0


# --------------------------------------------------------------------------
# Perintah: scale-out (beberapa posisi, SL sama, TP bertingkat)
# --------------------------------------------------------------------------

def bagi_volume(total: float, jumlah: int, step: float, vmin: float) -> List[float]:
    """Bagi total volume jadi `jumlah` bagian yang semuanya kelipatan step.

    Sisa pembagian diberikan ke leg-leg AWAL, yaitu yang TP-nya paling dekat.
    Dengan begitu bagian terbesar adalah yang paling cepat diamankan, bukan
    yang paling lama menggantung.
    """
    d = _decimals(step)
    langkah_total = int((Decimal(str(total)) / Decimal(str(step))).to_integral_value(ROUND_DOWN))
    langkah_min = int((Decimal(str(vmin)) / Decimal(str(step))).to_integral_value())

    if langkah_total < langkah_min * jumlah:
        raise SystemExit(
            "Total %.8g lot tidak cukup dibagi %d posisi: tiap bagian minimal "
            "%.8g lot, jadi butuh minimal %.8g lot."
            % (total, jumlah, vmin, vmin * jumlah))

    dasar, sisa = divmod(langkah_total, jumlah)
    bagian = [dasar + (1 if i < sisa else 0) for i in range(jumlah)]
    return [round(float(Decimal(str(step)) * b), d) for b in bagian]


def _urai_daftar(teks: str, nama: str) -> List[float]:
    hasil = []
    for potong in teks.split(","):
        potong = potong.strip()
        if not potong:
            continue
        try:
            hasil.append(float(potong))
        except ValueError:
            raise SystemExit("Nilai %s tidak valid: %r" % (nama, potong))
    if not hasil:
        raise SystemExit("Daftar %s kosong." % nama)
    return hasil


def _harga_leg(entry: float, is_buy: bool, point: float, gap: int,
               mode: str, ke: int) -> float:
    """Harga masuk leg ke-`ke` (0 = leg pertama, selalu harga pasar).

    Arah geser mengikuti arah posisi, bukan atas/bawah layar: limit selalu ke
    sisi yang LEBIH BAIK dari harga sekarang (buy makin murah, sell makin
    mahal), stop selalu ke sisi yang lebih buruk tapi searah tren.
    """
    if ke == 0 or not gap:
        return entry
    arahnya = 1.0 if is_buy else -1.0
    sisi = 1.0 if mode == "stop" else -1.0
    return entry + arahnya * sisi * ke * gap * point


def cmd_scale(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    is_buy = args.arah == "buy"
    info = prepare_symbol(mt5, args.symbol)
    guard_symbol_direction(mt5, info, is_buy)

    tick = current_tick(mt5, args.symbol)
    entry = normalize_price(float(tick.ask if is_buy else tick.bid), info)
    point = float(info.point)
    arahnya = 1.0 if is_buy else -1.0
    d = int(info.digits)

    # --- daftar TP ---
    if args.tp_points:
        tps = [entry + arahnya * p * point for p in _urai_daftar(args.tp_points, "--tp-points")]
    elif args.tp:
        tps = _urai_daftar(args.tp, "--tp")
    else:
        raise SystemExit("Isi --tp atau --tp-points dengan daftar dipisah koma.")

    # Satu TP tidak memberi tahu berapa posisi yang diinginkan, jadi di situlah
    # LEG_DEFAULT dipakai. Daftar TP yang panjangnya sendiri sudah menentukan
    # jumlah posisi dibiarkan apa adanya.
    legs = args.legs
    if legs is None and len(tps) == 1:
        legs = leg_default()

    if legs is not None:
        if legs < 2:
            raise SystemExit(
                "jumlah posisi minimal 2 (diminta %d). Untuk satu posisi pakai "
                "perintah %s biasa." % (legs, args.arah))
        if len(tps) == 1:
            tps = tps * legs
        elif len(tps) != legs:
            raise SystemExit(
                "--legs %d tidak cocok dengan %d nilai TP. Isi satu TP (dipakai "
                "semua posisi) atau tepat %d TP." % (legs, len(tps), legs))

    jumlah = len(tps)
    if jumlah < 2:
        raise SystemExit(
            "scale butuh minimal 2 posisi: isi beberapa TP, atau satu TP "
            "dengan --legs. Untuk satu posisi pakai perintah %s biasa."
            % args.arah)

    # --- SL tunggal, dipakai semua leg ---
    if args.sl_points is not None:
        sl_harga = entry - arahnya * args.sl_points * point
    elif args.sl is not None:
        sl_harga = args.sl
    else:
        raise SystemExit(
            "scale mewajibkan SL: tiga posisi tanpa SL berarti tiga kerugian "
            "yang tidak dibatasi. Isi --sl atau --sl-points.")

    # SL divalidasi sekali saja, lepas dari ada tidaknya TP: dengan semua leg
    # runner (--tp 0) daftar TP nyata kosong dan SL tetap harus diperiksa.
    sl_harga, _ = resolve_sltp(info, entry, is_buy, sl_harga, None, None, None)
    nyata = [t for t in tps if t]
    for t in nyata:
        resolve_sltp(info, entry, is_buy, sl_harga, t, None, None)

    # TP tidak boleh makin DEKAT ke entry, itu membalik urutan scale-out. Sama
    # jauh diperbolehkan: itu beberapa posisi yang berbagi satu TP.
    jarak = [abs(t - entry) for t in nyata]
    if any(b < a for a, b in zip(jarak, jarak[1:])):
        raise SystemExit(
            "Daftar TP tidak boleh makin dekat ke harga masuk. Diberikan: %s"
            % ", ".join("%.*f" % (d, t) for t in nyata))

    tps = [normalize_price(t, info) if t else 0.0 for t in tps]

    # --- harga masuk tiap leg ---
    # Tanpa --gap-points semua leg tembak di harga pasar yang sama. Dengan gap,
    # leg pertama tetap order pasar dan sisanya jadi pending yang menunggu
    # harga datang; arahnya mengikuti buy/sell, bukan atas/bawah layar.
    gap = args.gap_points or 0
    if gap < 0:
        raise SystemExit("--gap-points tidak boleh negatif; pakai --gap-mode "
                         "untuk membalik arah geser.")
    harga = [normalize_price(_harga_leg(entry, is_buy, point, gap, args.gap_mode, i), info)
             for i in range(jumlah)]

    # Tiap leg punya harga masuk sendiri, jadi sisi SL dan TP harus diperiksa
    # terhadap harga leg itu - bukan cuma terhadap harga pasar. Inilah yang
    # menangkap ladder yang terlanjur menembus SL atau melewati TP.
    for i, (h, t) in enumerate(zip(harga, tps), 1):
        try:
            resolve_sltp(info, h, is_buy, sl_harga, t or None, None, None)
        except SystemExit as e:
            raise SystemExit(
                "Leg %d (harga masuk %.*f): %s\n  Perkecil --gap-points, "
                "kurangi jumlah leg, atau lebarkan SL/TP." % (i, d, h, e))

    # --- pembagian volume ---
    volume = normalize_volume(args.volume, info)
    # Batas MT5_MAX_LOT berlaku ke TOTAL, bukan per leg: tiga posisi 1 lot
    # tetap eksposur 3 lot.
    guard_volume(volume, tcfg)
    bagian = bagi_volume(volume, jumlah, float(info.volume_step), float(info.volume_min))
    total_nyata = round(sum(bagian), _decimals(float(info.volume_step)))

    print("Scale-out  : %s %s %d posisi, total %.8g lot @ %.*f" % (
        args.arah.upper(), args.symbol, jumlah, total_nyata, d, entry))
    if total_nyata != args.volume:
        print("  total dibulatkan TURUN dari %.8g (kelipatan step %s)" % (
            args.volume, info.volume_step))
    print("  SL semua leg : %.*f  (%d poin)" % (
        d, sl_harga, round(abs(entry - sl_harga) / point)))
    if gap:
        print("  Jarak antar leg: %d poin, mode %s (leg 1 pasar, sisanya pending)"
              % (gap, args.gap_mode))

    # --- rakit permintaan tiap leg ---
    suffix = "limit" if args.gap_mode == "limit" else "stop"
    permintaan = []
    for i, (vol, tp, h) in enumerate(zip(bagian, tps, harga), 1):
        pending = gap > 0 and i > 1
        if pending:
            tipe = int(getattr(mt5, PENDING_TYPES["%s-%s" % (args.arah, suffix)]))
            aksi = int(mt5.TRADE_ACTION_PENDING)
        else:
            tipe = int(getattr(mt5, MARKET_TYPES[args.arah]))
            aksi = int(mt5.TRADE_ACTION_DEAL)
        req = {
            "action": aksi,
            "symbol": str(args.symbol),
            "volume": float(vol),
            "type": tipe,
            "price": float(h),
            "sl": float(sl_harga),
            "tp": float(tp),
            "magic": int(tcfg.magic),
            "comment": ("%s %d/%d" % (args.comment, i, jumlah))[:31],
            "type_time": int(mt5.ORDER_TIME_GTC),
        }
        # deviation hanya berarti untuk order pasar; pending menunggu harganya
        # sendiri, tidak ada slippage yang perlu dibatasi.
        if not pending:
            req["deviation"] = int(args.deviation)
        permintaan.append((req, pending))

    # --- pre-check SEMUA leg dulu, sebelum satu pun dikirim ---
    print()
    # Filling dicari terpisah untuk pasar dan pending: keduanya punya daftar
    # kandidat yang berbeda, jadi hasil satu jenis tidak boleh dipakai ulang
    # untuk jenis lainnya.
    filling = {True: None, False: None}
    rugi_total = 0.0
    tipe_pasar = int(getattr(mt5, MARKET_TYPES[args.arah]))
    for i, (req, pending) in enumerate(permintaan, 1):
        lolos = None
        kandidat = ([filling[pending]] if filling[pending] is not None
                    else filling_candidates(mt5, info, pending))
        for f in kandidat:
            coba = dict(req, type_filling=int(f))
            cek = order_check(mt5, coba)
            if cek is None:
                code, pesan = mt5.last_error()
                raise SystemExit("order_check() gagal [%s] %s" % (code, pesan))
            if int(cek.retcode) in OK_CHECK:
                lolos, filling[pending] = coba, int(f)
                break
            if int(cek.retcode) != 10030:
                raise SystemExit(
                    "Leg %d ditolak pre-check [%d] %s\n  komentar broker: %s"
                    % (i, int(cek.retcode), retcode_text(int(cek.retcode)),
                       getattr(cek, "comment", "") or "-"))
        if lolos is None:
            raise SystemExit("Leg %d: tidak ada tipe filling yang diterima broker." % i)
        permintaan[i - 1] = (lolos, pending)

        # order_calc_profit hanya mengenal tipe pasar, jadi leg pending pun
        # dihitung sebagai buy/sell biasa dari harga masuknya sendiri.
        h = float(req["price"])
        rugi = mt5.order_calc_profit(tipe_pasar, str(args.symbol),
                                     float(req["volume"]), h, float(sl_harga))
        rugi_total += abs(float(rugi or 0.0))
        tp_teks = "%.*f" % (d, req["tp"]) if req["tp"] else "tanpa TP (runner)"
        untung = ""
        if req["tp"]:
            u = mt5.order_calc_profit(tipe_pasar, str(args.symbol),
                                      float(req["volume"]), h, float(req["tp"]))
            untung = "  TP kena %+.2f" % (u or 0.0)
        print("  leg %d  %.8g lot  %-7s @ %.*f  TP %-16s  SL kena %+.2f%s" % (
            i, req["volume"], "pending" if pending else "pasar", d, h,
            tp_teks, -abs(float(rugi or 0.0)), untung))

    akun = mt5.account_info()
    print()
    print("  Semua leg lolos pre-check.")
    print("  RISIKO TOTAL bila SL kena: %+.2f %s (%.2f%% dari equity %.2f)" % (
        -rugi_total, akun.currency, rugi_total / float(akun.equity) * 100.0,
        akun.equity))
    if gap:
        print("  Angka di atas mengandaikan SEMUA leg terisi. Selama pending "
              "belum kena, risiko nyatanya lebih kecil.")

    if args.dry_run:
        print("\n--dry-run: tidak ada order yang dikirim.")
        return 0

    # --- kirim satu per satu ---
    print()
    berhasil, gagal = [], []
    for i, (req, pending) in enumerate(permintaan, 1):
        hasil = mt5.order_send(req)
        if hasil is None:
            code, pesan = mt5.last_error()
            gagal.append((i, req, pending, "order_send() kosong [%s] %s" % (code, pesan)))
            print("  leg %d  GAGAL  order_send() tidak mengembalikan hasil" % i)
            continue
        code = int(hasil.retcode)
        if code in OK_SEND:
            berhasil.append((i, req, hasil))
            print("  leg %d  OK     %s tiket %s  %.8g lot @ %s" % (
                i, "pending" if pending else "posisi", hasil.order,
                hasil.volume or req["volume"], hasil.price or req["price"]))
        else:
            gagal.append((i, req, pending, "[%d] %s" % (code, retcode_text(code))))
            print("  leg %d  GAGAL  [%d] %s" % (i, code, retcode_text(code)))

    n_pending = sum(1 for _, p in permintaan if p)
    print()
    if not gagal:
        if n_pending:
            print("%d posisi terbuka + %d order pending terpasang. Total %.8g lot "
                  "bila semua terisi." % (jumlah - n_pending, n_pending, total_nyata))
        else:
            print("Semua %d posisi terbuka. Total %.8g lot." % (len(berhasil), total_nyata))
        return 0

    # Sebagian terisi: keadaan ini harus dinyatakan terang-terangan, bukan
    # disembunyikan di balik exit code.
    terisi = round(sum(float(r["volume"]) for _, r, _ in berhasil),
                   _decimals(float(info.volume_step)))
    print("PERINGATAN: %d dari %d leg terkirim. Yang ada di pasar %.8g lot, "
          "bukan %.8g lot." % (len(berhasil), jumlah, terisi, total_nyata))
    if berhasil:
        print("  Leg yang terisi sudah membawa SL, jadi risikonya tetap terbatas.")
    for i, req, pending, sebab in gagal:
        print("  leg %d gagal: %s" % (i, sebab))
        if pending:
            print("    ulangi dengan: python mt5_order.py %s-%s %s %.8g "
                  "--price %.*f --sl %.*f%s" % (
                      args.arah, suffix, args.symbol, req["volume"],
                      d, req["price"], d, sl_harga,
                      "" if not req["tp"] else " --tp %.*f" % (d, req["tp"])))
        else:
            print("    ulangi dengan: python mt5_order.py %s %s %.8g --sl %.*f%s" % (
                args.arah, args.symbol, req["volume"], d, sl_harga,
                "" if not req["tp"] else " --tp %.*f" % (d, req["tp"])))
    return 1


# --------------------------------------------------------------------------
# Perintah: lihat & tutup posisi
# --------------------------------------------------------------------------

def _positions(mt5: Any, symbol: Optional[str] = None,
               ticket: Optional[int] = None) -> List[Any]:
    if ticket is not None:
        hasil = mt5.positions_get(ticket=int(ticket))
    elif symbol:
        hasil = mt5.positions_get(symbol=str(symbol))
    else:
        hasil = mt5.positions_get()
    return list(hasil or ())


def cmd_positions(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    posisi = _positions(mt5, args.symbol)
    if not posisi:
        print("Tidak ada posisi terbuka.")
        return 0

    print("%-13s %-9s %-5s %6s %10s %10s %10s %10s %9s" % (
        "TICKET", "SIMBOL", "TIPE", "VOLUME", "MASUK", "SEKARANG",
        "SL", "TP", "PROFIT"))
    print("-" * 88)

    total = 0.0
    risiko_total = 0.0
    tanpa_sl: List[str] = []
    digit_cache: Dict[str, int] = {}

    for p in posisi:
        simbol = str(p.symbol)
        if simbol not in digit_cache:
            info = mt5.symbol_info(simbol)
            digit_cache[simbol] = int(info.digits) if info is not None else 5
        d = digit_cache[simbol]

        beli = int(p.type) == mt5.POSITION_TYPE_BUY
        arah = "BUY" if beli else "SELL"
        total += float(p.profit)

        sl_teks = "%.*f" % (d, p.sl) if p.sl else "-"
        tp_teks = "%.*f" % (d, p.tp) if p.tp else "-"
        if not p.sl:
            tanpa_sl.append(str(p.ticket))
        else:
            # Berapa yang hilang kalau SL kena, dihitung server broker.
            rugi = mt5.order_calc_profit(
                int(mt5.ORDER_TYPE_BUY if beli else mt5.ORDER_TYPE_SELL),
                simbol, float(p.volume), float(p.price_open), float(p.sl))
            risiko_total += abs(float(rugi or 0.0))

        print("%-13s %-9s %-5s %6.2f %10.*f %10.*f %10s %10s %+9.2f" % (
            p.ticket, simbol, arah, p.volume, d, p.price_open,
            d, p.price_current, sl_teks, tp_teks, p.profit))

    print("-" * 88)
    print("%-68s %+9.2f" % ("TOTAL PROFIT BERJALAN", total))
    if risiko_total:
        print("%-68s %+9.2f" % ("TOTAL RISIKO bila semua SL kena", -risiko_total))
    if tanpa_sl:
        print()
        print("PERINGATAN: posisi tanpa SL: %s" % ", ".join(tanpa_sl))
        print("  Kerugiannya tidak dibatasi apa pun. Pasang SL dengan:")
        print("    python mt5_order.py modify %s --sl <harga>" % tanpa_sl[0])
    return 0


def cmd_orders(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    pesanan = list(mt5.orders_get(symbol=str(args.symbol)) if args.symbol
                   else mt5.orders_get() or ())
    if not pesanan:
        print("Tidak ada order pending.")
        return 0
    print("%-12s %-12s %-16s %8s %11s" % ("TICKET", "SIMBOL", "TIPE", "VOLUME", "HARGA"))
    nama_tipe = {int(getattr(mt5, v)): k.upper() for k, v in PENDING_TYPES.items()}
    for o in pesanan:
        print("%-12s %-12s %-16s %8.2f %11s" % (
            o.ticket, o.symbol, nama_tipe.get(int(o.type), o.type),
            o.volume_current, o.price_open))
    return 0


def _close_one(mt5: Any, pos: Any, volume: Optional[float], tcfg: TradeConfig,
               deviation: int, dry_run: bool) -> None:
    symbol = str(pos.symbol)
    info = prepare_symbol(mt5, symbol)
    tick = current_tick(mt5, symbol)

    buy_pos = int(pos.type) == mt5.POSITION_TYPE_BUY
    # Menutup = mengirim order berlawanan arah pada posisi yang sama.
    tipe = mt5.ORDER_TYPE_SELL if buy_pos else mt5.ORDER_TYPE_BUY
    harga = normalize_price(float(tick.bid if buy_pos else tick.ask), info)

    vol_posisi = float(pos.volume)
    vol = vol_posisi if volume is None else normalize_volume(volume, info)
    if vol > vol_posisi:
        raise SystemExit(
            "Volume close %.8g lebih besar dari volume posisi %.8g." % (vol, vol_posisi))

    sebagian = " (sebagian dari %.8g)" % vol_posisi if vol < vol_posisi else ""
    print("Close      : #%s %s %s %.8g lot @ %s%s" % (
        pos.ticket, symbol, "BUY" if buy_pos else "SELL", vol, harga, sebagian))
    print("  profit berjalan: %.2f" % float(pos.profit))

    request = {
        "action": int(mt5.TRADE_ACTION_DEAL),
        "symbol": symbol,
        "volume": float(vol),
        "type": int(tipe),
        "position": int(pos.ticket),
        "price": float(harga),
        "deviation": int(deviation),
        "magic": int(tcfg.magic),
        "comment": str(tcfg.comment)[:31],
        "type_time": int(mt5.ORDER_TIME_GTC),
    }
    submit(mt5, request, info, pending=False, dry_run=dry_run)


def cmd_close(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    posisi = _positions(mt5, ticket=args.ticket)
    if not posisi:
        raise SystemExit("Posisi #%s tidak ditemukan." % args.ticket)
    _close_one(mt5, posisi[0], args.volume, tcfg, args.deviation, args.dry_run)
    return 0


def cmd_close_all(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    posisi = _positions(mt5, args.symbol)
    if not posisi:
        print("Tidak ada posisi terbuka.")
        return 0
    print("Akan menutup %d posisi.\n" % len(posisi))
    gagal = 0
    for pos in posisi:
        try:
            _close_one(mt5, pos, None, tcfg, args.deviation, args.dry_run)
        except SystemExit as exc:
            # Satu posisi gagal tidak boleh menghentikan sisanya.
            gagal += 1
            print("  GAGAL #%s: %s" % (pos.ticket, exc))
        print()
    if gagal:
        print("%d dari %d posisi gagal ditutup." % (gagal, len(posisi)))
        return 1
    return 0


def cmd_modify(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    if args.sl is None and args.tp is None:
        raise SystemExit("Tidak ada yang diubah: isi --sl dan/atau --tp.")
    posisi = _positions(mt5, ticket=args.ticket)
    if not posisi:
        raise SystemExit("Posisi #%s tidak ditemukan." % args.ticket)
    pos = posisi[0]
    info = prepare_symbol(mt5, str(pos.symbol))
    buy_pos = int(pos.type) == mt5.POSITION_TYPE_BUY
    entry = float(pos.price_open)

    # Nilai yang tidak disebut dipertahankan apa adanya.
    sl = float(pos.sl) if args.sl is None else float(args.sl)
    tp = float(pos.tp) if args.tp is None else float(args.tp)
    sl, tp = resolve_sltp(info, entry, buy_pos, sl, tp, None, None)

    print("Modify     : #%s %s  SL %s -> %s  TP %s -> %s" % (
        pos.ticket, pos.symbol, pos.sl or "-", sl or "-", pos.tp or "-", tp or "-"))

    request = {
        "action": int(mt5.TRADE_ACTION_SLTP),
        "symbol": str(pos.symbol),
        "position": int(pos.ticket),
        "sl": float(sl),
        "tp": float(tp),
        "magic": int(tcfg.magic),
    }
    submit(mt5, request, info, pending=False, dry_run=args.dry_run,
           pakai_filling=False)
    return 0


def cmd_cancel(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    pesanan = list(mt5.orders_get(ticket=int(args.ticket)) or ())
    if not pesanan:
        raise SystemExit("Order pending #%s tidak ditemukan." % args.ticket)
    order = pesanan[0]
    info = prepare_symbol(mt5, str(order.symbol))
    print("Cancel     : #%s %s %.8g lot @ %s" % (
        order.ticket, order.symbol, order.volume_current, order.price_open))
    request = {
        "action": int(mt5.TRADE_ACTION_REMOVE),
        "order": int(order.ticket),
    }
    submit(mt5, request, info, pending=True, dry_run=args.dry_run,
           pakai_filling=False, check_wajib=False)
    return 0


def cmd_quote(mt5: Any, args: argparse.Namespace, tcfg: TradeConfig) -> int:
    """Lihat aturan simbol sebelum order: batas volume, digit, jarak stop."""
    info = prepare_symbol(mt5, args.symbol)
    tick = current_tick(mt5, args.symbol)
    mode = {0: "DISABLED", 1: "LONG-ONLY", 2: "SHORT-ONLY",
            3: "CLOSE-ONLY", 4: "FULL"}.get(int(info.trade_mode), str(info.trade_mode))
    fill = int(getattr(info, "filling_mode", 0) or 0)
    fill_txt = ", ".join([n for b, n in ((1, "FOK"), (2, "IOC")) if fill & b]) or "-"

    print("Simbol     : %s (%s)" % (info.name, info.description))
    print("Bid / Ask  : %s / %s   spread %s poin" % (tick.bid, tick.ask, info.spread))
    print("Digits     : %s   point %s" % (info.digits, info.point))
    print("Volume     : min %s  max %s  step %s" % (
        info.volume_min, info.volume_max, info.volume_step))
    print("Stops level: %s poin (jarak minimum SL/TP dari harga)" % info.trade_stops_level)
    print("Trade mode : %s   filling: %s" % (mode, fill_txt))
    print("Contract   : %s   margin 1 lot: %s" % (
        info.trade_contract_size,
        mt5.order_calc_margin(int(mt5.ORDER_TYPE_BUY), str(args.symbol), 1.0,
                              float(tick.ask)) or "-"))
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mt5_order.py",
        description="Kirim order ke MetaTrader 5.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=CONTOH,
    )
    sub = p.add_subparsers(dest="command", required=True)

    def tambah_umum(sp, dengan_deviation=True):
        sp.add_argument("--dry-run", action="store_true",
                        help="berhenti setelah order_check, jangan kirim")
        sp.add_argument("--yes", action="store_true",
                        help="lewati konfirmasi interaktif akun REAL")
        if dengan_deviation:
            sp.add_argument("--deviation", type=int,
                            default=int(os.getenv("MT5_DEVIATION", "20")),
                            help="slippage maksimum dalam poin (default: %(default)s)")
        return sp

    def tambah_sltp(sp):
        sp.add_argument("--sl", type=float, help="stop loss sebagai harga absolut")
        sp.add_argument("--tp", type=float, help="take profit sebagai harga absolut")
        sp.add_argument("--sl-points", type=int, help="stop loss dalam poin dari harga masuk")
        sp.add_argument("--tp-points", type=int, help="take profit dalam poin dari harga masuk")
        sp.add_argument("--comment", default=os.getenv("MT5_COMMENT", "mt5_order.py"),
                        help="komentar order (maks 31 karakter)")
        return sp

    for nama in MARKET_TYPES:
        sp = sub.add_parser(nama, help="order %s harga pasar" % nama)
        sp.add_argument("symbol")
        sp.add_argument("volume", type=float, help="ukuran lot")
        tambah_sltp(tambah_umum(sp))
        sp.set_defaults(func=cmd_market)

    for nama in PENDING_TYPES:
        sp = sub.add_parser(nama, help="pasang order pending %s" % nama)
        sp.add_argument("symbol")
        sp.add_argument("volume", type=float, help="ukuran lot")
        sp.add_argument("--price", type=float, required=True, help="harga aktivasi")
        tambah_sltp(tambah_umum(sp, dengan_deviation=False))
        sp.set_defaults(func=cmd_pending)

    sp = sub.add_parser(
        "scale",
        help="buka beberapa posisi sekaligus: SL sama, TP bertingkat",
        description="Bagi satu ukuran posisi jadi beberapa tiket terpisah dengan "
                    "SL sama tapi TP berbeda, supaya sebagian profit bisa "
                    "diamankan lebih dulu sementara sisanya jalan terus. "
                    "Butuh akun hedging; di akun netting ketiganya akan "
                    "digabung broker jadi satu posisi.")
    sp.add_argument("arah", choices=("buy", "sell"))
    sp.add_argument("symbol")
    sp.add_argument("volume", type=float, help="TOTAL lot, dibagi rata ke semua leg")
    sp.add_argument("--tp", help="daftar harga TP dipisah koma, 0 = leg tanpa TP")
    sp.add_argument("--tp-points", help="daftar jarak TP dalam poin, dipisah koma")
    sp.add_argument("--gap-points", type=int,
                    help="jarak antar harga masuk dalam poin; leg 1 tetap order "
                         "pasar, leg berikutnya jadi pending. Tanpa ini semua "
                         "leg tembak di harga pasar yang sama")
    sp.add_argument("--gap-mode", choices=("limit", "stop"), default="limit",
                    help="arah geser: limit = ke harga lebih baik (buy makin "
                         "murah, sell makin mahal), stop = searah tren "
                         "(default: %(default)s)")
    sp.add_argument("--legs", type=int,
                    help="jumlah posisi bila SEMUA berbagi TP yang sama; "
                         "dipakai saat --tp / --tp-points hanya berisi satu "
                         "nilai (default: %d)" % leg_default())
    sp.add_argument("--sl", type=float, help="SL sebagai harga, dipakai semua leg")
    sp.add_argument("--sl-points", type=int, help="SL dalam poin, dipakai semua leg")
    tambah_umum(sp)
    sp.add_argument("--comment", default=os.getenv("MT5_COMMENT", "mt5_order.py"),
                    help="komentar order, otomatis diberi penanda leg ke-berapa")
    sp.set_defaults(func=cmd_scale)

    sp = sub.add_parser("quote", help="lihat harga & aturan trading sebuah simbol")
    sp.add_argument("symbol", nargs="?", default=os.getenv("MT5_SYMBOL", "EURUSD"))
    sp.set_defaults(func=cmd_quote)

    sp = sub.add_parser("positions", help="daftar posisi terbuka")
    sp.add_argument("--symbol", help="saring berdasarkan simbol")
    sp.set_defaults(func=cmd_positions)

    sp = sub.add_parser("orders", help="daftar order pending")
    sp.add_argument("--symbol", help="saring berdasarkan simbol")
    sp.set_defaults(func=cmd_orders)

    sp = sub.add_parser("close", help="tutup satu posisi")
    sp.add_argument("ticket", type=int)
    sp.add_argument("--volume", type=float, help="tutup sebagian (default: seluruhnya)")
    tambah_umum(sp)
    sp.set_defaults(func=cmd_close)

    sp = sub.add_parser("close-all", help="tutup semua posisi")
    sp.add_argument("--symbol", help="batasi ke satu simbol")
    tambah_umum(sp)
    sp.set_defaults(func=cmd_close_all)

    sp = sub.add_parser("modify", help="ubah SL/TP posisi terbuka")
    sp.add_argument("ticket", type=int)
    sp.add_argument("--sl", type=float)
    sp.add_argument("--tp", type=float)
    tambah_umum(sp, dengan_deviation=False)
    sp.set_defaults(func=cmd_modify)

    sp = sub.add_parser("cancel", help="batalkan order pending")
    sp.add_argument("ticket", type=int)
    tambah_umum(sp, dengan_deviation=False)
    sp.set_defaults(func=cmd_cancel)

    return p


# Perintah yang cuma membaca: tidak perlu pengaman akun REAL.
BACA_SAJA = {"positions", "orders", "quote"}


def main(argv: Optional[List[str]] = None) -> int:
    # load_env() harus mendahului build_parser(): sebagian default argparse
    # dibaca dari os.getenv saat parser dirakit, jadi kalau .env belum masuk
    # nilai di .env akan kalah oleh fallback yang ditulis di kode.
    load_env()
    args = build_parser().parse_args(argv)
    cfg = Config()
    tcfg = TradeConfig()

    with connect(cfg) as mt5:
        if args.command not in BACA_SAJA:
            guard_account(mt5, tcfg, getattr(args, "yes", False))
            print()
        return args.func(mt5, args, tcfg)


if __name__ == "__main__":
    sys.exit(main())

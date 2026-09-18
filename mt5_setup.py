#!/usr/bin/env python3
"""Hitung harga entry, stop loss, take profit dan ukuran lot dari tren berjalan.

    python mt5_setup.py EURUSD
    python mt5_setup.py EURUSD --arah sell          # paksa arah, abaikan tren
    python mt5_setup.py EURUSD --risiko 0.5 --rr 3
    python mt5_setup.py EURUSD --tf-setup M15
    python mt5_setup.py EURUSD GBPUSD XAUUSD
    python mt5_setup.py --posisi

Cara angkanya didapat:

    arah  : dari kesimpulan mt5_trend.py antar timeframe. Kalau tidak jelas,
            tidak ada setup yang dikeluarkan kecuali arahnya dipaksa --arah.
    SL    : di luar swing terakhir pada timeframe setup, ditambah bantalan
            sekian ATR. Stop yang lebih sempit dari gerak normal pasar akan
            kena hanya karena riak biasa, jadi ada lantai minimal sekian ATR
            juga, plus jarak minimum yang diwajibkan broker.
    TP    : kelipatan risk-reward dari jarak SL. TP1 default 1:2, TP2 1:3.
    lot   : dari risiko rupiah/dolar yang diizinkan dibagi kerugian kalau SL
            kena, lalu dibulatkan TURUN ke step volume broker.

Skrip ini TIDAK mengirim order. Hasilnya berupa baris perintah mt5_order.py
yang tinggal disalin, supaya keputusan mengeksekusi tetap di tangan Anda.

PENTING: angka-angka ini turunan mekanis dari harga yang sudah lewat, bukan
ramalan. ATR, EMA dan swing semuanya indikator lagging - mereka menggambarkan
apa yang sudah terjadi. Tidak ada di sini yang tahu ke mana harga akan pergi.
"""

import argparse
import math
import os
import sys
from decimal import ROUND_DOWN, Decimal
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

import mt5_trend
from mt5_client import Config, connect, load_env
from mt5_order import (TradeConfig, current_tick, leg_default, normalize_price,
                       prepare_symbol)
from mt5_trend import Setelan, atr, ema, kesimpulan, nilai_timeframe


# --------------------------------------------------------------------------
# Setelan
# --------------------------------------------------------------------------

class SetelanSetup:
    def __init__(self) -> None:
        self.tf_setup = os.getenv("MT5_SETUP_TF", "H1").upper()
        self.risiko_persen = float(os.getenv("MT5_RISIKO_PERSEN", "1.0"))
        self.rr1 = float(os.getenv("MT5_RR", "2.0"))
        self.rr2 = float(os.getenv("MT5_RR2", "3.0"))
        self.rr3 = float(os.getenv("MT5_RR3", "4.0"))
        self.sl_atr_bantalan = float(os.getenv("MT5_SL_ATR_BANTALAN", "0.3"))
        self.sl_atr_minimal = float(os.getenv("MT5_SL_ATR_MINIMAL", "1.0"))
        # Plafon lebar SL dalam kelipatan ATR. <= 0 berarti tanpa plafon.
        self.sl_atr_maksimal = float(os.getenv("MT5_SL_ATR_MAKSIMAL", "2.0"))
        if 0 < self.sl_atr_maksimal < self.sl_atr_minimal:
            raise SystemExit(
                "MT5_SL_ATR_MAKSIMAL (%.2f) tidak boleh lebih kecil dari "
                "MT5_SL_ATR_MINIMAL (%.2f)."
                % (self.sl_atr_maksimal, self.sl_atr_minimal))
        self.swing_bar = int(os.getenv("MT5_SWING_BAR", "20"))
        # Jarak antar harga masuk pada perintah scale, dalam kelipatan ATR.
        self.gap_atr = float(os.getenv("MT5_SCALE_GAP_ATR", "0.25"))


# --------------------------------------------------------------------------
# Bantuan hitung
# --------------------------------------------------------------------------

def _desimal(nilai: float) -> int:
    return max(0, -int(Decimal(str(nilai)).normalize().as_tuple().exponent))


def bulatkan_lot_turun(volume: float, step: float) -> float:
    """Selalu ke bawah: pembulatan naik berarti mengambil risiko lebih besar
    dari yang diminta, dan itu justru yang mau dihindari di sini."""
    langkah = (Decimal(str(volume)) / Decimal(str(step))).to_integral_value(ROUND_DOWN)
    return round(float(Decimal(str(step)) * langkah), _desimal(step))


def swing(high: np.ndarray, low: np.ndarray, bar: int) -> Tuple[float, float]:
    """Puncak tertinggi dan lembah terendah `bar` batang terakhir."""
    bar = min(bar, len(high))
    return float(high[-bar:].max()), float(low[-bar:].min())


def rugi_per_lot(mt5: Any, symbol: str, tipe_order: int,
                 entry: float, sl: float) -> Optional[float]:
    """Kerugian 1 lot kalau harga bergerak dari entry ke SL, menurut broker.

    Sengaja memakai order_calc_profit(), bukan hitungan manual dari
    trade_tick_value. Pada broker ini trade_tick_value XAUUSD (0.1 per tick
    0.01) tidak konsisten dengan contract size 100 oz: gerak 1.00 dolar per
    ons pada 1 lot sebenarnya 100 dolar, bukan 10. Menghitung lot dari
    tick_value akan membuat posisi emas sepuluh kali lebih besar dari risiko
    yang diniatkan. order_calc_profit() dihitung server dan cocok di kedua
    simbol waktu diuji.
    """
    hasil = mt5.order_calc_profit(int(tipe_order), str(symbol), 1.0,
                                  float(entry), float(sl))
    if hasil is None:
        return None
    return float(hasil)


# --------------------------------------------------------------------------
# Penyusunan setup
# --------------------------------------------------------------------------

class Setup:
    def __init__(self) -> None:
        self.arah = ""            # "buy" / "sell"
        self.alasan_arah = ""
        self.entry = 0.0
        self.entry_limit: Optional[float] = None
        self.ket_limit = ""
        self.sl = 0.0
        self.ket_sl = ""
        self.tp1 = 0.0
        self.tp2 = 0.0
        self.tp3 = 0.0
        self.jarak_sl = 0.0
        self.atr = float("nan")
        self.lot = 0.0
        self.risiko_uang = 0.0
        self.rugi_sl = 0.0
        self.untung_tp1 = 0.0
        self.untung_tp2 = 0.0
        self.hedging = False
        self.rr_limit: Optional[float] = None
        self.rugi_sl_limit: Optional[float] = None
        self.catatan: List[str] = []


def susun_setup(mt5: Any, symbol: str, info: Any, arah: str,
                st: Setelan, ss: SetelanSetup, tcfg: TradeConfig) -> Setup:
    s = Setup()
    s.arah = arah
    beli = arah == "buy"

    tf = mt5_trend.resolve_timeframe(mt5, ss.tf_setup)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, st.bar)
    if rates is None or len(rates) < max(st.ema_lambat, ss.swing_bar) + 2:
        raise SystemExit(
            "Data %s di timeframe %s tidak cukup untuk menyusun setup."
            % (symbol, ss.tf_setup))

    high = np.asarray(rates["high"], dtype=float)
    low = np.asarray(rates["low"], dtype=float)
    close = np.asarray(rates["close"], dtype=float)

    s.atr = atr(high, low, close, st.adx_periode)
    if math.isnan(s.atr) or s.atr <= 0:
        raise SystemExit("ATR %s tidak bisa dihitung untuk %s." % (ss.tf_setup, symbol))

    tick = current_tick(mt5, symbol)
    s.entry = normalize_price(float(tick.ask if beli else tick.bid), info)

    puncak, lembah = swing(high, low, ss.swing_bar)
    bantalan = ss.sl_atr_bantalan * s.atr

    # --- stop loss: di luar struktur, lalu disaring tiga lantai ---
    if beli:
        sl_mentah = lembah - bantalan
    else:
        sl_mentah = puncak + bantalan
    s.ket_sl = "%s %d bar %s %.1f ATR" % (
        "swing low" if beli else "swing high", ss.swing_bar,
        "-" if beli else "+", ss.sl_atr_bantalan)

    # Plafon: struktur yang kebetulan jauh (swing low 5 jam lalu) bisa
    # melahirkan SL selebar puluhan dolar, dan TP ikut melar karena kelipatan
    # RR. Di sini jaraknya dipotong. Dipasang sebelum lantai karena plafon
    # hanya memperkecil jarak dan lantai hanya memperbesar, jadi lantai tetap
    # jadi kata terakhir.
    if ss.sl_atr_maksimal > 0:
        maksimal_atr = ss.sl_atr_maksimal * s.atr
        if abs(s.entry - sl_mentah) > maksimal_atr:
            jauh = abs(s.entry - sl_mentah)
            sl_mentah = s.entry - maksimal_atr if beli else s.entry + maksimal_atr
            s.ket_sl = "%.1f ATR dari entry (plafon; struktur %.0f poin jauhnya)" % (
                ss.sl_atr_maksimal, jauh / float(info.point))
            s.catatan.append(
                "SL dipotong ke plafon %.1f ATR. Stop TIDAK lagi di luar swing, "
                "jadi lebih mudah tersentuh riak pasar." % ss.sl_atr_maksimal)

    # Lantai 1: minimal sekian ATR dari entry, supaya stop tidak lebih sempit
    # dari gerak normal pasar.
    minimal_atr = ss.sl_atr_minimal * s.atr
    if abs(s.entry - sl_mentah) < minimal_atr:
        sl_mentah = s.entry - minimal_atr if beli else s.entry + minimal_atr
        s.ket_sl = "%.1f ATR dari entry (struktur terlalu dekat)" % ss.sl_atr_minimal

    # Lantai 2: jarak minimum yang diwajibkan broker.
    stops = int(getattr(info, "trade_stops_level", 0) or 0)
    if stops:
        minimal_broker = stops * float(info.point)
        if abs(s.entry - sl_mentah) < minimal_broker:
            sl_mentah = s.entry - minimal_broker if beli else s.entry + minimal_broker
            s.ket_sl = "jarak minimum broker %d poin" % stops
            s.catatan.append(
                "SL dilebarkan ke jarak minimum broker (%d poin)." % stops)

    s.sl = normalize_price(sl_mentah, info)
    s.jarak_sl = abs(s.entry - s.sl)
    if s.jarak_sl <= 0:
        raise SystemExit("Jarak SL nol setelah pembulatan; simbol ini butuh setelan lain.")

    # --- take profit dari kelipatan risk-reward ---
    arahnya = 1.0 if beli else -1.0
    s.tp1 = normalize_price(s.entry + arahnya * ss.rr1 * s.jarak_sl, info)
    s.tp2 = normalize_price(s.entry + arahnya * ss.rr2 * s.jarak_sl, info)
    s.tp3 = normalize_price(s.entry + arahnya * ss.rr3 * s.jarak_sl, info)

    # --- entry limit: pullback ke EMA cepat, kalau harga belum melewatinya ---
    ema_cepat = ema(close, st.ema_cepat)[-1]
    layak = (beli and ema_cepat < s.entry) or (not beli and ema_cepat > s.entry)
    if layak:
        s.entry_limit = normalize_price(float(ema_cepat), info)
        s.ket_limit = "pullback ke EMA%d %s" % (st.ema_cepat, ss.tf_setup)
    else:
        s.ket_limit = "tidak ada, harga sudah di sisi lain EMA%d" % st.ema_cepat

    # --- ukuran lot dari risiko ---
    akun = mt5.account_info()
    # Scale-out hanya berdiri sendiri-sendiri di akun hedging; di akun netting
    # broker menggabung semua leg jadi satu posisi.
    s.hedging = int(akun.margin_mode) == int(mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
    s.risiko_uang = float(akun.equity) * ss.risiko_persen / 100.0
    tipe = int(mt5.ORDER_TYPE_BUY if beli else mt5.ORDER_TYPE_SELL)

    per_lot = rugi_per_lot(mt5, symbol, tipe, s.entry, s.sl)
    if per_lot is None or per_lot >= 0:
        code, pesan = mt5.last_error()
        raise SystemExit(
            "Broker tidak bisa menghitung rugi per lot untuk %s [%s] %s"
            % (symbol, code, pesan))

    lot_mentah = s.risiko_uang / abs(per_lot)
    s.lot = bulatkan_lot_turun(lot_mentah, float(info.volume_step))

    vmin, vmax = float(info.volume_min), float(info.volume_max)
    if s.lot < vmin:
        s.catatan.append(
            "Lot hasil hitungan %.4f di bawah minimum broker %.2f. Memakai %.2f "
            "berarti risikonya jadi %.3g%% dari equity, bukan %.3g%%."
            % (lot_mentah, vmin, vmin,
               abs(per_lot) * vmin / float(akun.equity) * 100.0, ss.risiko_persen))
        s.lot = 0.0
    elif s.lot > vmax:
        s.catatan.append("Lot dibatasi ke maksimum broker %.2f." % vmax)
        s.lot = vmax

    if s.lot and s.lot > tcfg.max_lot:
        s.catatan.append(
            "Lot %.2f melewati MT5_MAX_LOT=%.2f, dipotong ke batas itu. "
            "Risiko sebenarnya jadi lebih kecil dari %.3g%%."
            % (s.lot, tcfg.max_lot, ss.risiko_persen))
        s.lot = bulatkan_lot_turun(tcfg.max_lot, float(info.volume_step))

    if s.lot:
        s.rugi_sl = abs(per_lot) * s.lot
        untung1 = rugi_per_lot(mt5, symbol, tipe, s.entry, s.tp1)
        untung2 = rugi_per_lot(mt5, symbol, tipe, s.entry, s.tp2)
        s.untung_tp1 = (untung1 or 0.0) * s.lot
        s.untung_tp2 = (untung2 or 0.0) * s.lot

    # SL dan TP di atas diukur dari harga PASAR. Kalau masuknya lewat entry
    # limit, jaraknya berubah - risikonya mengecil dan RR-nya membesar - jadi
    # angka sebenarnya disebutkan daripada dibiarkan tersirat.
    if s.entry_limit is not None:
        jarak_limit = abs(s.entry_limit - s.sl)
        if jarak_limit > 0:
            s.rr_limit = abs(s.tp1 - s.entry_limit) / jarak_limit
            if s.lot:
                rugi_limit = rugi_per_lot(mt5, symbol, tipe, s.entry_limit, s.sl)
                s.rugi_sl_limit = abs(rugi_limit or 0.0) * s.lot
    return s


# --------------------------------------------------------------------------
# Tampilan
# --------------------------------------------------------------------------

def poin(jarak: float, info: Any) -> int:
    return int(round(jarak / float(info.point)))


def tampilkan(symbol: str, info: Any, tick: Any, hasil_tf: Sequence[Any],
              label: str, normal: float, catatan: str,
              s: Optional[Setup], ss: SetelanSetup, akun: Any,
              pakai_warna: bool) -> None:
    d = int(info.digits)
    print("=" * 80)
    print("%s   bid %.*f / ask %.*f" % (symbol, d, tick.bid, d, tick.ask))
    print("=" * 80)

    ringkas = "  ".join("%s %s" % (h.nama, h.label) for h in hasil_tf if h.tersedia)
    print("TREN")
    print("  " + ringkas)
    print("  " + mt5_trend.warnai("Kesimpulan: %s (%+.0f%%)" % (label, normal * 100),
                                  label, pakai_warna))
    for potong in catatan.split(" | "):
        if potong:
            print("    - %s" % potong)
    print()

    if s is None:
        print("Tidak ada setup: arah antar timeframe belum jelas.")
        print("Pakai --arah buy atau --arah sell kalau tetap mau dihitung.")
        print()
        return

    judul = "SETUP " + s.arah.upper()
    print(mt5_trend.warnai(
        "%s   (dasar %s, ATR%d %.*f = %d poin)" % (
            judul, ss.tf_setup, 14, d, s.atr, poin(s.atr, info)),
        "BULLISH" if s.arah == "buy" else "BEARISH", pakai_warna))
    print("  %-14s %.*f" % ("Entry pasar", d, s.entry))
    if s.entry_limit is not None:
        print("  %-14s %.*f   (%s)" % ("Entry limit", d, s.entry_limit, s.ket_limit))
        if s.rr_limit is not None:
            tambahan = ""
            if s.rugi_sl_limit is not None:
                tambahan = ", rugi bila SL kena %+.2f %s" % (
                    -abs(s.rugi_sl_limit), akun.currency)
            print("  %-14s SL/TP di bawah diukur dari harga pasar; kalau terisi di"
                  % "")
            print("  %-14s limit ini jaraknya %d poin dan RR jadi 1:%.2g%s" % (
                "", poin(abs(s.entry_limit - s.sl), info), s.rr_limit, tambahan))
    else:
        print("  %-14s %s" % ("Entry limit", s.ket_limit))
    print("  %-14s %.*f   %4d poin   (%s)" % (
        "Stop loss", d, s.sl, poin(s.jarak_sl, info), s.ket_sl))
    print("  %-14s %.*f   %4d poin   RR 1:%.3g" % (
        "Take profit 1", d, s.tp1, poin(abs(s.tp1 - s.entry), info), ss.rr1))
    print("  %-14s %.*f   %4d poin   RR 1:%.3g" % (
        "Take profit 2", d, s.tp2, poin(abs(s.tp2 - s.entry), info), ss.rr2))
    print("  %-14s %.*f   %4d poin   RR 1:%.3g" % (
        "Take profit 3", d, s.tp3, poin(abs(s.tp3 - s.entry), info), ss.rr3))
    print()

    mata = akun.currency
    print("UKURAN LOT   risiko %.3g%% dari equity %.2f %s = %.2f %s" % (
        ss.risiko_persen, akun.equity, mata, s.risiko_uang, mata))
    if s.lot:
        print("  %.2f lot" % s.lot)
        print("      SL kena  : %+.2f %s" % (-abs(s.rugi_sl), mata))
        print("      TP1 kena : %+.2f %s" % (s.untung_tp1, mata))
        print("      TP2 kena : %+.2f %s" % (s.untung_tp2, mata))
    else:
        print("  tidak bisa dihitung, lihat catatan di bawah")
    print()

    for c in s.catatan:
        print("  CATATAN: %s" % c)
    if s.catatan:
        print()

    if s.lot:
        print("PERINTAH SIAP PAKAI")
        print("  .venv/bin/python mt5_order.py %s %s %.2f --sl %.*f --tp %.*f" % (
            s.arah, symbol, s.lot, d, s.sl, d, s.tp1))
        if s.entry_limit is not None:
            print("  .venv/bin/python mt5_order.py %s-limit %s %.2f --price %.*f --sl %.*f --tp %.*f" % (
                s.arah, symbol, s.lot, d, s.entry_limit, d, s.sl, d, s.tp1))
        # Scale-out hanya masuk akal di akun hedging, dan lotnya harus cukup
        # dibagi rata. Dua varian ditawarkan: TP seragam (semua leg keluar
        # bersamaan, gunanya untuk dikelola manual per tiket) dan TP bertingkat
        # (sebagian profit diamankan lebih dulu, sisanya jalan terus).
        n_leg = leg_default()
        if s.hedging and n_leg >= 2 and s.lot >= n_leg * float(info.volume_min):
            # Jarak antar leg diturunkan dari ATR supaya ikut volatilitas, lalu
            # dibatasi: seluruh ladder tidak boleh memakan lebih dari separuh
            # jarak ke SL, kalau tidak leg terakhir masuk nyaris tanpa ruang.
            batas = 0.5 * s.jarak_sl / (n_leg - 1)
            gap_poin = poin(min(ss.gap_atr * s.atr, batas), info)
            opsi = " --gap-points %d" % gap_poin if gap_poin >= 1 else ""
            print("  .venv/bin/python mt5_order.py scale %s %s %.2f --sl %.*f "
                  "--tp %.*f%s" % (
                      s.arah, symbol, s.lot, d, s.sl, d, s.tp1, opsi))
            if opsi:
                print("      ^ %d posisi, SL dan TP sama, entry bertingkat %d poin "
                      "(%.2f x ATR, dibatasi \u00bd jarak SL)"
                      % (n_leg, gap_poin, ss.gap_atr))
            else:
                print("      ^ %d posisi terpisah, SL dan TP sama semua "
                      "(MT5_SCALE_LEGS=%d)" % (n_leg, n_leg))
        if s.hedging and s.lot >= 3 * float(info.volume_min):
            print("  .venv/bin/python mt5_order.py scale %s %s %.2f --sl %.*f "
                  "--tp %.*f,%.*f,%.*f" % (
                      s.arah, symbol, s.lot, d, s.sl, d, s.tp1, d, s.tp2, d, s.tp3))
            print("      ^ 3 posisi terpisah, SL sama, TP bertingkat")
        print("  (tambahkan --dry-run untuk mengecek tanpa mengirim)")
    print()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ss = SetelanSetup()
    p = argparse.ArgumentParser(
        prog="mt5_setup.py",
        description="Hitung entry, SL, TP dan ukuran lot dari tren berjalan.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""contoh:
    python mt5_setup.py EURUSD
    python mt5_setup.py EURUSD --arah sell
    python mt5_setup.py EURUSD --risiko 0.5 --rr 3
    python mt5_setup.py EURUSD --tf-setup M15
    python mt5_setup.py --posisi""")
    p.add_argument("symbols", nargs="*")
    p.add_argument("--arah", choices=("buy", "sell"),
                   help="paksa arah, abaikan kesimpulan tren")
    p.add_argument("--tf-setup", default=ss.tf_setup,
                   help="timeframe dasar ATR & swing (default: %(default)s)")
    p.add_argument("--timeframes", "-t",
                   default=os.getenv("MT5_TREND_TF", mt5_trend.TF_DEFAULT),
                   help="timeframe untuk membaca arah (default: %(default)s)")
    p.add_argument("--risiko", type=float, default=ss.risiko_persen,
                   help="persen equity yang dipertaruhkan (default: %(default)s)")
    p.add_argument("--rr", type=float, default=ss.rr1,
                   help="risk-reward TP1 (default: %(default)s)")
    p.add_argument("--rr2", type=float, default=ss.rr2,
                   help="risk-reward TP2 (default: %(default)s)")
    p.add_argument("--posisi", action="store_true",
                   help="pakai simbol dari posisi yang sedang terbuka")
    p.add_argument("--no-color", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # load_env() harus mendahului build_parser(): sebagian default argparse
    # dibaca dari os.getenv saat parser dirakit, jadi kalau .env belum masuk
    # nilai di .env akan kalah oleh fallback yang ditulis di kode.
    load_env()
    args = build_parser().parse_args(argv)
    cfg = Config()
    st = Setelan()
    ss = SetelanSetup()
    ss.tf_setup = args.tf_setup.upper()
    ss.risiko_persen = args.risiko
    ss.rr1, ss.rr2 = args.rr, args.rr2
    tcfg = TradeConfig()

    nama_tf = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
    nama_tf.sort(key=lambda t: mt5_trend.TF_MENIT.get(t, 0))
    pakai_warna = sys.stdout.isatty() and not args.no_color

    with connect(cfg) as mt5:
        akun = mt5.account_info()
        symbols = list(args.symbols)
        if args.posisi:
            posisi = mt5.positions_get() or ()
            dari = sorted({str(p.symbol) for p in posisi})
            if not dari:
                print("Tidak ada posisi terbuka.")
                return 0
            symbols.extend(x for x in dari if x not in symbols)
        if not symbols:
            symbols = [cfg.symbol]

        for symbol in symbols:
            info = prepare_symbol(mt5, symbol)
            tick = current_tick(mt5, symbol)
            hasil_tf = [nilai_timeframe(mt5, symbol, tf, st) for tf in nama_tf]
            label, normal, catatan = kesimpulan(hasil_tf, st)

            arah = args.arah
            if arah is None:
                if label.startswith("BULLISH"):
                    arah = "buy"
                elif label.startswith("BEARISH"):
                    arah = "sell"

            s = None
            if arah is not None:
                s = susun_setup(mt5, symbol, info, arah, st, ss, tcfg)
                if args.arah is not None and not label.startswith(arah.upper()[:4]):
                    s.catatan.append(
                        "Arah %s dipaksa lewat --arah, sedangkan tren menunjukkan %s."
                        % (arah.upper(), label))
            tampilkan(symbol, info, tick, hasil_tf, label, normal, catatan,
                      s, ss, akun, pakai_warna)
    return 0


if __name__ == "__main__":
    sys.exit(main())

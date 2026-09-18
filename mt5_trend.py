#!/usr/bin/env python3
"""Baca arah tren (bullish / bearish / sideways) di beberapa timeframe sekaligus.

    python mt5_trend.py EURUSD
    python mt5_trend.py EURUSD GBPUSD XAUUSD
    python mt5_trend.py EURUSD --timeframes M5,M15,H1,H4,D1
    python mt5_trend.py EURUSD --detail

Tiap timeframe dinilai lewat empat sinyal yang saling melengkapi, masing-masing
bernilai +1 (bullish), -1 (bearish) atau 0 (netral):

    1. EMA cepat vs EMA lambat  - arah tren menengah
    2. harga penutupan vs EMA lambat - posisi harga terhadap tren
    3. kemiringan EMA lambat - tren sedang menguat atau melemah
    4. struktur swing (HH/HL vs LH/LL) - cara harga membentuk puncak & lembah

Skor -4..+4 itu lalu disaring ADX: kalau ADX di bawah ambang, pasar dianggap
SIDEWAYS berapa pun skornya, karena keempat sinyal di atas gampang menipu waktu
harga bergerak menyamping.

Kesimpulan antar-timeframe dibobot: timeframe besar berbobot lebih berat
(akar dari jumlah menitnya), karena tren H4 lebih menentukan daripada tren M5.

CATATAN: ini alat bantu baca arah, bukan sinyal beli/jual. Semua hitungan
memakai bar yang sudah tertutup maupun bar berjalan apa adanya dari broker.
"""

import argparse
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from mt5_client import Config, connect, load_env


# --------------------------------------------------------------------------
# Timeframe
# --------------------------------------------------------------------------

# Jumlah menit tiap timeframe. Dipakai untuk mengurutkan tampilan dan
# menentukan bobot pada kesimpulan akhir.
TF_MENIT: Dict[str, int] = {
    "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10,
    "M12": 12, "M15": 15, "M20": 20, "M30": 30,
    "H1": 60, "H2": 120, "H3": 180, "H4": 240, "H6": 360, "H8": 480, "H12": 720,
    "D1": 1440, "W1": 10080, "MN1": 43200,
}

TF_DEFAULT = "M1,M5,M15"


def resolve_timeframe(mt5: Any, nama: str) -> int:
    nama = nama.strip().upper()
    if nama not in TF_MENIT:
        raise SystemExit(
            "Timeframe %r tidak dikenal. Pilihan: %s" % (nama, ", ".join(TF_MENIT)))
    attr = "TIMEFRAME_" + nama
    if not hasattr(mt5, attr):
        raise SystemExit("Timeframe %s tidak tersedia di backend ini." % nama)
    return int(getattr(mt5, attr))


# --------------------------------------------------------------------------
# Indikator (numpy murni, tanpa dependency tambahan)
# --------------------------------------------------------------------------

def ema(nilai: np.ndarray, periode: int) -> np.ndarray:
    """Exponential moving average. Diseed dengan SMA periode pertama supaya
    nilai awalnya tidak terlalu condong ke harga bar pertama."""
    nilai = np.asarray(nilai, dtype=float)
    if len(nilai) < periode:
        raise ValueError("butuh minimal %d bar" % periode)
    k = 2.0 / (periode + 1.0)
    keluar = np.empty(len(nilai), dtype=float)
    keluar[:periode] = np.nan
    keluar[periode - 1] = nilai[:periode].mean()
    for i in range(periode, len(nilai)):
        keluar[i] = nilai[i] * k + keluar[i - 1] * (1.0 - k)
    return keluar


def rma(nilai: np.ndarray, periode: int) -> np.ndarray:
    """Rata-rata bergerak ala Wilder (alpha = 1/periode). Dipakai ADX."""
    nilai = np.asarray(nilai, dtype=float)
    keluar = np.full(len(nilai), np.nan, dtype=float)
    if len(nilai) < periode:
        return keluar
    keluar[periode - 1] = nilai[:periode].mean()
    for i in range(periode, len(nilai)):
        keluar[i] = (keluar[i - 1] * (periode - 1) + nilai[i]) / periode
    return keluar


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """True Range Wilder, dihitung mulai bar kedua (butuh close sebelumnya)."""
    return np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - close[:-1]),
        np.abs(low[1:] - close[:-1]),
    ])


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        periode: int = 14) -> float:
    """Average True Range terakhir. Dipakai mengukur lebar wajar stop loss:
    stop yang lebih sempit dari gerak normal pasar akan kena hanya karena
    riak biasa, bukan karena arahnya salah."""
    if len(close) < periode + 1:
        return float("nan")
    garis = rma(true_range(high, low, close), periode)
    return float(garis[-1]) if not np.isnan(garis[-1]) else float("nan")


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        periode: int = 14) -> Tuple[float, float, float]:
    """ADX + DI plus/minus versi Wilder. Mengembalikan nilai terakhir.

    ADX mengukur KEKUATAN tren, bukan arahnya: nilai kecil berarti pasar
    menyamping, dan di situ sinyal arah apa pun jadi tidak bisa dipercaya.
    """
    n = len(close)
    if n < periode * 2 + 1:
        return float("nan"), float("nan"), float("nan")

    up = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    tr = true_range(high, low, close)

    tr_s = rma(tr, periode)
    plus_s = rma(plus_dm, periode)
    minus_s = rma(minus_dm, periode)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * plus_s / tr_s
        minus_di = 100.0 * minus_s / tr_s
        jumlah = plus_di + minus_di
        dx = 100.0 * np.abs(plus_di - minus_di) / np.where(jumlah == 0, np.nan, jumlah)

    adx_garis = rma(dx[~np.isnan(dx)], periode)
    nilai_adx = float(adx_garis[-1]) if len(adx_garis) and not np.isnan(adx_garis[-1]) else float("nan")
    return nilai_adx, float(plus_di[-1]), float(minus_di[-1])


def struktur_swing(high: np.ndarray, low: np.ndarray, bar: int) -> Tuple[int, str]:
    """Bandingkan puncak & lembah paruh terakhir dengan paruh sebelumnya.

    Higher-High + Higher-Low  -> struktur naik  (+1)
    Lower-Low  + Lower-High   -> struktur turun (-1)
    campur                    -> tidak jelas    ( 0)
    """
    bar = min(bar, len(high))
    if bar < 8:
        return 0, "data kurang"
    separuh = bar // 2
    lama_h, baru_h = high[-bar:-separuh], high[-separuh:]
    lama_l, baru_l = low[-bar:-separuh], low[-separuh:]

    # Tanpa toleransi, pasar menyamping pun hampir selalu menghasilkan "HH+HL"
    # karena satu tick beda saja sudah dihitung puncak lebih tinggi. Selisihnya
    # harus berarti dulu: minimal 5% dari rentang jendela yang diperiksa.
    rentang = float(high[-bar:].max() - low[-bar:].min())
    tol = rentang * 0.05
    if rentang <= 0:
        return 0, "datar"

    hh = baru_h.max() > lama_h.max() + tol
    hl = baru_l.min() > lama_l.min() + tol
    lh = baru_h.max() < lama_h.max() - tol
    ll = baru_l.min() < lama_l.min() - tol

    if hh and hl:
        return 1, "HH+HL"
    if lh and ll:
        return -1, "LL+LH"
    return 0, "campur"


# --------------------------------------------------------------------------
# Penilaian satu timeframe
# --------------------------------------------------------------------------

class HasilTF:
    def __init__(self, nama: str) -> None:
        self.nama = nama
        self.tersedia = False
        self.alasan = ""
        self.skor = 0
        self.maks = 4
        self.label = "-"
        self.adx = float("nan")
        self.plus_di = float("nan")
        self.minus_di = float("nan")
        self.sinyal: List[Tuple[str, int, str]] = []   # (nama, nilai, keterangan)
        self.harga = float("nan")
        self.ema_cepat = float("nan")
        self.ema_lambat = float("nan")
        self.bar = 0


class Setelan:
    def __init__(self) -> None:
        self.ema_cepat = int(os.getenv("MT5_TREND_EMA_CEPAT", "20"))
        self.ema_lambat = int(os.getenv("MT5_TREND_EMA_LAMBAT", "50"))
        self.adx_periode = int(os.getenv("MT5_TREND_ADX_PERIODE", "14"))
        self.adx_ambang = float(os.getenv("MT5_TREND_ADX_AMBANG", "20"))
        self.slope_bar = int(os.getenv("MT5_TREND_SLOPE_BAR", "10"))
        self.struktur_bar = int(os.getenv("MT5_TREND_STRUKTUR_BAR", "40"))
        self.bar = int(os.getenv("MT5_TREND_BAR", "300"))


def nilai_timeframe(mt5: Any, symbol: str, nama_tf: str, setelan: Setelan) -> HasilTF:
    hasil = HasilTF(nama_tf)
    tf = resolve_timeframe(mt5, nama_tf)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, setelan.bar)

    if rates is None or len(rates) == 0:
        code, pesan = mt5.last_error()
        hasil.alasan = "tidak ada data [%s] %s" % (code, pesan)
        return hasil

    butuh = max(setelan.ema_lambat + setelan.slope_bar, setelan.adx_periode * 2 + 2)
    if len(rates) < butuh:
        hasil.alasan = "cuma %d bar, butuh %d" % (len(rates), butuh)
        return hasil

    high = np.asarray(rates["high"], dtype=float)
    low = np.asarray(rates["low"], dtype=float)
    close = np.asarray(rates["close"], dtype=float)

    cepat = ema(close, setelan.ema_cepat)
    lambat = ema(close, setelan.ema_lambat)

    hasil.tersedia = True
    hasil.bar = len(rates)
    hasil.harga = float(close[-1])
    hasil.ema_cepat = float(cepat[-1])
    hasil.ema_lambat = float(lambat[-1])

    # 1. EMA cepat vs EMA lambat
    selisih = cepat[-1] - lambat[-1]
    s1 = 1 if selisih > 0 else (-1 if selisih < 0 else 0)
    hasil.sinyal.append(("EMA%d/%d" % (setelan.ema_cepat, setelan.ema_lambat), s1,
                         "cepat di atas" if s1 > 0 else "cepat di bawah"))

    # 2. Harga vs EMA lambat
    s2 = 1 if close[-1] > lambat[-1] else (-1 if close[-1] < lambat[-1] else 0)
    hasil.sinyal.append(("harga vs EMA%d" % setelan.ema_lambat, s2,
                         "di atas" if s2 > 0 else "di bawah"))

    # 3. Kemiringan EMA lambat. Dinormalkan ke harga supaya ambangnya tidak
    #    tergantung simbol (EURUSD 1.15 vs XAUUSD 2600 beda skala jauh).
    sebelum = lambat[-1 - setelan.slope_bar]
    slope_rel = (lambat[-1] - sebelum) / sebelum if sebelum else 0.0
    batas = 0.0002          # 0.02% dalam rentang slope_bar
    s3 = 1 if slope_rel > batas else (-1 if slope_rel < -batas else 0)
    hasil.sinyal.append(("slope EMA%d" % setelan.ema_lambat, s3,
                         "naik" if s3 > 0 else ("turun" if s3 < 0 else "datar")))

    # 4. Struktur swing
    s4, ket4 = struktur_swing(high, low, setelan.struktur_bar)
    hasil.sinyal.append(("struktur", s4, ket4))

    hasil.skor = s1 + s2 + s3 + s4
    hasil.adx, hasil.plus_di, hasil.minus_di = adx(high, low, close, setelan.adx_periode)

    # ADX rendah = pasar menyamping; arah apa pun tidak bisa dipegang.
    lemah = not math.isnan(hasil.adx) and hasil.adx < setelan.adx_ambang
    if lemah or abs(hasil.skor) < 2:
        hasil.label = "SIDEWAYS"
    elif hasil.skor > 0:
        hasil.label = "BULLISH"
    else:
        hasil.label = "BEARISH"
    return hasil


# --------------------------------------------------------------------------
# Kesimpulan antar timeframe
# --------------------------------------------------------------------------

def bobot(nama_tf: str) -> float:
    """Timeframe besar berbobot lebih berat. Akar dipakai supaya D1 tidak
    langsung menelan semua timeframe kecil."""
    return math.sqrt(TF_MENIT[nama_tf.upper()])


def kesimpulan(hasil: Sequence[HasilTF], setelan: Setelan) -> Tuple[str, float, str]:
    dipakai = [h for h in hasil if h.tersedia]
    if not dipakai:
        return "TIDAK ADA DATA", 0.0, "semua timeframe gagal dibaca"

    total_bobot = sum(bobot(h.nama) for h in dipakai)
    skor_tertimbang = sum(bobot(h.nama) * h.skor for h in dipakai)
    # -1.0 .. +1.0
    normal = skor_tertimbang / (total_bobot * 4.0) if total_bobot else 0.0

    naik = [h.nama for h in dipakai if h.label == "BULLISH"]
    turun = [h.nama for h in dipakai if h.label == "BEARISH"]
    datar = [h.nama for h in dipakai if h.label == "SIDEWAYS"]

    if normal >= 0.5:
        label = "BULLISH KUAT"
    elif normal >= 0.2:
        label = "BULLISH"
    elif normal <= -0.5:
        label = "BEARISH KUAT"
    elif normal <= -0.2:
        label = "BEARISH"
    else:
        label = "SIDEWAYS / TIDAK JELAS"

    bagian = []
    if naik:
        bagian.append("bullish di " + ",".join(naik))
    if turun:
        bagian.append("bearish di " + ",".join(turun))
    if datar:
        bagian.append("sideways di " + ",".join(datar))
    catatan = "; ".join(bagian)

    # Konflik timeframe besar vs kecil adalah informasi yang paling berguna di
    # sini, jadi disebut eksplisit daripada dibiarkan tersirat dari tabel.
    # Yang dibandingkan bukan cuma TF terkecil, tapi SEMUA TF di bawah TF besar
    # berarah yang paling tinggi - kalau tidak, konflik H1/H4 lawan D1 lolos
    # begitu saja hanya karena TF terkecil kebetulan sideways.
    urut = sorted(dipakai, key=lambda h: TF_MENIT[h.nama.upper()])
    berarah = [h for h in urut if h.label in ("BULLISH", "BEARISH")]
    if berarah:
        besar = berarah[-1]
        lawan = [h.nama for h in berarah[:-1] if h.label != besar.label]
        if lawan:
            if besar.label == "BULLISH":
                catatan += " | %s bullish tapi %s sudah bearish: bisa jadi koreksi turun di dalam tren naik" % (
                    besar.nama, ",".join(lawan))
            else:
                catatan += " | %s bearish tapi %s sudah bullish: bisa jadi pantulan naik di dalam tren turun" % (
                    besar.nama, ",".join(lawan))
        elif len(berarah) > 1:
            catatan += " | %s searah %s, tren sejalan di semua timeframe berarah" % (
                besar.nama, ",".join(h.nama for h in berarah[:-1]))
    return label, normal, catatan


# --------------------------------------------------------------------------
# Tampilan
# --------------------------------------------------------------------------

WARNA = {"BULLISH": "\033[32m", "BEARISH": "\033[31m", "SIDEWAYS": "\033[33m"}
RESET = "\033[0m"


def warnai(teks: str, label: str, pakai: bool) -> str:
    if not pakai:
        return teks
    for kunci, kode in WARNA.items():
        if label.startswith(kunci):
            return kode + teks + RESET
    return teks


def tampilkan(symbol: str, hasil: Sequence[HasilTF], setelan: Setelan,
              detail: bool, pakai_warna: bool, digit: int = 5) -> None:
    aktif = [h for h in hasil if h.tersedia]
    harga = aktif[0].harga if aktif else float("nan")

    print("=" * 80)
    print("%s   harga %.*f" % (symbol, digit, harga))
    print("=" * 80)
    print("%-5s %-9s %-6s %-15s %-10s %-7s %-11s %-10s" % (
        "TF", "TREN", "SKOR", "EMA%d/%d" % (setelan.ema_cepat, setelan.ema_lambat),
        "HRG vs EMA", "SLOPE", "STRUKTUR", "ADX"))
    print("-" * 80)

    for h in hasil:
        if not h.tersedia:
            print("%-5s %-9s  %s" % (h.nama, "-", h.alasan))
            continue
        ket = {n: k for n, _, k in h.sinyal}
        adx_teks = "-" if math.isnan(h.adx) else "%.1f" % h.adx
        if not math.isnan(h.adx):
            adx_teks += " kuat" if h.adx >= setelan.adx_ambang else " lemah"
        baris = "%-5s %-9s %+3d/%-2d %-15s %-10s %-7s %-11s %-10s" % (
            h.nama, h.label, h.skor, h.maks,
            ket.get("EMA%d/%d" % (setelan.ema_cepat, setelan.ema_lambat), "-"),
            ket.get("harga vs EMA%d" % setelan.ema_lambat, "-"),
            ket.get("slope EMA%d" % setelan.ema_lambat, "-"),
            ket.get("struktur", "-"), adx_teks)
        print(warnai(baris, h.label, pakai_warna))

        if detail:
            print("      EMA%d %.*f | EMA%d %.*f | +DI %.1f  -DI %.1f | %d bar" % (
                setelan.ema_cepat, digit, h.ema_cepat,
                setelan.ema_lambat, digit, h.ema_lambat,
                h.plus_di, h.minus_di, h.bar))

    label, normal, catatan = kesimpulan(hasil, setelan)
    print("-" * 80)
    print(warnai("KESIMPULAN: %s  (%+.0f%%)" % (label, normal * 100), label, pakai_warna))
    if catatan:
        for potong in catatan.split(" | "):
            print("  - %s" % potong)
    print()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mt5_trend.py",
        description="Baca arah tren di beberapa timeframe sekaligus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""contoh:
    python mt5_trend.py EURUSD
    python mt5_trend.py EURUSD GBPUSD XAUUSD
    python mt5_trend.py EURUSD --timeframes M5,M15,H1,H4,D1
    python mt5_trend.py EURUSD --detail
    python mt5_trend.py --posisi          # simbol yang sedang punya posisi""")
    p.add_argument("symbols", nargs="*",
                   help="simbol yang dibaca (default: MT5_SYMBOL di .env)")
    p.add_argument("--timeframes", "-t",
                   default=os.getenv("MT5_TREND_TF", TF_DEFAULT),
                   help="daftar timeframe dipisah koma (default: %(default)s)")
    p.add_argument("--detail", action="store_true",
                   help="tampilkan nilai EMA dan DI mentahnya")
    p.add_argument("--posisi", action="store_true",
                   help="baca simbol dari posisi yang sedang terbuka")
    p.add_argument("--no-color", action="store_true", help="matikan warna")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # load_env() harus mendahului build_parser(): sebagian default argparse
    # dibaca dari os.getenv saat parser dirakit, jadi kalau .env belum masuk
    # nilai di .env akan kalah oleh fallback yang ditulis di kode.
    load_env()
    args = build_parser().parse_args(argv)
    cfg = Config()
    setelan = Setelan()

    nama_tf = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
    if not nama_tf:
        raise SystemExit("Daftar timeframe kosong.")
    # Urutkan dari kecil ke besar supaya tabelnya enak dibaca.
    nama_tf.sort(key=lambda t: TF_MENIT.get(t, 0))

    pakai_warna = sys.stdout.isatty() and not args.no_color

    with connect(cfg) as mt5:
        symbols = list(args.symbols)
        if args.posisi:
            posisi = mt5.positions_get() or ()
            dari_posisi = sorted({str(p.symbol) for p in posisi})
            if not dari_posisi:
                print("Tidak ada posisi terbuka.")
                return 0
            symbols.extend(s for s in dari_posisi if s not in symbols)
        if not symbols:
            symbols = [cfg.symbol]

        for symbol in symbols:
            info = mt5.symbol_info(symbol)
            if info is None:
                print("Simbol %r tidak dikenal broker ini, dilewati.\n" % symbol)
                continue
            mt5.symbol_select(symbol, True)
            hasil = [nilai_timeframe(mt5, symbol, tf, setelan) for tf in nama_tf]
            # Jumlah desimal ikut simbol: EURUSD 5, XAUUSD 2, indeks kadang 1.
            tampilkan(symbol, hasil, setelan, args.detail, pakai_warna,
                      int(info.digits))
    return 0


if __name__ == "__main__":
    sys.exit(main())

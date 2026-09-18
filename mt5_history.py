#!/usr/bin/env python3
"""Riwayat trade: daftar posisi yang sudah ditutup beserta statistiknya.

    python mt5_history.py                        # 30 hari terakhir
    python mt5_history.py --hari 7
    python mt5_history.py --dari 2026-09-01 --sampai 2026-09-16
    python mt5_history.py --symbol XAUUSD
    python mt5_history.py --detail               # per deal, bukan per trade
    python mt5_history.py --csv riwayat.csv      # ekspor untuk jurnal

MT5 menyimpan riwayat sebagai DEAL, bukan sebagai trade. Satu posisi biasanya
terdiri dari dua deal (satu membuka, satu menutup), dan bisa lebih kalau
ditutup sebagian. Skrip ini menyatukannya kembali per position_id supaya yang
Anda lihat adalah trade utuh: masuk di harga berapa, keluar di harga berapa,
berapa lama, untung atau rugi berapa.

Profit yang ditampilkan sudah BERSIH: profit kotor + swap + komisi + fee.

CATATAN WAKTU: stempel waktu MT5 adalah jam dinding server broker, bukan epoch
UTC. Offset server dihitung otomatis dengan membandingkan tick terakhir
terhadap jam UTC, lalu semua waktu ditampilkan dalam zona waktu Mac Anda.
"""

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from mt5_client import Config, connect, load_env


# --------------------------------------------------------------------------
# Konstanta deal
# --------------------------------------------------------------------------

DEAL_TYPE_BUY, DEAL_TYPE_SELL = 0, 1
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3

# Deal non-trading yang tidak boleh ikut statistik: setoran, penarikan, kredit,
# bunga, koreksi, bonus, komisi terpisah, dividen, pajak.
JENIS_SALDO = {
    2: "saldo", 3: "kredit", 4: "biaya", 5: "koreksi", 6: "bonus",
    7: "komisi", 8: "komisi harian", 9: "komisi bulanan",
    10: "komisi agen harian", 11: "komisi agen bulanan", 12: "bunga",
    13: "buy dibatalkan", 14: "sell dibatalkan", 15: "dividen",
    16: "dividen franked", 17: "pajak",
}

# Urutan field yang diambil dari sisi server, sekali jalan.
KOLOM = ("ticket", "position_id", "order", "time", "type", "entry", "symbol",
         "volume", "price", "profit", "swap", "commission", "fee", "comment",
         "magic")


# --------------------------------------------------------------------------
# Pengambilan data
# --------------------------------------------------------------------------

def offset_server(mt5: Any, symbol: str = "EURUSD") -> int:
    """Selisih detik antara jam server broker dan UTC.

    Stempel waktu MT5 adalah jam dinding server yang dikemas seolah-olah epoch
    UTC. Tanpa koreksi ini, datetime.fromtimestamp() menggeser semuanya sebesar
    offset server DITAMBAH offset lokal - di sini itu 10 jam, cukup untuk
    menaruh trade kemarin di besok.
    """
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or not tick.time:
        return 0
    selisih = float(tick.time) - time.time()
    # Zona waktu broker selalu kelipatan 30 menit dari UTC.
    return int(round(selisih / 1800.0) * 1800)


def ambil_deals(mt5: Any, dari: dt.datetime, sampai: dt.datetime) -> List[tuple]:
    """Ambil deal dalam rentang waktu sebagai tuple biasa.

    Dua hal dihindari di sini:

    1. mt5linux 0.1.9 menyisipkan objek datetime ke source yang dieval di sisi
       server, padahal namespace server tidak meng-import datetime - hasilnya
       NameError. Karena itu batas waktunya dikirim sebagai integer, yang juga
       diterima MetaTrader5.
    2. Tiap TradeDeal yang kembali adalah netref, jadi membaca 15 field dari
       100 deal berarti 1500 round-trip. rpyc.classic.obtain() tidak bisa
       dipakai karena TradeDeal gagal di-pickle. Jadi field-nya diekstrak di
       sisi server dalam satu eval, dan yang menyeberang cuma tuple primitif.
    """
    dari_ts = int(dari.timestamp())
    sampai_ts = int(sampai.timestamp())

    conn = getattr(mt5, "_MetaTrader5__conn", None)
    if conn is None:
        # Backend local: paket MetaTrader5 resmi, panggil apa adanya.
        deals = mt5.history_deals_get(dari, sampai) or ()
        return [tuple(getattr(d, k) for k in KOLOM) for d in deals]

    kode = "[(%s) for d in mt5.history_deals_get(%d,%d) or ()]" % (
        ",".join("d." + k for k in KOLOM), dari_ts, sampai_ts)
    hasil = conn.eval(kode)
    return list(hasil or [])


# --------------------------------------------------------------------------
# Penyatuan deal jadi trade
# --------------------------------------------------------------------------

class Trade:
    def __init__(self, position_id: int) -> None:
        self.position_id = position_id
        self.symbol = ""
        self.arah = ""
        self.volume = 0.0
        self.harga_masuk = 0.0
        self.harga_keluar = 0.0
        self.waktu_masuk: Optional[int] = None
        self.waktu_keluar: Optional[int] = None
        self.profit = 0.0        # kotor
        self.swap = 0.0
        self.komisi = 0.0
        self.fee = 0.0
        self.komentar = ""
        self.magic = 0
        self.selesai = False

    @property
    def bersih(self) -> float:
        return self.profit + self.swap + self.komisi + self.fee

    @property
    def durasi(self) -> Optional[int]:
        if self.waktu_masuk is None or self.waktu_keluar is None:
            return None
        return self.waktu_keluar - self.waktu_masuk


def satukan(baris: Sequence[tuple]) -> Tuple[List[Trade], List[tuple]]:
    """Kelompokkan deal per position_id jadi trade utuh.

    Harga masuk dan keluar dirata-rata tertimbang volume, supaya posisi yang
    ditutup bertahap (lihat perintah scale) tetap jujur angkanya.
    """
    kolom = {n: i for i, n in enumerate(KOLOM)}
    trades: Dict[int, Trade] = {}
    saldo: List[tuple] = []

    # bobot sementara untuk rata-rata tertimbang
    tumpuk: Dict[int, List[float]] = {}

    for b in baris:
        tipe = int(b[kolom["type"]])
        if tipe >= 2:
            saldo.append(b)
            continue

        pid = int(b[kolom["position_id"]])
        if pid not in trades:
            trades[pid] = Trade(pid)
            tumpuk[pid] = [0.0, 0.0, 0.0, 0.0]  # vol_in, nilai_in, vol_out, nilai_out
        t = trades[pid]
        v = float(b[kolom["volume"]])
        harga = float(b[kolom["price"]])
        entry = int(b[kolom["entry"]])
        waktu = int(b[kolom["time"]])

        t.symbol = str(b[kolom["symbol"]]) or t.symbol
        t.swap += float(b[kolom["swap"]])
        t.komisi += float(b[kolom["commission"]])
        t.fee += float(b[kolom["fee"]])
        t.profit += float(b[kolom["profit"]])

        if entry == DEAL_ENTRY_IN:
            t.arah = "BUY" if tipe == DEAL_TYPE_BUY else "SELL"
            t.komentar = str(b[kolom["comment"]]) or t.komentar
            t.magic = int(b[kolom["magic"]])
            tumpuk[pid][0] += v
            tumpuk[pid][1] += v * harga
            t.waktu_masuk = waktu if t.waktu_masuk is None else min(t.waktu_masuk, waktu)
        else:
            # OUT, INOUT dan OUT_BY semuanya menutup sebagian atau seluruhnya.
            tumpuk[pid][2] += v
            tumpuk[pid][3] += v * harga
            t.waktu_keluar = waktu if t.waktu_keluar is None else max(t.waktu_keluar, waktu)

    for pid, t in trades.items():
        vin, nin, vout, nout = tumpuk[pid]
        t.volume = round(vin, 8)
        t.harga_masuk = nin / vin if vin else 0.0
        t.harga_keluar = nout / vout if vout else 0.0
        # Selesai kalau volume yang keluar sudah menyamai yang masuk.
        t.selesai = vout > 0 and abs(vout - vin) < 1e-9

    urut = sorted(trades.values(),
                  key=lambda x: (x.waktu_keluar or x.waktu_masuk or 0))
    return urut, saldo


# --------------------------------------------------------------------------
# Statistik
# --------------------------------------------------------------------------

class Statistik:
    def __init__(self, trades: Sequence[Trade]) -> None:
        self.selesai = [t for t in trades if t.selesai]
        self.terbuka = [t for t in trades if not t.selesai]

        self.menang = [t for t in self.selesai if t.bersih > 0]
        self.kalah = [t for t in self.selesai if t.bersih < 0]
        self.impas = [t for t in self.selesai if t.bersih == 0]

        self.total = sum(t.bersih for t in self.selesai)
        self.total_menang = sum(t.bersih for t in self.menang)
        self.total_kalah = sum(t.bersih for t in self.kalah)   # negatif
        self.swap = sum(t.swap for t in self.selesai)
        self.komisi = sum(t.komisi + t.fee for t in self.selesai)

    @property
    def jumlah(self) -> int:
        return len(self.selesai)

    @property
    def win_rate(self) -> float:
        return len(self.menang) / self.jumlah * 100.0 if self.jumlah else 0.0

    @property
    def rata_menang(self) -> float:
        return self.total_menang / len(self.menang) if self.menang else 0.0

    @property
    def rata_kalah(self) -> float:
        return self.total_kalah / len(self.kalah) if self.kalah else 0.0

    @property
    def profit_factor(self) -> Optional[float]:
        """Total untung dibagi total rugi. Di bawah 1 berarti merugi.

        None kalau belum pernah rugi sama sekali - pembaginya nol, dan
        menampilkan 'tak hingga' lebih jujur daripada angka besar yang
        terkesan bermakna.
        """
        if not self.kalah or self.total_kalah == 0:
            return None
        return self.total_menang / abs(self.total_kalah)

    @property
    def ekspektasi(self) -> float:
        return self.total / self.jumlah if self.jumlah else 0.0

    def per_simbol(self) -> List[Tuple[str, int, float]]:
        agg: Dict[str, List[float]] = {}
        for t in self.selesai:
            a = agg.setdefault(t.symbol, [0.0, 0.0])
            a[0] += 1
            a[1] += t.bersih
        return sorted(((s, int(v[0]), v[1]) for s, v in agg.items()),
                      key=lambda x: x[2], reverse=True)


# --------------------------------------------------------------------------
# Serialisasi ke struktur JSON
#
# Dipakai mt5_api.py. Ditaruh di sini, bukan di sana, supaya bentuk datanya
# cuma punya satu sumber kebenaran: kalau kolom trade berubah, CLI dan API
# ikut berubah bersama-sama.
# --------------------------------------------------------------------------

def _bulat(nilai: float, digit: int = 2) -> float:
    """Bulatkan supaya JSON tidak berisi 4289.700000000001."""
    return round(float(nilai), digit)


def trade_ke_dict(t: Trade, ke_lokal, digit: int = 5) -> dict:
    return {
        "position_id": int(t.position_id),
        "symbol": t.symbol,
        "arah": t.arah,
        "volume": _bulat(t.volume, 4),
        "waktu_masuk": ke_lokal(t.waktu_masuk).isoformat() if t.waktu_masuk else None,
        "waktu_keluar": ke_lokal(t.waktu_keluar).isoformat() if t.waktu_keluar else None,
        "harga_masuk": _bulat(t.harga_masuk, digit),
        "harga_keluar": _bulat(t.harga_keluar, digit) if t.selesai else None,
        "durasi_detik": t.durasi,
        "profit_kotor": _bulat(t.profit),
        "swap": _bulat(t.swap),
        "komisi": _bulat(t.komisi),
        "fee": _bulat(t.fee),
        "profit_bersih": _bulat(t.bersih),
        "magic": int(t.magic),
        "komentar": t.komentar,
        "selesai": bool(t.selesai),
    }


def hasil_ke_dict(trades: Sequence[Trade], saldo: Sequence[tuple],
                  st: "Statistik", akun: Any, dari: dt.datetime,
                  sampai: dt.datetime, ke_lokal, offset: int,
                  digit: Dict[str, int]) -> dict:
    kolom = {n: i for i, n in enumerate(KOLOM)}
    return {
        "akun": {
            "login": int(akun.login),
            "nama": str(akun.name),
            "broker": str(akun.company),
            "server": str(akun.server),
            "mata_uang": str(akun.currency),
            "balance": _bulat(akun.balance),
            "equity": _bulat(akun.equity),
            "demo": int(akun.trade_mode) != 2,
        },
        "rentang": {
            "dari": dari.isoformat(),
            "sampai": sampai.isoformat(),
            # Waktu dikembalikan dalam zona waktu mesin ini, sudah dikoreksi
            # dari jam server broker. Offset-nya disertakan supaya klien bisa
            # memverifikasi sendiri, bukan cuma percaya.
            "zona_waktu_mesin": str(dt.datetime.now().astimezone().tzinfo),
            "offset_server_broker_detik": int(offset),
        },
        "statistik": {
            "trade_selesai": st.jumlah,
            "menang": len(st.menang),
            "kalah": len(st.kalah),
            "impas": len(st.impas),
            "masih_terbuka": len(st.terbuka),
            "win_rate_persen": _bulat(st.win_rate, 1),
            "profit_bersih": _bulat(st.total),
            "rata_menang": _bulat(st.rata_menang),
            "rata_kalah": _bulat(st.rata_kalah),
            # null kalau belum pernah rugi: pembaginya nol, dan angka besar
            # di situ akan terbaca seolah-olah bermakna.
            "profit_factor": _bulat(st.profit_factor) if st.profit_factor is not None else None,
            "ekspektasi_per_trade": _bulat(st.ekspektasi),
            "total_swap": _bulat(st.swap),
            "total_komisi": _bulat(st.komisi),
            "per_simbol": [
                {"symbol": sym, "trade": n, "profit_bersih": _bulat(p)}
                for sym, n, p in st.per_simbol()
            ],
        },
        "trades": [trade_ke_dict(t, ke_lokal, digit.get(t.symbol, 5))
                   for t in trades],
        "operasi_saldo": [
            {
                "waktu": ke_lokal(int(b[kolom["time"]])).isoformat(),
                "jenis": JENIS_SALDO.get(int(b[kolom["type"]]),
                                         "tipe %d" % int(b[kolom["type"]])),
                "jumlah": _bulat(b[kolom["profit"]]),
            }
            for b in saldo
        ],
    }


# --------------------------------------------------------------------------
# Tampilan
# --------------------------------------------------------------------------

def durasi_teks(detik: Optional[int]) -> str:
    if detik is None:
        return "-"
    if detik < 60:
        return "%ds" % detik
    menit, sisa = divmod(detik, 60)
    if menit < 60:
        return "%dm %ds" % (menit, sisa)
    jam, menit = divmod(menit, 60)
    if jam < 24:
        return "%dj %dm" % (jam, menit)
    hari, jam = divmod(jam, 24)
    return "%dh %dj" % (hari, jam)


HIJAU, MERAH, RESET = "\033[32m", "\033[31m", "\033[0m"


def warnai(teks: str, nilai: float, pakai: bool) -> str:
    if not pakai or nilai == 0:
        return teks
    return (HIJAU if nilai > 0 else MERAH) + teks + RESET


def tampilkan(trades: Sequence[Trade], saldo: Sequence[tuple], st: Statistik,
              digit: Dict[str, int], mata: str, ke_lokal, dari: dt.datetime,
              sampai: dt.datetime, akun: Any, pakai_warna: bool) -> None:
    print("=" * 92)
    print("RIWAYAT TRADE   %s s/d %s   akun %s (%s)" % (
        dari.strftime("%d/%m/%Y"), sampai.strftime("%d/%m/%Y"),
        akun.login, akun.company))
    print("=" * 92)

    if not trades:
        print("Tidak ada trade dalam rentang ini.")
        print()
        return

    print("%-16s %-8s %-5s %7s %11s %11s %9s %11s" % (
        "TUTUP", "SIMBOL", "ARAH", "VOLUME", "MASUK", "KELUAR", "DURASI", "BERSIH"))
    print("-" * 92)

    for t in trades:
        d = digit.get(t.symbol, 5)
        if t.selesai:
            waktu = ke_lokal(t.waktu_keluar).strftime("%d/%m %H:%M:%S")
            keluar = "%.*f" % (d, t.harga_keluar)
            bersih = "%+.2f" % t.bersih
        else:
            waktu = "MASIH TERBUKA"
            keluar = "-"
            bersih = "-"
        baris = "%-16s %-8s %-5s %7.2f %11.*f %11s %9s %11s" % (
            waktu, t.symbol, t.arah, t.volume, d, t.harga_masuk, keluar,
            durasi_teks(t.durasi) if t.selesai else "-", bersih)
        print(warnai(baris, t.bersih if t.selesai else 0, pakai_warna))

    print("-" * 92)
    print()
    print("RINGKASAN")
    if st.jumlah == 0:
        print("  Belum ada trade yang selesai dalam rentang ini.")
    else:
        print("  Trade selesai     : %d  (menang %d, kalah %d%s)" % (
            st.jumlah, len(st.menang), len(st.kalah),
            ", impas %d" % len(st.impas) if st.impas else ""))
        print("  Win rate          : %.1f%%" % st.win_rate)
        print("  " + warnai("Profit bersih     : %+.2f %s" % (st.total, mata),
                            st.total, pakai_warna))
        print("  Rata-rata menang  : %+.2f" % st.rata_menang)
        print("  Rata-rata kalah   : %+.2f" % st.rata_kalah)
        pf = st.profit_factor
        print("  Profit factor     : %s%s" % (
            "%.2f" % pf if pf is not None else "tak hingga (belum pernah rugi)",
            "   (di bawah 1.00 berarti merugi)" if pf is not None and pf < 1 else ""))
        print("  Ekspektasi/trade  : %+.2f %s" % (st.ekspektasi, mata))
        if st.menang:
            b = max(st.menang, key=lambda t: t.bersih)
            print("  Menang terbesar   : %+.2f  (%s %s)" % (
                b.bersih, b.symbol, ke_lokal(b.waktu_keluar).strftime("%d/%m %H:%M")))
        if st.kalah:
            w = min(st.kalah, key=lambda t: t.bersih)
            print("  Rugi terbesar     : %+.2f  (%s %s)" % (
                w.bersih, w.symbol, ke_lokal(w.waktu_keluar).strftime("%d/%m %H:%M")))
        if st.swap or st.komisi:
            print("  Swap + komisi     : %+.2f %s (sudah termasuk di profit bersih)" % (
                st.swap + st.komisi, mata))

        per = st.per_simbol()
        if len(per) > 1:
            print()
            print("  PER SIMBOL")
            for sym, n, p in per:
                print("    " + warnai("%-10s %3d trade   %+10.2f" % (sym, n, p),
                                      p, pakai_warna))

    if st.terbuka:
        print()
        print("  %d posisi masih terbuka, tidak ikut dihitung statistik." % len(st.terbuka))

    if saldo:
        print()
        print("  OPERASI SALDO (bukan trade, tidak ikut statistik)")
        for b in saldo:
            i = {n: k for k, n in enumerate(KOLOM)}
            jenis = JENIS_SALDO.get(int(b[i["type"]]), "tipe %d" % int(b[i["type"]]))
            print("    %-16s %-14s %+12.2f %s" % (
                ke_lokal(int(b[i["time"]])).strftime("%d/%m %H:%M:%S"),
                jenis, float(b[i["profit"]]), mata))
    print()


def tulis_csv(path: str, trades: Sequence[Trade], ke_lokal) -> int:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["position_id", "symbol", "arah", "volume", "waktu_masuk",
                    "waktu_keluar", "harga_masuk", "harga_keluar", "durasi_detik",
                    "profit_kotor", "swap", "komisi", "fee", "profit_bersih",
                    "magic", "komentar", "selesai"])
        for t in trades:
            w.writerow([
                t.position_id, t.symbol, t.arah, t.volume,
                ke_lokal(t.waktu_masuk).isoformat() if t.waktu_masuk else "",
                ke_lokal(t.waktu_keluar).isoformat() if t.waktu_keluar else "",
                "%.5f" % t.harga_masuk, "%.5f" % t.harga_keluar,
                t.durasi if t.durasi is not None else "",
                "%.2f" % t.profit, "%.2f" % t.swap, "%.2f" % t.komisi,
                "%.2f" % t.fee, "%.2f" % t.bersih,
                t.magic, t.komentar, "ya" if t.selesai else "tidak"])
    return len(trades)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def tanggal(teks: str) -> dt.datetime:
    for pola in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(teks, pola)
        except ValueError:
            continue
    raise SystemExit("Tanggal %r tidak dikenal. Pakai format 2026-09-01." % teks)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mt5_history.py",
        description="Riwayat trade beserta statistiknya.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""contoh:
    python mt5_history.py
    python mt5_history.py --hari 7
    python mt5_history.py --dari 2026-09-01 --sampai 2026-09-16
    python mt5_history.py --symbol XAUUSD
    python mt5_history.py --detail
    python mt5_history.py --csv riwayat.csv""")
    p.add_argument("--hari", type=int, default=int(os.getenv("MT5_HISTORY_HARI", "30")),
                   help="berapa hari ke belakang (default: %(default)s)")
    p.add_argument("--dari", help="tanggal mulai, misal 2026-09-01")
    p.add_argument("--sampai", help="tanggal akhir, default sekarang")
    p.add_argument("--symbol", help="saring satu simbol saja")
    p.add_argument("--magic", type=int, help="saring berdasarkan magic number")
    p.add_argument("--detail", action="store_true",
                   help="tampilkan deal mentah, bukan trade yang sudah disatukan")
    p.add_argument("--csv", help="tulis hasilnya ke berkas CSV")
    p.add_argument("--json", action="store_true",
                   help="cetak JSON ke stdout, bukan tabel (dipakai server Go)")
    p.add_argument("--no-color", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # load_env() harus mendahului build_parser(): sebagian default argparse
    # dibaca dari os.getenv saat parser dirakit, jadi kalau .env belum masuk
    # nilai di .env akan kalah oleh fallback yang ditulis di kode.
    load_env()
    args = build_parser().parse_args(argv)
    cfg = Config()
    pakai_warna = sys.stdout.isatty() and not args.no_color

    sampai = tanggal(args.sampai) + dt.timedelta(days=1) if args.sampai else dt.datetime.now()
    if args.dari:
        dari = tanggal(args.dari)
    else:
        dari = sampai - dt.timedelta(days=args.hari)
    if dari >= sampai:
        raise SystemExit("Rentang tanggal terbalik: --dari harus sebelum --sampai.")

    with connect(cfg) as mt5:
        akun = mt5.account_info()
        offset = offset_server(mt5, cfg.symbol)
        def ke_lokal(ts: int) -> dt.datetime:
            return dt.datetime.fromtimestamp(int(ts) - offset)

        # Rentang dikirim dalam jam server, karena begitulah MT5 menyaringnya.
        baris = ambil_deals(mt5, dari + dt.timedelta(seconds=offset),
                            sampai + dt.timedelta(seconds=offset))

        kolom = {n: i for i, n in enumerate(KOLOM)}
        if args.symbol:
            baris = [b for b in baris
                     if str(b[kolom["symbol"]]).upper() == args.symbol.upper()
                     or int(b[kolom["type"]]) >= 2]
        if args.magic is not None:
            baris = [b for b in baris if int(b[kolom["magic"]]) == args.magic]

        if args.detail and not args.json:
            print("%-16s %-14s %-8s %-5s %-7s %8s %11s %10s" % (
                "WAKTU", "POSISI", "SIMBOL", "TIPE", "ENTRY", "VOLUME", "HARGA", "PROFIT"))
            print("-" * 92)
            for b in baris:
                tipe = int(b[kolom["type"]])
                nama_tipe = ("BUY", "SELL")[tipe] if tipe < 2 else JENIS_SALDO.get(tipe, str(tipe))
                nama_entry = {0: "masuk", 1: "keluar", 2: "balik", 3: "keluar-by"}.get(
                    int(b[kolom["entry"]]), "?")
                print("%-16s %-14s %-8s %-5s %-7s %8.2f %11.5f %+10.2f" % (
                    ke_lokal(int(b[kolom["time"]])).strftime("%d/%m %H:%M:%S"),
                    b[kolom["position_id"]], b[kolom["symbol"]] or "-",
                    nama_tipe, nama_entry, float(b[kolom["volume"]]),
                    float(b[kolom["price"]]), float(b[kolom["profit"]])))
            print()
            return 0

        trades, saldo = satukan(baris)
        st = Statistik(trades)

        digit: Dict[str, int] = {}
        for t in trades:
            if t.symbol and t.symbol not in digit:
                info = mt5.symbol_info(t.symbol)
                digit[t.symbol] = int(info.digits) if info is not None else 5

        if args.json:
            # Stdout harus berisi JSON saja - server Go mem-parse-nya mentah.
            data = hasil_ke_dict(trades, saldo, st, akun, dari, sampai,
                                 ke_lokal, offset, digit)
            json.dump(data, sys.stdout, ensure_ascii=False)
            sys.stdout.write("\n")
            return 0

        tampilkan(trades, saldo, st, digit, akun.currency, ke_lokal,
                  dari, sampai, akun, pakai_warna)

        if args.csv:
            n = tulis_csv(args.csv, trades, ke_lokal)
            print("%d trade ditulis ke %s" % (n, args.csv))
    return 0


if __name__ == "__main__":
    sys.exit(main())

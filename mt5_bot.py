#!/usr/bin/env python3
"""Robot: pantau tren, buka posisi sendiri saat syarat terpenuhi.

    python mt5_bot.py                      # simulasi, tidak mengirim apa pun
    python mt5_bot.py XAUUSD --live        # benar-benar mengirim order
    python mt5_bot.py XAUUSD --sekali      # satu putaran lalu berhenti

Alurnya tiap putaran, per simbol:

    pagar dompet -> pagar pasar -> baca tren -> susun setup -> kirim

Pagar diperiksa dari yang paling murah ke yang paling mahal, supaya putaran
yang tidak akan menghasilkan order berhenti secepat mungkin dan tidak
membebani jembatan RPC.

Yang HARUS dipahami sebelum menyalakan --live:

  * Robot ini hanya MEMBUKA posisi. Ia tidak memindahkan SL, tidak melakukan
    trailing, dan tidak menutup posisi lebih awal. Yang menutup posisi adalah
    SL/TP yang sudah menempel di tiap order, jadi tiap posisi selalu punya
    batas rugi sejak detik pertama.
  * Semua pagar di bawah bersifat menahan, bukan menjamin. Gap harga di
    pembukaan pasar atau rilis berita bisa melewati SL, dan kerugian nyata
    bisa lebih besar dari hitungan.
  * Tanpa --live, tidak ada satu pun order yang dikirim: seluruh alur tetap
    berjalan dan dicetak, tapi berhenti di order_check(). Jalankan begitu
    sampai Anda percaya keputusannya.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import mt5_history
import mt5_order
import mt5_setup
import mt5_trend
from mt5_client import Config, connect, load_env
from mt5_order import TradeConfig, leg_default
from mt5_trend import Setelan, kesimpulan, nilai_timeframe


# --------------------------------------------------------------------------
# Setelan
# --------------------------------------------------------------------------

class SetelanBot:
    def __init__(self) -> None:
        self.interval = int(os.getenv("MT5_BOT_INTERVAL", "60"))
        # Ambang |skor| kesimpulan antar-timeframe. 0.5 = setara label "KUAT".
        self.skor_min = float(os.getenv("MT5_BOT_SKOR_MIN", "0.5"))
        self.max_posisi = int(os.getenv("MT5_BOT_MAX_POSISI", "1"))
        self.max_trade_hari = int(os.getenv("MT5_BOT_MAX_TRADE_HARI", "3"))
        self.rugi_harian_persen = float(os.getenv("MT5_BOT_RUGI_HARIAN_PERSEN", "2.0"))
        self.spread_max = int(os.getenv("MT5_BOT_SPREAD_MAX", "0"))
        self.cooldown = int(os.getenv("MT5_BOT_COOLDOWN", "300"))
        self.jam = os.getenv("MT5_BOT_JAM", "").strip()

        if self.interval < 5:
            raise SystemExit("MT5_BOT_INTERVAL minimal 5 detik.")
        if self.skor_min <= 0 or self.skor_min > 1:
            raise SystemExit("MT5_BOT_SKOR_MIN harus di antara 0 dan 1.")


def urai_jam(teks: str) -> Optional[Tuple[dt.time, dt.time]]:
    """"08:00-22:00" -> (08:00, 22:00). Kosong berarti 24 jam."""
    if not teks:
        return None
    try:
        a, b = teks.split("-", 1)
        jam = lambda t: dt.time(*[int(x) for x in t.strip().split(":")])
        return jam(a), jam(b)
    except (ValueError, TypeError):
        raise SystemExit(
            "MT5_BOT_JAM tidak terbaca: %r. Format: 08:00-22:00" % teks)


def dalam_jam(rentang: Optional[Tuple[dt.time, dt.time]],
              sekarang: dt.time) -> bool:
    if rentang is None:
        return True
    mulai, selesai = rentang
    if mulai <= selesai:
        return mulai <= sekarang <= selesai
    # Rentang yang melewati tengah malam, misal 22:00-06:00.
    return sekarang >= mulai or sekarang <= selesai


# --------------------------------------------------------------------------
# Membaca keadaan akun
# --------------------------------------------------------------------------

def _awal_hari() -> dt.datetime:
    n = dt.datetime.now()
    return dt.datetime(n.year, n.month, n.day)


def catatan_hari_ini(mt5: Any, magic: int) -> Tuple[float, int, Optional[dt.datetime]]:
    """(profit bersih hari ini, jumlah posisi dibuka, waktu entry terakhir).

    Dibaca dari riwayat deal, bukan dari hitungan di memori, supaya robot yang
    baru dinyalakan ulang tetap tahu sudah berapa kali masuk hari ini dan
    tidak mengulang dari nol.
    """
    kolom = {n: i for i, n in enumerate(mt5_history.KOLOM)}
    deals = mt5_history.ambil_deals(mt5, _awal_hari(), dt.datetime.now())

    bersih = 0.0
    masuk = 0
    terakhir: Optional[dt.datetime] = None
    for d in deals:
        if int(d[kolom["magic"]] or 0) != magic:
            continue
        bersih += (float(d[kolom["profit"]]) + float(d[kolom["swap"]])
                   + float(d[kolom["commission"]]) + float(d[kolom["fee"]]))
        if int(d[kolom["entry"]]) == mt5_history.DEAL_ENTRY_IN:
            masuk += 1
            saat = dt.datetime.fromtimestamp(int(d[kolom["time"]]))
            if terakhir is None or saat > terakhir:
                terakhir = saat
    return bersih, masuk, terakhir


def milik_kita(mt5: Any, magic: int, symbol: Optional[str] = None) -> Tuple[int, int]:
    """(jumlah posisi, jumlah order pending) bermagic robot ini."""
    posisi = mt5.positions_get() or ()
    pending = mt5.orders_get() or ()
    cocok = lambda x: (int(getattr(x, "magic", 0)) == magic
                       and (symbol is None or str(x.symbol) == symbol))
    return sum(1 for p in posisi if cocok(p)), sum(1 for o in pending if cocok(o))


# --------------------------------------------------------------------------
# Keputusan
# --------------------------------------------------------------------------

class Lewat(Exception):
    """Alasan satu simbol dilewati pada putaran ini. Bukan kesalahan."""


def arah_dari_tren(mt5: Any, symbol: str, nama_tf: List[str],
                   st: Setelan, sb: SetelanBot) -> Tuple[str, str]:
    hasil = [nilai_timeframe(mt5, symbol, tf, st) for tf in nama_tf]
    label, normal, catatan = kesimpulan(hasil, st)

    tersedia = [h for h in hasil if h.tersedia]
    if not tersedia:
        raise Lewat("semua timeframe gagal dibaca")

    if normal >= sb.skor_min:
        arah = "buy"
    elif normal <= -sb.skor_min:
        arah = "sell"
    else:
        raise Lewat("tren belum cukup kuat: %s (%+.0f%%, ambang %.0f%%)"
                    % (label, normal * 100, sb.skor_min * 100))

    # Satu timeframe yang melawan arah sudah cukup untuk membatalkan: di
    # rentang menit, TF kecil yang berbalik biasanya mendahului koreksi.
    lawan = "BEARISH" if arah == "buy" else "BULLISH"
    bantah = [h.nama for h in tersedia if h.label == lawan]
    if bantah:
        raise Lewat("%s melawan arah %s" % (",".join(bantah), arah.upper()))

    return arah, "%s (%+.0f%%) - %s" % (label, normal * 100, catatan)


def periksa_pagar(mt5: Any, symbol: str, info: Any, sb: SetelanBot,
                  tcfg: TradeConfig, akun: Any,
                  rugi_hari: float, trade_hari: int,
                  entry_terakhir: Optional[dt.datetime]) -> None:
    """Semua alasan untuk TIDAK masuk, dari yang paling murah diperiksa."""
    if trade_hari >= sb.max_trade_hari:
        raise Lewat("jatah trade hari ini habis (%d/%d)"
                    % (trade_hari, sb.max_trade_hari))

    batas_rugi = sb.rugi_harian_persen / 100.0 * float(akun.equity)
    if rugi_hari <= -batas_rugi:
        raise Lewat("batas rugi harian tersentuh (%.2f dari batas -%.2f)"
                    % (rugi_hari, batas_rugi))

    if entry_terakhir is not None:
        jeda = (dt.datetime.now() - entry_terakhir).total_seconds()
        if jeda < sb.cooldown:
            raise Lewat("masih jeda %ds lagi setelah entry terakhir"
                        % int(sb.cooldown - jeda))

    n_pos, n_pend = milik_kita(mt5, tcfg.magic)
    if n_pos + n_pend >= sb.max_posisi:
        raise Lewat("sudah ada %d posisi + %d pending (batas %d)"
                    % (n_pos, n_pend, sb.max_posisi))

    n_pos_s, n_pend_s = milik_kita(mt5, tcfg.magic, symbol)
    if n_pos_s or n_pend_s:
        raise Lewat("%s sudah dipegang robot ini" % symbol)

    if sb.spread_max:
        spread = int(getattr(info, "spread", 0) or 0)
        if spread > sb.spread_max:
            raise Lewat("spread %d poin di atas batas %d"
                        % (spread, sb.spread_max))


# --------------------------------------------------------------------------
# Eksekusi
# --------------------------------------------------------------------------

def rakit_perintah(s: Any, symbol: str, info: Any, ss: mt5_setup.SetelanSetup,
                   tcfg: TradeConfig, kirim: bool) -> SimpleNamespace:
    """Susun argumen untuk cmd_scale, sama persis dengan yang dicetak
    mt5_setup.py di bagian PERINTAH SIAP PAKAI."""
    n_leg = leg_default()
    batas = 0.5 * s.jarak_sl / (n_leg - 1) if n_leg > 1 else 0.0
    gap = mt5_setup.poin(min(ss.gap_atr * s.atr, batas), info) if n_leg > 1 else 0

    return SimpleNamespace(
        arah=s.arah, symbol=symbol, volume=s.lot,
        tp="%.*f" % (int(info.digits), s.tp1), tp_points=None,
        sl=s.sl, sl_points=None,
        legs=n_leg, gap_points=gap if gap >= 1 else None, gap_mode="limit",
        deviation=int(os.getenv("MT5_DEVIATION", "20")),
        comment=os.getenv("MT5_COMMENT", "mt5_bot.py"),
        dry_run=not kirim, yes=True,
    )


def tangani_simbol(mt5: Any, symbol: str, nama_tf: List[str], st: Setelan,
                   ss: mt5_setup.SetelanSetup, sb: SetelanBot,
                   tcfg: TradeConfig, akun: Any, rugi_hari: float,
                   trade_hari: int, entry_terakhir: Optional[dt.datetime],
                   kirim: bool) -> bool:
    """True kalau order benar-benar dikirim."""
    info = mt5_order.prepare_symbol(mt5, symbol)
    periksa_pagar(mt5, symbol, info, sb, tcfg, akun,
                  rugi_hari, trade_hari, entry_terakhir)

    arah, alasan = arah_dari_tren(mt5, symbol, nama_tf, st, sb)
    print("  %s: %s -> %s" % (symbol, alasan, arah.upper()))

    s = mt5_setup.susun_setup(mt5, symbol, info, arah, st, ss, tcfg)
    if not s.lot:
        raise Lewat("ukuran lot keluar 0, setup dibatalkan")
    for c in s.catatan:
        print("    catatan: %s" % c)

    args = rakit_perintah(s, symbol, info, ss, tcfg, kirim)
    n = args.legs if args.legs else 1
    if not s.hedging or s.lot < n * float(info.volume_min):
        # Tanpa akun hedging, beberapa tiket digabung broker jadi satu dan
        # scale tidak ada gunanya. Satu posisi biasa lebih jujur.
        # cmd_market membaca arah dari args.command dan mau TP berupa angka,
        # bukan daftar string seperti cmd_scale.
        args.command = s.arah
        args.tp = float(s.tp1)
        args.legs, args.gap_points = None, None
        print("    (akun non-hedging atau lot terlalu kecil: satu posisi saja)")
        kode = mt5_order.cmd_market(mt5, args, tcfg)
    else:
        kode = mt5_order.cmd_scale(mt5, args, tcfg)

    if not kirim:
        print("    SIMULASI - tidak ada order yang dikirim (pakai --live)")
        return False
    return kode == 0


def satu_putaran(mt5: Any, symbols: List[str], nama_tf: List[str],
                 st: Setelan, ss: mt5_setup.SetelanSetup, sb: SetelanBot,
                 tcfg: TradeConfig, kirim: bool) -> int:
    akun = mt5.account_info()
    if akun is None:
        print("  account_info() kosong, putaran dilewati.")
        return 0

    rugi_hari, trade_hari, entry_terakhir = catatan_hari_ini(mt5, tcfg.magic)
    n_pos, n_pend = milik_kita(mt5, tcfg.magic)
    print("[%s] equity %.2f | hari ini %+.2f, %d trade | terbuka %d pos + %d pending"
          % (dt.datetime.now().strftime("%H:%M:%S"), akun.equity,
             rugi_hari, trade_hari, n_pos, n_pend))

    dikirim = 0
    for symbol in symbols:
        try:
            if tangani_simbol(mt5, symbol, nama_tf, st, ss, sb, tcfg, akun,
                              rugi_hari, trade_hari, entry_terakhir, kirim):
                dikirim += 1
                # Keadaan akun berubah setelah order masuk, jadi dibaca ulang
                # supaya simbol berikutnya dinilai dengan angka terbaru.
                rugi_hari, trade_hari, entry_terakhir = catatan_hari_ini(
                    mt5, tcfg.magic)
        except Lewat as e:
            print("  %s: lewat - %s" % (symbol, e))
        except SystemExit as e:
            # cmd_scale dan kawan-kawan berhenti lewat SystemExit. Di robot,
            # itu tidak boleh mematikan proses: satu simbol bermasalah bukan
            # alasan berhenti memantau simbol lain.
            print("  %s: DITOLAK - %s" % (symbol, str(e).replace("\n", " ")))
    return dikirim


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    sb = SetelanBot()
    p = argparse.ArgumentParser(
        prog="mt5_bot.py",
        description="Pantau tren dan buka posisi otomatis saat syarat terpenuhi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""contoh:
    python mt5_bot.py                      # simulasi semua simbol default
    python mt5_bot.py XAUUSD --sekali      # satu putaran, lalu berhenti
    python mt5_bot.py XAUUSD --live        # benar-benar mengirim order

Tanpa --live tidak ada order yang dikirim.""")
    p.add_argument("symbols", nargs="*",
                   help="simbol yang dipantau (default: MT5_SYMBOL di .env)")
    p.add_argument("--live", action="store_true",
                   help="benar-benar kirim order (tanpa ini hanya simulasi)")
    p.add_argument("--sekali", action="store_true",
                   help="jalankan satu putaran lalu keluar")
    p.add_argument("--interval", type=int, default=sb.interval,
                   help="jeda antar putaran dalam detik (default: %(default)s)")
    p.add_argument("--timeframes", "-t",
                   default=os.getenv("MT5_TREND_TF", mt5_trend.TF_DEFAULT),
                   help="timeframe yang dibaca (default: %(default)s)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    args = build_parser().parse_args(argv)
    cfg = Config()
    st = Setelan()
    ss = mt5_setup.SetelanSetup()
    sb = SetelanBot()
    sb.interval = args.interval
    tcfg = TradeConfig()

    nama_tf = [t.strip().upper() for t in args.timeframes.split(",") if t.strip()]
    if not nama_tf:
        raise SystemExit("Daftar timeframe kosong.")
    nama_tf.sort(key=lambda t: mt5_trend.TF_MENIT.get(t, 0))
    rentang = urai_jam(sb.jam)

    with connect(cfg) as mt5:
        akun = mt5_order.guard_account(mt5, tcfg, assume_yes=False)
        symbols = list(args.symbols) or [cfg.symbol]

        print()
        print("=" * 78)
        print("ROBOT %s" % ("LIVE - ORDER SUNGGUHAN" if args.live else "SIMULASI"))
        print("=" * 78)
        print("  simbol         : %s" % ", ".join(symbols))
        print("  timeframe      : %s" % ",".join(nama_tf))
        print("  ambang tren    : |skor| >= %.0f%%" % (sb.skor_min * 100))
        print("  dasar SL/TP    : %s, swing %d bar, RR 1:%.3g" % (
            ss.tf_setup, ss.swing_bar, ss.rr1))
        print("  batas posisi   : %d terbuka, %d trade/hari" % (
            sb.max_posisi, sb.max_trade_hari))
        print("  batas rugi     : %.2g%% equity per hari" % sb.rugi_harian_persen)
        print("  jeda entry     : %ds" % sb.cooldown)
        print("  jam trading    : %s" % (sb.jam or "24 jam"))
        print("  spread maks    : %s" % (sb.spread_max or "tidak dibatasi"))
        print("  putaran        : tiap %ds%s" % (
            sb.interval, ", sekali jalan" if args.sekali else ""))
        if not args.live:
            print("  ORDER TIDAK DIKIRIM. Tambahkan --live kalau sudah yakin.")
        print("=" * 78)
        print()

        total = 0
        try:
            while True:
                if not dalam_jam(rentang, dt.datetime.now().time()):
                    print("[%s] di luar jam trading (%s), menunggu."
                          % (dt.datetime.now().strftime("%H:%M:%S"), sb.jam))
                else:
                    total += satu_putaran(mt5, symbols, nama_tf, st, ss, sb,
                                          tcfg, args.live)
                if args.sekali:
                    break
                time.sleep(sb.interval)
        except KeyboardInterrupt:
            print("\nDihentikan. %d order dikirim selama sesi ini." % total)
            print("Posisi yang sudah terbuka TETAP jalan dengan SL/TP-nya "
                  "masing-masing - robot tidak menutup apa pun saat berhenti.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

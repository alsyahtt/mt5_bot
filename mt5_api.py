#!/usr/bin/env python3
"""Server HTTP yang menyajikan riwayat trade MT5 sebagai JSON.

    python mt5_api.py                                 # 127.0.0.1:8080
    python mt5_api.py --addr 0.0.0.0:8080 --token RAHASIA
    python mt5_api.py --cors                          # izinkan akses dari peramban

Endpoint:

    GET  /health                status server dan jembatan MT5
    GET  /posisi                posisi terbuka dan order pending
    POST /setup                 hitung entry/SL/TP/lot, tidak mengirim apa pun
    POST /order                 hitung lalu KIRIM order (butuh --allow-order)
    GET  /history               riwayat trade + statistik
         ?hari=30               berapa hari ke belakang
         ?dari=2026-09-01       batas awal (menimpa hari)
         ?sampai=2026-09-16     batas akhir
         ?symbol=XAUUSD         saring satu simbol
         ?magic=990215          saring magic number

Koneksi ke MetaTrader 5 dibuka SEKALI saat server start dan dipakai ulang untuk
semua permintaan, bukan dibuka-tutup tiap kali. Konsekuensinya jembatan
mt5linux harus sudah hidup sebelum server dijalankan (make server), dan kalau
jembatan itu mati, server ini ikut kehilangan sambungan sampai direstart.

Hanya memakai pustaka standar. requirements.txt proyek ini sengaja dipasang
dengan --no-deps, jadi menambahkan Flask atau FastAPI akan merusak susunannya.
"""

import argparse
import datetime as dt
import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import contextlib
import io as _io

import mt5_bot
import mt5_order
import mt5_setup
import mt5_trend
from mt5_client import Config, connect, load_env
from mt5_history import (KOLOM, Statistik, ambil_deals, hasil_ke_dict,
                         offset_server, satukan, tanggal)
from mt5_order import TradeConfig
from mt5_trend import Setelan


# Nilai query diteruskan ke logika riwayat, bukan ke shell, jadi tidak ada
# risiko injeksi perintah. Tetap divalidasi ketat supaya masukan ngawur
# ditolak dengan pesan yang jelas, bukan meledak jadi 500 di tengah jalan.
POLA_SYMBOL = re.compile(r"^[A-Za-z0-9._#&-]{1,32}$")
POLA_TANGGAL = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Galat(Exception):
    """Kesalahan yang layak dijawab dengan kode status tertentu."""

    def __init__(self, kode: int, pesan: str, detail: str = "") -> None:
        super().__init__(pesan)
        self.kode = kode
        self.pesan = pesan
        self.detail = detail


# --------------------------------------------------------------------------
# Validasi parameter
# --------------------------------------------------------------------------

def _satu(q: Dict[str, List[str]], nama: str) -> str:
    nilai = q.get(nama)
    return nilai[0].strip() if nilai else ""


def urai_query(q: Dict[str, List[str]], bawaan_hari: int) -> Dict[str, Any]:
    p: Dict[str, Any] = {"hari": bawaan_hari, "dari": None, "sampai": None,
                         "symbol": None, "magic": None}

    if v := _satu(q, "hari"):
        if not v.lstrip("-").isdigit() or not 1 <= int(v) <= 3650:
            raise Galat(400, "parameter tidak valid",
                        "'hari' harus bilangan bulat 1-3650, dapat %r" % v)
        p["hari"] = int(v)

    for nama in ("dari", "sampai"):
        if v := _satu(q, nama):
            if not POLA_TANGGAL.match(v):
                raise Galat(400, "parameter tidak valid",
                            "%r harus format YYYY-MM-DD, dapat %r" % (nama, v))
            p[nama] = tanggal(v)

    if v := _satu(q, "symbol"):
        if not POLA_SYMBOL.match(v):
            raise Galat(400, "parameter tidak valid",
                        "'symbol' mengandung karakter yang tidak diizinkan: %r" % v)
        p["symbol"] = v.upper()

    if v := _satu(q, "magic"):
        if not v.isdigit():
            raise Galat(400, "parameter tidak valid",
                        "'magic' harus bilangan bulat >= 0, dapat %r" % v)
        p["magic"] = int(v)

    return p


# --------------------------------------------------------------------------
# Keadaan bersama
# --------------------------------------------------------------------------

class Layanan:
    def __init__(self, mt5: Any, cfg: Config, token: str, cors: bool,
                 bawaan_hari: int, izin_order: bool = False) -> None:
        self.mt5 = mt5
        self.cfg = cfg
        self.token = token
        self.cors = cors
        self.bawaan_hari = bawaan_hari
        # Satu koneksi rpyc dipakai bersama semua thread penangan. rpyc tidak
        # aman dipakai bersamaan, jadi setiap sentuhan ke MT5 lewat kunci ini.
        # Artinya permintaan diproses berurutan - itu memang batas jembatannya,
        # bukan sesuatu yang bisa diakali dengan menambah thread.
        self.kunci = threading.Lock()
        self.mulai = time.time()
        self.jumlah_permintaan = 0

        # Setelan trading dibaca sekali saat start, sama seperti skrip CLI.
        self.st = Setelan()
        self.ss = mt5_setup.SetelanSetup()
        self.sb = mt5_bot.SetelanBot()
        self.tcfg = TradeConfig()
        self.nama_tf = sorted(
            [t.strip().upper()
             for t in os.getenv("MT5_TREND_TF", "M1,M5,M15").split(",") if t.strip()],
            key=lambda t: mt5_trend.TF_MENIT.get(t, 0))
        self.izin_order = izin_order

    # -- endpoint yang MENGUBAH keadaan akun -----------------------------

    def _wajib_izin(self) -> None:
        if not self.izin_order:
            raise Galat(403, "endpoint order dimatikan",
                        "jalankan server dengan --allow-order, atau set "
                        "MT5_API_ALLOW_ORDER=yes di .env")

    def _symbol(self, p: dict) -> str:
        symbol = str(p.get("symbol") or self.cfg.symbol).upper()
        if not POLA_SYMBOL.match(symbol):
            raise Galat(400, "symbol tidak valid", symbol)
        return symbol

    def _susun(self, symbol: str, arah: str) -> Tuple[Any, Any, str]:
        """(info, Setup, alasan). Dipanggil dengan kunci sudah dipegang."""
        info = mt5_order.prepare_symbol(self.mt5, symbol)
        if arah == "auto":
            try:
                arah, alasan = mt5_bot.arah_dari_tren(
                    self.mt5, symbol, self.nama_tf, self.st, self.sb)
            except mt5_bot.Lewat as e:
                raise Galat(409, "tren belum memenuhi syarat", str(e))
        else:
            if arah not in ("buy", "sell"):
                raise Galat(400, "arah harus buy, sell, atau auto", arah)
            alasan = "arah dipaksa lewat permintaan"
        s = mt5_setup.susun_setup(self.mt5, symbol, info, arah,
                                  self.st, self.ss, self.tcfg)
        if not s.lot:
            raise Galat(409, "ukuran lot keluar 0", "risiko terlalu kecil "
                        "untuk jarak SL saat ini")
        return info, s, alasan

    @staticmethod
    def _setup_ke_dict(s: Any, info: Any) -> dict:
        d = int(info.digits)
        bulat = lambda x: round(float(x), d)
        return {
            "arah": s.arah,
            "entry": bulat(s.entry),
            "entry_limit": bulat(s.entry_limit) if s.entry_limit is not None else None,
            "sl": bulat(s.sl),
            "sl_poin": mt5_setup.poin(s.jarak_sl, info),
            "ket_sl": s.ket_sl,
            "tp1": bulat(s.tp1), "tp2": bulat(s.tp2), "tp3": bulat(s.tp3),
            "atr": bulat(s.atr),
            "lot": float(s.lot),
            "risiko_uang": round(float(s.rugi_sl), 2),
            "untung_tp1": round(float(s.untung_tp1), 2),
            "hedging": bool(s.hedging),
            "catatan": list(s.catatan),
        }

    def setup(self, p: dict) -> dict:
        """Hitung setup TANPA mengirim apa pun. Aman dipanggil sesering apa pun."""
        symbol = self._symbol(p)
        arah = str(p.get("arah") or "auto").lower()
        with self.kunci:
            self.jumlah_permintaan += 1
            info, s, alasan = self._susun(symbol, arah)
            return {"symbol": symbol, "alasan": alasan,
                    "setup": self._setup_ke_dict(s, info)}

    def order(self, p: dict) -> dict:
        """Hitung setup lalu kirim. dry_run=true (bawaan) berhenti di order_check."""
        self._wajib_izin()
        symbol = self._symbol(p)
        arah = str(p.get("arah") or "auto").lower()
        # Bawaannya SIMULASI. Pemanggil harus menyatakan dry_run=false secara
        # sadar; salah ketik atau field yang lupa diisi tidak boleh berakhir
        # jadi posisi sungguhan.
        kirim = p.get("dry_run") is False

        with self.kunci:
            self.jumlah_permintaan += 1
            if not p.get("abaikan_posisi"):
                n_pos, n_pend = mt5_bot.milik_kita(self.mt5, self.tcfg.magic, symbol)
                if n_pos or n_pend:
                    raise Galat(409, "simbol sudah dipegang",
                                "%s punya %d posisi dan %d pending bermagic %d"
                                % (symbol, n_pos, n_pend, self.tcfg.magic))

            info, s, alasan = self._susun(symbol, arah)
            args = mt5_bot.rakit_perintah(s, symbol, info, self.ss, self.tcfg, kirim)
            n = args.legs or 1
            if not s.hedging or s.lot < n * float(info.volume_min):
                args.command, args.tp = s.arah, float(s.tp1)
                args.legs = args.gap_points = None
                jalan = mt5_order.cmd_market
            else:
                jalan = mt5_order.cmd_scale

            # cmd_* menulis ke stdout dan berhenti lewat SystemExit. Keduanya
            # ditangkap di sini supaya jadi JSON, bukan mematikan server.
            tangkap = _io.StringIO()
            try:
                with contextlib.redirect_stdout(tangkap):
                    kode = jalan(self.mt5, args, self.tcfg)
            except SystemExit as exc:
                raise Galat(422, "order ditolak",
                            str(exc) or tangkap.getvalue().strip())

        log = [b for b in tangkap.getvalue().split("\n") if b.strip()]
        return {
            "symbol": symbol,
            "alasan": alasan,
            "dry_run": not kirim,
            "terkirim": bool(kirim and kode == 0),
            "sebagian_gagal": bool(kirim and kode != 0),
            "setup": self._setup_ke_dict(s, info),
            "legs": args.legs, "gap_points": args.gap_points,
            "log": log,
        }

    def posisi(self) -> dict:
        with self.kunci:
            self.jumlah_permintaan += 1
            posisi = self.mt5.positions_get() or ()
            pending = self.mt5.orders_get() or ()
        bungkus = lambda x: {
            "tiket": int(x.ticket), "symbol": str(x.symbol),
            "volume": float(getattr(x, "volume", getattr(x, "volume_current", 0))),
            "harga": float(getattr(x, "price_open", 0)),
            "sl": float(x.sl), "tp": float(x.tp),
            "magic": int(getattr(x, "magic", 0)),
            "profit": round(float(getattr(x, "profit", 0.0)), 2),
        }
        return {"posisi": [bungkus(x) for x in posisi],
                "pending": [bungkus(x) for x in pending]}

    def riwayat(self, p: Dict[str, Any]) -> dict:
        sampai = p["sampai"] + dt.timedelta(days=1) if p["sampai"] else dt.datetime.now()
        dari = p["dari"] or (sampai - dt.timedelta(days=p["hari"]))
        if dari >= sampai:
            raise Galat(400, "rentang tanggal terbalik",
                        "'dari' harus sebelum 'sampai'")

        with self.kunci:
            self.jumlah_permintaan += 1
            akun = self.mt5.account_info()
            if akun is None:
                raise Galat(503, "tidak bisa membaca akun",
                            "jembatan mt5linux mungkin terputus")
            offset = offset_server(self.mt5, self.cfg.symbol)
            baris = ambil_deals(self.mt5,
                                dari + dt.timedelta(seconds=offset),
                                sampai + dt.timedelta(seconds=offset))

            kolom = {n: i for i, n in enumerate(KOLOM)}
            if p["symbol"]:
                baris = [b for b in baris
                         if str(b[kolom["symbol"]]).upper() == p["symbol"]
                         or int(b[kolom["type"]]) >= 2]
            if p["magic"] is not None:
                baris = [b for b in baris if int(b[kolom["magic"]]) == p["magic"]]

            trades, saldo = satukan(baris)
            digit: Dict[str, int] = {}
            for t in trades:
                if t.symbol and t.symbol not in digit:
                    info = self.mt5.symbol_info(t.symbol)
                    digit[t.symbol] = int(info.digits) if info is not None else 5

        def ke_lokal(ts: int) -> dt.datetime:
            return dt.datetime.fromtimestamp(int(ts) - offset)

        st = Statistik(trades)
        return hasil_ke_dict(trades, saldo, st, akun, dari, sampai,
                             ke_lokal, offset, digit)

    def kesehatan(self) -> Tuple[int, dict]:
        dasar = {
            "status": "ok",
            "waktu": dt.datetime.now().isoformat(timespec="seconds"),
            "uptime_detik": int(time.time() - self.mulai),
            "permintaan_dilayani": self.jumlah_permintaan,
        }
        # Sengaja menyentuh MT5 sungguhan. Health check yang cuma menjawab "ok"
        # tanpa menembus jembatan akan tetap hijau saat terminalnya mati.
        try:
            with self.kunci:
                akun = self.mt5.account_info()
                terminal = self.mt5.terminal_info()
            if akun is None:
                raise RuntimeError("account_info() kosong")
            dasar["mt5"] = {
                "terhubung": True,
                "login": int(akun.login),
                "broker": str(akun.company),
                "server": str(akun.server),
                "mata_uang": str(akun.currency),
                "balance": round(float(akun.balance), 2),
                "equity": round(float(akun.equity), 2),
                "demo": int(akun.trade_mode) != 2,
                "trade_diizinkan": bool(getattr(terminal, "trade_allowed", False)),
            }
            return 200, dasar
        except Exception as exc:
            dasar["status"] = "gagal"
            dasar["mt5"] = {"terhubung": False,
                            "pesan": "%s: %s" % (type(exc).__name__, exc)}
            return 503, dasar


# --------------------------------------------------------------------------
# Penangan HTTP
# --------------------------------------------------------------------------

def samarkan(query: str) -> str:
    """Sembunyikan token supaya tidak ikut tercatat di log."""
    if not query:
        return ""
    bagian = [("token=***" if b.startswith("token=") else b)
              for b in query.split("&")]
    return "?" + "&".join(bagian)


class Penangan(BaseHTTPRequestHandler):
    server_version = "mt5_api"
    sys_version = ""
    layanan: Layanan = None          # diisi saat server dibuat
    protocol_version = "HTTP/1.1"

    # -- utilitas --------------------------------------------------------

    def _kirim(self, kode: int, isi: dict) -> None:
        tubuh = json.dumps(isi, ensure_ascii=False).encode("utf-8")
        self.send_response(kode)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(tubuh)))
        if self.layanan.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.end_headers()
        self.wfile.write(tubuh)

    def _galat(self, kode: int, pesan: str, detail: str = "") -> None:
        isi = {"error": pesan}
        if detail:
            isi["detail"] = detail
        self._kirim(kode, isi)

    def _terotorisasi(self, q: Dict[str, List[str]]) -> bool:
        token = self.layanan.token
        if not token:
            return True
        kepala = self.headers.get("Authorization", "")
        if kepala.startswith("Bearer ") and kepala[7:] == token:
            return True
        # Token lewat query memudahkan uji dari peramban, tapi ikut tercatat
        # di riwayat peramban dan log proxy mana pun di jalurnya.
        return _satu(q, "token") == token

    def log_message(self, format: str, *args: Any) -> None:
        # Bawaan BaseHTTPRequestHandler mencetak URL mentah, termasuk token.
        return

    def _catat(self, kode: int, mulai: float) -> None:
        sys.stderr.write("%s  %s %s%s -> %d  %dms\n" % (
            dt.datetime.now().strftime("%H:%M:%S"),
            self.command, urlparse(self.path).path,
            samarkan(urlparse(self.path).query), kode,
            (time.time() - mulai) * 1000))

    # -- metode --------------------------------------------------------

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        if self.layanan.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        mulai = time.time()
        bagian = urlparse(self.path)
        q = parse_qs(bagian.query)
        kode = 500
        try:
            if not self._terotorisasi(q):
                raise Galat(401, "token tidak valid",
                            "kirim header 'Authorization: Bearer <token>'")
            if bagian.path == "/health":
                kode, isi = self.layanan.kesehatan()
                self._kirim(kode, isi)
            elif bagian.path == "/history":
                isi = self.layanan.riwayat(urai_query(q, self.layanan.bawaan_hari))
                kode = 200
                self._kirim(kode, isi)
            elif bagian.path == "/posisi":
                isi = self.layanan.posisi()
                kode = 200
                self._kirim(kode, isi)
            else:
                raise Galat(404, "endpoint tidak dikenal",
                            "tersedia: GET /health, GET /history, GET /posisi, "
                            "POST /setup, POST /order")
        except Galat as exc:
            kode = exc.kode
            self._galat(kode, exc.pesan, exc.detail)
        except Exception as exc:
            # Jembatan putus di tengah jalan, simbol aneh, apa pun: jawab 503
            # dengan pesan aslinya, jangan diam-diam mengembalikan data kosong
            # yang terlihat seperti "tidak ada trade".
            kode = 503
            self._galat(kode, "gagal mengambil data dari MetaTrader 5",
                        "%s: %s" % (type(exc).__name__, exc))
        finally:
            self._catat(kode, mulai)

    def _baca_json(self) -> dict:
        panjang = int(self.headers.get("Content-Length") or 0)
        if panjang <= 0:
            return {}
        if panjang > 64 * 1024:
            raise Galat(413, "badan permintaan terlalu besar")
        mentah = self.rfile.read(panjang)
        try:
            isi = json.loads(mentah.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Galat(400, "badan permintaan bukan JSON yang sah", str(exc))
        if not isinstance(isi, dict):
            raise Galat(400, "badan permintaan harus objek JSON")
        return isi

    def do_POST(self) -> None:
        mulai = time.time()
        bagian = urlparse(self.path)
        q = parse_qs(bagian.query)
        kode = 500
        try:
            if not self._terotorisasi(q):
                raise Galat(401, "token tidak valid",
                            "kirim header 'Authorization: Bearer <token>'")
            isi_masuk = self._baca_json()
            if bagian.path == "/setup":
                isi = self.layanan.setup(isi_masuk)
            elif bagian.path == "/order":
                isi = self.layanan.order(isi_masuk)
            else:
                raise Galat(404, "endpoint tidak dikenal",
                            "tersedia: POST /setup, POST /order")
            kode = 200
            self._kirim(kode, isi)
        except Galat as exc:
            kode = exc.kode
            self._galat(kode, exc.pesan, exc.detail)
        except Exception as exc:
            kode = 503
            self._galat(kode, "gagal berkomunikasi dengan MetaTrader 5",
                        "%s: %s" % (type(exc).__name__, exc))
        finally:
            self._catat(kode, mulai)

    def do_PUT(self) -> None:
        self._galat(405, "metode tidak didukung", self.command)

    do_DELETE = do_PATCH = do_PUT


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def urai_alamat(teks: str) -> Tuple[str, int]:
    if ":" not in teks:
        raise SystemExit("Alamat harus berbentuk host:port, dapat %r" % teks)
    host, _, port = teks.rpartition(":")
    if not port.isdigit():
        raise SystemExit("Port harus angka, dapat %r" % port)
    return host or "127.0.0.1", int(port)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mt5_api.py",
        description="Server HTTP riwayat trade MT5 dalam bentuk JSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""contoh:
    python mt5_api.py
    python mt5_api.py --addr 0.0.0.0:8080 --token RAHASIA
    python mt5_api.py --cors

    curl 'http://127.0.0.1:8080/history?hari=7'
    curl 'http://127.0.0.1:8080/history?symbol=XAUUSD'
    curl -H 'Authorization: Bearer RAHASIA' 'http://127.0.0.1:8080/history'""")
    p.add_argument("--addr", default=os.getenv("MT5_API_ADDR", "127.0.0.1:8080"),
                   help="alamat dengar (default: %(default)s)")
    p.add_argument("--token", default=os.getenv("MT5_API_TOKEN", ""),
                   help="token Bearer; kosong berarti tanpa autentikasi")
    p.add_argument("--cors", action="store_true",
                   help="izinkan akses lintas-asal dari peramban")
    p.add_argument("--hari", type=int, default=int(os.getenv("MT5_HISTORY_HARI", "30")),
                   help="rentang bawaan kalau klien tidak menyebut (default: %(default)s)")
    p.add_argument("--allow-order", action="store_true",
                   default=os.getenv("MT5_API_ALLOW_ORDER", "").lower()
                           in ("1", "yes", "ya", "true"),
                   help="nyalakan POST /order (mati secara bawaan)")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # load_env() harus mendahului build_parser(): sebagian default argparse
    # dibaca dari os.getenv saat parser dirakit, jadi kalau .env belum masuk
    # nilai di .env akan kalah oleh fallback yang ditulis di kode.
    load_env()
    args = build_parser().parse_args(argv)
    cfg = Config()
    host, port = urai_alamat(args.addr)

    # Riwayat trade adalah data finansial. Mendengar di alamat non-lokal tanpa
    # token berarti siapa pun di jaringan yang sama bisa membacanya, jadi
    # kombinasi itu ditolak daripada dibiarkan lolos diam-diam.
    lokal = host in ("127.0.0.1", "localhost", "::1", "")
    if not lokal and not args.token:
        raise SystemExit(
            "Menolak mendengar di %s tanpa token.\n"
            "Riwayat trade akan terbuka untuk semua orang di jaringan ini.\n"
            "Pakai --token RAHASIA, atau dengarkan di 127.0.0.1 saja." % args.addr)

    # Membaca riwayat tanpa token masih bisa dimaafkan di localhost. MEMBUKA
    # POSISI tidak: proses lain mana pun di mesin ini, termasuk tab peramban
    # yang salah buka, bisa menembakkan order atas nama Anda.
    if args.allow_order and not args.token:
        raise SystemExit(
            "--allow-order butuh --token.\n"
            "Tanpa token, apa pun yang bisa menjangkau port ini bisa membuka "
            "posisi atas nama akun Anda.")
    if args.allow_order and not lokal:
        print("*** PERINGATAN: endpoint order terbuka di alamat non-lokal (%s)."
              % args.addr)
        print("*** Token adalah satu-satunya yang menghalangi. Pastikan "
              "jalurnya tepercaya.")

    print("Menyambung ke MetaTrader 5...")
    with connect(cfg) as mt5:
        akun = mt5.account_info()
        layanan = Layanan(mt5, cfg, args.token, args.cors, args.hari,
                          izin_order=args.allow_order)
        Penangan.layanan = layanan

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            address_family = socket.AF_INET6 if ":" in host and host else socket.AF_INET

        try:
            httpd = Server((host, port), Penangan)
        except OSError as exc:
            raise SystemExit("Tidak bisa mendengar di %s: %s" % (args.addr, exc))

        print("Riwayat trade MT5 di http://%s" % args.addr)
        print("  akun    : %s %s (%s)" % (
            akun.login, akun.name, "DEMO" if int(akun.trade_mode) != 2 else "REAL"))
        print("  auth    : %s" % (
            "token Bearer wajib" if args.token
            else "TIDAK ADA (hanya aman untuk localhost)"))
        print("  cors    : %s" % ("ya" if args.cors else "tidak"))
        print("  order   : %s" % (
            "AKTIF - POST /order bisa membuka posisi" if args.allow_order
            else "mati (pakai --allow-order untuk menyalakan)"))
        if args.allow_order:
            print("            dry_run bawaannya TRUE; kirim \"dry_run\": false "
                  "untuk benar-benar mengirim")
        print("  coba    : curl 'http://%s/history?hari=7'" % args.addr)
        print("  Ctrl-C untuk berhenti.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nBerhenti.")
        finally:
            httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

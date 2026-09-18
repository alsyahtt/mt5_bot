"""Koneksi ke MetaTrader 5.

Dua backend, API pemakaian sama persis:

  local : paket resmi `MetaTrader5`. HANYA Windows, terminal MT5 harus jalan
          di mesin yang sama.
  rpc   : paket `mt5linux`. Dipakai dari macOS/Linux, menembak server MT5
          yang jalan di Windows/Wine (mt5linux server).

Pilih lewat MT5_BACKEND di file .env (lihat .env.example).
"""

import os
import sys
from contextlib import contextmanager
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------
# Konfigurasi
# --------------------------------------------------------------------------

def load_env(path: str = ".env") -> None:
    """Baca file .env sederhana (KEY=VALUE) ke os.environ."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Config:
    def __init__(self) -> None:
        self.backend = os.getenv("MT5_BACKEND", "local").lower()
        self.login = os.getenv("MT5_LOGIN", "")
        self.password = os.getenv("MT5_PASSWORD", "")
        self.server = os.getenv("MT5_SERVER", "")
        self.terminal_path = os.getenv("MT5_PATH", "")   # opsional, backend local
        self.rpc_host = os.getenv("MT5_RPC_HOST", "localhost")
        self.rpc_port = int(os.getenv("MT5_RPC_PORT", "18812"))
        self.rpc_timeout = float(os.getenv("MT5_RPC_TIMEOUT", "180"))
        self.symbol = os.getenv("MT5_SYMBOL", "EURUSD")

    @property
    def has_credentials(self) -> bool:
        return bool(self.login)

    def validate(self) -> None:
        # MT5_LOGIN boleh kosong: kalau terminal sudah login manual, kita pakai
        # akun yang sedang aktif di terminal itu.
        if not self.has_credentials:
            return
        if not self.login.isdigit():
            raise SystemExit("MT5_LOGIN harus berupa angka (nomor akun).")
        missing = [k for k, v in (
            ("MT5_PASSWORD", self.password),
            ("MT5_SERVER", self.server),
        ) if not v]
        if missing:
            raise SystemExit(
                "MT5_LOGIN diisi tapi ini belum: " + ", ".join(missing) +
                "\nKosongkan MT5_LOGIN untuk memakai akun yang sudah login di terminal."
            )


# --------------------------------------------------------------------------
# Pemilihan backend
# --------------------------------------------------------------------------

def get_mt5(cfg: Config) -> Any:
    """Kembalikan objek MT5 sesuai backend. Antarmukanya identik."""
    if cfg.backend == "local":
        try:
            import MetaTrader5 as mt5  # noqa: N813
        except ImportError:
            raise SystemExit(
                "Paket MetaTrader5 tidak tersedia.\n"
                "Paket ini Windows-only, jadi di macOS ia memang tidak bisa dipasang.\n"
                "Dari macOS pakai MT5_BACKEND=rpc (butuh mt5linux server di sisi Windows)."
            )
        return mt5

    if cfg.backend == "rpc":
        try:
            from mt5linux import MetaTrader5
        except ImportError:
            raise SystemExit("Paket mt5linux belum terpasang. Jalankan: make install")
        try:
            client = MetaTrader5(host=cfg.rpc_host, port=cfg.rpc_port)
        except OSError as exc:
            raise SystemExit(
                "Tidak bisa menghubungi mt5linux server di %s:%s (%s).\n"
                "Pastikan di mesin Windows terminal MT5 sudah jalan dan server aktif:\n"
                "  python -m mt5linux --host 0.0.0.0 -p %s <path\\python.exe>"
                % (cfg.rpc_host, cfg.rpc_port, exc, cfg.rpc_port)
            )
        # Default rpyc cuma 30 detik, sedangkan initialize() pertama ke terminal
        # di dalam Wine bisa jauh lebih lama. Naikkan lewat koneksi di baliknya.
        conn = getattr(client, "_MetaTrader5__conn", None)
        if conn is not None:
            conn._config["sync_request_timeout"] = cfg.rpc_timeout
        return client

    raise SystemExit("MT5_BACKEND harus 'local' atau 'rpc', dapat: %r" % cfg.backend)


# --------------------------------------------------------------------------
# Koneksi
# --------------------------------------------------------------------------

@contextmanager
def connect(cfg: Optional[Config] = None):
    """Context manager: initialize + login, dijamin shutdown di akhir."""
    cfg = cfg or Config()
    cfg.validate()
    mt5 = get_mt5(cfg)

    init_kwargs: Dict[str, Any] = {}
    if cfg.has_credentials:
        init_kwargs.update(
            login=int(cfg.login), password=cfg.password, server=cfg.server
        )
    if cfg.terminal_path:
        init_kwargs["path"] = cfg.terminal_path

    if not mt5.initialize(**init_kwargs):
        code, message = mt5.last_error()
        raise SystemExit("Gagal initialize MT5 [%s] %s" % (code, message))

    try:
        # initialize() sudah login kalau kredensial diberikan, tapi login ulang
        # memastikan akun yang benar dipakai bila terminal terhubung ke akun lain.
        if cfg.has_credentials:
            if not mt5.login(int(cfg.login), password=cfg.password, server=cfg.server):
                code, message = mt5.last_error()
                raise SystemExit(
                    "Gagal login akun %s [%s] %s" % (cfg.login, code, message))
        yield mt5
    finally:
        mt5.shutdown()


# --------------------------------------------------------------------------
# Ringkasan untuk verifikasi koneksi
# --------------------------------------------------------------------------

def show_status(mt5: Any, symbol: str) -> None:
    terminal = mt5.terminal_info()
    account = mt5.account_info()

    print("Terminal   :", getattr(terminal, "name", "-"), "build", mt5.version()[1])
    print("Trade allow:", getattr(terminal, "trade_allowed", "-"))
    print()
    print("Akun       :", account.login, "-", account.name)
    print("Broker     :", account.company)
    print("Server     :", account.server)
    print("Leverage   : 1:%s" % account.leverage)
    print("Balance    : %.2f %s" % (account.balance, account.currency))
    print("Equity     : %.2f %s" % (account.equity, account.currency))
    print("Margin free: %.2f %s" % (account.margin_free, account.currency))
    print("Mode       :", "DEMO" if account.trade_mode == 0 else "REAL")
    print()

    if not mt5.symbol_select(symbol, True):
        print("Simbol %s tidak tersedia di Market Watch." % symbol)
        return

    tick = mt5.symbol_info_tick(symbol)
    print("%-10s bid=%s ask=%s spread=%s" % (
        symbol, tick.bid, tick.ask, mt5.symbol_info(symbol).spread))

    positions = mt5.positions_get() or ()
    print("Posisi open:", len(positions))
    for pos in positions:
        print("  #%s %-10s vol=%s profit=%.2f" % (
            pos.ticket, pos.symbol, pos.volume, pos.profit))


def main() -> int:
    load_env()
    cfg = Config()
    print("Backend    : %s%s" % (
        cfg.backend,
        " (%s:%s)" % (cfg.rpc_host, cfg.rpc_port) if cfg.backend == "rpc" else "",
    ))
    if not cfg.has_credentials:
        print("Kredensial : kosong, pakai akun yang aktif di terminal")
    with connect(cfg) as mt5:
        show_status(mt5, cfg.symbol)
    print("\nKoneksi OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

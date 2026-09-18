PYTHON  ?= python3
VENV    := .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
SCRIPT  ?= hello.py
SYMBOL  ?=XAUUSD
ARGS    ?=
TREND_ARGS ?=
SETUP_ARGS ?=
HIST_ARGS  ?=
BOT_ARGS   ?=
API_ADDR   ?= 127.0.0.1:8080
API_ARGS   ?=

.PHONY: help run venv install env connect quote positions order trend setup history bot bot-live api api-order wine-setup wine-check terminal status stop server clean distclean

help: ## Tampilkan daftar target
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

run: ## Jalankan script Python (override: make run SCRIPT=file.py)
	$(PYTHON) $(SCRIPT)

venv: $(VENV)/bin/activate ## Buat virtualenv
$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

install: venv ## Pasang dependency ke virtualenv
	# --no-deps wajib: install_requires mt5linux 0.1.9 adalah pip freeze penulisnya
	# dan mem-pin rpyc==5.0.1. Closure lengkap ditulis manual di requirements.txt.
	$(PIP) install --no-deps -r requirements.txt

env: ## Buat .env dari template (tidak menimpa yang sudah ada)
	@test -f .env && echo ".env sudah ada, dilewati." || (cp .env.example .env && echo ".env dibuat, isi kredensialnya.")

connect: venv ## Tes koneksi ke MetaTrader 5
	$(PY) mt5_client.py

quote: venv ## Harga & aturan trading simbol (make quote SYMBOL=EURUSD)
	$(PY) mt5_order.py quote $(SYMBOL)

positions: venv ## Daftar posisi terbuka
	$(PY) mt5_order.py positions

trend: venv ## Baca tren multi-timeframe (make trend SYMBOL=EURUSD)
	$(PY) mt5_trend.py $(SYMBOL) $(TREND_ARGS)

setup: venv ## Entry/SL/TP + ukuran lot dari tren (make setup SYMBOL=EURUSD)
	$(PY) mt5_setup.py $(SYMBOL) $(SETUP_ARGS)

history: venv ## Riwayat trade + statistik (make history HIST_ARGS="--hari 7")
	$(PY) mt5_history.py $(HIST_ARGS)

order: venv ## Kirim order (make order ARGS="buy EURUSD 0.01 --dry-run")
	@test -n '$(ARGS)' || { echo 'Contoh: make order ARGS="buy EURUSD 0.01 --dry-run"'; exit 1; }
	$(PY) mt5_order.py $(ARGS)

bot: venv ## Robot pantau tren, SIMULASI saja (make bot SYMBOL=XAUUSD)
	$(PY) mt5_bot.py $(SYMBOL) $(BOT_ARGS)

bot-live: venv ## Robot BENAR-BENAR kirim order (make bot-live SYMBOL=XAUUSD)
	@echo "Robot akan MENGIRIM ORDER SUNGGUHAN untuk $(SYMBOL)."
	@printf 'Ketik YA untuk lanjut: ' && read jawab && test "$$jawab" = "YA"
	$(PY) mt5_bot.py $(SYMBOL) --live $(BOT_ARGS)

api: venv ## Jalankan API riwayat trade (make api API_ADDR=127.0.0.1:8080)
	$(PY) mt5_api.py --addr $(API_ADDR) $(API_ARGS)

api-order: venv ## API + endpoint order AKTIF (butuh MT5_API_TOKEN terisi)
	@test -n "$$MT5_API_TOKEN" || grep -q '^MT5_API_TOKEN=..*' .env || \
		{ echo "Isi MT5_API_TOKEN di .env dulu."; exit 1; }
	@echo "POST /order AKTIF. dry_run bawaannya true."
	$(PY) mt5_api.py --addr $(API_ADDR) --allow-order $(API_ARGS)

wine-setup: ## Pasang MT5 + Python Windows ke prefix Wine bawaan MT5
	./setup_wine_mt5.sh all

wine-check: ## Cek status MT5.app / prefix / Python Windows
	./setup_wine_mt5.sh check

terminal: ## Buka GUI MetaTrader 5 (untuk login akun demo)
	./setup_wine_mt5.sh terminal

status: ## Cek jembatan mt5linux nyala dan sehat atau tidak
	./setup_wine_mt5.sh status

stop: ## Hentikan jembatan mt5linux
	./setup_wine_mt5.sh stop

server: venv ## Jalankan jembatan mt5linux (biarkan terbuka di tab terpisah)
	./setup_wine_mt5.sh server

clean: ## Hapus file cache Python
	rm -rf __pycache__ .pytest_cache
	find . -name '*.pyc' -delete

distclean: clean ## Hapus juga virtualenv
	rm -rf $(VENV)

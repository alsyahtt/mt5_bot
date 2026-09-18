# API MT5 — Auto Order

Server HTTP yang menyajikan data akun MetaTrader 5 dan **membuka posisi** lewat
JSON. Hanya memakai pustaka standar Python, tanpa Flask/FastAPI (`requirements.txt`
proyek ini dipasang dengan `--no-deps`, jadi menambah framework akan merusak
susunannya).

> **Endpoint order mati secara bawaan.** Ia hanya hidup kalau dijalankan dengan
> `--allow-order` **dan** token terisi. Baca [Keamanan](#keamanan) sebelum
> menyalakannya.

---

## Daftar isi

- [Menjalankan](#menjalankan)
- [Autentikasi](#autentikasi)
- [Endpoint](#endpoint)
  - [GET /health](#get-health)
  - [GET /posisi](#get-posisi)
  - [GET /history](#get-history)
  - [POST /setup](#post-setup)
  - [POST /order](#post-order)
- [Kode kesalahan](#kode-kesalahan)
- [Keamanan](#keamanan)
- [Setelan `.env`](#setelan-env)
- [Batasan yang perlu diketahui](#batasan-yang-perlu-diketahui)

---

## Menjalankan

Jembatan `mt5linux` harus hidup lebih dulu:

```bash
make server        # biarkan terbuka di tab terpisah
```

Lalu servernya:

```bash
make api                       # baca-saja, 127.0.0.1:8080
make api-order                 # endpoint order AKTIF
```

Atau langsung:

```bash
.venv/bin/python mt5_api.py --addr 127.0.0.1:8080 --token RAHASIA --allow-order
```

| Flag | Bawaan | Arti |
|---|---|---|
| `--addr` | `MT5_API_ADDR` / `127.0.0.1:8080` | alamat dengar |
| `--token` | `MT5_API_TOKEN` | token Bearer; kosong = tanpa auth |
| `--allow-order` | `MT5_API_ALLOW_ORDER` | nyalakan `POST /order` |
| `--cors` | mati | izinkan akses dari peramban |
| `--hari` | `MT5_HISTORY_HARI` / 30 | rentang bawaan `/history` |

Koneksi ke MT5 dibuka **sekali** saat start dan dipakai ulang. Kalau jembatan
mati, server ikut kehilangan sambungan sampai direstart — `GET /health` akan
menjawab `503`, bukan diam-diam mengembalikan data kosong.

---

## Autentikasi

Token dikirim lewat header:

```http
Authorization: Bearer RAHASIA
```

Bisa juga lewat query `?token=RAHASIA` untuk memudahkan uji dari peramban, tapi
cara itu ikut tercatat di riwayat peramban dan log proxy mana pun di jalurnya.
Pakai header untuk apa pun yang bukan sekadar coba-coba.

Token yang lewat query disamarkan di log server. Log bawaan
`BaseHTTPRequestHandler` yang mencetak URL mentah sudah dimatikan.

---

## Endpoint

### GET /health

Status server dan jembatan MT5. Sengaja menyentuh MT5 sungguhan — health check
yang cuma menjawab `ok` tanpa menembus jembatan akan tetap hijau saat
terminalnya mati.

```bash
curl -H 'Authorization: Bearer RAHASIA' http://127.0.0.1:8080/health
```

```json
{
  "status": "ok",
  "waktu": "2026-09-18T10:41:57",
  "uptime_detik": 40,
  "permintaan_dilayani": 12,
  "mt5": {
    "terhubung": true,
    "login": 10012690911,
    "broker": "MetaQuotes Ltd.",
    "server": "MetaQuotes-Demo",
    "mata_uang": "USD",
    "balance": 102805.03,
    "equity": 102805.03,
    "demo": true,
    "trade_diizinkan": true
  }
}
```

`200` kalau sehat, `503` kalau jembatan putus.

---

### GET /posisi

Posisi terbuka dan order pending, **semua magic**, bukan hanya milik robot.

```bash
curl -H 'Authorization: Bearer RAHASIA' http://127.0.0.1:8080/posisi
```

```json
{
  "posisi": [
    {"tiket": 152636686138, "symbol": "XAUUSD", "volume": 0.07,
     "harga": 4327.26, "sl": 4322.57, "tp": 4338.17,
     "magic": 990215, "profit": 11.2}
  ],
  "pending": []
}
```

---

### GET /history

Riwayat trade dan statistik. Profit sudah bersih (kotor + swap + komisi + fee).

| Query | Arti |
|---|---|
| `hari=30` | berapa hari ke belakang |
| `dari=2026-09-01` | batas awal (menimpa `hari`) |
| `sampai=2026-09-18` | batas akhir |
| `symbol=XAUUSD` | saring satu simbol |
| `magic=990215` | saring magic number |

```bash
curl -H 'Authorization: Bearer RAHASIA' \
  'http://127.0.0.1:8080/history?hari=7&symbol=XAUUSD'
```

---

### POST /setup

Hitung entry, SL, TP, dan ukuran lot dari tren berjalan. **Tidak mengirim apa
pun** — aman dipanggil sesering apa pun, dan tidak butuh `--allow-order`.

**Badan permintaan**

```json
{
  "symbol": "XAUUSD",
  "arah": "auto"
}
```

| Field | Wajib | Bawaan | Arti |
|---|---|---|---|
| `symbol` | tidak | `MT5_SYMBOL` | simbol yang dihitung |
| `arah` | tidak | `auto` | `buy`, `sell`, atau `auto` (dari tren) |

Dengan `auto`, arah diambil dari kesimpulan antar-timeframe: `|skor|` harus
≥ `MT5_BOT_SKOR_MIN` **dan** tidak ada satu pun timeframe yang berlabel
berlawanan. Kalau syarat itu tidak terpenuhi, jawabannya `409`.

**Jawaban**

```json
{
  "symbol": "XAUUSD",
  "alasan": "BULLISH KUAT (+78%) - bullish di M15; sideways di M1,M5",
  "setup": {
    "arah": "buy",
    "entry": 4354.98,
    "entry_limit": 4348.45,
    "sl": 4344.48,
    "sl_poin": 1050,
    "ket_sl": "2.0 ATR dari entry (plafon; struktur 2235 poin jauhnya)",
    "tp1": 4370.73, "tp2": 4386.48, "tp3": 4396.98,
    "atr": 5.25,
    "lot": 0.97,
    "risiko_uang": 1018.5,
    "untung_tp1": 1527.75,
    "hedging": true,
    "catatan": [
      "SL dipotong ke plafon 2.0 ATR. Stop TIDAK lagi di luar swing, jadi lebih mudah tersentuh riak pasar."
    ]
  }
}
```

**Baca `catatan`.** Isinya peringatan yang mengubah arti angka di atasnya —
misalnya SL yang tidak lagi menempel struktur, atau lot yang dipotong
`MT5_MAX_LOT` sehingga risiko nyatanya lebih kecil dari target.

---

### POST /order

Hitung setup lalu **kirim order**. Butuh `--allow-order`.

**Badan permintaan**

```json
{
  "symbol": "XAUUSD",
  "arah": "auto",
  "dry_run": true,
  "abaikan_posisi": false
}
```

| Field | Wajib | Bawaan | Arti |
|---|---|---|---|
| `symbol` | tidak | `MT5_SYMBOL` | simbol |
| `arah` | tidak | `auto` | `buy`, `sell`, `auto` |
| `dry_run` | tidak | **`true`** | `false` = benar-benar kirim |
| `abaikan_posisi` | tidak | `false` | `true` = lewati pemeriksaan "simbol sudah dipegang" |

> **`dry_run` bawaannya `true`.** Field yang lupa diisi atau salah ketik tidak
> akan berakhir jadi posisi sungguhan. Untuk benar-benar mengirim, `dry_run`
> harus bernilai `false` secara eksplisit.

**Yang dikirim.** Kalau akun hedging dan lot mencukupi, dipakai `scale`:
`MT5_SCALE_LEGS` posisi terpisah, SL dan TP sama, entry bertingkat sejauh
`MT5_SCALE_GAP_ATR` × ATR (dipangkas agar seluruh ladder tidak memakan lebih
dari setengah jarak ke SL). Kalau tidak, satu order pasar biasa. Ini logika yang
sama persis dengan yang dicetak `make setup` — tidak ada jalur eksekusi kedua.

**Jawaban**

```json
{
  "symbol": "XAUUSD",
  "alasan": "arah dipaksa lewat permintaan",
  "dry_run": true,
  "terkirim": false,
  "sebagian_gagal": false,
  "legs": 3,
  "gap_points": 133,
  "setup": { "...": "sama seperti POST /setup" },
  "log": [
    "Scale-out  : BUY XAUUSD 3 posisi, total 0.96 lot @ 4356.16",
    "  SL semua leg : 4345.48  (1068 poin)",
    "  Jarak antar leg: 133 poin, mode limit (leg 1 pasar, sisanya pending)",
    "  leg 1  0.32 lot  pasar   @ 4356.16  TP 4372.18  SL kena -341.76  TP kena +512.64",
    "  leg 2  0.32 lot  pending @ 4354.83  TP 4372.18  SL kena -299.20  TP kena +555.20",
    "  leg 3  0.32 lot  pending @ 4353.50  TP 4372.18  SL kena -256.64  TP kena +597.76",
    "  Semua leg lolos pre-check.",
    "  RISIKO TOTAL bila SL kena: -897.60 USD (0.87% dari equity 102805.03)",
    "  --dry-run: tidak ada order yang dikirim."
  ]
}
```

| Field | Arti |
|---|---|
| `terkirim` | `true` hanya kalau `dry_run: false` **dan** semua leg berhasil |
| `sebagian_gagal` | `true` kalau sebagian leg terkirim dan sebagian gagal — **periksa `log`** |
| `log` | keluaran mentah dari mesin order, baris demi baris |

**`sebagian_gagal` bukan kesalahan biasa.** Artinya posisi Anda lebih kecil dari
yang diminta. Leg yang terisi tetap membawa SL sehingga risikonya terbatas, dan
`log` memuat perintah untuk mengulang leg yang gagal.

**Contoh**

```bash
# Simulasi — aman, tidak mengirim apa pun
curl -H 'Authorization: Bearer RAHASIA' -X POST \
  http://127.0.0.1:8080/order \
  -d '{"symbol":"XAUUSD","arah":"auto"}'

# Benar-benar mengirim
curl -H 'Authorization: Bearer RAHASIA' -X POST \
  http://127.0.0.1:8080/order \
  -d '{"symbol":"XAUUSD","arah":"auto","dry_run":false}'
```

---

## Kode kesalahan

Semua kesalahan berbentuk `{"error": "...", "detail": "..."}`.

| Kode | Kapan |
|---|---|
| `400` | JSON tidak sah, `symbol` tidak valid, `arah` bukan buy/sell/auto |
| `401` | token salah atau tidak dikirim |
| `403` | `POST /order` dipanggil tapi server tidak dijalankan dengan `--allow-order` |
| `404` | endpoint tidak dikenal |
| `405` | metode tidak didukung |
| `409` | tren belum memenuhi syarat, simbol sudah dipegang, atau lot keluar 0 |
| `413` | badan permintaan di atas 64 KB |
| `422` | order ditolak broker saat pre-check — `detail` memuat alasannya |
| `503` | jembatan mt5linux putus atau MT5 tidak menjawab |

`409` bukan kegagalan sistem. Itu jawaban normal saat pasar sedang tidak
menawarkan apa pun — klien yang memanggil berkala harus memperlakukannya
sebagai "belum waktunya", bukan error yang perlu diulang segera.

---

## Keamanan

Endpoint order bisa memindahkan uang sungguhan. Empat lapis yang berlaku:

1. **Mati secara bawaan.** Tanpa `--allow-order` atau `MT5_API_ALLOW_ORDER=yes`,
   `POST /order` menjawab `403`.
2. **Token wajib.** Server **menolak start** kalau `--allow-order` aktif tanpa
   token — bahkan di `127.0.0.1`. Membaca riwayat tanpa token masih bisa
   dimaafkan di localhost; membuka posisi tidak, karena proses lain mana pun di
   mesin itu (termasuk tab peramban yang salah buka) bisa menembakkan order.
3. **`dry_run` bawaannya `true`.** Mengirim sungguhan butuh pernyataan eksplisit.
4. **Akun REAL terkunci.** `MT5_ALLOW_LIVE=no` menghentikan order di akun uang
   sungguhan, terlepas dari semua di atas.

Selain itu: mendengar di alamat non-lokal tanpa token ditolak sejak awal, dan
kalau endpoint order aktif di alamat non-lokal server mencetak peringatan bahwa
token adalah satu-satunya penghalang.

**Yang TIDAK dilakukan API ini:** tidak ada rate limit, tidak ada kunci
idempotensi. Dua permintaan `dry_run: false` yang sama, dikirim berdekatan untuk
simbol berbeda, akan menghasilkan dua posisi. Untuk simbol yang sama,
pemeriksaan "sudah dipegang" menahannya — kecuali Anda mengirim
`abaikan_posisi: true`.

---

## Setelan `.env`

Yang langsung memengaruhi API:

| Key | Bawaan | Arti |
|---|---|---|
| `MT5_API_ADDR` | `127.0.0.1:8080` | alamat dengar |
| `MT5_API_TOKEN` | kosong | token Bearer |
| `MT5_API_ALLOW_ORDER` | `no` | nyalakan `POST /order` |
| `MT5_HISTORY_HARI` | 30 | rentang bawaan `/history` |

Yang menentukan **isi** setup dan order:

| Key | Bawaan | Arti |
|---|---|---|
| `MT5_TREND_TF` | `M1,M5,M15` | timeframe yang dibaca untuk arah |
| `MT5_BOT_SKOR_MIN` | 0.5 | ambang kekuatan tren untuk `arah: auto` |
| `MT5_SETUP_TF` | `M5` | dasar ATR dan swing pembentuk SL |
| `MT5_SWING_BAR` | 8 | lookback swing pembentuk SL |
| `MT5_SL_ATR_MINIMAL` | 1.0 | lantai lebar SL, kelipatan ATR |
| `MT5_SL_ATR_MAKSIMAL` | 2.0 | plafon lebar SL; 0 = tanpa plafon |
| `MT5_RR` | 1.5 | kelipatan risk-reward TP1 |
| `MT5_RISIKO_PERSEN` | 1.0 | persen equity yang dipertaruhkan |
| `MT5_MAX_LOT` | 1.0 | pagar lot **total** |
| `MT5_SCALE_LEGS` | 3 | jumlah posisi saat TP tunggal |
| `MT5_SCALE_GAP_ATR` | 0.25 | jarak antar entry, kelipatan ATR |
| `MT5_MAGIC` | 990215 | penanda order milik sistem ini |
| `MT5_ALLOW_LIVE` | `no` | izin trading di akun REAL |

---

## Batasan yang perlu diketahui

**Permintaan diproses berurutan.** Satu koneksi rpyc dipakai bersama semua
thread, dan rpyc tidak aman dipakai bersamaan, jadi setiap sentuhan ke MT5
melewati satu kunci. Menambah thread tidak akan mempercepat apa pun — itu batas
jembatannya. `POST /order` bisa memakan beberapa detik karena harus menarik data
beberapa timeframe lalu melakukan pre-check tiap leg.

**API ini hanya membuka posisi.** Tidak ada endpoint untuk memindahkan SL,
trailing, atau menutup posisi. Yang menutup adalah SL/TP yang sudah menempel di
tiap tiket sejak detik pertama. Untuk menutup, pakai
`mt5_order.py close` / `close-all` dari baris perintah.

**Pemeriksaan "sudah dipegang" hanya melihat magic sendiri**
(`MT5_MAGIC`). Posisi yang Anda buka manual di terminal MT5 tidak terlihat dan
tidak akan menahan order baru.

**Pagar menahan, bukan menjamin.** Gap harga saat pembukaan pasar atau rilis
berita bisa melewati SL, dan kerugian nyata bisa lebih besar dari
`RISIKO TOTAL` yang tercetak di `log`.

**`RISIKO TOTAL` mengandaikan semua leg terisi.** Selama order pending belum
kena, risiko nyatanya lebih kecil — tapi angka itu yang harus dipakai sebagai
batas atas.

---

## Terkait

| Berkas | Isi |
|---|---|
| `mt5_api.py` | server ini |
| `mt5_bot.py` | robot yang memantau dan masuk sendiri, tanpa HTTP |
| `mt5_setup.py` | perhitungan entry/SL/TP/lot |
| `mt5_order.py` | mesin order: market, pending, `scale` |
| `mt5_trend.py` | pembacaan tren antar-timeframe |
| `.env.example` | seluruh knob, lengkap dengan penjelasannya |

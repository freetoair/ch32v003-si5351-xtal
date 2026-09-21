# PLL — beleške o radu

Cilj ovog fajla: da znam gde sam i šta sam uradio, da ne bih se gubio.

## Gde sam
- Projekat: `/home/nb/Documents/PlatformIO/Projects/PLL/` (PlatformIO)
- **Pažnja:** `/home/nb/opencode_workbench/pll/` je *zasebna kopija* istog
  projekta (različiti inode-ovi, ne symlink). Razlikuje se samo `NOTES.md`.
  Popravke iz septembra 2025 su primenjene **samo u** `Documents/PlatformIO/Projects/PLL`.
- MCU: **WCH CH32V003J4M6** (RISC-V, SOP-8, 16 KB flash / 2 KB RAM, 48 MHz HSI+PLL),
  board `genericCH32V003J4M6`, framework `arduino` (core je ch32v003fun-baziran)
- PLL čip: **Si5351** (Silicon Labs) — ne SI5361 (moja ranija greška)
- Si5351 I2C adresa: **0x60** (`SI5351_BUS_BASE_ADDR`, `si5351.h:38`)
- Biblioteka: `etherkit/Etherkit Si5351@^2.2.0` + `lib_extra_dirs` ka sketchbook libraries

## Pinout (SOP-8, iz `~/Downloads/CH32v003J4M6.pdf`)
| Pin | Signal | Uloga |
|---|---|---|
| 1 | PD6 (+PA1) | UART **RX** |
| 2 | VSS | masa |
| 3 | PA2 | slobodan |
| 4 | VDD | 3.3 V |
| 5 | PC1 / SDA | Si5351 SDA |
| 6 | PC2 / SCL | Si5351 SCL |
| 7 | **PC4** | UART **TX** (softverski) — na RX adaptera |
| 8 | PD1 (+PD5, +PD4) | **SWIO** — samo WCH-Link |

- I2C je hardverski fiksiran na PC1/PC2 u core-u (`wch-hal-i2c.c`, 100 kHz).
- **Pin 8 deli SWIO i USART TX (PD5).** Zato firmware NE koristi `Serial.begin()`
  nego sopstveni `uart_rx_begin()`: samo RX na PD6, bez TX stepena, PD5 se nikad
  ne konfiguriše. Ulaz nikad ne tera liniju, pa pin 8 ostaje čist SWIO.
- **TX je softverski, na PC4 (pin 7)** (`tx_byte()`). PC4 nema UART funkciju ni u
  jednom remapu, ali bit-bang je ne traži: SysTick slobodno broji na 48 MHz, što
  daje 417 taktova po bitu na 115200 (greška 0.08%). Prekidi su maskirani tokom
  ~87 µs koliko traje bajt. Tako imamo i ACK i čist SWIO.
- ACK je 4 bajta: `STATUS` (0xA6 ok / 0xE5 Si5351 ne odgovara), broj parova,
  checksum, `FLAGS` (bit0 = upisano u flash, bit1 = Si5351 zdrav).
- Remap ne pomaže: `01` traži PD0, `11` traži PC0 (nisu bondovani na SOP-8),
  a `10` bi stavio RX na pin 8 gde bi PC-ov TX terao liniju protiv sonde.

## Šta projekat radi
- `src/main.cpp`: init-uje Si5351 preko sopstvenog I2C sloja; čuva konfiguraciju
  registara u flash (poslednja stranica **0x08003C00**), na startu puni iz flasha, u suprotnom padne na
  default `set_freq(82 MHz, CLK0)`.
- `tools/si5351_gen.py` (CLI) i `tools/si5351_tool.py` (PyQt5 GUI): izračunavaju
  PLL/multisynth registre za ciljnu frekvenciju, šalju bajt-sekvencu kontroleru
  preko seriala (115200); kontroler je čuva u flash i primenjuje na Si5351.
- Serial protokol: `SYNC | LEN | (reg,val) parovi | CHECKSUM`, bez odgovora.
  **`SYNC 0xA5`** = upiši u flash pa primeni (commit).
  **`SYNC 0xA7`** = samo primeni, ne diraj flash (tuning).
  Auto-send u GUI-ju koristi 0xA7 da vrtenje korekcije ne troši flash;
  dugme `Send sequence` šalje 0xA5.
- Kristal na ploči: 25 ili 27 MHz (`SI5351_XTAL`); korekcija u ppb se radi preko
  alata (firmverski `SI5351_CORRECTION` važi samo za fallback na startu).

## Šta sam uradio (istorija)
1. Pronašao pravi projekat u `PlatformIO/Projects/PLL`. Utvrdio da je čip **Si5351**.
2. Potvrdio I2C adresu 0x60 i pinout iz datasheet-a.
3. **Analiza i popravke (21.09.2026.)** — projekat ranije nije mogao da radi preko
   serijskog puta. Popravljeno:
   - `si5351_gen.py`: `CLKn_CTRL` sada nosi `CLKn_SRC = 0b11` (sopstveni
     multisynth). Ranije je bilo 0b00 = **sirovi kristal na izlaz**.
   - `si5351_gen.py`: sekvenca sada uključuje **reg 177** (PLL soft reset) i
     **reg 3** (output enable). Bez reg 3 nema signala jer `Si5351::init()`
     na kraju ugasi sve izlaze.
   - `si5351_gen.py`: popravljene zagrade u `pll_calc` — ppb korekcija je
     pomerala ceo izraz pa je `ref_freq` padao sa 2.5e9 na 250.
   - `si5351_gen.py`: CLK6/CLK7 pišu ceo delilac i pravi reg 92 (ranije mešavina
     `p1` i `r_div` u istim bitovima).
   - `main.cpp`: `flash_unlock()` **pre** `flash_erase_page()` (ranije obrnuto →
     brisanje se tiho ignoriše, drugi upis pada na neobrisani flash).
   - `main.cpp`: `words = (total+3)/4` umesto `(total+3) & ~3` — ranije je pisao
     4× više i čitao 160–256 bajtova iz bafera od 64.
   - `main.cpp`: config stranica 0x3000 → **0x3C00** (stvarna poslednja stranica).
     Kod se završavao na 0x2FC8, tj. 56 bajtova od config stranice.
   - `main.cpp`: `loop()` više ne koristi `Serial.available()` — core ga računa
     kao `tail - head` bez modula, pa postane negativan kad ring (100 B) pregori.
   - `main.cpp`: proverava povratnu vrednost `init()` i I2C greške; ACK nosi status.
   - `si5351_tool.py`: `QApplication.clipboard()` umesto nepostojećeg
     `window().windowHandle().clipboard()`; uklonjeno čekanje ACK-a.
4. **Rad na ploči (21.09.2026.)**
   - Sonda (WCH-LinkE v2.18, `1a86:8010`) nije mogla da se zakači: `Probe is not
     attached to an MCU`. Nije bilo do option bajtova ni do ožičenja — stari
     firmware je terao pin 8 preko PD5 (USART TX).
   - Rešenje: `minichlink -u` (unbrick) — seče 3V3, čeka 240 ms, vraća napajanje
     i u petlji od 500 pokušaja gađa debug modul dok ga ne uhvati pre `setup()`.
     Briše ceo flash. Izgrađen iz `ch32v003fun/minichlink`.
   - Firmware prebačen na RX-only UART. Posle toga sonda ostaje dostupna i dok
     firmware radi — provereno.
   - Čip potvrđen: **CH32V003J4M6, ChipID 0x00330510**.
   - `si5351_ok` (RAM `0x2000001d`) = **0x00** → Si5351 se NE javlja na 0x60.
     Treba proveriti da li je uopšte spojen na PC1/PC2 i napajan.

## I2C sloj (21.09.2026.)
Biblioteka Etherkit i `Wire` se **više ne koriste** — neupotrebljivi su na ovom core-u:
- `TwoWire::endTransmission()` vraća 0 bez obzira da li je slave potvrdio, a za
  prazan prenos vraća 5. Baš prazan prenos je provera prisustva u `Si5351::init()`,
  pa je biblioteka **uvek** zaključivala da čipa nema i preskakala celu inicijalizaciju.
- Core-ov HAL čeka svaki I2C flag bezuslovnim `while` — nespojen čip zakuca firmware
  pre nego što stigne do `loop()`.

`src/main.cpp` sada direktno vozi I2C1: svako čekanje ograničeno na 5 ms, NAK se
čita sa AF flega, 3 pokušaja po transakciji (prva transakcija posle
`i2c_periph_init()` redovno padne — retry je strukturni, ne kozmetički), i
oporavak magistrale sa 9 taktova na SCL ako ostane BUSY.

Posledica: flash 12096 → **2972 bajta**, RAM 728 → **216 bajtova**.
Usput otpala i greška da se `SI5351_BUS_BASE_ADDR` (0x60) prosleđivao kao
`xtal_load_c` u `init()` — `0x60 & 0xC0` = 6 pF umesto 10 pF. Sada se registar 183
ne dira, pa ostaje POR default (10 pF).

## Dijagnostika bez TX linije
Firmware beleži stanje u RAM, čita se sondom (adrese se menjaju između buildova,
naći ih sa `nm`): `si5351_ok`, `cfg_from_flash`, `i2c_diag_probe_ok`,
`i2c_diag_fail_idx` (0xFF = nijedan nije pao), `i2c_diag_fail_cnt`,
`i2c_diag_step`, `i2c_diag_star1/2`, `i2c_scan[16]` (bitmapa skena magistrale).

Zdrava ploča: `si5351_ok=01 cfg_from_flash=01 fail_cnt=00 fail_idx=FF`,
bajt 12 od `i2c_scan` = `01` (adresa 0x60).

## Izvršne verzije (za drugare iz kluba)
```bash
./tools/build_app.sh linux      # -> dist/si5351-tool      (46 MB, jedan fajl)
./tools/build_app.sh windows    # -> dist/si5351-tool.exe  (preko Docker-a)
```
Linux binarni fajl nosi Python i Qt u sebi; linkuje se samo na `libc`, `libdl`,
`libz`, `libpthread`. Testiran sa `env -i` — radi bez ičega instaliranog.
Građen na Ubuntu 22.04 (glibc 2.35); za mnogo stariju distribuciju treba
ponoviti build tamo.

Windows verzija ide kroz `tobix/pywine:3.11` jer PyInstaller ne ume cross-build,
a Wine 6.0.3 sa Ubuntu 22.04 je prestar da instalira moderan Windows Python.
Kontejner se vrti kao root (njegov wineprefix je root-ov), pa na kraju vraća
vlasništvo nad `dist/` i `.build/` pozivaocu.

GUI je podeljen: podrazumevano prost prikaz (frekvencija, izlaz, kristal,
korekcija, Connect, Send). Generisanje registara, C kod i „Embed into main.cpp"
su iza checkbox-a **Advanced**.

## Kako se upisuje firmware
Ako se sonda ne kači (`Probe is not attached to an MCU`), prvo unbrick:
```bash
/tmp/.../minichlink/minichlink -u        # briše flash, oslobađa pin 8
pio run -t upload
```
Sa RX-only firmwareom unbrick više ne bi trebalo da bude potreban.

## Šta ostaje / sledeći koraci
- [x] Si5351 spojen na PC1/PC2 sa pull-up otpornicima — **skenom potvrđen na 0x60**.
- [x] TX sa WCH-LinkE spojen na pin 1 (`/dev/ttyACM0`) — prijem potvrđen.
- [x] Ceo lanac proveren: kadar → flash → reset → replay, bez ijedne I2C greške.
- [ ] **Merenje izlaza na frekvencmetru** — jedino što još nije potvrđeno.
- [ ] Odlučiti šta sa duplikatom `/home/nb/opencode_workbench/pll/`.

## VAŽNO: `pio run -t upload` briše sačuvanu frekvenciju
Upload firmvera briše ceo flash, uključujući config stranicu 0x08003C00.
Posle svakog upisa firmvera treba ponovo poslati sekvencu alatom.
- [ ] Izbor kristala (25 ili 27 MHz) — podesiti `SI5351_XTAL` u `main.cpp`.
- [ ] Odlučiti šta sa duplikatom `/home/nb/opencode_workbench/pll/`
      (obrisati ili zameniti symlink-om).

## Važni putovi
- Firmware: `/home/nb/Documents/PlatformIO/Projects/PLL/src/main.cpp`
- Alat CLI: `/home/nb/Documents/PlatformIO/Projects/PLL/tools/si5351_gen.py`
- Alat GUI: `/home/nb/Documents/PlatformIO/Projects/PLL/tools/si5351_tool.py`
- Biblioteka: `/home/nb/arduino/arduino-1.8.19/portable/sketchbook/libraries/Etherkit_Si5351/`
- Datasheet pinout: `/home/nb/Downloads/CH32v003J4M6.pdf`
- WCH alati za flashovanje: `/home/nb/opencode_workbench/wch_flash/` (openocd-wchlink, wlink, Qt GUI, run.sh)

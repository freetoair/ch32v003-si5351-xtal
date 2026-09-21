# Engineering notes

Why the firmware looks the way it does. Most of what follows was found on the
bench rather than reasoned out in advance, and several of the fixes are not
obvious from reading the final code.

## Hardware

- **CH32V003J4M6** — RISC-V, SOP-8, 16 KB flash / 2 KB RAM, 48 MHz from the
  internal HSI + PLL. PlatformIO board `genericCH32V003J4M6`, `arduino`
  framework (the core is built on ch32v003fun).
- **Si5351A** on I²C address `0x60`, with a 25 MHz or 27 MHz reference crystal.

### Pinout and the constraint it imposes

Several die pads are bonded to one physical pin on SOP-8, which matters more
than the pin count suggests:

| Pin | Signal | Role |
|---|---|---|
| 1 | PD6 (+PA1) | UART **RX** |
| 2 | VSS | ground |
| 3 | PA2 | free |
| 4 | VDD | 3.3 V |
| 5 | PC1 / SDA | Si5351 SDA |
| 6 | PC2 / SCL | Si5351 SCL |
| 7 | **PC4** | UART **TX** (bit-banged) |
| 8 | PD1 (+PD5, +PD4) | **SWIO** — debug probe only |

I²C is fixed at PC1/PC2 by the core (`wch-hal-i2c.c`, 100 kHz); it cannot be
moved from the sketch.

**Pin 8 carries SWIO and the hardware USART TX (PD5) on the same pad.** This is
the single biggest constraint in the design. `Serial.begin()` configures PD5 as
a push-pull alternate-function output, so within milliseconds of reset the
firmware is driving the line the debug probe needs, and the probe can no longer
reach the chip. Recovering it then requires a power-cycle unbrick, which erases
the flash.

This was not deduced, it was observed: flashing succeeded, and the probe was
locked out again the instant the firmware resumed.

So the firmware brings USART1 up itself (`uart_rx_begin()`): **receive only, on
PD6**, and PD5 is never configured. An input never drives its pin, so pin 8
stays a clean SWIO line.

The reply goes out on a **software transmitter on PC4** (`tx_byte()`). PC4 has
no UART function in any remap, but a bit-banged transmitter does not need one:
SysTick free-runs at HCLK, giving 417 ticks per bit at 115200 — 0.08 % off, far
inside what any receiver tolerates. Interrupts are masked for the ~87 µs of a
byte so nothing can stretch a bit; the 1 ms tick is only delayed, never lost,
because its handler advances `CMP` rather than reloading.

The USART1 remap options do not help: remap `01` needs PD0 and remap `11` needs
PC0, neither of which is bonded on SOP-8, and remap `10` would put RX on pin 8,
where the PC's TX line would then fight the probe.

## How it works

`src/main.cpp` initialises the Si5351 over its own I²C layer, stores the
register configuration in the last flash page (**`0x08003C00`**), loads it back
on boot, and falls back to a built-in 82 MHz / CLK0 table when the page is
empty.

`tools/si5351_gen.py` (CLI) and `tools/si5351_tool.py` (GUI) compute the
PLL/multisynth registers for a target frequency and send them over serial at
115200.

Frame: `SYNC | LEN | (reg,val) pairs | CHECKSUM`.

- **`SYNC 0xA5`** — commit: write to flash, then apply.
- **`SYNC 0xA7`** — apply only, leave the stored config alone.

The GUI's auto-send uses `0xA7` so that turning the correction spinner does not
spend flash write endurance; the **Send sequence** button sends `0xA5`.

The controller replies with four bytes: `STATUS` (`0xA6` ok / `0xE5` the Si5351
did not answer), pair count, checksum, and `FLAGS` (bit0 = written to flash,
bit1 = Si5351 healthy).

## What was broken

The serial path had never worked. Every one of these failed silently.

### In the register generator

- `CLKn_CTRL` was written as `int_mode | drive`, leaving the `CLKn_SRC` bits
  (3:2) at `0b00` — which selects the **raw crystal**, not the multisynth. The
  output pin would have carried 25 MHz regardless of the requested frequency.
  They must be `0b11`.
- The sequence omitted **register 177** (PLL soft reset) and **register 3**
  (output enable). Without reg 3 there is no output at all, because
  `Si5351::init()` ends by disabling every output.
- The ppb correction lost a pair of parentheses:
  `ref_freq + (...) * ref_freq >> 31` shifts the *whole* expression, because
  `+` binds tighter than `>>`. For a 100 ppb correction the reference frequency
  collapsed from 2 500 000 000 to **250**, and the registers became noise.
- CLK6/CLK7 mixed `p1` and `r_div` into the same bits and wrote the CLK0–5
  register layout into reg 92.

### In the firmware

- `flash_erase_page()` was called **before** `flash_unlock()`. Erasing locked
  flash is silently ignored (it sets WRPRTERR), so the first write succeeded
  only because the page was already erased, and every later write landed on
  stale flash.
- `words = (total + 3) & ~3` rounds *bytes* up to a multiple of four, but the
  value was used as a *word* count. It wrote four times too much and read
  160–256 bytes out of a 64-byte buffer.
- The config page was at `0x3000`, described in a comment as "the last 1KB
  page". The last page of 16 KB is `0x3C00`; `0x3000` sat **56 bytes** past the
  end of the firmware image.
- Worse, it used the `0x00000000` execution alias. **The flash controller only
  accepts the real `0x08000000` mapping** — erases and programs aimed at the
  alias are ignored without any error. Reads through the alias work fine, which
  is what makes this one hard to spot.
- `loop()` used `Serial.available()`, which the core computes as
  `tail - head` with no modular arithmetic. It goes negative as soon as the
  100-byte RX ring wraps, and reception stalls. The firmware reads from
  `Serial.read()` directly instead.
- `si5351.init(SI5351_I2C_ADDRESS, ...)` passed `0x60` as the first argument,
  but that parameter is `xtal_load_c`, not the address. `0x60 & 0xC0` selects
  6 pF instead of 10 pF. Register 183 is now left alone, so the chip keeps its
  power-on default of 10 pF.

## The I²C layer

Neither the Etherkit library nor `Wire` is used any more. Both are unusable on
this core:

- `TwoWire::endTransmission()` returns 0 whether or not the slave acknowledged,
  and returns 5 for an empty transmission. An empty transmission is exactly the
  presence check `Si5351::init()` performs, so the library **always** concluded
  the chip was absent and skipped its entire initialisation — crystal load
  capacitance, reference frequency, correction and `reset()` included.
- The core's HAL waits on every I²C flag with an unbounded `while`, so a
  missing or half-wired device hangs the firmware before it reaches `loop()`.

`src/main.cpp` drives I2C1 directly: every wait bounded by 5 ms, NAK read
honestly from the AF flag, three attempts per transaction, and recovery of a
stuck bus by clocking SCL nine times by hand and issuing a STOP.

The retry is structural rather than defensive: the first transaction after
`i2c_periph_init()` reliably fails, which was masked for a while by a bus scan
that happened to run first and warm things up.

Dropping the library took the firmware from **12 096 to 2 972 bytes** of flash
and **728 to 216 bytes** of RAM.

## Diagnostics

There is no console — the reply channel is four bytes wide. The firmware
therefore records its state in RAM for the debug probe to read. Addresses move
between builds; find them with `nm`:

```bash
riscv-wch-elf-nm .pio/build/genericCH32V003J4M6/firmware.elf \
  | grep -E "si5351_ok|cfg_from_flash|i2c_diag|i2c_scan"
wlink --chip CH32V003 dump 0x2000000C 16
```

| Symbol | Meaning |
|---|---|
| `si5351_ok` | 1 = the Si5351 answered and every write was acknowledged |
| `cfg_from_flash` | 1 = booted from the stored config, 0 = built-in fallback |
| `i2c_diag_probe_ok` | 1 = the `0x60` presence probe passed |
| `i2c_diag_fail_idx` | first register write that failed (`0xFF` = none) |
| `i2c_diag_fail_cnt` | how many writes failed |
| `i2c_diag_step` | 1 bus busy, 2 no SB, 3 no ADDR (NAK), 4 no TXE, 5 no BTF |
| `i2c_diag_star1/2` | I2C1 status registers captured at that failure |
| `diag_applied` / `diag_stored` | frames applied, and how many were committed |
| `i2c_scan[16]` | bus sweep bitmap: bit `a & 7` of byte `a >> 3` = address `a` answered |

A healthy board reads `si5351_ok = 01`, `cfg_from_flash = 01`,
`i2c_diag_fail_cnt = 00`, `i2c_diag_fail_idx = FF`, and byte 12 of `i2c_scan`
set to `01` (address `0x60`).

## Measured

- **Frequency accuracy**: reconstructed from the generated registers for 14
  typical odd crystals (3.579545, 4.194304, 6.5, 10.245, 11.0592, 14.31818,
  16.9344, 24.576, 27.145, 38.9, 45, 70 MHz, 100 kHz, 32.768 kHz). Worst case
  **0.044 ppm**; 32.768 kHz and 100 kHz come out exact. That is roughly a
  thousand times better than the tolerance of the crystal being replaced.
- **Usable range**: 3.907 kHz to 133.333 MHz. Below the minimum the R divider
  (max 128) can no longer hold the multisynth above its 500 kHz floor; above
  the maximum the divider hits its floor of 6 and the output jumps to 160 MHz,
  and past 150 MHz divide-by-4 pins it at 200 MHz. All of this used to happen
  with no warning at all — the tool now refuses out-of-range targets.
- **Serial**: 21 tuning frames back to back, 945 bytes through a 100-byte ring
  buffer, zero errors, 12 ms per frame including the reply.
- **Persistence**: verified that tuning frames leave the stored config alone and
  that a reset restores the last committed setting.
- Chip identified as **CH32V003J4M6, ChipID `0x00330510`**.

## Flashing

```bash
pio run -t upload
```

If the probe reports `Probe is not attached to an MCU`, the running firmware is
driving pin 8 — this happens with any build whose UART TX is the hardware one.
Recover with minichlink's unbrick, built from `ch32v003fun/minichlink`:

```bash
minichlink -u      # cuts 3V3, restores it, catches the debug module
                   # before setup() runs. Erases the whole flash.
pio run -t upload
```

Current builds should never need this, because pin 8 is left alone.

**`pio run -t upload` erases the stored frequency.** The upload wipes the whole
flash, config page included, so resend the sequence with the tool afterwards.

## Building the executables

```bash
./tools/build_app.sh linux      # -> dist/si5351-tool      (~46 MB, one file)
./tools/build_app.sh windows    # -> dist/si5351-tool.exe  (~28 MB, via Docker)
```

The Linux binary carries Python and Qt inside and links only against `libc`,
`libdl`, `libz` and `libpthread`; it was verified to start under `env -i`. It
is built on Ubuntu 22.04 (glibc 2.35), so a much older distribution would need
the build repeated there.

The Windows build runs inside `tobix/pywine:3.11`, because PyInstaller cannot
cross-compile and Ubuntu 22.04's Wine 6.0.3 is too old to install a modern
Windows Python directly. The container runs as root — its Wine prefix is
root-owned — and hands ownership of `dist/` and `.build/` back at the end.

CI builds both natively on every tag, which avoids the container entirely.

## Open

- [ ] Set `SI5351_XTAL` in `main.cpp` to match the board's crystal (25 or
      27 MHz). It only affects the boot-time fallback; the serial path carries
      its own crystal choice.

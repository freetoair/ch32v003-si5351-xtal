# Si5351 Tool — Frequency Generator + Serial

Tool for the PLL project (WCH CH32V003 + Si5351). You enter a target
frequency, an output (CLK0–7), the output-stage drive current and the
board's reference crystal (25 or 27 MHz), and the tool:
- computes the Si5351 PLL/multisynth registers,
- produces a C line for anyone driving the chip with the Etherkit library,
- sends the byte sequence to the microcontroller over serial — the controller
  stores it in flash and keeps running at that frequency,
- or writes the boot-time fallback table into `src/main.cpp`.

## Running

### Standalone executable (no Python needed)

Prebuilt single-file binaries live in `dist/` — hand one to someone and they
just run it:

| File | Platform |
|---|---|
| `dist/si5351-tool` | Linux x86-64 |
| `dist/si5351-tool.exe` | Windows x86-64 |

To rebuild them:

```bash
./tools/build_app.sh linux      # builds on this machine
./tools/build_app.sh windows    # builds in a Wine container (needs docker)
```

The Windows build runs inside `tobix/pywine`, because PyInstaller cannot
cross-compile and Ubuntu 22.04's Wine 6.0 is too old to install a modern
Windows Python directly.

The Linux binary bundles Python and Qt and links only against `libc`, `libdl`,
`libz` and `libpthread`, so it runs on any reasonably current glibc. It is
built on Ubuntu 22.04 (glibc 2.35); a much older distro would need the build
repeated there.

### From source

```bash
# GUI (PyQt5)
python3 tools/si5351_tool.py

# CLI (no GUI)
python3 tools/si5351_gen.py <freq_Hz> <clk_0-7> [drive_0-3] [xtal_hz] [correction_ppb]
# e.g.:
python3 tools/si5351_gen.py 82000000 0 0
python3 tools/si5351_gen.py 82000000 0 0 27000000 100
```

## GUI buttons

| Button | What it does |
|---|---|
| **Advanced** | Reveals the register maths and the C-code helpers. Off by default. |
| **Generate** | *(Advanced)* Computes the registers and prints the C code + register table. |
| **Copy C code to clipboard** | *(Advanced)* Copies the `si5351.set_freq(...)` line, for use in a sketch that does use the Etherkit library. |
| **Connect** | Opens the selected serial port (115200 8N1). |
| **Send sequence** | Commits: the controller stores the frame in flash, then applies it. No ACK — the firmware is receive-only. |
| **Set boot-time fallback in main.cpp** | *(Advanced)* Rewrites the `FALLBACK_REGS` table — what an unconfigured board comes up on. Rebuild and flash for it to take effect. |

The **Drive** dropdown (2/4/6/8 mA) selects the output-stage drive current;
it is written to the low 2 bits of the CLKn control register.

The **Crystal (XTAL)** dropdown (25 MHz / 27 MHz) selects the board's reference
crystal. The PLL registers (Si5351 PLLA/PLLB block) are computed from this
frequency: with a 25 MHz crystal the PLL multiplier is an integer (a = 32,
800 MHz VCO / 25 MHz), while with a 27 MHz crystal a fractional PLL is used
(a ≈ 29.63). The generated register set includes the PLL block plus the
multisynth block for the chosen output.

### Crystal correction (runtime)

The crystals on these boards are not very accurate, so the output usually
needs a frequency correction in parts-per-billion (ppb). Use the **Correction
(ppb)** field (default 0, range −500000…+500000, i.e. ±500 ppm) to
fine-tune the output. The **Step** list next to it sets how far one arrow
click moves: 100000 or 10000 ppb to close a large offset, 100 or 10 ppb to trim.
Steps smaller than about 30 ppb may not change the output, because the PLL
fraction is not that fine.

The ppb figure needed is the measured error, with the same sign:

    correction_ppb = (measured − target) / target × 10⁹

A positive correction lowers the output. For example, one board read
100.033330 MHz instead of 100 MHz. That is +333300 ppb, and entering it put the
output within 50 Hz of 100 MHz, which was as close as the spectrum analyzer
could read. Offsets that large come from the Si5351 board's crystal, so do not
assume ±10 ppm.


1. Generate with correction = 0 and send the sequence.
2. Measure the actual output on a reliable frequency counter or spectrum
   analyzer.
3. If the reading is off, set the correction so the output matches the target,
   regenerate, and send again. The correction is baked into the PLL
   registers (only the PLL block changes; the multisynth block stays the same
   because the VCO is fixed at 800 MHz).
4. The controller stores the corrected register set in flash, so the
   correction survives a reboot.

The correction lives entirely in the tool: it is baked into the PLL registers
that get sent, so the firmware needs no command and no setting of its own.

### Two frequencies: A and B (e.g. a BFO for USB/LSB)

Fill in **Frequency B** as well, and the board holds two frequencies on the same
output: **A** while package pin 3 is open, **B** while pin 3 is grounded. Output,
drive, crystal, correction and load are shared. Leave **Frequency B** empty for
a single frequency; pin 3 then does nothing.

- **Send sequence** stores both. **Auto-send** applies both without storing.
- The log says which one is on the output. The other one is marked *kept for
  when pin 3 selects it*.
- The pin is debounced for 10 ms. Both frequencies come off the same 800 MHz
  PLL, so a switch rewrites only the registers that differ (the multisynth
  block) with no PLL reset: a few milliseconds, and the PLL stays locked.
- Measured on the bench board with A = 100.000 MHz and B = 100.003 MHz: the
  output followed the wire on pin 3 every time, stepping by 3.000 kHz with a
  clean carrier.

### Crystal load (experimental)

**Crystal load**, under **Advanced**, writes the Si5351 crystal load
capacitance (reg 183: 0, 6, 8 or 10 pF) at the start of the sequence, ahead of
the PLL, whose soft reset at the end relocks it. The default, *chip default
(not written)*, leaves the register as the chip powered up, which is how
every earlier version behaved.

On one board (chip marked only "5351", crystal "25,00") the output ran
+333 ppm high. Reading the chip back (**Read registers**) showed why: reg 183
powers up as `0x00`, i.e. 0 pF, not the datasheet's `0xD2`. The crystal is an
ordinary one; measured with only bits 7:6 written and bits 5:0 left at the
chip's `000000`:

| reg 183 | load | crystal |
|---|---|---|
| `0x00` | 0 pF (power-on) | +333 ppm |
| `0x40` | 6 pF | +49 ppm |
| `0x80` | 8 pF | +9 ppm |
| `0xC0` | 10 pF | −15 ppm |

Bits 5:0 are reserved. The datasheet and the Etherkit library write them as
`010010`, and on that board that broke the carrier up into noise spread over
several MHz until a power cycle. So when a board is connected, the tool reads
reg 183 first and changes only bits 7:6; if the read fails (for example,
firmware older than the register read), it refuses to send a load rather than
guess. With 8 pF, that board came out at +9.3 ppm with no correction at all.
**Generate** and the fallback button use the datasheet's bits when no board is
connected. So:

- Experiment with **Auto-send on change (apply only)** ticked. Nothing is
  written to flash, and **Restore chip defaults** brings it back.
- If you press **Send sequence** with a load selected, the load is stored and
  replayed on every boot. To undo it, send again with *chip default*, then
  press **Restore chip defaults** (or power-cycle the board).
- Unticking **Advanced** puts it back to *chip default*, so a load nobody can
  see is never sent by accident.

**Hands-free correction**: tick **Auto-send on change (apply only)** and the
tool regenerates and sends the sequence automatically every time you change
the correction (or the crystal / frequency) — just turn the correction knob
and watch the counter converge. When the reading matches, press
**Send sequence** once to commit it.

Auto-send deliberately uses the apply-only frame (`0xA7`). A commit erases and
rewrites a flash page, and the correction spinner emits an event per step, so
committing on every step would spend the page's write endurance in one tuning
session. Tune freely, commit once.

`Generate` is optional (preview only); `Send sequence` already recomputes from
the current fields, so you never need to click `Generate` before sending.

## Serial protocol

Frame sent by the tool to the controller:

```
+------+-------+---------------------+----------+
| SYNC | LEN   | PAYLOAD (pairs)    | CHECKSUM |
| 0xA5 | 1 B   | 2*N bytes          | 1 B       |
+------+-------+---------------------+----------+
```

- `SYNC` selects what the controller does with the frame:
  - `0xA5` — **commit**: save to flash, then apply.
  - `0xA7` — **apply only**: drive the Si5351, leave the stored config alone.
  - `0xAA` / `0xAB` — the same two for **frequency B**. A B frame with no
    pairs removes B.

  Everything after the SYNC byte is identical for both.
- `LEN` = number of bytes in PAYLOAD (= 2 × number of (reg, val) pairs)
- `PAYLOAD` = sequence of (register, value) pairs — the Si5351 registers to write.
- `CHECKSUM` = sum of all PAYLOAD bytes, taken modulo 256.

The controller (firmware) parses the frame in `loop()`. For a valid frame it:
1. stores the pairs in flash — last 1KB page, A at **`0x08003C00`** and B at
   **`0x08003C40`**, each `[count][pairs][checksum]`: unlock, page
   erase, word program (WCH FPEC, keys `0x45670123`/`0xCDEF89AB`). The flash
   controller only accepts the real `0x08000000` mapping; erases and programs
   issued against the `0x00000000` execution alias are silently ignored.
2. applies them to the Si5351 over a bounded I2C path (see **I2C** below) —
   but only if pin 3 selects that frequency; otherwise it keeps them for when
   it does.
The controller replies on its software TX pin with four bytes:

```
STATUS  0xA6 = applied, every register acknowledged by the Si5351
        0xE5 = frame received, but nothing answered at 0x60
COUNT   number of (reg, val) pairs it acted on
CHKSUM  the payload checksum it computed
FLAGS   bit0 = written to flash, bit1 = Si5351 healthy,
        bit2 = kept for later (pin 3 selects the other frequency)
```

The tool shows this in plain language, so a missing or miswired Si5351 is
reported rather than silently ignored.

### Reading registers back

`SYNC 0xA8` asks for Si5351 registers instead of writing them. `PAYLOAD` is a
list of register numbers, one byte each, at most 60, and the checksum is formed
the same way. The reply is:

```
STATUS  0xA6 = every read acknowledged / 0xE5 = at least one was not
COUNT   number of registers
VALUES  one byte per register, in the order asked (0x00 where a read failed)
CHKSUM  sum of VALUES modulo 256
```

**Read registers** under **Advanced** in the tool sends this frame. It accepts
decimal or `0x` hex numbers and ranges, e.g. `0, 3, 26-33, 183`. Reading
changes nothing on the chip. Firmware older than this frame does not answer it.

### Back to normal without a power cycle

The Si5351 has no software reset for the whole chip, and it shares its supply
with the controller. So at start-up, before writing anything, the firmware
reads reg 183 (crystal load) and remembers it. Two buttons under **Advanced**
use that:

- **Restore chip defaults** writes the remembered reg 183 back, re-applies the
  stored frequency without any crystal load it carries (or the built-in
  82 MHz fallback if nothing is stored), and resets both PLLs.
- **Clear stored config** asks first, erases the stored setting, then does the
  same. The board runs the 82 MHz fallback until **Send sequence** stores a new
  one. This is the way out when something bad got stored.

The remembered value is the chip's true power-on value only if the Si5351 was
powered up together with the controller. Reflashing resets the controller
alone, so after flashing, power-cycle the board once before relying on
**Restore**.

Both are `SYNC 0xA9` frames with a one-byte payload (`0x01` restore, `0x02`
clear). The reply is four bytes: `STATUS` (`0xA6` ok, `0xE5` the Si5351 did
not acknowledge every write, `0xE0` unknown command), the command echoed, the
remembered reg 183 value, and `FLAGS` (bit0 = the stored config was applied,
bit1 = Si5351 healthy, bit2 = the power-on reg 183 is known).

You can also read the stored config back over the WCH-Link while the firmware
runs:

```bash
wlink --chip CH32V003 dump 0x08003C00 64
```

The page holds `[uint16 count][(reg,val) pairs][uint8 checksum]`, i.e. the exact
sequence the tool sent.

Note that `pio run -t upload` erases this page along with the rest of the flash,
so a stored frequency does not survive a firmware update — resend it afterwards.

### What is in the sequence

Beyond the PLL block and the multisynth block, the sequence ends with two
registers that the output will not appear without:

- `[0xB1]` (reg 177) — **PLL soft reset** (`0x20` for PLLA, `0x80` for PLLB).
  The Si5351 does not adopt new PLL parameters until the PLL is reset.
- `[0x03]` — **output enable** (0 = on). `Si5351::init()` ends by disabling
  every output, so a register replay that omits reg 3 produces silence.

`CLKn_CTRL` (`[0x10]` + n) also carries `CLKn_SRC = 0b11` (bits 3:2), i.e. the
clock's own multisynth. With those bits at 0 the pin would output the raw
crystal instead of the synthesized frequency.

On boot (`setup()`), the controller tries to load the stored config from flash
(page 0x08003C00) and apply it to the Si5351; if there is no stored config, it
falls back to the compiled-in `FALLBACK_REGS` table (82 MHz on CLK0).

`0x08003C00` is the last 1KB page of the 16KB flash. The firmware image must
stay below it — check the end of `.data` in `firmware.map` after changing the
code.

## Example

For 96 MHz on CLK1 with a 25 MHz crystal the tool gives:
```
si5351.set_freq(9600000000ULL, SI5351_CLK1);
[0x1A] = 0x00
[0x1B] = 0x01
[0x1C] = 0x00
[0x1D] = 0x0E
[0x1E] = 0x00
[0x1F] = 0x00
[0x20] = 0x00
[0x21] = 0x00
[0x32] = 0x42
[0x33] = 0x40
[0x34] = 0x00
[0x35] = 0x02
[0x36] = 0x2A
[0x37] = 0xFA
[0x38] = 0x2C
[0x39] = 0x00
[0x11] = 0x0C
[0xB1] = 0x20
[0x03] = 0xFD
```
With a 27 MHz crystal only the PLLA block changes (fractional PLL):
`[0x1A]=0x42 [0x1B]=0x40 [0x1C]=0x00 [0x1D]=0x0C [0x1E]=0xD0 [0x1F]=0xF9 [0x20]=0x0A [0x21]=0x80`;
the multisynth block and the trailing reg 177 / reg 3 stay the same, because
the VCO target is fixed at 800 MHz.

## I2C

The Si5351 sits on PC1/PC2 at address `0x60`. The firmware uses neither the
Etherkit library nor `Wire`: both are unusable on this core.

- `TwoWire::endTransmission()` returns 0 whether or not the slave acknowledged,
  and returns 5 for an empty transmission — which is exactly the presence check
  `Si5351::init()` performs, so the library always concluded the chip was
  absent and skipped its entire initialisation.
- The core's HAL waits on every I2C flag with an unbounded `while`, so a missing
  or half-wired device hangs the firmware before it reaches `loop()`.

`src/main.cpp` therefore drives I2C1 directly. Every wait is bounded by
`I2C_TIMEOUT_US` (5 ms), NAK is reported honestly via the AF flag, each
transaction gets `I2C_ATTEMPTS` (3) tries — the first transaction after
`i2c_periph_init()` reliably fails, so one retry is structural, not defensive —
and a bus left stuck busy is recovered by clocking SCL nine times by hand and
issuing a STOP (`i2c_bus_recover()`).

### Diagnostics

With no TX line there is no console, so the firmware records its state in RAM
for the debug probe to read. Addresses shift between builds; get them with:

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
| `i2c_diag_fail_idx` | index of the first register write that failed (`0xFF` = none) |
| `i2c_diag_fail_cnt` | how many writes failed |
| `i2c_diag_step` | where the last failure was: 1 bus busy, 2 no SB, 3 no ADDR (NAK), 4 no TXE, 5 no BTF |
| `i2c_diag_star1/2` | I2C1 status registers captured at that failure |
| `diag_applied` | frames applied to the Si5351 since boot |
| `diag_stored` | how many of those were also committed to flash |
| `i2c_scan[16]` | bus sweep bitmap: bit `a & 7` of byte `a >> 3` set means address `a` acknowledged |

A healthy board reads `si5351_ok = 01`, `i2c_diag_fail_cnt = 00`,
`i2c_diag_fail_idx = FF`, and byte 12 of `i2c_scan` = `01` (address `0x60`).
The sweep runs last in `setup()`, so it never delays the output coming up.

## Board wiring (CH32V003J4M6, SOP-8)

Several die pads are bonded to a single pin on this package:

| Pin | Signal | Use |
|---|---|---|
| 1 | PD6 (+ PA1) | UART **RX** — from the USB-serial TX |
| 2 | VSS | ground |
| 3 | PA2 | free |
| 4 | VDD | 3.3 V |
| 5 | **PC1 / SDA** | Si5351 SDA |
| 6 | **PC2 / SCL** | Si5351 SCL |
| 7 | **PC4** | UART **TX** (software) — to the adapter's RX |
| 8 | PD1 / **SWIO** | WCH-Link only |

I2C is fixed at PC1/PC2 in the core (`wch-hal-i2c.c`, 100 kHz); it is not
configurable from the sketch.

### Why the TX is bit-banged

Pin 8 carries PD1 (**SWIO**), PD5 (**USART1 TX**) and PD4 on one bonded pad.
`Serial.begin()` configures PD5 as a push-pull alternate-function output, so as
soon as the firmware starts it drives pin 8 and the debug probe can no longer
reach the chip — recovering it then needs a power-cycle unbrick
(`minichlink -u`), which erases the flash. This was observed on the bench, not
deduced: flashing succeeded, and the probe was locked out again the moment the
firmware resumed.

The firmware therefore brings USART1 up itself (`uart_rx_begin()` in
`main.cpp`) for **receive only, on PD6**, and never configures PD5. An input
never drives its pin, so pin 8 stays a clean SWIO line.

For the reply it uses a **software transmitter on PC4** (`tx_byte()`). PC4 has
no UART function in any remap, but a bit-banged transmitter does not need one:
SysTick free-runs at HCLK (48 MHz), which gives 417 ticks per bit at 115200 —
0.08 % off, far inside what a receiver tolerates. Interrupts are masked for the
~87 µs of a byte so nothing can stretch a bit; the 1 ms tick is only delayed,
never lost, because its handler advances `CMP` instead of reloading.

The USART1 remap alternatives do not help: remap `01` needs PD0 and remap `11`
needs PC0, neither of which is bonded on SOP-8, and remap `10` would put RX on
pin 8 where the PC's TX line would then fight the probe (CH32V003 datasheet
v1.3, pin table p. 11).

## Live test (board + WCH-Link)

1. Connect the board over WCH-Link (USB) and flash the firmware:
   `platformio run -t upload` (or `apio upload`).
2. Open the tool, select the serial port the CH32V003 is on (e.g. `/dev/ttyUSBx` or `/dev/ttyACMx`).
3. Enter the frequency, output and drive, select the board's crystal (25/27 MHz),
   click **Connect**, then **Send sequence**. Wire the USB-serial adapter's
   **TX to pin 1** and its **RX to pin 7**.
   The crystal you pick here is carried in the sequence, so the firmware needs
   no matching setting — see the note on `FALLBACK_REGS` in NOTES.md.
4. The controller stores the sequence in flash and keeps running at that frequency —
   after a restart the same frequency is restored from flash.

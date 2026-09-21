"""
si5351_gen.py — computes Si5351 PLL/multisynth registers for a given frequency.

Written as a faithful Python equivalent of the Etherkit Si5351 library
(formulas and constants taken directly from si5351.cpp / si5351.h), which is
Copyright (C) Jason Milldrum NT7S and licensed under the GNU GPL v3. This file
is therefore a derivative work and carries the same licence.

    Copyright (C) 2026 freetoair (YT1BN), written with Claude Code (Anthropic).

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
"""

# Constants (from si5351.h)
FREQ_MULT = 100
XTAL_FREQ = 25000000          # referentni kristal (Hz)
PLL_FIXED = 80000000000       # SI5351_PLL_FIXED (800 MHz * FREQ_MULT)
VCO_MIN = 600000000
VCO_MAX = 900000000
MS_MIN = 500000
MS_DIVBY4 = 150000000
MS_MAX = 225000000
MS_SHARE_MAX = 100000000
CLKOUT_MIN = 4000
MS_A_MIN = 6
MS_A_MAX = 1800
PLL_A_MIN = 15
PLL_A_MAX = 90
RFRAC_DENOM = 1000000

# Usable output range with the VCO parked at 800 MHz.
#   low  — below this the R divider (max 128) can no longer keep the multisynth
#          above its 500 kHz floor, and the output silently lands too high.
#   high — 800 MHz / MS_A_MIN(6); above it the divider hits its floor and the
#          output jumps to 160 MHz, and past 150 MHz divide-by-4 pins it at
#          200 MHz. Both used to happen with no warning at all.
FREQ_MIN = 3907
FREQ_MAX = PLL_FIXED // FREQ_MULT // MS_A_MIN      # 133_333_333


class FrequencyOutOfRange(ValueError):
    """Raised for a target the fixed-800 MHz-VCO scheme cannot actually produce."""


def _fmt_hz(hz):
    if hz >= 1000000:
        return "%g MHz" % (hz / 1000000.0)
    if hz >= 1000:
        return "%g kHz" % (hz / 1000.0)
    return "%d Hz" % hz


def parse_freq(text):
    """Accept '3579545', '10.245M', '10.245 MHz', '32.768k', '0,5 MHz'."""
    t = str(text).strip().lower().replace(",", ".")
    for suffix in ("hertz", "hz"):
        if t.endswith(suffix):
            t = t[:-len(suffix)].strip()
            break
    mult = 1.0
    if t.endswith("g"):
        mult, t = 1e9, t[:-1]
    elif t.endswith("m"):
        mult, t = 1e6, t[:-1]
    elif t.endswith("k"):
        mult, t = 1e3, t[:-1]
    t = t.strip()
    if not t:
        raise ValueError("no frequency given")
    return int(round(float(t) * mult))

# Register addresses
PLLA_PARAMETERS = 26
PLLB_PARAMETERS = 34
CLK0_PARAMETERS = 42
CLK6_PARAMETERS = 90
CLK7_PARAMETERS = 91
CLK67_OUTPUT_DIVIDER = 92
CLK_CTRL_BASE = 16
CLK_INTEGER_MODE = 1 << 6
CLK_PLL_SELECT = 1 << 5          # 0 = PLLA, 1 = PLLB
CLK_SRC_MULTISYNTH_N = 3 << 2    # CLKn_SRC = own multisynth (NOT the raw XTAL)
OUT_DIVBY4 = 3 << 2          # 0x0C
OUT_DIV_SHIFT = 4
OUTPUT_ENABLE_CTRL = 3       # reg 3: 0 = output enabled, 1 = disabled
PLL_RESET = 177
CRYSTAL_LOAD = 183
# Crystal load capacitance, bits 7:6 of reg 183. Bits 5:0 are reserved and
# must be written as 0b010010 (Si5351 datasheet / AN619, Etherkit library).
CRYSTAL_LOAD_PF = {0: 0x00, 6: 0x40, 8: 0x80, 10: 0xC0}
CRYSTAL_LOAD_RESERVED = 0x12
PLL_RESET_A = 1 << 5
PLL_RESET_B = 1 << 7

def _clamp(v, lo, hi):
    if v < lo: return lo
    if v > hi: return hi
    return v

def do_div(n, base):
    """C macro do_div: returns remainder, n becomes n/base. We return the quotient here."""
    return n // base

def select_r_div(freq):
    """Select R divider exactly as in si5351.cpp. Returns (r_div_code, scaled_freq)."""
    r_div = 0               # DIV_1
    lo = CLKOUT_MIN * FREQ_MULT
    if freq < lo * 2:
        r_div = 7           # DIV_128
        freq *= 128
    elif freq < lo * 4:
        r_div = 6           # DIV_64
        freq *= 64
    elif freq < lo * 8:
        r_div = 5           # DIV_32
        freq *= 32
    elif freq < lo * 16:
        r_div = 4           # DIV_16
        freq *= 16
    elif freq < lo * 32:
        r_div = 3           # DIV_8
        freq *= 8
    elif freq < lo * 64:
        r_div = 2           # DIV_4
        freq *= 4
    elif freq < lo * 128:
        r_div = 1           # DIV_2
        freq *= 2
    return r_div, freq

def multisynth_calc(freq, pll_freq):
    """Equivalent of Si5351::multisynth_calc. Returns (p1, p2, p3, out_pll_freq)."""
    freq = _clamp(freq, MS_MIN * FREQ_MULT, MS_MAX * FREQ_MULT)
    divby4 = 1 if freq >= MS_DIVBY4 * FREQ_MULT else 0

    if pll_freq == 0:
        if divby4 == 0:
            a = do_div(VCO_MAX * FREQ_MULT, freq)
            if a == 5: a = 4
            elif a == 7: a = 6
        else:
            a = 4
        b, c = 0, 1
        pll_freq = a * freq
    else:
        a = pll_freq // freq
        if a < MS_A_MIN:
            freq = pll_freq // MS_A_MIN
        if a > MS_A_MAX:
            freq = pll_freq // MS_A_MAX
        b = (pll_freq % freq * RFRAC_DENOM) // freq
        c = RFRAC_DENOM if b else 1

    if divby4 == 1:
        p3, p2, p1 = 1, 0, 128 * a - 512
    else:
        p1 = 128 * a + (128 * b) // c - 512
        p2 = 128 * b - c * ((128 * b) // c)
        p3 = c

    return p1, p2, p3, pll_freq

def pll_calc(freq, ref_freq, correction=0):
    """Equivalent of Si5351::pll_calc (vcxo=0). Returns (p1, p2, p3, actual_vco)."""
    if correction:
        ref_freq = ref_freq + (((((correction << 31) // 1000000000) * ref_freq) >> 31))
    freq = _clamp(freq, VCO_MIN * FREQ_MULT, VCO_MAX * FREQ_MULT)
    a = freq // ref_freq
    if a < PLL_A_MIN:
        freq = ref_freq * PLL_A_MIN
    if a > PLL_A_MAX:
        freq = ref_freq * PLL_A_MAX
    b = (freq % ref_freq * RFRAC_DENOM) // ref_freq
    c = RFRAC_DENOM if b else 1
    p1 = 128 * a + (128 * b) // c - 512
    p2 = 128 * b - c * ((128 * b) // c)
    p3 = c
    actual = ((ref_freq * b) // c) + ref_freq * a
    return p1, p2, p3, actual

def _byte(v, shift=0, mask=0xFF):
    return ((v >> shift) & mask) & 0xFF

def _rf(p3, p2):
    return (((p3 >> 12) & 0xF0) + ((p2 >> 16) & 0x0F)) & 0xFF

def _divreg(p1, r_div, div_by_4):
    return (((p1 >> 16) & 0x03) | (OUT_DIVBY4 if div_by_4 else 0) | (r_div << OUT_DIV_SHIFT)) & 0xFF

def pll_registers(pll, p1, p2, p3):
    """Returns (address, value) list for PLLA (26..) or PLLB (34..)."""
    base = PLLA_PARAMETERS if pll == 0 else PLLB_PARAMETERS
    regs = [
        (base + 0, _byte(p3, 8)),
        (base + 1, _byte(p3)),
        (base + 2, _byte(p1, 16, 0x03)),
        (base + 3, _byte(p1, 8)),
        (base + 4, _byte(p1)),
        (base + 5, _rf(p3, p2)),
        (base + 6, _byte(p2, 8)),
        (base + 7, _byte(p2)),
    ]
    return regs

def clk_ctrl(clk, int_mode, drive):
    """CLKn_CTRL (reg 16+n): powered up, MSn as clock source, PLL select, drive.

    The CLKn_SRC bits (3:2) MUST be 0b11 (own multisynth); leaving them at 0b00
    routes the raw crystal to the pin instead of the synthesized frequency."""
    val = (CLK_INTEGER_MODE if int_mode else 0)
    val |= CLK_PLL_SELECT if clk >= 6 else 0     # CLK0-5 -> PLLA, CLK6/7 -> PLLB
    val |= CLK_SRC_MULTISYNTH_N
    val |= (drive & 0x03)
    return (CLK_CTRL_BASE + clk, val & 0xFF)


def ms_registers(clk, p1, p2, p3, int_mode, r_div, div_by_4, drive=0):
    """Return (address, value) list for the CLKn multisynth block.
    drive: 0=2mA, 1=4mA, 2=6mA, 3=8mA (low 2 bits of CLKn_CTRL)."""
    if clk <= 5:
        base = CLK0_PARAMETERS + clk * 8
        regs = [
            (base + 0, _byte(p3, 8)),
            (base + 1, _byte(p3)),
            (base + 2, _divreg(p1, r_div, div_by_4)),
            (base + 3, _byte(p1, 8)),
            (base + 4, _byte(p1)),
            (base + 5, _rf(p3, p2)),
            (base + 6, _byte(p2, 8)),
            (base + 7, _byte(p2)),
        ]
        regs.append(clk_ctrl(clk, int_mode, drive))
        return regs

    # MS6/MS7 are integer-only dividers held in a single register each,
    # and their R dividers share reg 92 (bits 2:0 for R6, bits 6:4 for R7).
    a = (p1 + 512) // 128
    if clk == 6:
        return [
            (CLK6_PARAMETERS, a & 0xFF),
            (CLK67_OUTPUT_DIVIDER, r_div & 0x07),
            clk_ctrl(clk, int_mode, drive),
        ]
    return [
        (CLK7_PARAMETERS, a & 0xFF),
        (CLK67_OUTPUT_DIVIDER, (r_div & 0x07) << OUT_DIV_SHIFT),
        clk_ctrl(clk, int_mode, drive),
    ]

def set_freq(freq_hz, clk=0, pll_freq=PLL_FIXED, drive=0, xtal_hz=XTAL_FREQ, correction=0,
             xtal_load_pf=None):
    """Main function: compute everything and return the result.

    freq_hz  - target output frequency in Hz
    clk       - 0..7 (CLKn)
    pll_freq  - fixed PLL (default 800 MHz * FREQ_MULT)
    drive     - output drive current: 0=2mA, 1=4mA, 2=6mA, 3=8mA
    xtal_hz   - board crystal frequency in Hz (25000000 or 27000000)
    correction- crystal frequency correction in parts-per-billion (ppb),
                  adjusted at runtime against a reliable frequency counter
    xtal_load_pf - crystal load capacitance (0, 6, 8 or 10 pF) written to
                  reg 183 ahead of the PLL; None leaves the register alone
    """
    if freq_hz < FREQ_MIN or freq_hz > FREQ_MAX:
        raise FrequencyOutOfRange(
            "%s is outside the range this generator can produce (%s to %s). "
            "Below the minimum the output lands too high; above the maximum it "
            "jumps to 160 MHz, and past 150 MHz it is pinned at 200 MHz."
            % (_fmt_hz(freq_hz), _fmt_hz(FREQ_MIN), _fmt_hz(FREQ_MAX)))
    freq = freq_hz * FREQ_MULT
    r_div, scaled = select_r_div(freq)
    p1, p2, p3, _pll_freq = multisynth_calc(scaled, pll_freq)
    div_by_4 = 1 if scaled >= MS_DIVBY4 * FREQ_MULT else 0
    int_mode = 0
    ms_regs = ms_registers(clk, p1, p2, p3, int_mode, r_div, div_by_4, drive)
    # PLL block: CLK0-5 use PLLA (0), CLK6/7 use PLLB (1), as in the C library
    pll_idx = 0 if clk <= 5 else 1
    # PLL registers, computed from the board crystal frequency + correction
    pl1, pl2, pl3, _ = pll_calc(pll_freq, xtal_hz * FREQ_MULT, correction)
    pll_regs = pll_registers(pll_idx, pl1, pl2, pl3)
    # After the PLL parameters change the PLL needs a soft reset, and the
    # output has to be un-muted: Si5351::init() leaves every output disabled
    # (reg 3 = 0xFF), so a register replay that omits reg 3 produces silence.
    tail = [
        (PLL_RESET, PLL_RESET_B if pll_idx else PLL_RESET_A),
        (OUTPUT_ENABLE_CTRL, 0xFF & ~(1 << clk)),
    ]
    # The load sets the crystal's actual frequency, so it goes first and the
    # PLL reset in the tail relocks onto the result.
    head = []
    if xtal_load_pf is not None:
        head = [(CRYSTAL_LOAD, CRYSTAL_LOAD_PF[xtal_load_pf] | CRYSTAL_LOAD_RESERVED)]
    regs = head + pll_regs + ms_regs + tail
    code = "si5351.set_freq({}ULL, SI5351_CLK{});".format(freq, clk)
    return {
        "freq_hz": freq_hz,
        "clk": clk,
        "drive": drive,
        "xtal_hz": xtal_hz,
        "correction": correction,
        "xtal_load_pf": xtal_load_pf,
        "r_div": r_div,
        "div_by_4": div_by_4,
        "p1": p1, "p2": p2, "p3": p3,
        "registers": regs,
        "code": code,
    }

def _fmt(regs):
    lines = []
    for addr, val in regs:
        lines.append("  [0x%02X] = 0x%02X" % (addr, val))
    return "\n".join(lines)

DRIVE_LABELS = {0: "2mA", 1: "4mA", 2: "6mA", 3: "8mA"}

if __name__ == "__main__":
    import sys
    f = parse_freq(sys.argv[1]) if len(sys.argv) > 1 else 82000000
    c = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    d = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    x = int(sys.argv[4]) if len(sys.argv) > 4 else XTAL_FREQ
    corr = int(sys.argv[5]) if len(sys.argv) > 5 else 0
    try:
        r = set_freq(f, c, drive=d, xtal_hz=x, correction=corr)
    except FrequencyOutOfRange as e:
        print("Error: %s" % e)
        sys.exit(1)
    print("C code:")
    print("  " + r["code"])
    print("Registers (CLK%d, crystal %d Hz, corr %d ppb):" % (c, x, corr))
    print(_fmt(r["registers"]))
    print("p1=%d p2=%d p3=%d  r_div=%d div_by_4=%d drive=%s" % (
        r["p1"], r["p2"], r["p3"], r["r_div"], r["div_by_4"], DRIVE_LABELS.get(d, "?")))

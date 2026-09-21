"""
si5351_tool.py — set the frequency of a CH32V003 + Si5351 board over serial.

Simple view by default: type a frequency, press Send. The register maths and
the C-code helpers live behind the Advanced checkbox.

Run:  python3 si5351_tool.py

Copyright (C) 2026 freetoair (YT1BN), written with Claude Code (Anthropic).
Licensed under the GNU GPL v3 — see LICENSE. It ships with si5351_gen.py,
which derives from the GPL-licensed Etherkit Si5351 library.
"""
import os
import re
import sys

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QCheckBox, QComboBox, QFrame,
    QSpinBox, QPushButton, QTextEdit, QVBoxLayout, QHBoxLayout, QGroupBox,
    QMessageBox
)

import serial
from serial.tools.list_ports import comports

import si5351_gen as g

FRAME_SYNC_STORE = 0xA5   # controller saves to flash, then applies
FRAME_SYNC_APPLY = 0xA7   # controller only applies; the stored config is untouched
FRAME_SYNC_READ = 0xA8    # controller reads the listed Si5351 registers back
FRAME_SYNC_CMD = 0xA9     # one-byte command, below
CMD_RESTORE_DEFAULTS = 0x01   # power-on crystal load + stored config
CMD_CLEAR_STORED = 0x02       # erase the stored config, then restore
ACK_BAD_CMD = 0xE0
ACK_OK = 0xA6             # applied, every register acknowledged
ACK_I2C_FAIL = 0xE5       # frame arrived but the Si5351 did not answer
ACK_LEN = 4               # status, pair count, checksum, flags

DRIVES = {0: "2 mA", 1: "4 mA", 2: "6 mA", 3: "8 mA"}
XTALS = {25000000: "25 MHz", 27000000: "27 MHz"}


def build_read_frame(addrs):
    """Register numbers -> a read frame (same SYNC LEN PAYLOAD CHECKSUM shape)."""
    payload = bytes(a & 0xFF for a in addrs)
    return bytes([FRAME_SYNC_READ, len(payload)]) + payload + bytes([sum(payload) & 0xFF])


def parse_reg_list(text):
    """'0, 3, 26-33, 0xB7' -> [0, 3, 26, ..., 33, 183]. Raises ValueError."""
    addrs = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = (int(x.strip(), 0) for x in part.split("-", 1))
            addrs.extend(range(lo, hi + 1))
        else:
            addrs.append(int(part, 0))
    if not addrs or any(not 0 <= a <= 255 for a in addrs) or len(addrs) > 60:
        raise ValueError(text)
    return addrs


def build_frame(registers, store=True):
    """Registers as a list of (addr, val) -> byte sequence for serial.

    Frame format: SYNC LEN PAYLOAD CHECKSUM (CHECKSUM = sum(payload) & 0xFF).
    store=False sends the apply-only variant, which does not touch the flash —
    used while tuning so that turning the correction knob costs no write cycles.
    """
    payload = bytearray()
    for addr, val in registers:
        payload.append(addr & 0xFF)
        payload.append(val & 0xFF)
    checksum = sum(payload) & 0xFF
    frame = bytearray()
    frame.append(FRAME_SYNC_STORE if store else FRAME_SYNC_APPLY)
    frame.append(len(payload))
    frame += payload
    frame.append(checksum)
    return bytes(frame)



def register_role(addr):
    """A short label for what a register does, for the generated table."""
    if addr == 3:
        return "output enable"
    if addr == 177:
        return "PLL soft reset"
    if addr == 183:
        return "crystal load capacitance"
    if addr == 0:
        return "device status (SYS_INIT, LOL_A/B, LOS)"
    if addr == 15:
        return "PLL input source"
    if addr == 187:
        return "fanout enable"
    if 16 <= addr <= 23:
        return "CLK%d_CTRL: multisynth source, drive" % (addr - 16)
    if 26 <= addr <= 33:
        return "PLLA"
    if 34 <= addr <= 41:
        return "PLLB"
    if 42 <= addr <= 89:
        return "MS%d" % ((addr - 42) // 8)
    if addr in (90, 91, 92):
        return "MS6/MS7"
    return ""


def format_table(registers):
    """C initialiser rows, four per line, each group labelled.

    Regenerating the table should not cost the reader the comments that
    explain which block is which."""
    groups = []
    for addr, val in registers:
        role = register_role(addr)
        if groups and groups[-1][0] == role:
            groups[-1][1].append((addr, val))
        else:
            groups.append((role, [(addr, val)]))
    lines = []
    for role, pairs in groups:
        for i in range(0, len(pairs), 4):
            chunk = pairs[i:i + 4]
            body = "  " + " ".join("{0x%02X, 0x%02X}," % p for p in chunk)
            if i == 0 and role:
                lines.append("%-60s// %s" % (body, role))
            else:
                lines.append(body)
    return "\n".join(lines).rstrip(",")


class Si5351Tool(QWidget):
    def __init__(self):
        super().__init__()
        self.serial = None
        self.setWindowTitle("YT1BN Si5351 PLL Tool")
        self._build_ui()

    # ---------------- UI ----------------

    def _build_ui(self):
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("Frequency:"))
        self.freq_edit = QLineEdit()
        self.freq_edit.setPlaceholderText("e.g. 10.245 MHz, 32.768k, 3579545")
        self.freq_edit.setToolTip(
            "Plain Hz, or with a k / M suffix. Range %s to %s."
            % (g._fmt_hz(g.FREQ_MIN), g._fmt_hz(g.FREQ_MAX)))
        row.addWidget(self.freq_edit)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Output (CLK):"))
        self.clk_combo = QComboBox()
        for i in range(8):
            self.clk_combo.addItem("CLK%d" % i, i)
        row2.addWidget(self.clk_combo)
        row2.addWidget(QLabel("Drive:"))
        self.drive_combo = QComboBox()
        for code, label in DRIVES.items():
            self.drive_combo.addItem(label, code)
        row2.addWidget(self.drive_combo)
        row2.addStretch(1)
        layout.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Crystal (XTAL):"))
        self.xtal_combo = QComboBox()
        for hz, label in XTALS.items():
            self.xtal_combo.addItem(label, hz)
        row3.addWidget(self.xtal_combo)
        row3.addWidget(QLabel("Correction (ppb):"))
        self.corr_spin = QSpinBox()
        # A cheap Si5351 board can be hundreds of ppm off (one measured
        # +333 ppm), so the range has to reach far past a good crystal's spec.
        self.corr_spin.setRange(-500000, 500000)
        self.corr_spin.setValue(0)
        self.corr_spin.setSuffix(" ppb")
        row3.addWidget(self.corr_spin)
        # Coarse steps close a large offset quickly, fine ones trim it. Steps
        # under ~30 ppb can do nothing: the PLL fraction is only that fine.
        row3.addWidget(QLabel("Step:"))
        self.corr_step = QComboBox()
        for step in (10, 100, 1000, 10000, 100000):
            self.corr_step.addItem("%d ppb" % step, step)
        self.corr_step.setCurrentIndex(2)
        self.corr_step.currentIndexChanged.connect(
            lambda _: self.corr_spin.setSingleStep(self.corr_step.currentData()))
        self.corr_spin.setSingleStep(self.corr_step.currentData())
        row3.addWidget(self.corr_step)
        row3.addStretch(1)
        layout.addLayout(row3)

        # ---- Serial ----
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Serial port:"))
        self.port_combo = QComboBox()
        self.refresh_ports()
        srow.addWidget(self.port_combo, 1)
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.clicked.connect(self.refresh_ports)
        srow.addWidget(self.rescan_btn)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.on_connect)
        srow.addWidget(self.connect_btn)
        layout.addLayout(srow)

        srow2 = QHBoxLayout()
        self.send_btn = QPushButton("Send sequence")
        self.send_btn.clicked.connect(self.on_send)
        srow2.addWidget(self.send_btn)
        self.auto_send_chk = QCheckBox("Auto-send on change (apply only)")
        self.auto_send_chk.setToolTip(
            "Sends an apply-only frame on every change, so tuning costs no flash\n"
            "write cycles. Press 'Send sequence' to commit the final setting.")
        srow2.addWidget(self.auto_send_chk)
        srow2.addStretch(1)
        layout.addLayout(srow2)

        self.serial_status = QLabel("not connected")
        self.serial_status.setFrameShape(QFrame.StyledPanel)
        layout.addWidget(self.serial_status)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(110)
        layout.addWidget(self.log)

        # ---- Advanced ----
        self.adv_chk = QCheckBox("Advanced (register maths, C code)")
        self.adv_chk.toggled.connect(self.on_advanced_toggled)
        layout.addWidget(self.adv_chk)

        self.adv_box = QGroupBox("Advanced")
        adv = QVBoxLayout(self.adv_box)
        # For experimenting with a board whose crystal runs far off: the load
        # capacitance pulls it. By default the register is not written and the
        # chip keeps whatever it powered up with. It sits under Advanced
        # because on at least one board any write here broke the output up.
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Crystal load:"))
        self.load_combo = QComboBox()
        self.load_combo.addItem("chip default (not written)", None)
        for pf in (0, 6, 8, 10):
            self.load_combo.addItem("%d pF" % pf, pf)
        row4.addWidget(self.load_combo)
        row4.addStretch(1)
        adv.addLayout(row4)

        # Read registers back from the chip, to see what it actually holds.
        row5 = QHBoxLayout()
        row5.addWidget(QLabel("Read registers:"))
        self.read_edit = QLineEdit("0, 3, 15, 16, 26-33, 42-49, 177, 183, 187")
        self.read_edit.setToolTip("Register numbers, decimal or 0x hex, ranges "
                                  "like 26-33; at most 60.")
        row5.addWidget(self.read_edit, 1)
        self.read_btn = QPushButton("Read")
        self.read_btn.clicked.connect(self.on_read)
        row5.addWidget(self.read_btn)
        adv.addLayout(row5)

        # Back to normal without a power cycle, and a way out of a bad
        # stored config.
        row6 = QHBoxLayout()
        self.restore_btn = QPushButton("Restore chip defaults")
        self.restore_btn.setToolTip(
            "Put the crystal load back to the chip's own power-on value and "
            "re-apply the stored frequency.")
        self.restore_btn.clicked.connect(self.on_restore)
        row6.addWidget(self.restore_btn)
        self.clear_btn = QPushButton("Clear stored config")
        self.clear_btn.setToolTip(
            "Erase the stored setting; the board falls back to 82 MHz on CLK0.")
        self.clear_btn.clicked.connect(self.on_clear)
        row6.addWidget(self.clear_btn)
        row6.addStretch(1)
        adv.addLayout(row6)
        arow = QHBoxLayout()
        self.gen_btn = QPushButton("Generate")
        self.gen_btn.clicked.connect(self.on_generate)
        arow.addWidget(self.gen_btn)
        self.copy_btn = QPushButton("Copy C code to clipboard")
        self.copy_btn.clicked.connect(self.on_copy)
        arow.addWidget(self.copy_btn)
        self.embed_btn = QPushButton("Set boot-time fallback in main.cpp")
        self.embed_btn.clicked.connect(self.on_embed)
        arow.addWidget(self.embed_btn)
        adv.addLayout(arow)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        adv.addWidget(self.output)
        self.adv_box.setVisible(False)
        layout.addWidget(self.adv_box)

        # auto-send: regenerate + send when a parameter changes (if enabled)
        self.corr_spin.valueChanged.connect(self.on_auto_send)
        self.xtal_combo.currentIndexChanged.connect(self.on_auto_send)
        self.load_combo.currentIndexChanged.connect(self.on_auto_send)
        self.freq_edit.editingFinished.connect(self.on_auto_send)

        self.resize(560, 480)

    def on_advanced_toggled(self, on):
        self.adv_box.setVisible(bool(on))
        if not on:
            # A load nobody can see must not keep going out with every send.
            self.load_combo.setCurrentIndex(0)
        self.resize(self.width(), 700 if on else 480)

    def refresh_ports(self):
        """List serial ports, real USB adapters first.

        A PC typically enumerates 32 legacy /dev/ttyS* nodes that are never the
        board; listing them first buries the one port that matters."""
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        ports = list(comports())
        usb = [p for p in ports if not p.device.startswith("/dev/ttyS")]
        legacy = [p for p in ports if p.device.startswith("/dev/ttyS")]
        ordered = usb + legacy
        if not ordered:
            self.port_combo.addItem("(no ports)", None)
        for p in ordered:
            desc = (p.description or "").strip()
            label = p.device if desc in ("", "n/a") else "%s — %s" % (p.device, desc)
            self.port_combo.addItem(label, p.device)
        self.port_combo.blockSignals(False)

    # ---------------- helpers ----------------

    def _read_freq(self):
        """Parse the frequency field. Returns None and reports why if unusable."""
        try:
            return g.parse_freq(self.freq_edit.text())
        except ValueError:
            self.log.append(
                "Could not read the frequency. Enter it in Hz, or with a k / M "
                "suffix — for example 10.245 MHz, 32.768k, or 3579545.")
            self.serial_status.setText("bad frequency")
            return None

    def _registers(self):
        """Current settings -> (result dict, None) or (None, message).

        With a crystal load selected and a board connected, reg 183 is read
        first and only its bits 7:6 are changed: the reserved bits 5:0 are not
        the same on every chip, and writing the datasheet's value broke the
        oscillator on one. Without a board the datasheet's bits are used."""
        freq_hz = self._read_freq()
        if freq_hz is None:
            return None, None
        keep = g.CRYSTAL_LOAD_RESERVED
        if (self.load_combo.currentData() is not None
                and self.serial is not None and self.serial.is_open):
            vals, err = self._read_regs([g.CRYSTAL_LOAD])
            if vals is None:
                return None, ("Crystal load needs reg 183 read from the board "
                              "first, and that failed: %s" % err)
            keep = vals[0] & 0x3F
        try:
            return g.set_freq(
                freq_hz,
                self.clk_combo.currentData(),
                drive=self.drive_combo.currentData(),
                xtal_hz=self.xtal_combo.currentData(),
                correction=self.corr_spin.value(),
                xtal_load_pf=self.load_combo.currentData(),
                xtal_load_keep=keep,
            ), None
        except g.FrequencyOutOfRange as e:
            return None, "Out of range: %s" % e

    # ---------------- serial ----------------

    def on_connect(self):
        port = self.port_combo.currentData()
        if not port:
            self.serial_status.setText("select a port")
            return
        if self.serial:
            try:
                self.serial.close()
            except Exception:
                pass
        try:
            self.serial = serial.Serial(port, 115200, timeout=0.4)
            self.serial_status.setText("connected: %s" % port)
            self.log.append("[serial] connected to %s (115200 8N1)" % port)
        except Exception as e:
            self.serial = None
            self.serial_status.setText("error: %s" % e)
            self.log.append("[serial] could not connect: %s" % e)

    def on_send(self):
        """'Send sequence' button: commit — the controller stores it in flash."""
        self._send(store=True)

    def _send(self, store):
        if self.serial is None or not self.serial.is_open:
            self.log.append("[serial] click 'Connect' first")
            return
        res, err = self._registers()
        if res is None:
            if err:
                self.log.append("Not sent — %s" % err)
                self.serial_status.setText("not sent")
            return
        frame = build_frame(res["registers"], store=store)
        try:
            self.serial.reset_input_buffer()
            n = self.serial.write(frame)
            self.serial.flush()
        except Exception as e:
            self.serial_status.setText("send error: %s" % e)
            self.log.append("[serial] error: %s" % e)
            return
        load = res["xtal_load_pf"]
        if load is None:
            load_txt = "chip default"
        else:
            load_txt = "%d pF (reg 183 = 0x%02X)" % (load, res["registers"][0][1])
        self.log.append("[serial] sent %d bytes, %d pairs — %s, %s, %s, %d ppb, load %s" % (
            n, len(res["registers"]), g._fmt_hz(res["freq_hz"]),
            "CLK%d" % res["clk"], DRIVES.get(res["drive"], "?"), res["correction"],
            load_txt))
        self._read_ack(store, len(res["registers"]))

    def _read_ack(self, store, pairs):
        try:
            ack = self.serial.read(ACK_LEN)
        except Exception as e:
            self.serial_status.setText("read error: %s" % e)
            return
        if len(ack) < ACK_LEN:
            self.serial_status.setText("no reply from controller")
            self.log.append(
                "No reply. The controller answers on package pin 7 (PC4) — check "
                "that it is wired to the adapter's RX. The frequency may still "
                "have been set; only the confirmation is missing.")
            return
        status, count, checksum, flags = ack[0], ack[1], ack[2], ack[3]
        stored = bool(flags & 1)
        chip_ok = bool(flags & 2)
        if status == ACK_OK:
            what = "stored in flash and applied" if stored else "applied (not stored)"
            self.serial_status.setText("confirmed — %s" % what)
            self.log.append("[controller] %d registers %s." % (count, what))
            if count != pairs:
                self.log.append(
                    "Warning: controller reports %d registers, %d were sent."
                    % (count, pairs))
        elif status == ACK_I2C_FAIL:
            self.serial_status.setText("Si5351 not responding")
            self.log.append(
                "[controller] received %d registers but the Si5351 did not answer "
                "on I2C (address 0x60). Check SDA on pin 5, SCL on pin 6, the "
                "pull-up resistors, and that the Si5351 board has power."
                % count)
        else:
            self.serial_status.setText("unexpected reply 0x%02X" % status)
            self.log.append("[controller] unexpected reply: %s"
                            % " ".join("%02X" % b for b in ack))
        if status == ACK_OK and not chip_ok:
            self.log.append("Note: the controller flagged an earlier I2C problem.")
        del checksum

    def on_auto_send(self, *_):
        # Apply-only, so turning the knob costs no flash write cycles; press
        # 'Send sequence' once to commit the setting you settled on.
        if not self.auto_send_chk.isChecked():
            return
        if self.serial is None or not self.serial.is_open:
            return
        self._send(store=False)

    # ---------------- advanced ----------------

    def _read_regs(self, addrs, partial=False):
        """Read Si5351 registers through the controller.
        Returns (values, None) or (None, reason). With `partial`, a reply in
        which some reads failed still returns the values (0x00 for those)."""
        try:
            self.serial.reset_input_buffer()
            self.serial.write(build_read_frame(addrs))
            self.serial.flush()
            reply = self.serial.read(len(addrs) + 3)
        except Exception as e:
            return None, "serial error: %s" % e
        if len(reply) < len(addrs) + 3:
            return None, ("no reply (%d of %d bytes). Firmware older than the "
                          "register read does not answer it — flash the current one."
                          % (len(reply), len(addrs) + 3))
        status, count, vals, checksum = reply[0], reply[1], reply[2:-1], reply[-1]
        if count != len(addrs) or checksum != sum(vals) & 0xFF:
            return None, "the reply was garbled; try again."
        if status != ACK_OK and not partial:
            return None, "the Si5351 did not acknowledge every read."
        return list(vals), (None if status == ACK_OK else "partial")

    def on_read(self):
        """'Read' button: ask the controller for the listed Si5351 registers."""
        if self.serial is None or not self.serial.is_open:
            self.log.append("[serial] click 'Connect' first")
            return
        try:
            addrs = parse_reg_list(self.read_edit.text())
        except ValueError:
            self.log.append("Could not read the register list. Use numbers 0-255, "
                            "decimal or 0x hex, ranges like 26-33, at most 60.")
            return
        vals, err = self._read_regs(addrs, partial=True)
        if vals is None:
            self.serial_status.setText("read failed")
            self.log.append("[controller] read failed: %s" % err)
            return
        if err:
            self.log.append("[controller] some reads were not acknowledged by the "
                            "Si5351; those show as 0x00.")
        count = len(vals)
        self.serial_status.setText("read %d registers" % count)
        for a, v in zip(addrs, vals):
            role = register_role(a)
            self.log.append("  reg %3d (0x%02X) = 0x%02X  %s%s" % (
                a, a, v, format(v, "08b"), "  " + role if role else ""))

    def _command(self, cmd):
        """Send a one-byte command frame and report the reply in the log."""
        if self.serial is None or not self.serial.is_open:
            self.log.append("[serial] click 'Connect' first")
            return
        frame = bytes([FRAME_SYNC_CMD, 1, cmd, cmd])
        try:
            self.serial.reset_input_buffer()
            self.serial.write(frame)
            self.serial.flush()
            reply = self.serial.read(ACK_LEN)
        except Exception as e:
            self.serial_status.setText("serial error: %s" % e)
            self.log.append("[serial] error: %s" % e)
            return
        if len(reply) < ACK_LEN:
            self.serial_status.setText("no reply from controller")
            self.log.append("No reply. Firmware older than this command does not "
                            "answer it — flash the current one.")
            return
        status, echo, load, flags = reply
        if status == ACK_BAD_CMD or echo != cmd:
            self.log.append("[controller] did not recognise the command.")
            return
        # What is on screen should not go out again on the next auto-send.
        self.load_combo.blockSignals(True)
        self.load_combo.setCurrentIndex(0)
        self.load_combo.blockSignals(False)
        what = "stored frequency" if flags & 1 else "built-in 82 MHz fallback"
        if not flags & 4:
            self.log.append("[controller] the chip's power-on crystal load is not "
                            "known (the Si5351 did not answer at start-up). Power-"
                            "cycle the board to restore it.")
        if status == ACK_OK:
            self.serial_status.setText("restored")
            self.log.append("[controller] crystal load back to 0x%02X, %s applied, "
                            "PLLs reset." % (load, what))
        else:
            self.serial_status.setText("Si5351 did not answer")
            self.log.append("[controller] %s applied, but the Si5351 did not "
                            "acknowledge every write." % what)

    def on_restore(self):
        self._command(CMD_RESTORE_DEFAULTS)

    def on_clear(self):
        ask = QMessageBox.question(
            self, "Clear stored config",
            "Erase the frequency stored on the board? It will run the built-in "
            "82 MHz on CLK0 until you press Send sequence again.")
        if ask != QMessageBox.Yes:
            return
        self._command(CMD_CLEAR_STORED)

    def on_generate(self):
        res, err = self._registers()
        if res is None:
            if err:
                self.output.append(err)
                self.serial_status.setText("not generated")
            return
        lines = ["// C code (replace the line in main.cpp):", res["code"], ""]
        lines.append("// Registers (CLK%d, drive %s, crystal %s, corr %d ppb):" % (
            res["clk"], DRIVES.get(res["drive"], "?"),
            XTALS.get(res["xtal_hz"], "?"), res["correction"]))
        for addr, val in res["registers"]:
            lines.append("  [0x%02X] = 0x%02X" % (addr, val))
        lines.append("")
        lines.append("p1=%d p2=%d p3=%d  r_div=%d  div_by_4=%d" % (
            res["p1"], res["p2"], res["p3"], res["r_div"], res["div_by_4"]))
        self.output.setPlainText("\n".join(lines))

    def on_copy(self):
        cb = QApplication.clipboard()
        for line in self.output.toPlainText().splitlines():
            s = line.strip()
            if s and not s.startswith("//") and not s.startswith("[") and not s.startswith("p1="):
                cb.setText(s)
                return

    def on_embed(self):
        """Rewrite FALLBACK_REGS in main.cpp — what an unconfigured board boots to.

        The firmware no longer calls the Etherkit library, so there is no
        `si5351.set_freq(...)` line to replace; the boot-time frequency is a
        compiled-in register table. Editing that by hand is what NOTES.md used
        to ask for."""
        res, err = self._registers()
        if res is None:
            if err:
                self.output.append(err)
            return
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir, "src", "main.cpp")
        try:
            with open(path, "r") as f:
                text = f.read()
        except Exception as e:
            self.output.append("[main.cpp] cannot read file: %s" % e)
            return

        m = re.search(r"(static const uint8_t FALLBACK_REGS\[\]\[2\] = \{\n)(.*?)(\n\};)",
                      text, re.S)
        if not m:
            self.output.append("[main.cpp] no FALLBACK_REGS table found.")
            return

        text = text[:m.start(2)] + format_table(res["registers"]) + text[m.end(2):]

        # keep the comment above the table honest about what generated it
        text = re.sub(
            r"(//   python3 tools/si5351_gen\.py )\S+ \d+ \d+ \d+ -?\d+",
            r"\g<1>%d %d %d %d %d" % (res["freq_hz"], res["clk"], res["drive"],
                                     res["xtal_hz"], res["correction"]),
            text)
        text = re.sub(
            r"// Generated by tools/si5351_gen\.py for a \d+ MHz crystal, drive \d+ mA, -?\d+ ppb:",
            "// Generated by tools/si5351_gen.py for a %d MHz crystal, drive %s, %d ppb:"
            % (res["xtal_hz"] // 1000000, DRIVES.get(res["drive"], "?"), res["correction"]),
            text)
        text = re.sub(r"// ---- Boot-time fallback: [^\n]*----",
                      "// ---- Boot-time fallback: %s on CLK%d ----"
                      % (g._fmt_hz(res["freq_hz"]), res["clk"]),
                      text)

        try:
            with open(path, "w") as f:
                f.write(text)
        except Exception as e:
            self.output.append("[main.cpp] cannot write file: %s" % e)
            return
        self.output.append(
            "[main.cpp] FALLBACK_REGS set to %s on CLK%d (%d pairs). "
            "Rebuild and flash for it to take effect."
            % (g._fmt_hz(res["freq_hz"]), res["clk"], len(regs)))


def main():
    app = QApplication(sys.argv)
    w = Si5351Tool()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

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
import sys

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QCheckBox, QComboBox, QFrame,
    QSpinBox, QPushButton, QTextEdit, QVBoxLayout, QHBoxLayout, QGroupBox
)

import serial
from serial.tools.list_ports import comports

import si5351_gen as g

FRAME_SYNC_STORE = 0xA5   # controller saves to flash, then applies
FRAME_SYNC_APPLY = 0xA7   # controller only applies; the stored config is untouched
ACK_OK = 0xA6             # applied, every register acknowledged
ACK_I2C_FAIL = 0xE5       # frame arrived but the Si5351 did not answer
ACK_LEN = 4               # status, pair count, checksum, flags

DRIVES = {0: "2 mA", 1: "4 mA", 2: "6 mA", 3: "8 mA"}
XTALS = {25000000: "25 MHz", 27000000: "27 MHz"}


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
        self.corr_spin.setRange(-10000, 10000)
        self.corr_spin.setValue(0)
        self.corr_spin.setSuffix(" ppb")
        row3.addWidget(self.corr_spin)
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
        arow = QHBoxLayout()
        self.gen_btn = QPushButton("Generate")
        self.gen_btn.clicked.connect(self.on_generate)
        arow.addWidget(self.gen_btn)
        self.copy_btn = QPushButton("Copy C code to clipboard")
        self.copy_btn.clicked.connect(self.on_copy)
        arow.addWidget(self.copy_btn)
        self.embed_btn = QPushButton("Embed C line into main.cpp")
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
        self.freq_edit.editingFinished.connect(self.on_auto_send)

        self.resize(560, 480)

    def on_advanced_toggled(self, on):
        self.adv_box.setVisible(bool(on))
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
        """Current settings -> (result dict, None) or (None, message)."""
        freq_hz = self._read_freq()
        if freq_hz is None:
            return None, None
        try:
            return g.set_freq(
                freq_hz,
                self.clk_combo.currentData(),
                drive=self.drive_combo.currentData(),
                xtal_hz=self.xtal_combo.currentData(),
                correction=self.corr_spin.value(),
            ), None
        except g.FrequencyOutOfRange as e:
            return None, str(e)

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
                self.log.append("Not sent — out of range: %s" % err)
                self.serial_status.setText("out of range")
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
        self.log.append("[serial] sent %d bytes, %d pairs — %s, %s, %s, %d ppb" % (
            n, len(res["registers"]), g._fmt_hz(res["freq_hz"]),
            "CLK%d" % res["clk"], DRIVES.get(res["drive"], "?"), res["correction"]))
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

    def on_generate(self):
        res, err = self._registers()
        if res is None:
            if err:
                self.output.append("Out of range: %s" % err)
                self.serial_status.setText("out of range")
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
        res, err = self._registers()
        if res is None:
            if err:
                self.output.append("Out of range: %s" % err)
            return
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), os.pardir, "src", "main.cpp")
        try:
            with open(path, "r") as f:
                lines = f.readlines()
        except Exception as e:
            self.output.append("[main.cpp] cannot read file: %s" % e)
            return
        for i, line in enumerate(lines):
            if "si5351.set_freq(" in line:
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = indent + res["code"] + ("\n" if line.endswith("\n") else "")
                break
        else:
            self.output.append("No existing set_freq line found in main.cpp.")
            return
        try:
            with open(path, "w") as f:
                f.writelines(lines)
        except Exception as e:
            self.output.append("[main.cpp] cannot write file: %s" % e)
            return
        self.output.append("[main.cpp] replaced with: %s" % res["code"])


def main():
    app = QApplication(sys.argv)
    w = Si5351Tool()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

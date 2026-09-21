// CH32V003 + Si5351 programmable crystal replacement.
//
// Copyright (C) 2026 freetoair (YT1BN)
// Written together with Claude Code (Anthropic).
// Licensed under the GNU General Public License v3 or later; see LICENSE.
//
// This firmware no longer uses the Etherkit Si5351 library: it drives the chip
// directly over a bounded I2C path (see the I2C section in tools/README.md).

#include <Arduino.h>

// ---- Software UART TX on PC4 (package pin 7) ----
// The hardware USART TX (PD5) shares pin 8 with SWIO, so using it locks the
// debug probe out of the chip. PC4 has no UART function in any remap, but a
// bit-banged transmitter does not need one: SysTick free-runs at HCLK, which
// gives the bit edges directly. Pin 8 stays a clean SWIO line.
#define TX_PIN 4
#define TX_BAUD 115200
#define TX_BIT_TICKS ((SYSTEM_CORE_CLOCK + TX_BAUD / 2) / TX_BAUD)

static void tx_begin(void) {
  RCC->APB2PCENR |= RCC_APB2Periph_GPIOC;
  GPIOC->BSHR = 1u << TX_PIN;                    // idle high before driving
  GPIOC->CFGLR &= ~(0xFu << (4 * TX_PIN));
  GPIOC->CFGLR |= ((uint32_t)(GPIO_Speed_50MHz | GPIO_CNF_OUT_PP)) << (4 * TX_PIN);
  GPIOC->BSHR = 1u << TX_PIN;
}

static void tx_byte(uint8_t b) {
  uint16_t frame = (uint16_t)((0x100u | b) << 1);  // start 0, 8 data LSB first, stop 1
  // ~87 us with interrupts off, so nothing can stretch a bit. The 1 ms tick is
  // only delayed, never lost: its handler advances CMP rather than reloading.
  __disable_irq();
  uint32_t next = SysTick->CNT;
  for (int i = 0; i < 10; i++) {
    if (frame & 1) GPIOC->BSHR = 1u << TX_PIN;
    else           GPIOC->BCR  = 1u << TX_PIN;
    frame >>= 1;
    next += TX_BIT_TICKS;
    while ((int32_t)(SysTick->CNT - next) < 0) { }
  }
  __enable_irq();                                  // line rests high (stop bit)
}

// ---- I2C master, bounded ----
// The core's HAL (wch-hal-i2c.c) waits on every I2C flag with an unbounded
// `while`, and TwoWire::endTransmission() returns 0 whether or not the slave
// acknowledged. A missing or half-wired Si5351 therefore either hangs the
// firmware before it reaches loop(), or reports success into the void.
// Every I2C access below is bounded by a timeout and reports NAK honestly.
#define SI5351_ADDR 0x60
#define I2C_TIMEOUT_US 5000u
#define I2C_SDA_PIN 1   // PC1, package pin 5
#define I2C_SCL_PIN 2   // PC2, package pin 6

static void i2c_periph_init(void) {
  RCC->APB2PCENR |= RCC_APB2Periph_GPIOC | RCC_APB2Periph_AFIO;
  RCC->APB1PCENR |= RCC_APB1Periph_I2C1;

  const uint32_t af_od = GPIO_Speed_50MHz | GPIO_CNF_OUT_OD_AF;
  GPIOC->CFGLR &= ~(0xFu << (4 * I2C_SDA_PIN));
  GPIOC->CFGLR |= af_od << (4 * I2C_SDA_PIN);
  GPIOC->CFGLR &= ~(0xFu << (4 * I2C_SCL_PIN));
  GPIOC->CFGLR |= af_od << (4 * I2C_SCL_PIN);

  I2C1->CTLR1 &= ~I2C_CTLR1_PE;
  I2C1->CTLR2 = (uint16_t)(SYSTEM_CORE_CLOCK / 1000000);   // FREQ in MHz
  I2C1->CKCFGR = (uint16_t)(SYSTEM_CORE_CLOCK / (100000 * 2)); // 100 kHz, Sm
  I2C1->CTLR1 |= I2C_CTLR1_PE;
  I2C1->CTLR1 |= I2C_CTLR1_ACK;
}

// SDA held low by a confused slave cannot be cleared by the peripheral:
// drive SCL manually until the slave lets go, then issue a STOP by hand.
static void i2c_bus_recover(void) {
  I2C1->CTLR1 &= ~I2C_CTLR1_PE;

  const uint32_t od = GPIO_Speed_50MHz | GPIO_CNF_OUT_OD;
  GPIOC->CFGLR &= ~(0xFu << (4 * I2C_SDA_PIN));
  GPIOC->CFGLR |= od << (4 * I2C_SDA_PIN);
  GPIOC->CFGLR &= ~(0xFu << (4 * I2C_SCL_PIN));
  GPIOC->CFGLR |= od << (4 * I2C_SCL_PIN);
  GPIOC->BSHR = (1u << I2C_SDA_PIN) | (1u << I2C_SCL_PIN); // both released high

  for (int i = 0; i < 9; i++) {
    GPIOC->BCR = 1u << I2C_SCL_PIN;   // SCL low
    delayMicroseconds(5);
    GPIOC->BSHR = 1u << I2C_SCL_PIN;  // SCL high
    delayMicroseconds(5);
  }

  // STOP: SDA low while SCL high, then release SDA.
  GPIOC->BCR = 1u << I2C_SDA_PIN;
  delayMicroseconds(5);
  GPIOC->BSHR = 1u << I2C_SCL_PIN;
  delayMicroseconds(5);
  GPIOC->BSHR = 1u << I2C_SDA_PIN;
  delayMicroseconds(5);

  I2C1->CTLR1 |= I2C_CTLR1_SWRST;
  I2C1->CTLR1 &= ~I2C_CTLR1_SWRST;
  i2c_periph_init();
}

// ---- diagnostics, readable over the debug probe ----
// i2c_diag_step: 0 = ok, 1 = bus stuck busy, 2 = no SB, 3 = no ADDR (NAK/timeout),
//                4 = no TXE, 5 = no BTF, 6 = no RXNE
volatile uint8_t  i2c_diag_step;
volatile uint16_t i2c_diag_star1;
volatile uint16_t i2c_diag_star2;
volatile uint8_t  i2c_scan[16];   // bitmap: bit (a & 7) of byte (a >> 3) set = 0xa ACKed
volatile uint8_t  i2c_diag_probe_ok;   // did the 0x60 presence probe pass?
volatile uint8_t  i2c_diag_fail_idx;   // index of the first pair that failed (0xFF = none)
volatile uint8_t  i2c_diag_fail_cnt;   // how many pairs failed
volatile uint8_t  cfg_from_flash;      // 1 = booted from the stored config, 0 = fallback
volatile uint16_t diag_applied;        // frames applied to the Si5351
volatile uint16_t diag_stored;         // frames also written to flash

static void i2c_diag(uint8_t step) {
  i2c_diag_step = step;
  i2c_diag_star1 = I2C1->STAR1;
  i2c_diag_star2 = I2C1->STAR2;
}

// Wait for a STAR1 flag. Returns false on NAK or timeout.
static bool i2c_wait(uint16_t flag) {
  uint32_t t0 = micros();
  while (!(I2C1->STAR1 & flag)) {
    if (I2C1->STAR1 & I2C_STAR1_AF) return false;
    if ((uint32_t)(micros() - t0) > I2C_TIMEOUT_US) return false;
  }
  return true;
}

static void i2c_abort(void) {
  I2C1->CTLR1 |= I2C_CTLR1_STOP;
  I2C1->STAR1 &= (uint16_t)~I2C_STAR1_AF;  // clear the NAK flag
}

// Address a device for writing. Returns false if it does not acknowledge.
static bool i2c_start_write(uint8_t addr7) {
  uint32_t t0 = micros();
  while (I2C1->STAR2 & I2C_STAR2_BUSY) {
    if ((uint32_t)(micros() - t0) > I2C_TIMEOUT_US) {
      i2c_diag(1);
      i2c_bus_recover();
      return false;
    }
  }
  I2C1->CTLR1 |= I2C_CTLR1_START;
  if (!i2c_wait(I2C_STAR1_SB)) { i2c_diag(2); i2c_abort(); return false; }
  I2C1->DATAR = (uint16_t)(addr7 << 1);         // write direction
  if (!i2c_wait(I2C_STAR1_ADDR)) { i2c_diag(3); i2c_abort(); return false; }
  (void)I2C1->STAR1;                            // reading STAR1 then STAR2
  (void)I2C1->STAR2;                            // clears the ADDR flag
  return true;
}

// The first transaction issued after i2c_periph_init() reliably fails: the
// peripheral needs the bus to be sensed free before it will drive a START.
// Rather than depend on something else warming the bus up, every transaction
// gets a couple of retries.
#define I2C_ATTEMPTS 3

// Write one Si5351 register. Returns false on NAK or timeout.
static bool i2c_write_reg_once(uint8_t reg, uint8_t val) {
  if (!i2c_start_write(SI5351_ADDR)) return false;
  if (!i2c_wait(I2C_STAR1_TXE)) { i2c_diag(4); i2c_abort(); return false; }
  I2C1->DATAR = reg;
  if (!i2c_wait(I2C_STAR1_TXE)) { i2c_diag(4); i2c_abort(); return false; }
  I2C1->DATAR = val;
  if (!i2c_wait(I2C_STAR1_BTF)) { i2c_diag(5); i2c_abort(); return false; }
  I2C1->CTLR1 |= I2C_CTLR1_STOP;
  i2c_diag_step = 0;
  return true;
}

static bool i2c_write_reg(uint8_t reg, uint8_t val) {
  for (int a = 0; a < I2C_ATTEMPTS; a++) {
    if (i2c_write_reg_once(reg, val)) return true;
  }
  return false;
}

// Read one Si5351 register: write the register number, then a repeated
// START in the read direction. A single-byte read has to NAK its only byte,
// so ACK is cleared before ADDR is, and STOP is set before the byte arrives.
static bool i2c_read_reg_once(uint8_t reg, uint8_t *val) {
  if (!i2c_start_write(SI5351_ADDR)) return false;
  if (!i2c_wait(I2C_STAR1_TXE)) { i2c_diag(4); i2c_abort(); return false; }
  I2C1->DATAR = reg;
  if (!i2c_wait(I2C_STAR1_BTF)) { i2c_diag(5); i2c_abort(); return false; }
  I2C1->CTLR1 |= I2C_CTLR1_START;
  if (!i2c_wait(I2C_STAR1_SB)) { i2c_diag(2); i2c_abort(); return false; }
  I2C1->DATAR = (uint16_t)((SI5351_ADDR << 1) | 1);   // read direction
  if (!i2c_wait(I2C_STAR1_ADDR)) { i2c_diag(3); i2c_abort(); return false; }
  I2C1->CTLR1 &= ~I2C_CTLR1_ACK;
  (void)I2C1->STAR1;
  (void)I2C1->STAR2;
  I2C1->CTLR1 |= I2C_CTLR1_STOP;
  bool ok = i2c_wait(I2C_STAR1_RXNE);
  if (ok) *val = (uint8_t)I2C1->DATAR;
  else i2c_diag(6);
  I2C1->CTLR1 |= I2C_CTLR1_ACK;
  if (ok) i2c_diag_step = 0;
  return ok;
}

static bool i2c_read_reg(uint8_t reg, uint8_t *val) {
  for (int a = 0; a < I2C_ATTEMPTS; a++) {
    if (i2c_read_reg_once(reg, val)) return true;
  }
  return false;
}

// Address-only transaction: does anything answer at this address?
static bool i2c_probe_addr(uint8_t addr7) {
  if (!i2c_start_write(addr7)) return false;
  I2C1->CTLR1 |= I2C_CTLR1_STOP;
  i2c_diag_step = 0;
  return true;
}

static bool i2c_probe(void) {
  for (int a = 0; a < I2C_ATTEMPTS; a++) {
    if (i2c_probe_addr(SI5351_ADDR)) return true;
  }
  return false;
}

// Sweep the 7-bit address space once and record who answers.
static void i2c_scan_bus(void) {
  for (int i = 0; i < 16; i++) i2c_scan[i] = 0;
  for (uint8_t a = 0x08; a <= 0x77; a++) {
    if (i2c_probe_addr(a)) i2c_scan[a >> 3] |= (uint8_t)(1u << (a & 7));
  }
}

// ---- Boot-time fallback: 82 MHz on CLK0 ----
// Generated by tools/si5351_gen.py for a 25 MHz crystal, drive 2 mA, 0 ppb:
//   python3 tools/si5351_gen.py 82000000 0 0 25000000 0
// The Etherkit library is no longer used at runtime: its init() probes the bus
// with an empty transmission, which this core always reports as a failure, so
// the library never got past its own presence check. Replaying a register
// sequence is what the serial path already does, so the fallback does the same.
static const uint8_t FALLBACK_REGS[][2] = {
  {0x1A, 0x00}, {0x1B, 0x01}, {0x1C, 0x00}, {0x1D, 0x0E},   // PLLA
  {0x1E, 0x00}, {0x1F, 0x00}, {0x20, 0x00}, {0x21, 0x00},
  {0x2A, 0x42}, {0x2B, 0x40}, {0x2C, 0x00}, {0x2D, 0x02},   // MS0
  {0x2E, 0xE0}, {0x2F, 0xFB}, {0x30, 0xE8}, {0x31, 0x80},
  {0x10, 0x0C},                                             // CLK0_CTRL: multisynth source, drive
  {0xB1, 0x20},                                             // PLL soft reset
  {0x03, 0xFE},                                             // output enable
};

// ---- Serial RX state machine: frame = SYNC LEN PAYLOAD CHECKSUM ----
// Two write frame kinds, so tuning does not wear the flash out. The correction
// spinner can emit a frame per step; at one page erase each that would burn
// through the endurance budget in a tuning session.
#define FRAME_SYNC_STORE 0xA5   // save to flash, then apply
#define FRAME_SYNC_APPLY 0xA7   // apply only, leave the stored config alone
// Read frame: the payload is a list of register numbers. Reply: STATUS, count,
// one value per register (0 where the read failed), checksum of the values.
#define FRAME_SYNC_READ  0xA8
#define RX_MAX_LEN 60
// Reply on the software TX pin: STATUS, pair count, payload checksum,
// FLAGS (bit0 = written to flash, bit1 = Si5351 acknowledged every write).
#define ACK_OK 0xA6
#define ACK_I2C_FAIL 0xE5

// ---- UART: receive only, on PD6 (pin 1) ----
// On the SOP-8 package PD5 (USART1 TX) is bonded to PD1 (SWIO) on pin 8.
// Serial.begin() drives PD5 as a push-pull alternate-function output, which
// fights the debug probe and makes the chip unreachable until minichlink -u
// power-cycles it. So we bring USART1 up ourselves: RX on PD6 only, PD5 left
// untouched as a floating input, and no TX stage at all. Pin 8 stays SWIO.
//
// The core's USART1_IRQHandler still fills its ring buffer, so Serial.read()
// keeps working; only Serial.begin()/Serial.write() are off limits.
static void uart_rx_begin(unsigned long baud) {
  RCC->APB2PCENR |= RCC_APB2Periph_GPIOD | RCC_APB2Periph_USART1;

  // PD6 = floating input (URX). PD5 is deliberately not configured.
  GPIOD->CFGLR &= ~(0xf << (4 * 6));
  GPIOD->CFGLR |= ((uint32_t)GPIO_CNF_IN_FLOATING) << (4 * 6);

  USART1->CTLR1 = USART_WordLength_8b | USART_Parity_No | USART_Mode_Rx;
  USART1->CTLR2 = USART_StopBits_1;
  USART1->CTLR3 = USART_HardwareFlowControl_None;

  uint32_t integerDivider = (25 * APB_CLOCK) / (OVER8DIV * baud);
  uint32_t fractionalDivider = integerDivider % 100;
  USART1->BRR = ((integerDivider / 100) << 4) |
                (((fractionalDivider * (OVER8DIV * 2) + 50) / 100) & 7);

  USART1->CTLR1 |= USART_FLAG_RXNE;
  NVIC_EnableIRQ(USART1_IRQn);
  USART1->CTLR1 |= CTLR1_UE_Set;
}

// True once something has actually acknowledged at 0x60.
static bool si5351_ok = false;

static bool si5351_begin(void) {
  i2c_periph_init();
  si5351_ok = i2c_probe();
  i2c_diag_probe_ok = si5351_ok ? 1 : 0;
  return si5351_ok;
}

// Replay a register sequence. Returns false if any write was not acknowledged.
static bool si5351_apply(const uint8_t *pairs, int n) {
  bool ok = true;
  i2c_diag_fail_idx = 0xFF;
  i2c_diag_fail_cnt = 0;
  for (int i = 0; i < n; i++) {
    if (!i2c_write_reg(pairs[2 * i], pairs[2 * i + 1])) {
      ok = false;
      if (i2c_diag_fail_idx == 0xFF) i2c_diag_fail_idx = (uint8_t)i;
      i2c_diag_fail_cnt++;
    }
  }
  if (!ok) si5351_ok = false;
  return ok;
}

// ---- Flash storage for Si5351 register config ----
// CH32V003J4M6 has 16KB of flash (0x0000..0x3FFF) in 1KB pages, so the last
// page starts at 0x3C00. The firmware image must stay below this address;
// check the .text/.data end in firmware.map after changing the code.
// The flash controller (FLASH->ADDR and the programming stores) needs the real
// 0x08000000 mapping; the 0x00000000 alias is execute/read only, and erases or
// programs issued against it are silently ignored.
#define SI5351_FLASH_SIZE 0x4000
#define SI5351_CFG_PAGE (FLASH_BASE + SI5351_FLASH_SIZE - 0x400)   // 0x08003C00

static void flash_unlock(void) {
  FLASH->KEYR = 0x45670123; // FLASH_KEY1
  FLASH->KEYR = 0xCDEF89AB; // FLASH_KEY2
}

static void flash_lock(void) {
  FLASH->CTLR |= 0x80; // LOCK
}

static bool flash_wait(void) {
  while (FLASH->STATR & 0x01) { /* BSY */ }
  return (FLASH->STATR & 0x10) == 0; // no write-protect error
}

static void flash_erase_page(uint32_t addr) {
  flash_wait();
  FLASH->CTLR |= 0x0002; // PER (page erase 1KB)
  FLASH->ADDR = addr;
  FLASH->CTLR |= 0x0040; // STRT
  flash_wait();
  FLASH->CTLR &= ~0x0002;
}

static void flash_program_word(uint32_t addr, uint32_t data) {
  flash_wait();
  FLASH->CTLR |= 0x0001; // PG (word program)
  *(__IO uint16_t *)addr = (uint16_t)data;
  flash_wait();
  *(__IO uint16_t *)(addr + 2) = (uint16_t)(data >> 16);
  flash_wait();
  FLASH->CTLR &= ~0x0001;
}

// Save (reg,val) pairs into the 1KB config page.
// Layout: [uint16 count][pairs (2*n bytes)][uint8 checksum = sum of pair bytes]
static void flash_save_pairs(const uint8_t *p, int n) {
  uint8_t buf[64] = {0};
  if (n < 0 || 2 + 2 * n + 1 > (int)sizeof(buf)) return;
  buf[0] = (uint8_t)(n & 0xFF);
  buf[1] = (uint8_t)((n >> 8) & 0xFF);
  uint8_t sum = 0;
  for (int i = 0; i < n; i++) {
    buf[2 + 2 * i] = p[2 * i];
    buf[2 + 2 * i + 1] = p[2 * i + 1];
    sum = (sum + p[2 * i] + p[2 * i + 1]) & 0xFF;
  }
  buf[2 + 2 * n] = sum;
  int total = 2 + 2 * n + 1;
  int words = (total + 3) / 4; // number of 32-bit words, not bytes
  // The page must be unlocked before the erase, otherwise the erase is
  // silently ignored (WRPRTERR) and the following program writes land on
  // stale, non-erased flash.
  flash_unlock();
  flash_erase_page(SI5351_CFG_PAGE);
  for (int w = 0; w < words; w++) {
    uint32_t word = 0;
    int base = w * 4;
    for (int k = 0; k < 4; k++) {
      word |= ((uint32_t)(buf[base + k] & 0xFF) << (k * 8));
    }
    flash_program_word(SI5351_CFG_PAGE + base, word);
  }
  flash_lock();
}

// Load saved config from flash and replay it to Si5351. Returns false if no valid config.
static bool flash_load_and_apply(void) {
  uint16_t count = *(volatile uint16_t *)SI5351_CFG_PAGE;
  if (count == 0xFFFF || count == 0 || count > RX_MAX_LEN / 2) return false;
  uint8_t sum = 0;
  for (int i = 0; i < count; i++) {
    uint8_t reg = *(volatile uint8_t *)(SI5351_CFG_PAGE + 2 + 2 * i);
    uint8_t val = *(volatile uint8_t *)(SI5351_CFG_PAGE + 3 + 2 * i);
    sum = (sum + reg + val) & 0xFF;
  }
  uint8_t stored = *(volatile uint8_t *)(SI5351_CFG_PAGE + 2 + 2 * count);
  if (sum != stored) return false;
  si5351_apply((const uint8_t *)(SI5351_CFG_PAGE + 2), count);
  return true;
}

static uint8_t rx_state = 0;
static uint8_t rx_sync = 0;
static uint8_t rx_payload[RX_MAX_LEN];
static uint8_t rx_len = 0;
static uint8_t rx_expected = 0;
static uint16_t rx_sum = 0;

static void process_config(const uint8_t *p, int n, bool store) {
  if (!si5351_ok) si5351_begin();  // retry if the chip was absent at boot
  if (store) {
    flash_save_pairs(p, n);
    diag_stored++;
  }
  bool applied = si5351_apply(p, n);
  diag_applied++;

  uint8_t sum = 0;
  for (int i = 0; i < 2 * n; i++) sum = (uint8_t)(sum + p[i]);
  tx_byte(applied ? ACK_OK : ACK_I2C_FAIL);
  tx_byte((uint8_t)n);
  tx_byte(sum);
  tx_byte((uint8_t)((store ? 1 : 0) | (si5351_ok ? 2 : 0)));
}

// Answer a read frame. Reading changes nothing on the Si5351.
static void process_read(const uint8_t *regs, int n) {
  if (!si5351_ok) si5351_begin();
  uint8_t vals[RX_MAX_LEN];
  bool ok = true;
  for (int i = 0; i < n; i++) {
    vals[i] = 0;
    if (!i2c_read_reg(regs[i], &vals[i])) ok = false;
  }
  uint8_t sum = 0;
  tx_byte(ok ? ACK_OK : ACK_I2C_FAIL);
  tx_byte((uint8_t)n);
  for (int i = 0; i < n; i++) {
    tx_byte(vals[i]);
    sum = (uint8_t)(sum + vals[i]);
  }
  tx_byte(sum);
}

static void on_serial_byte(uint8_t b) {
  switch (rx_state) {
    case 0:
      if (b == FRAME_SYNC_STORE || b == FRAME_SYNC_APPLY || b == FRAME_SYNC_READ) {
        rx_sync = b;
        rx_state = 1;
      }
      break;
    case 1:
      if (b > RX_MAX_LEN) { rx_state = 0; break; }
      rx_expected = b;
      rx_len = 0;
      rx_sum = 0;
      rx_state = (b == 0) ? 3 : 2;
      break;
    case 2:
      rx_payload[rx_len++] = b;
      rx_sum = (rx_sum + b) & 0xFF;
      if (rx_len == rx_expected) rx_state = 3;
      break;
    case 3:
      if (b == (uint8_t)rx_sum) {
        if (rx_sync == FRAME_SYNC_READ) process_read(rx_payload, rx_expected);
        else process_config(rx_payload, rx_expected / 2, rx_sync == FRAME_SYNC_STORE);
      }
      rx_state = 0;
      break;
  }
}

void setup() {
  uart_rx_begin(115200);
  tx_begin();
  si5351_begin();
  cfg_from_flash = flash_load_and_apply() ? 1 : 0;
  if (!cfg_from_flash) {
    si5351_apply(&FALLBACK_REGS[0][0],
                 (int)(sizeof(FALLBACK_REGS) / sizeof(FALLBACK_REGS[0])));
  }
  // Diagnostic sweep last, so it never delays the output coming up. Costs
  // ~100 ms of NAKs; the result is readable over the debug probe.
  i2c_scan_bus();
}

void loop() {
  // Read straight from the driver instead of testing Serial.available():
  // the core computes it as (tail - head) without wrapping, so it goes
  // negative once the 100-byte RX ring wraps and reception would stall.
  for (;;) {
    int b = Serial.read();
    if (b < 0) break;
    on_serial_byte((uint8_t)b);
  }
}

// wire.cpp — inter-chip SPI link + a loopback self-test. See wire.h for the role/pin contract.
//
// Uses the ESP-IDF SPI drivers (driver/spi_master.h / spi_slave.h). Host = SPI2_HOST (FSPI) on both
// chips; the TFT keeps SPI3. SPI-DMA needs word-aligned buffers (WORD_ALIGNED_ATTR, internal DRAM)
// and transfer lengths that are a multiple of 4 bytes; .length is in BITS.
#include <Arduino.h>
#include "wire.h"

uint32_t wire_fnv1a(const uint8_t *data, size_t len)
{
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < len; ++i) { h ^= data[i]; h *= 16777619u; }
    return h;
}

#if WIRE_ENABLED

#include "driver/spi_master.h"
#include "driver/spi_slave.h"

// The loopback uses one fixed-size frame (a multiple of 4; the real transport below sizes per
// message). Frame layout:
//   byte 0     : message type (MSG_PING)
//   byte 1     : payload length (bytes, <= FRAME-4)
//   byte 2..3  : a little-endian counter, so every frame is unique
//   byte 4..   : payload
static constexpr spi_host_device_t WIRE_HOST = SPI2_HOST;
static constexpr int      FRAME_BYTES    = 32;          // multiple of 4
static constexpr int      PING_PAYLOAD   = 16;          // bytes actually checksummed
// 40 MHz (80MHz APB / 2) — shrinks the ~2KB command transfer from ~1.6ms (at 10MHz) to ~0.4ms, so
// the wire is no longer meaningfully on the frame's critical path. This is MASTER-ONLY (the slave
// clocks off SCLK), so only the CPU chip reflashes to change it. NOTE: the inter-chip pins route via
// the GPIO matrix (not the SPI2 IO_MUX pins), which lowers the reliable ceiling; if the display tears
// or games glitch at 40MHz, step down: 26666667 (80/3) or 20000000 (80/4).
static constexpr uint32_t WIRE_CLOCK_HZ  = 40 * 1000 * 1000;

// DMA buffers — word-aligned, internal DRAM (a plain static array is not PSRAM, so it is DMA-capable).
static WORD_ALIGNED_ATTR uint8_t tx_buf[FRAME_BYTES];
static WORD_ALIGNED_ATTR uint8_t rx_buf[FRAME_BYTES];

static spi_bus_config_t make_bus_config()
{
    spi_bus_config_t b = {};
    b.mosi_io_num     = WIRE_MOSI;
    b.miso_io_num     = WIRE_MISO;
    b.sclk_io_num     = WIRE_SCLK;
    b.quadwp_io_num   = -1;
    b.quadhd_io_num   = -1;
    // Must be >= the largest transaction: the transport queues up to WIRE_MAX_MSG-byte buffers, and
    // a too-small value makes spi_slave_queue_trans fail.
    b.max_transfer_sz = WIRE_MAX_MSG;
    return b;
}

// ─────────────────────────────────────────────────────────────────────────────────────────────────
#if defined(WIRE_ROLE_CPU)   // ---- MASTER (the clone / CPU chip) ----

static spi_device_handle_t s_dev;
static bool s_master_ready = false;   // guards against double spi_bus_initialize

static void wire_master_init_spi()
{
    if (s_master_ready) return;
    pinMode(WIRE_READY, INPUT);   // READY comes FROM the PPU

    spi_bus_config_t bus = make_bus_config();
    ESP_ERROR_CHECK(spi_bus_initialize(WIRE_HOST, &bus, SPI_DMA_CH_AUTO));

    spi_device_interface_config_t dev = {};
    dev.mode           = 0;                 // CPOL=0, CPHA=0
    dev.clock_speed_hz = WIRE_CLOCK_HZ;
    dev.spics_io_num   = WIRE_CS;           // hardware-driven CS
    dev.queue_size     = 3;
    ESP_ERROR_CHECK(spi_bus_add_device(WIRE_HOST, &dev, &s_dev));
    s_master_ready = true;
}

// Real transport: wait for the PPU to be armed (READY high), then send one padded message.
static WORD_ALIGNED_ATTR uint8_t s_tx[WIRE_MAX_MSG];

void wire_master_begin()
{
    wire_master_init_spi();
    Serial.println("wire: ROLE = CPU (SPI master), transport ready");
    Serial.printf("  SCLK=%d MOSI=%d MISO=%d CS=%d  READY(in)=%d  @ %lu Hz\n",
                  WIRE_SCLK, WIRE_MOSI, WIRE_MISO, WIRE_CS, WIRE_READY, (unsigned long)WIRE_CLOCK_HZ);
}

void wire_send(uint8_t type, const uint8_t *payload, uint16_t len)
{
    // Backpressure: don't send while the PPU is mid-compose/push (it holds READY low).
    uint32_t t0 = millis();
    while (digitalRead(WIRE_READY) == LOW) {
        if (millis() - t0 > WIRE_TIMEOUT_MS) {
            Serial.println("wire: WARNING READY stuck low (PPU link down?) — sending anyway");
            break;   // B3 will turn this into a real recovery path
        }
    }
    int total  = 4 + (int)len;
    int padded = (total + 3) & ~3;                 // 4-byte multiple for SPI-DMA
    if (padded > WIRE_MAX_MSG) padded = WIRE_MAX_MSG;
    memset(s_tx, 0, padded);
    s_tx[0] = type;
    s_tx[1] = len & 0xFF;
    s_tx[2] = (len >> 8) & 0xFF;
    s_tx[3] = 0;
    int copy = padded - 4; if (copy > len) copy = len;
    memcpy(s_tx + 4, payload, copy);

    spi_transaction_t t = {};
    t.length    = padded * 8;   // bits
    t.tx_buffer = s_tx;
    t.rx_buffer = nullptr;      // fire-and-forget; no response path
    spi_device_transmit(s_dev, &t);
}

void wire_test_setup()
{
    wire_master_init_spi();
    Serial.println("wire test: ROLE = CPU (SPI master)");
    Serial.printf("  SCLK=%d MOSI=%d MISO=%d CS=%d  READY(in)=%d  @ %lu Hz\n",
                  WIRE_SCLK, WIRE_MOSI, WIRE_MISO, WIRE_CS, WIRE_READY, (unsigned long)WIRE_CLOCK_HZ);
}

void wire_test_loop()
{
    static uint16_t ctr = 0;
    static uint32_t expected_prev = 0;   // checksum the slave should echo back next (one-behind)
    static bool     have_prev = false;
    static uint32_t sent = 0, matched = 0, mismatched = 0;
    static uint32_t last_report = 0;

    // Build a unique frame.
    memset(tx_buf, 0, FRAME_BYTES);
    tx_buf[0] = MSG_PING;
    tx_buf[1] = PING_PAYLOAD;
    tx_buf[2] = ctr & 0xFF;
    tx_buf[3] = (ctr >> 8) & 0xFF;
    for (int i = 0; i < PING_PAYLOAD; ++i)
        tx_buf[4 + i] = (uint8_t)(ctr * 31 + i * 7 + 1);
    uint32_t my_cs = wire_fnv1a(tx_buf + 4, PING_PAYLOAD);

    // Full-duplex: send this frame, simultaneously receive the slave's response to the PREVIOUS one.
    spi_transaction_t t = {};
    t.length    = FRAME_BYTES * 8;   // bits
    t.tx_buffer = tx_buf;
    t.rx_buffer = rx_buf;
    esp_err_t e = spi_device_transmit(s_dev, &t);
    ++sent;

    if (e == ESP_OK && have_prev) {
        uint32_t got = (uint32_t)rx_buf[0] | (rx_buf[1] << 8) | (rx_buf[2] << 16) | ((uint32_t)rx_buf[3] << 24);
        if (got == expected_prev) ++matched;
        else {
            ++mismatched;
            if (mismatched <= 10)
                Serial.printf("  MISMATCH #%lu: got %08lX expected %08lX\n",
                              (unsigned long)mismatched, (unsigned long)got, (unsigned long)expected_prev);
        }
    }
    expected_prev = my_cs;
    have_prev = true;
    ++ctr;

    uint32_t now = millis();
    if (now - last_report >= 1000) {
        Serial.printf("CPU: sent=%lu matched=%lu mismatched=%lu  READY=%d\n",
                      (unsigned long)sent, (unsigned long)matched, (unsigned long)mismatched,
                      digitalRead(WIRE_READY));
        last_report = now;
    }
    delay(5);   // ~200 pings/sec -> thousands within seconds
}

// ─────────────────────────────────────────────────────────────────────────────────────────────────
#elif defined(WIRE_ROLE_PPU)   // ---- SLAVE (the original / PPU chip) ----

static bool s_slave_ready = false;

static void wire_slave_init_spi()
{
    if (s_slave_ready) return;
    pinMode(WIRE_READY, OUTPUT);
    digitalWrite(WIRE_READY, LOW);

    spi_bus_config_t bus = make_bus_config();
    spi_slave_interface_config_t slv = {};
    slv.spics_io_num = WIRE_CS;
    slv.flags        = 0;
    slv.queue_size   = 3;
    slv.mode         = 0;
    ESP_ERROR_CHECK(spi_slave_initialize(WIRE_HOST, &bus, &slv, SPI_DMA_CH_AUTO));
    s_slave_ready = true;
}

// Real transport: receive exactly one message. Uses queue+get_result so READY is raised only AFTER
// the DMA is armed (closing the window where the master could send before the slave is listening).
static WORD_ALIGNED_ATTR uint8_t s_rx[WIRE_MAX_MSG];

void wire_slave_begin()
{
    wire_slave_init_spi();
    Serial.println("wire: ROLE = PPU (SPI slave), transport ready");
    Serial.printf("  SCLK=%d MOSI=%d MISO=%d CS=%d  READY(out)=%d\n",
                  WIRE_SCLK, WIRE_MOSI, WIRE_MISO, WIRE_CS, WIRE_READY);
}

int wire_slave_receive(uint8_t *type_out, uint8_t *buf, int maxlen)
{
    spi_slave_transaction_t t = {};
    t.length     = WIRE_MAX_MSG * 8;
    t.rx_buffer  = s_rx;
    t.tx_buffer  = nullptr;
    if (spi_slave_queue_trans(WIRE_HOST, &t, portMAX_DELAY) != ESP_OK) return -1;

    digitalWrite(WIRE_READY, HIGH);          // armed: master may send now
    spi_slave_transaction_t *done = nullptr;
    spi_slave_get_trans_result(WIRE_HOST, &done, portMAX_DELAY);   // blocks until the master clocks it
    digitalWrite(WIRE_READY, LOW);           // busy

    if (!done || done->trans_len < 32) return -1;   // fewer than the 4-byte header arrived -> ignore
    uint8_t type = s_rx[0];
    int len = (int)s_rx[1] | ((int)s_rx[2] << 8);
    if (len > maxlen) len = maxlen;
    if (len < 0) len = 0;
    *type_out = type;
    memcpy(buf, s_rx + 4, len);
    return len;
}

void wire_test_setup()
{
    wire_slave_init_spi();
    memset(tx_buf, 0, FRAME_BYTES);   // one-behind response starts defined
    Serial.println("wire test: ROLE = PPU (SPI slave)");
    Serial.printf("  SCLK=%d MOSI=%d MISO=%d CS=%d  READY(out)=%d\n",
                  WIRE_SCLK, WIRE_MOSI, WIRE_MISO, WIRE_CS, WIRE_READY);
}

void wire_test_loop()
{
    static uint32_t recv = 0;
    static uint32_t last_report = 0;

    // Arm one full-duplex transaction: transmit the previous checksum (already in tx_buf), receive
    // the new ping. spi_slave_transmit BLOCKS until the master clocks a transaction.
    digitalWrite(WIRE_READY, HIGH);          // idle + armed: master may send
    spi_slave_transaction_t t = {};
    t.length    = FRAME_BYTES * 8;
    t.tx_buffer = tx_buf;
    t.rx_buffer = rx_buf;
    esp_err_t e = spi_slave_transmit(WIRE_HOST, &t, portMAX_DELAY);
    digitalWrite(WIRE_READY, LOW);           // busy processing
    if (e != ESP_OK) return;
    ++recv;

    // Checksum the payload we just received, and arm it as the NEXT response.
    uint8_t len = rx_buf[1];
    if (len > FRAME_BYTES - 4) len = FRAME_BYTES - 4;
    uint32_t cs = wire_fnv1a(rx_buf + 4, len);
    memset(tx_buf, 0, FRAME_BYTES);
    tx_buf[0] = cs & 0xFF;
    tx_buf[1] = (cs >> 8) & 0xFF;
    tx_buf[2] = (cs >> 16) & 0xFF;
    tx_buf[3] = (cs >> 24) & 0xFF;

    uint32_t now = millis();
    if (now - last_report >= 1000) {
        uint16_t ctr = (uint16_t)rx_buf[2] | (rx_buf[3] << 8);
        Serial.printf("PPU: recv=%lu  last[type=%u len=%u ctr=%u cs=%08lX]\n",
                      (unsigned long)recv, rx_buf[0], rx_buf[1], ctr, (unsigned long)cs);
        last_report = now;
    }
}

#endif  // role

#endif  // WIRE_ENABLED

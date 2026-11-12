// tjdiag SCI bridge for an ESP32 DevKit (WROOM-32).
//
// A read-only bridge between the laptop (USB) and a Chrysler engine
// computer's SCI serial link. All behaviour lives in bridge_core.h; this file
// only connects it to the ESP32's hardware.
//
// Wiring is in docs/sci-interface-esp32.md. The ESP32's pins are 3.3 V and
// the SCI lines are 5 V: the level shifter described there is required.
//
// Board: "ESP32 Dev Module". No extra libraries.

#include <Arduino.h>

#include "bridge_core.h"

namespace {

const uint32_t USB_BAUD = 250000;
const int SCI_RX_PIN = 16;   // from the level shifter's low-voltage side (computer transmit)
const int SCI_TX_PIN = 17;   // to the level shifter's low-voltage side (computer receive)
const int BATTERY_PIN = 34;  // battery divider; input-only pin on ADC1

// Battery divider: 100 k from the connector's pin 16, 15 k to ground.
const float DIVIDER_HIGH_OHMS = 100000.0f;
const float DIVIDER_LOW_OHMS = 15000.0f;
const int BATTERY_SAMPLES = 16;

class EspHal : public tjbridge::Hal {
 public:
  int usbRead() override { return Serial.available() ? Serial.read() : -1; }

  void usbWrite(const uint8_t* data, size_t length) override { Serial.write(data, length); }

  void sciWrite(uint8_t value) override { Serial2.write(value); }

  int sciRead(uint32_t timeoutMs) override {
    const uint32_t start = ::millis();
    while (!Serial2.available()) {
      if (::millis() - start >= timeoutMs) return -1;
      delayMicroseconds(50);
    }
    return Serial2.read();
  }

  void sciDiscardInput() override {
    while (Serial2.available()) Serial2.read();
  }

  void sciSetBaud(uint32_t baud) override {
    Serial2.flush();  // let anything still being sent finish at the old speed
    Serial2.updateBaudRate(baud);
  }

  uint32_t millis() override { return ::millis(); }

  uint16_t batteryMillivolts() override {
    uint32_t total = 0;
    for (int i = 0; i < BATTERY_SAMPLES; i++) total += analogReadMilliVolts(BATTERY_PIN);
    const float atPin = static_cast<float>(total) / BATTERY_SAMPLES;
    const float atConnector = atPin * (DIVIDER_HIGH_OHMS + DIVIDER_LOW_OHMS) / DIVIDER_LOW_OHMS;
    return atConnector > 65535.0f ? 65535 : static_cast<uint16_t>(atConnector);
  }
};

EspHal hal;
tjbridge::Bridge bridge(hal);

}  // namespace

void setup() {
  Serial.setRxBufferSize(2048);  // a full message list arrives in one burst
  Serial.begin(USB_BAUD);
  // The ADC is left at its default range (about 0 to 3.1 V), which the
  // divider is sized for.
  Serial2.begin(7812, SERIAL_8N1, SCI_RX_PIN, SCI_TX_PIN);
  bridge.begin();
}

void loop() {
  bridge.poll();
}

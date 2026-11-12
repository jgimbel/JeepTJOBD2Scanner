// Minimal stand-in for the parts of the Arduino-ESP32 API that the bridge
// sketch uses, with the same signatures. It exists so the sketch can be
// syntax- and type-checked on a machine without the ESP32 toolchain. It is
// not a substitute for compiling in the Arduino IDE.

#ifndef TJDIAG_ARDUINO_STUB_H
#define TJDIAG_ARDUINO_STUB_H

#include <stddef.h>
#include <stdint.h>

#define SERIAL_8N1 0x800001c

class HardwareSerial {
 public:
  void begin(unsigned long baud, uint32_t config = SERIAL_8N1, int8_t rxPin = -1, int8_t txPin = -1);
  size_t setRxBufferSize(size_t size);
  void updateBaudRate(unsigned long baud);
  int available();
  int read();
  size_t write(uint8_t value);
  size_t write(const uint8_t* data, size_t length);
  void flush();
};

extern HardwareSerial Serial;
extern HardwareSerial Serial1;
#if !defined(CONFIG_IDF_TARGET_ESP32C5)
extern HardwareSerial Serial2;  // the ESP32-C5 has no third serial port
#endif

unsigned long millis();
void delayMicroseconds(uint32_t microseconds);
uint32_t analogReadMilliVolts(uint8_t pin);

#endif  // TJDIAG_ARDUINO_STUB_H

// Host build of the SCI bridge core, for testing without an ESP32.
//
// The laptop side is stdin/stdout. The SCI side is a pair of file descriptors
// passed on the command line, so a test can sit on the other end and play the
// engine computer:
//
//   bridge_host <fd to read SCI from> <fd to write SCI to> [battery millivolts]
//
// Baud rate changes are reported on stderr as "BAUD <rate>".

#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

#include "../bridge_core.h"

namespace {

class HostHal : public tjbridge::Hal {
 public:
  HostHal(int sciIn, int sciOut, uint16_t batteryMillivolts)
      : sciIn_(sciIn), sciOut_(sciOut), battery_(batteryMillivolts) {}

  int usbRead() override { return readByte(STDIN_FILENO, 0); }

  void usbWrite(const uint8_t* data, size_t length) override { writeAll(STDOUT_FILENO, data, length); }

  void sciWrite(uint8_t value) override { writeAll(sciOut_, &value, 1); }

  int sciRead(uint32_t timeoutMs) override { return readByte(sciIn_, static_cast<int>(timeoutMs)); }

  void sciDiscardInput() override {
    while (readByte(sciIn_, 0) >= 0) {
    }
  }

  void sciSetBaud(uint32_t baud) override {
    fprintf(stderr, "BAUD %u\n", static_cast<unsigned>(baud));
    fflush(stderr);
  }

  uint32_t millis() override {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return static_cast<uint32_t>(now.tv_sec * 1000 + now.tv_nsec / 1000000);
  }

  uint16_t batteryMillivolts() override { return battery_; }

  bool usbClosed() const { return usbClosed_; }

 private:
  int readByte(int fd, int timeoutMs) {
    struct pollfd request = {fd, POLLIN, 0};
    if (poll(&request, 1, timeoutMs) <= 0) return -1;
    uint8_t value;
    const ssize_t got = read(fd, &value, 1);
    if (got == 1) return value;
    if (got == 0 && fd == STDIN_FILENO) usbClosed_ = true;
    return -1;
  }

  static void writeAll(int fd, const uint8_t* data, size_t length) {
    while (length > 0) {
      const ssize_t written = write(fd, data, length);
      if (written <= 0) return;
      data += written;
      length -= static_cast<size_t>(written);
    }
  }

  int sciIn_;
  int sciOut_;
  uint16_t battery_;
  bool usbClosed_ = false;
};

}  // namespace

int main(int argc, char** argv) {
  if (argc < 3) {
    fprintf(stderr, "usage: %s <sci read fd> <sci write fd> [battery millivolts]\n", argv[0]);
    return 2;
  }
  const uint16_t battery = argc > 3 ? static_cast<uint16_t>(atoi(argv[3])) : 12600;
  HostHal hal(atoi(argv[1]), atoi(argv[2]), battery);
  tjbridge::Bridge bridge(hal);
  bridge.begin();
  while (!hal.usbClosed()) {
    struct pollfd wait = {STDIN_FILENO, POLLIN, 0};
    poll(&wait, 1, 1);  // idle briefly instead of spinning
    bridge.poll();
  }
  return 0;
}

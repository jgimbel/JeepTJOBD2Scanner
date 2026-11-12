// tjdiag SCI bridge: core logic.
//
// A read-only bridge between a laptop (USB serial) and a Chrysler engine
// computer's SCI serial link. This file holds all of the behaviour and has no
// dependency on Arduino or the ESP32, so the same code runs on the board and
// in the host test build (host/host_main.cpp).
//
// USB packet format, both directions:
//
//   3D  LEN_HI LEN_LO  DATACODE  SUBCODE  payload...  CHECKSUM
//
//   LEN       bytes from DATACODE through the last payload byte
//   DATACODE  bit 7 set = from the bridge; bits 6-4 = bus; bits 3-0 = command
//   CHECKSUM  low byte of the sum of every preceding byte, 3D included
//
// Packets that carry SCI traffic start their payload with a four byte
// millisecond timestamp. This is the packet format used by the open-source
// ChryslerScanner firmware, so the same laptop software can drive either
// board. The implementation here is independent of that project's code.
//
// Safety: the only SCI messages this firmware will transmit are the reads
// listed in isReadOnly(). It contains no code to write, erase or reprogram.

#ifndef TJDIAG_BRIDGE_CORE_H
#define TJDIAG_BRIDGE_CORE_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace tjbridge {

const uint8_t FW_MAJOR = 1;
const uint8_t FW_MINOR = 0;
const uint8_t FW_PATCH = 0;

// Hardware access the core needs. Implemented once for the ESP32 and once for
// the host test build.
struct Hal {
  virtual int usbRead() = 0;  // next byte from the laptop, or -1 if none
  virtual void usbWrite(const uint8_t* data, size_t length) = 0;
  virtual void sciWrite(uint8_t value) = 0;
  virtual int sciRead(uint32_t timeoutMs) = 0;  // next byte, or -1 on timeout
  virtual void sciDiscardInput() = 0;
  virtual void sciSetBaud(uint32_t baud) = 0;
  virtual uint32_t millis() = 0;
  virtual uint16_t batteryMillivolts() = 0;  // at the diagnostic connector
  virtual ~Hal() {}
};

class Bridge {
 public:
  explicit Bridge(Hal& hal) : hal_(hal) {}

  void begin() { hal_.sciSetBaud(BAUD_LOW); }

  // Call continuously. Handles laptop packets and steps through a message list.
  void poll() {
    readUsb();
    while (parsePacket()) {
    }
    stepList();
  }

 private:
  // -- protocol constants ---------------------------------------------------
  enum : uint8_t { SYNC = 0x3D };
  enum : uint8_t { BUS_USB = 0, BUS_PCM = 2 };
  enum : uint8_t {
    CMD_HANDSHAKE = 0x01,
    CMD_STATUS = 0x02,
    CMD_SETTINGS = 0x03,
    CMD_REQUEST = 0x04,
    CMD_RESPONSE = 0x05,
    CMD_MSG_TX = 0x06,
    CMD_MSG_RX = 0x07,
    CMD_ERROR = 0x0F
  };
  enum : uint8_t { SET_SCI_BUS = 0x03, SET_REPEAT = 0x04 };
  enum : uint8_t { REQ_HWFW = 0x01, REQ_BATTERY = 0x03 };
  enum : uint8_t { TX_STOP = 0x01, TX_SINGLE = 0x02, TX_LIST = 0x03 };
  enum : uint8_t { RX_LOW_SPEED = 0x01, RX_HIGH_SPEED = 0x02 };
  enum : uint8_t {
    ERR_LENGTH = 0x01,
    ERR_COMMAND = 0x02,
    ERR_SUBCOMMAND = 0x03,
    ERR_PAYLOAD = 0x04,
    ERR_CHECKSUM = 0x05,
    ERR_TIMEOUT = 0x06,
    ERR_BUS = 0x08
  };
  enum : uint8_t { SCI_ENABLE = 0x80, SCI_SPEED_MASK = 0x03, SCI_SPEED_LOW = 0x01, SCI_SPEED_HIGH = 0x02 };
  // Settings bits for things this board does not have: transmission
  // controller channel, older and newer controller families, inverted logic.
  enum : uint8_t { SCI_UNSUPPORTED_BITS = 0x78 };

  // SCI commands (engine computer, low-speed mode).
  enum : uint8_t {
    SCI_FAULT_LIST = 0x10,
    SCI_HIGH_SPEED = 0x12,
    SCI_ROM_16 = 0x15,
    SCI_ROM_24 = 0x26,
    SCI_EEPROM = 0x28,
    SCI_LOW_SPEED = 0xFE
  };
  enum : uint8_t { RAM_TABLE_FIRST = 0xF0, RAM_TABLE_SIZE = 0xF0 };

  enum : uint32_t { BAUD_LOW = 7812, BAUD_HIGH = 62500 };
  enum : uint32_t {
    ECHO_TIMEOUT_MS = 50,
    REPLY_TIMEOUT_MS = 50,
    EEPROM_REPLY_TIMEOUT_MS = 300,  // EEPROM reads are slow on some controllers
    HIGH_SPEED_TIMEOUT_MS = 10,
    PARTIAL_PACKET_TIMEOUT_MS = 200
  };
  enum : size_t {
    MAX_PACKET_LENGTH = 1024,
    USB_BUFFER = MAX_PACKET_LENGTH + 8,
    MAX_SCI_MESSAGE = 1 + RAM_TABLE_SIZE,
    MAX_REPLY = 64,
    LIST_BUFFER = 256
  };

  // -- the read-only guard --------------------------------------------------

  // True only for messages that read. Lengths are checked as well, so that
  // nothing can ride along behind an allowed command byte.
  bool isReadOnly(const uint8_t* message, size_t length) const {
    if (length == 0) return false;
    const uint8_t command = message[0];
    if (highSpeed()) {
      if (command == SCI_LOW_SPEED) return length == 1;
      if (command < RAM_TABLE_FIRST) return false;
      if (length == 2 && message[1] == 0xFF) return true;  // whole table
      if (length > MAX_SCI_MESSAGE) return false;
      for (size_t i = 1; i < length; i++) {
        if (message[i] >= RAM_TABLE_SIZE) return false;
      }
      return true;
    }
    switch (command) {
      case SCI_FAULT_LIST:
      case SCI_HIGH_SPEED:
      case SCI_LOW_SPEED:
        return length == 1;
      case SCI_ROM_16:
      case SCI_EEPROM:
        return length == 3;
      case SCI_ROM_24:
        return length == 4;
      default:
        return false;
    }
  }

  // -- USB side ---------------------------------------------------------------

  void readUsb() {
    int value;
    while (usbLength_ < USB_BUFFER && (value = hal_.usbRead()) >= 0) {
      usbBuffer_[usbLength_++] = static_cast<uint8_t>(value);
      lastUsbByteMs_ = hal_.millis();
    }
    // A packet that never completes must not block the ones after it.
    if (usbLength_ > 0 && hal_.millis() - lastUsbByteMs_ > PARTIAL_PACKET_TIMEOUT_MS) {
      usbLength_ = 0;
      sendError(ERR_TIMEOUT);
    }
  }

  void dropUsb(size_t count) {
    memmove(usbBuffer_, usbBuffer_ + count, usbLength_ - count);
    usbLength_ -= count;
  }

  // Handles one complete packet if there is one. Returns true if it consumed
  // anything, so the caller should try again.
  bool parsePacket() {
    size_t start = 0;
    while (start < usbLength_ && usbBuffer_[start] != SYNC) start++;
    if (start > 0) dropUsb(start);
    if (usbLength_ < 3) return false;

    const size_t length = (static_cast<size_t>(usbBuffer_[1]) << 8) | usbBuffer_[2];
    if (length < 2 || length > MAX_PACKET_LENGTH) {
      dropUsb(1);
      sendError(ERR_LENGTH);
      return true;
    }
    const size_t total = 3 + length + 1;
    if (usbLength_ < total) return false;

    uint8_t sum = 0;
    for (size_t i = 0; i < total - 1; i++) sum = static_cast<uint8_t>(sum + usbBuffer_[i]);
    if (sum != usbBuffer_[total - 1]) {
      dropUsb(1);
      sendError(ERR_CHECKSUM);
      return true;
    }

    // Copy out before handling: handling may take a while and reads more USB.
    const uint8_t datacode = usbBuffer_[3];
    const uint8_t sub = usbBuffer_[4];
    const size_t payloadLength = length - 2;
    memcpy(packetPayload_, usbBuffer_ + 5, payloadLength);
    dropUsb(total);
    handlePacket((datacode >> 4) & 0x07, datacode & 0x0F, sub, packetPayload_, payloadLength);
    return true;
  }

  void sendPacket(uint8_t bus, uint8_t command, uint8_t sub, const uint8_t* payload, size_t length) {
    uint8_t header[9];
    size_t headerLength = 5;
    const size_t dataLength = length + (bus ? 4 : 0);
    header[0] = SYNC;
    header[1] = static_cast<uint8_t>((dataLength + 2) >> 8);
    header[2] = static_cast<uint8_t>(dataLength + 2);
    header[3] = static_cast<uint8_t>(0x80 | (bus << 4) | command);
    header[4] = sub;
    if (bus) {
      const uint32_t now = hal_.millis();
      header[5] = static_cast<uint8_t>(now >> 24);
      header[6] = static_cast<uint8_t>(now >> 16);
      header[7] = static_cast<uint8_t>(now >> 8);
      header[8] = static_cast<uint8_t>(now);
      headerLength = 9;
    }
    uint8_t sum = 0;
    for (size_t i = 0; i < headerLength; i++) sum = static_cast<uint8_t>(sum + header[i]);
    for (size_t i = 0; i < length; i++) sum = static_cast<uint8_t>(sum + payload[i]);
    hal_.usbWrite(header, headerLength);
    if (length) hal_.usbWrite(payload, length);
    hal_.usbWrite(&sum, 1);
  }

  void sendError(uint8_t code) {
    const uint8_t payload[1] = {0xFF};
    sendPacket(BUS_USB, CMD_ERROR, code, payload, 1);
  }

  // -- commands ---------------------------------------------------------------

  bool highSpeed() const { return (settings_ & SCI_SPEED_MASK) == SCI_SPEED_HIGH; }
  bool enabled() const { return (settings_ & SCI_ENABLE) != 0; }

  uint16_t battery() {
    const uint16_t millivolts = hal_.batteryMillivolts();
    return millivolts < 6000 ? 0 : millivolts;  // below 6 V: nothing connected
  }

  void handlePacket(uint8_t bus, uint8_t command, uint8_t sub, const uint8_t* payload, size_t length) {
    if (bus == BUS_USB) {
      handleBoardCommand(command, sub, payload, length);
    } else if (bus == BUS_PCM) {
      if (command != CMD_MSG_TX) {
        sendError(ERR_COMMAND);
      } else {
        handleTransmit(sub, payload, length);
      }
    } else {
      sendError(ERR_BUS);
    }
  }

  void handleBoardCommand(uint8_t command, uint8_t sub, const uint8_t* payload, size_t length) {
    switch (command) {
      case CMD_HANDSHAKE: {
        static const char name[] = "TJDIAGSCIBRIDGE";
        sendPacket(BUS_USB, CMD_HANDSHAKE, 0x00, reinterpret_cast<const uint8_t*>(name), sizeof(name) - 1);
        break;
      }
      case CMD_STATUS: {
        uint8_t status[53];
        memset(status, 0, sizeof(status));
        status[0] = 'E';
        status[1] = 'S';
        status[2] = 'P';
        const uint16_t millivolts = battery();
        status[12] = millivolts ? 1 : 0;
        status[13] = static_cast<uint8_t>(millivolts >> 8);
        status[14] = static_cast<uint8_t>(millivolts);
        status[24] = settings_;
        sendPacket(BUS_USB, CMD_STATUS, 0x00, status, sizeof(status));
        break;
      }
      case CMD_SETTINGS:
        if (sub == SET_SCI_BUS && length >= 1) {
          applySettings(payload[0]);
        } else if (sub == SET_REPEAT && length >= 3) {
          if (payload[0] == BUS_PCM) {
            intervalMs_ = (static_cast<uint32_t>(payload[1]) << 8) | payload[2];
          }
          sendPacket(BUS_USB, CMD_SETTINGS, SET_REPEAT, payload, 3);
        } else {
          sendError(length ? ERR_SUBCOMMAND : ERR_PAYLOAD);
        }
        break;
      case CMD_REQUEST:
        if (sub == REQ_HWFW) {
          uint8_t info[32];
          memset(info, 0, sizeof(info));
          info[26] = FW_MAJOR;
          info[27] = FW_MINOR;
          info[28] = FW_PATCH;
          sendPacket(BUS_USB, CMD_RESPONSE, REQ_HWFW, info, sizeof(info));
        } else if (sub == REQ_BATTERY) {
          const uint16_t millivolts = battery();
          const uint8_t reply[2] = {static_cast<uint8_t>(millivolts >> 8), static_cast<uint8_t>(millivolts)};
          sendPacket(BUS_USB, CMD_RESPONSE, REQ_BATTERY, reply, 2);
        } else {
          sendError(ERR_SUBCOMMAND);
        }
        break;
      default:
        sendError(ERR_COMMAND);
    }
  }

  void applySettings(uint8_t requested) {
    const uint8_t speed = requested & SCI_SPEED_MASK;
    if ((requested & SCI_UNSUPPORTED_BITS) || (speed != SCI_SPEED_LOW && speed != SCI_SPEED_HIGH)) {
      sendError(ERR_PAYLOAD);
      return;
    }
    setSettings(requested);
  }

  void setSettings(uint8_t value) {
    settings_ = value;
    hal_.sciSetBaud(highSpeed() ? BAUD_HIGH : BAUD_LOW);
    listCount_ = 0;
    sendPacket(BUS_USB, CMD_SETTINGS, SET_SCI_BUS, &settings_, 1);
  }

  void handleTransmit(uint8_t sub, const uint8_t* payload, size_t length) {
    if (sub == TX_STOP) {
      listCount_ = 0;
      const uint8_t reply[1] = {BUS_PCM};
      sendPacket(BUS_USB, CMD_MSG_TX, TX_STOP, reply, 1);
    } else if (sub == TX_SINGLE) {
      if (!isReadOnly(payload, length)) {
        sendError(ERR_PAYLOAD);
        return;
      }
      listCount_ = 0;
      transmit(payload, length);
    } else if (sub == TX_LIST) {
      startList(payload, length);
    } else {
      sendError(ERR_SUBCOMMAND);
    }
  }

  // List payload: count, then for each message its length and its bytes.
  // Every message is checked before the first one is sent.
  void startList(const uint8_t* payload, size_t length) {
    if (length < 3 || length - 1 > LIST_BUFFER) {
      sendError(ERR_PAYLOAD);
      return;
    }
    const uint8_t count = payload[0];
    size_t position = 1;
    for (uint8_t i = 0; i < count; i++) {
      if (position >= length) {
        sendError(ERR_PAYLOAD);
        return;
      }
      const size_t messageLength = payload[position];
      if (position + 1 + messageLength > length || !isReadOnly(payload + position + 1, messageLength)) {
        sendError(ERR_PAYLOAD);
        return;
      }
      position += 1 + messageLength;
    }
    if (count == 0 || position != length) {
      sendError(ERR_PAYLOAD);
      return;
    }
    memcpy(list_, payload + 1, length - 1);
    listCount_ = count;
    listIndex_ = 0;
    listPosition_ = 0;
    listStarted_ = false;
  }

  void stepList() {
    if (listCount_ == 0) return;
    const uint32_t now = hal_.millis();
    if (listStarted_ && now - lastListMs_ < intervalMs_) return;
    listStarted_ = true;
    lastListMs_ = now;

    const size_t messageLength = list_[listPosition_];
    const uint8_t* message = list_ + listPosition_ + 1;
    // The speed mode can change while a list runs, so check again.
    const bool answered = isReadOnly(message, messageLength) && transmit(message, messageLength);
    listPosition_ += 1 + messageLength;
    listIndex_++;
    if (!answered || listIndex_ >= listCount_) {
      listCount_ = 0;  // done, or the computer went silent: stop here
    }
  }

  // -- SCI side ---------------------------------------------------------------

  // Sends one message and reports what came back. Returns false if the engine
  // computer sent nothing at all.
  bool transmit(const uint8_t* message, size_t length) {
    if (!enabled()) return false;
    return highSpeed() ? transmitHighSpeed(message, length) : transmitLowSpeed(message, length);
  }

  // Low speed is half duplex: the computer echoes each byte, then answers.
  bool transmitLowSpeed(const uint8_t* message, size_t length) {
    uint8_t received[MAX_REPLY];
    size_t count = 0;
    bool echoed = true;
    hal_.sciDiscardInput();
    for (size_t i = 0; i < length; i++) {
      hal_.sciWrite(message[i]);
      const int value = hal_.sciRead(ECHO_TIMEOUT_MS);
      if (value < 0) {
        echoed = false;
        break;
      }
      received[count++] = static_cast<uint8_t>(value);
    }
    if (echoed) {
      const uint8_t command = message[0];
      if (command == SCI_ROM_16 || command == SCI_ROM_24 || command == SCI_EEPROM) {
        const int value = hal_.sciRead(command == SCI_EEPROM ? EEPROM_REPLY_TIMEOUT_MS : REPLY_TIMEOUT_MS);
        if (value >= 0) received[count++] = static_cast<uint8_t>(value);
      } else {
        // Variable-length reply (fault list): read until the line goes quiet.
        while (count < MAX_REPLY) {
          const int value = hal_.sciRead(REPLY_TIMEOUT_MS);
          if (value < 0) break;
          received[count++] = static_cast<uint8_t>(value);
        }
      }
    }
    if (count == 0) return false;
    sendPacket(BUS_PCM, CMD_MSG_RX, RX_LOW_SPEED, received, count);
    if (received[0] == SCI_HIGH_SPEED) {
      // The computer has switched to high speed; follow it.
      setSettings(static_cast<uint8_t>((settings_ & ~SCI_SPEED_MASK) | SCI_SPEED_HIGH));
    }
    return true;
  }

  // High speed: one byte back for every byte sent. The table number is
  // echoed; each offset is answered with the value stored there.
  bool transmitHighSpeed(const uint8_t* message, size_t length) {
    uint8_t request[MAX_SCI_MESSAGE];
    if (length == 2 && message[1] == 0xFF) {
      request[0] = message[0];
      for (size_t i = 0; i < RAM_TABLE_SIZE; i++) request[1 + i] = static_cast<uint8_t>(i);
      length = MAX_SCI_MESSAGE;
    } else {
      memcpy(request, message, length);
    }

    uint8_t report[1 + 2 * RAM_TABLE_SIZE];
    size_t count = 0;
    size_t answered = 0;
    hal_.sciDiscardInput();
    for (size_t i = 0; i < length; i++) {
      hal_.sciWrite(request[i]);
      const int value = hal_.sciRead(HIGH_SPEED_TIMEOUT_MS);
      if (value < 0) break;
      answered++;
      if (i == 0) {
        report[count++] = static_cast<uint8_t>(value);
      } else {
        report[count++] = request[i];
        report[count++] = static_cast<uint8_t>(value);
      }
    }
    if (answered == 0) return false;
    sendPacket(BUS_PCM, CMD_MSG_RX, RX_HIGH_SPEED, report, count);
    if (report[0] == SCI_LOW_SPEED) {
      setSettings(static_cast<uint8_t>((settings_ & ~SCI_SPEED_MASK) | SCI_SPEED_LOW));
    }
    return true;
  }

  Hal& hal_;
  uint8_t settings_ = SCI_SPEED_LOW;  // link disabled until the laptop enables it
  uint32_t intervalMs_ = 100;

  uint8_t usbBuffer_[USB_BUFFER];
  size_t usbLength_ = 0;
  uint32_t lastUsbByteMs_ = 0;
  uint8_t packetPayload_[MAX_PACKET_LENGTH];

  uint8_t list_[LIST_BUFFER];
  uint8_t listCount_ = 0;
  uint8_t listIndex_ = 0;
  size_t listPosition_ = 0;
  bool listStarted_ = false;
  uint32_t lastListMs_ = 0;
};

}  // namespace tjbridge

#endif  // TJDIAG_BRIDGE_CORE_H

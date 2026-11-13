"""A simulated interface board and engine computer for the memory tests.

SimMega imitates the ChryslerScanner firmware's USB protocol as read from its
source, including the awkward parts the driver has to cope with: start-up
chatter, no acknowledgement for a single message, a message list that stalls
when a message gets no bytes back, and a one-entry list that sends nothing.

SimPcm imitates an engine computer on the SCI link, with faults that can be
planted: a bit that flips between reads, an erased block, corrupted bytes, a
command it does not support, or going silent part-way through.

Like the OBD simulator, this is a stand-in. It shows that the scripts handle
the behaviour described in the firmware and its documentation; it is not
evidence of what the real computer does.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from . import scanner as sc
from . import sci

SIM_ROM_SIZE = 0x4000
SIM_EEPROM_SIZE = 0x200
SIM_VIN = "1J4FA49S04P700001"


def golden_rom(size: int = SIM_ROM_SIZE) -> bytes:
    """The simulated computer's correct ROM image."""
    data = bytearray(random.Random(2004).randbytes(size))
    text = b"TJDIAG SIMULATED ROM"
    data[0x40 : 0x40 + len(text)] = text
    return bytes(data)


def golden_eeprom(size: int = SIM_EEPROM_SIZE) -> bytes:
    data = bytearray(random.Random(601).randbytes(size))
    data[0x20 : 0x20 + len(SIM_VIN)] = SIM_VIN.encode("ascii")
    return bytes(data)


@dataclass
class SimPcm:
    rom: bytearray
    eeprom: bytearray
    supports_rom24: bool = True
    supports_rom16: bool = True
    rom_beyond: str = "mirror"  # what addresses past the ROM do: "mirror" or "silent"
    key_on: bool = True
    faults: list[int] = field(default_factory=lambda: [0x01])
    flaky: dict[int, int] = field(default_factory=dict)  # ROM address -> bits that flip
    die_after: int | None = None  # stop answering after this many exchanges
    high_speed: bool = False
    exchanges: int = 0
    forbidden_seen: list[bytes] = field(default_factory=list)
    _flaky_toggle: dict[int, bool] = field(default_factory=dict)

    def _rom_byte(self, address: int) -> int | None:
        if address >= len(self.rom):
            if self.rom_beyond != "mirror":
                return None
            address %= len(self.rom)
        value = self.rom[address]
        if address in self.flaky:
            flip = self._flaky_toggle.get(address, False)
            self._flaky_toggle[address] = not flip
            if flip:
                value ^= self.flaky[address]
        return value

    def exchange(self, message: bytes) -> bytes:
        """Bytes that appear on the computer's transmit line for this message."""
        if not self.key_on:
            return b""
        self.exchanges += 1
        if self.die_after is not None and self.exchanges > self.die_after:
            return b""
        if self.high_speed:
            return self._high_speed(message)

        command = message[0]
        reply = bytearray(message)  # every byte is echoed
        if command == sci.CMD_ROM_24 and len(message) == 4 and self.supports_rom24:
            value = self._rom_byte(int.from_bytes(message[1:], "big"))
            if value is not None:
                reply.append(value)
        elif command == sci.CMD_ROM_16 and len(message) == 3 and self.supports_rom16:
            address = int.from_bytes(message[1:], "big")
            if address < len(self.rom):
                reply.append(self._rom_byte(address))
        elif command == sci.CMD_EEPROM and len(message) == 3:
            address = int.from_bytes(message[1:], "big")
            if address < len(self.eeprom):
                reply.append(self.eeprom[address])
        elif command == sci.CMD_FAULT_LIST:
            reply += bytes(self.faults) + b"\xFE"
            reply.append(sum(reply) & 0xFF)
        elif command == sci.CMD_HIGH_SPEED:
            self.high_speed = True
        elif command not in sci.READ_ONLY_COMMANDS:
            self.forbidden_seen.append(bytes(message))
        return bytes(reply)

    def _high_speed(self, message: bytes) -> bytes:
        table = message[0]
        if table == sci.CMD_LOW_SPEED:
            self.high_speed = False
            return bytes([table])
        if table not in sci.RAM_TABLES:
            return b""
        # One byte back per byte sent: the table number, then a value per offset.
        seed = random.Random(table)
        contents = seed.randbytes(sci.RAM_TABLE_SIZE)
        return bytes([table]) + bytes(contents[o] for o in message[1:] if o < sci.RAM_TABLE_SIZE)


class SimMega:
    """Transport that behaves like a Mega running the ChryslerScanner firmware."""

    def __init__(
        self,
        pcm: SimPcm | None,
        battery_volts: float | list[float] = 12.6,
        loopback: bool = False,
        boot_chatter: bool = True,
    ):
        self.pcm = pcm
        self.battery = battery_volts
        self.loopback = loopback
        self.sci_settings = 0x01
        self.interval_ms = 100
        self.unexpected: list[tuple[int, int, int]] = []
        self.sci_messages = 0
        self._reader = sc.PacketReader()
        self._out = bytearray()
        self._millis = 0
        if boot_chatter:
            self._emit(sc.BUS_USB, sc.CMD_RESET, 0x01, b"\x00")
            self._emit(sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_SCI_BUS, bytes([self.sci_settings]))
            self._out += b"\x00\xff"  # line noise the reader must skip

    # -- Transport ------------------------------------------------------------

    def write(self, data: bytes) -> None:
        for packet in self._reader.feed(data):
            self._handle(packet)
        if self._reader.discarded:
            self._reader.discarded = 0
            self._emit(sc.BUS_USB, sc.CMD_ERROR, 0x05, b"\xFF")

    def read(self, timeout: float) -> bytes:
        if self._out:
            data = bytes(self._out)
            self._out.clear()
            return data
        time.sleep(min(timeout, 0.0005))
        return b""

    def close(self) -> None:
        pass

    # -- firmware ---------------------------------------------------------------

    def _emit(self, bus: int, command: int, sub: int, payload: bytes) -> None:
        self._millis += 7
        self._out += sc.encode_scanner_packet(bus, command, sub, payload, self._millis)

    def _battery_mv(self) -> int:
        if isinstance(self.battery, list):
            volts = self.battery.pop(0) if len(self.battery) > 1 else self.battery[0]
        else:
            volts = self.battery
        return 0 if volts < 6.0 else int(volts * 1000)

    def _status(self) -> bytes:
        status = bytearray(53)
        status[0:3] = bytes([0x1E, 0x98, 0x01])  # ATmega2560
        status[10:12] = (6000).to_bytes(2, "big")
        millivolts = self._battery_mv()
        status[12] = 1 if millivolts else 0
        status[13:15] = millivolts.to_bytes(2, "big")
        status[24] = self.sci_settings
        return bytes(status)

    def _handle(self, packet: sc.Packet) -> None:
        key = (packet.bus, packet.command, packet.sub)
        if packet.bus == sc.BUS_USB and packet.command == sc.CMD_HANDSHAKE:
            self._emit(sc.BUS_USB, sc.CMD_HANDSHAKE, 0x00, sc.HANDSHAKE_TEXT)
        elif packet.bus == sc.BUS_USB and packet.command == sc.CMD_STATUS:
            self._emit(sc.BUS_USB, sc.CMD_STATUS, 0x00, self._status())
        elif key == (sc.BUS_USB, sc.CMD_REQUEST, sc.REQ_HWFW):
            info = bytearray(32)
            info[26:30] = bytes([0x00, 0x09, 0x0C, 0x00])
            self._emit(sc.BUS_USB, sc.CMD_RESPONSE, sc.REQ_HWFW, bytes(info))
        elif key == (sc.BUS_USB, sc.CMD_REQUEST, sc.REQ_BATTERY):
            payload = self._battery_mv().to_bytes(2, "big")
            self._emit(sc.BUS_USB, sc.CMD_RESPONSE, sc.REQ_BATTERY, payload)
        elif key == (sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_SCI_BUS) and packet.payload:
            self.sci_settings = packet.payload[0]
            self._emit(sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_SCI_BUS, bytes([self.sci_settings]))
        elif key == (sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_REPEAT) and len(packet.payload) >= 3:
            self.interval_ms = int.from_bytes(packet.payload[1:3], "big")
            self._emit(sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_REPEAT, packet.payload[:3])
        elif key == (sc.BUS_PCM, sc.CMD_MSG_TX, sc.TX_STOP):
            self._emit(sc.BUS_USB, sc.CMD_MSG_TX, sc.TX_STOP, bytes([sc.BUS_PCM]))
        elif key == (sc.BUS_PCM, sc.CMD_MSG_TX, sc.TX_SINGLE) and packet.payload:
            self._transmit(packet.payload)
        elif key == (sc.BUS_PCM, sc.CMD_MSG_TX, sc.TX_LIST) and len(packet.payload) >= 3:
            count = packet.payload[0]
            if count < 2:
                return  # the real firmware sends nothing for a one-entry list
            position = 1
            for _ in range(count):
                length = packet.payload[position]
                message = packet.payload[position + 1 : position + 1 + length]
                position += 1 + length
                if not self._transmit(message):
                    break  # the list only advances when bytes come back
        else:
            self.unexpected.append(key)
            self._emit(sc.BUS_USB, sc.CMD_ERROR, 0x02, b"\xFF")

    def _line(self, message: bytes) -> bytes:
        """What comes back on the board's receive pin for a transmitted message."""
        if not self.sci_settings & sc.SCI_ENABLE:
            return b""
        if self.loopback:
            return bytes(message)
        if self.pcm is None:
            return b""
        return self.pcm.exchange(message)

    def _transmit(self, message: bytes) -> bool:
        self.sci_messages += 1
        high_speed = (self.sci_settings & 0x03) == sc.SCI_SPEED_HIGH
        if high_speed and len(message) > 1 and message[1] == 0xFF and message[0] in sci.RAM_TABLES:
            message = bytes([message[0]]) + bytes(range(sci.RAM_TABLE_SIZE))
        received = self._line(message)
        if not received:
            return False
        if high_speed and message[0] in sci.RAM_TABLES:
            payload = bytearray([message[0]])
            for offset, value in zip(message[1:], received[1:]):
                payload += bytes([offset, value])
            self._emit(sc.BUS_PCM, sc.CMD_MSG_RX, sc.RX_HIGH_SPEED, bytes(payload))
        else:
            sub = sc.RX_HIGH_SPEED if high_speed else sc.RX_LOW_SPEED
            self._emit(sc.BUS_PCM, sc.CMD_MSG_RX, sub, received)

        # The firmware follows the computer's speed changes on its own.
        if not high_speed and received[0] == sci.CMD_HIGH_SPEED:
            self._set_speed(sc.SCI_SPEED_HIGH)
        elif high_speed and received[0] == sci.CMD_LOW_SPEED:
            self._set_speed(sc.SCI_SPEED_LOW)
        return True

    def _set_speed(self, speed: int) -> None:
        self.sci_settings = (self.sci_settings & ~0x03) | speed | sc.SCI_ENABLE
        self._emit(sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_SCI_BUS, bytes([self.sci_settings]))


# -- ready-made scenarios -------------------------------------------------------

SCENARIOS = {
    "healthy": "Computer answers everything; memory is sound",
    "flaky": "One ROM bit reads differently from pass to pass",
    "erased": "One ROM block reads as all FF",
    "corrupt": "A few ROM bytes differ from the correct image",
    "no-rom24": "Computer does not support the 24-bit ROM read command",
    "key-off": "Board connected, ignition off",
    "dies": "Computer stops answering part-way through a dump",
    "lowbatt": "Battery below the safe limit",
    "bench": "Board on the bench with the loopback jumper fitted",
    "bench-open": "Board on the bench, no loopback jumper",
}


def make_mem_sim(name: str) -> SimMega:
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}; choose from {', '.join(SCENARIOS)}")
    if name == "bench":
        return SimMega(None, battery_volts=0.0, loopback=True)
    if name == "bench-open":
        return SimMega(None, battery_volts=0.0)

    pcm = SimPcm(bytearray(golden_rom()), bytearray(golden_eeprom()))
    options: dict = {}
    if name == "flaky":
        pcm.flaky[0x1234] = 0x08
    elif name == "erased":
        pcm.rom[0x2000:0x3000] = b"\xFF" * 0x1000
    elif name == "corrupt":
        pcm.rom[0x0100] ^= 0x01
        pcm.rom[0x0101] = 0x00
        pcm.rom[0x3ABC] ^= 0xF0
    elif name == "no-rom24":
        pcm.supports_rom24 = False
    elif name == "key-off":
        pcm.key_on = False
    elif name == "dies":
        pcm.die_after = 900
    elif name == "lowbatt":
        options["battery_volts"] = 11.2
    return SimMega(pcm, **options)

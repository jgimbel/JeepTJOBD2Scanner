"""Chrysler SCI commands for the engine computer, read-only.

Low-speed mode (7812.5 baud) is half duplex: the computer echoes every byte it
receives, then sends its answer. A one-byte memory read therefore comes back as
the request followed by one value byte:

    sent      26 01 23 45
    received  26 01 23 45 9C      -> ROM[0x012345] == 0x9C

Command list from the ChryslerScanner user's guide (section 7.1).

Only commands that read are allowed out of this module. Writing EEPROM (27),
erasing fault codes (17), resetting adaptive memory (23), actuator tests (13),
idle speed control (19) and the security commands are refused before anything
reaches the wire.
"""

from __future__ import annotations

from dataclasses import dataclass

CMD_FAULT_LIST = 0x10
CMD_HIGH_SPEED = 0x12
CMD_ROM_16 = 0x15
CMD_ROM_24 = 0x26
CMD_EEPROM = 0x28
CMD_LOW_SPEED = 0xFE

READ_ONLY_COMMANDS = {
    CMD_FAULT_LIST: "read fault code list",
    CMD_HIGH_SPEED: "select high-speed mode",
    CMD_ROM_16: "read ROM byte (16-bit address)",
    CMD_ROM_24: "read ROM byte (24-bit address)",
    CMD_EEPROM: "read EEPROM byte",
    CMD_LOW_SPEED: "select low-speed mode",
}
_LOW_SPEED_LENGTHS = {
    CMD_FAULT_LIST: 1,
    CMD_HIGH_SPEED: 1,
    CMD_ROM_16: 3,
    CMD_ROM_24: 4,
    CMD_EEPROM: 3,
    CMD_LOW_SPEED: 1,
}
# High-speed mode: F0-FF select a RAM table to read from (FE is "low speed").
RAM_TABLES = tuple(t for t in range(0xF0, 0x100) if t != CMD_LOW_SPEED)
RAM_TABLE_SIZE = 0xF0


class ForbiddenCommand(Exception):
    """A message that is not on the read-only list was about to be sent."""


def check_read_only(message: bytes, high_speed: bool = False) -> None:
    """Raise unless the message is a known read in the current speed mode.

    The same byte can mean different things in the two modes, so the RAM table
    selectors are accepted in high-speed mode only, and the ordinary commands
    in low-speed mode only.
    """
    if not message:
        raise ForbiddenCommand("empty message")
    command = message[0]
    if high_speed:
        if command == CMD_LOW_SPEED:
            allowed = len(message) == 1
        elif command in RAM_TABLES:
            # Offsets only (or FF for the whole table): a byte of F0 or above
            # would be taken as a new command.
            whole_table = len(message) == 2 and message[1] == 0xFF
            allowed = whole_table or (
                len(message) <= 1 + RAM_TABLE_SIZE
                and all(offset < RAM_TABLE_SIZE for offset in message[1:])
            )
        else:
            allowed = False
    else:
        # Exact lengths, so nothing can ride along behind an allowed command.
        allowed = _LOW_SPEED_LENGTHS.get(command) == len(message)
    if not allowed:
        mode = "high-speed" if high_speed else "low-speed"
        raise ForbiddenCommand(
            f"message {bytes(message).hex(' ').upper()} is not an allowed {mode} read "
            "and was not sent"
        )


@dataclass(frozen=True)
class Region:
    """A memory area that can be read one byte per request."""

    name: str
    title: str
    command: int
    address_bytes: int
    block_size: int
    batch: int  # requests per list packet (bounded by the board's buffer)

    @property
    def address_space(self) -> int:
        return 1 << (8 * self.address_bytes)

    def request(self, address: int) -> bytes:
        if not 0 <= address < self.address_space:
            raise ValueError(f"address {address:#x} is outside the {self.name} address space")
        return bytes([self.command]) + address.to_bytes(self.address_bytes, "big")

    def value_from(self, address: int, reply: bytes | None) -> int | None:
        """The byte at `address`, or None if the reply is not a clean answer."""
        request = self.request(address)
        if reply is None or len(reply) != len(request) + 1:
            return None
        if reply[: len(request)] != request:
            return None
        return reply[-1]


REGIONS = {
    "rom": Region("rom", "ROM (program and calibration)", CMD_ROM_24, 3, 4096, 48),
    "rom16": Region("rom16", "ROM, 16-bit address command", CMD_ROM_16, 2, 4096, 60),
    "eeprom": Region("eeprom", "EEPROM (VIN, mileage, learned values)", CMD_EEPROM, 2, 64, 60),
}


@dataclass
class FaultList:
    raw: bytes
    codes: list[int]
    checksum_ok: bool | None  # None when the reply was too short to carry one

    def format(self) -> str:
        if not self.raw:
            return "The engine computer did not answer the fault list request."
        lines = ["Reply: " + self.raw.hex(" ").upper()]
        if self.checksum_ok is None:
            lines.append("The reply is incomplete (no end marker and checksum).")
        else:
            lines.append("Checksum: " + ("ok" if self.checksum_ok else "WRONG"))
            if self.codes:
                lines.append(
                    "Fault identifiers (Chrysler internal numbers, not P-codes): "
                    + " ".join(f"{c:02X}" for c in self.codes)
                )
            else:
                lines.append("No faults stored according to the engine computer.")
        return "\n".join(lines)


def parse_fault_list(reply: bytes | None) -> FaultList:
    """Reply layout: 10 <code>... [FD] FE <checksum>, checksum = sum of the rest."""
    if not reply or reply[0] != CMD_FAULT_LIST:
        return FaultList(reply or b"", [], None)
    if len(reply) < 3 or reply[-2] != 0xFE:
        return FaultList(reply, [], None)
    body, check = reply[:-1], reply[-1]
    codes = [b for b in body[1:-1] if b != 0xFD]
    return FaultList(reply, codes, (sum(body) & 0xFF) == check)

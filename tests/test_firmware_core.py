"""Runs the real driver against the ESP32 firmware's core logic.

The firmware keeps all of its behaviour in bridge_core.h, which has no ESP32
dependencies. These tests compile that file for the host, run it as a
subprocess, and sit on both of its sides: the driver on the USB side, and a
byte-level simulated engine computer on the SCI side.

What this proves: the firmware's packet handling, command logic and read-only
guard. What it cannot prove: serial timing, the level shifter, or anything
else about the real board.
"""

import os
import select
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tjdiag import memcli, memory, sci
from tjdiag import scanner as sc
from tjdiag.scanner import Scanner, ScannerError, encode_packet
from tjdiag.scisim import SimPcm, golden_eeprom, golden_rom

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "esp32-sci-bridge"
MESSAGE_LENGTHS = {0x10: 1, 0x12: 1, 0x15: 3, 0x26: 4, 0x28: 3, 0xFE: 1}


@pytest.fixture(scope="session")
def bridge_binary(tmp_path_factory):
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("no C++ compiler available to build the firmware core")
    binary = tmp_path_factory.mktemp("firmware") / "bridge_host"
    subprocess.run(
        [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-O1", "-o", str(binary),
         str(FIRMWARE / "host" / "host_main.cpp")],
        check=True,
    )
    return binary


class WirePcm:
    """Plays the engine computer, byte by byte, on the firmware's SCI pins."""

    def __init__(self, pcm, read_fd, write_fd):
        self.pcm = pcm
        self.seen = bytearray()  # every byte the firmware put on the wire
        self.unexpected = bytearray()  # bytes that did not start a known read
        self._read_fd, self._write_fd = read_fd, write_fd
        self._message = bytearray()
        self._table = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                data = os.read(self._read_fd, 1)
            except OSError:
                return
            if not data:
                return
            self._byte(data[0])

    def _send(self, data):
        if data:
            os.write(self._write_fd, bytes(data))

    def _silent(self):
        pcm = self.pcm
        return pcm is None or not pcm.key_on or (
            pcm.die_after is not None and pcm.exchanges >= pcm.die_after
        )

    def _byte(self, value):
        self.seen.append(value)
        if self._silent():
            self._message.clear()
            return
        pcm = self.pcm
        if pcm.high_speed:
            if value >= 0xF0:
                self._send([value])  # table numbers and FE are echoed
                self._table = None if value == sci.CMD_LOW_SPEED else value
                if value == sci.CMD_LOW_SPEED:
                    pcm.high_speed = False
            elif self._table is not None:
                self._send(pcm._high_speed(bytes([self._table, value]))[1:])
            return
        self._send([value])  # low speed: every byte is echoed
        self._message.append(value)
        needed = MESSAGE_LENGTHS.get(self._message[0])
        if needed is None:
            self.unexpected.append(value)
            self._message.clear()
        elif len(self._message) == needed:
            message = bytes(self._message)
            self._message.clear()
            self._send(pcm.exchange(message)[len(message):])


class WireLoopback(WirePcm):
    """The bench jumper: whatever is transmitted comes straight back."""

    def _byte(self, value):
        self.seen.append(value)
        self._send([value])


class FirmwareBoard:
    """Transport backed by the host build of the firmware."""

    def __init__(self, binary, pcm, battery_mv=12600, wire=WirePcm):
        to_pcm_read, to_pcm_write = os.pipe()
        to_firmware_read, to_firmware_write = os.pipe()
        self.process = subprocess.Popen(
            [str(binary), str(to_firmware_read), str(to_pcm_write), str(battery_mv)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(to_firmware_read, to_pcm_write), bufsize=0,
        )
        os.close(to_firmware_read)
        os.close(to_pcm_write)
        self._fds = (to_pcm_read, to_firmware_write)
        self.wire = wire(pcm, to_pcm_read, to_firmware_write)
        self.pcm = pcm
        self.stderr = ""

    def write(self, data):
        self.process.stdin.write(data)

    def read(self, timeout):
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        return os.read(self.process.stdout.fileno(), 4096) if ready else b""

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=5)
        self.stderr = self.process.stderr.read().decode()
        self.process.stdout.close()
        self.process.stderr.close()
        for fd in self._fds:
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.fixture
def bench(bridge_binary):
    """Factory for (driver, board) pairs; everything is shut down afterwards."""
    boards = []

    def make(pcm="default", configure=True, **options):
        if pcm == "default":
            pcm = SimPcm(bytearray(golden_rom()), bytearray(golden_eeprom()))
        board = FirmwareBoard(bridge_binary, pcm, **options)
        boards.append(board)
        driver = Scanner(board, timeout=1.0)
        driver.open(boot_wait=0.05)
        if configure:
            driver.configure_sci()
        return driver, board

    yield make
    for board in boards:
        if board.process.poll() is None:
            board.close()


def raw_single(driver, message):
    """Send a message to the engine computer without the Python-side guard."""
    driver.transport.write(
        encode_packet(sc.BUS_PCM, sc.CMD_MSG_TX, sc.TX_SINGLE, bytes.fromhex(message))
    )


# -- identity and housekeeping ------------------------------------------------


def test_the_bridge_identifies_itself(bench):
    driver, _ = bench()
    assert driver.firmware == sc.BRIDGE
    assert driver.firmware_version() == "1.0.0"
    status = driver.status()
    assert status["mcu"] == "ESP32" and status["connected_to_vehicle"]
    assert status["battery_volts"] == 12.6 and driver.battery_volts() == 12.6


def test_no_battery_voltage_reads_as_zero(bench):
    driver, _ = bench(battery_mv=300)
    assert driver.battery_volts() == 0.0
    assert not driver.status()["connected_to_vehicle"]


def test_nothing_is_transmitted_until_the_link_is_enabled(bench):
    driver, board = bench(configure=False)
    assert driver.send_sci(bytes.fromhex("28 00 20"), timeout=0.3) is None
    assert board.wire.seen == b""


def test_garbage_before_a_packet_is_ignored(bench):
    driver, board = bench()
    board.write(b"\x00\xff\x12ets Jun  8 2016")  # like a board's start-up text
    assert driver.handshake()


def test_a_corrupted_packet_is_reported_and_the_next_one_works(bench):
    driver, board = bench()
    packet = bytearray(encode_packet(sc.BUS_USB, sc.CMD_HANDSHAKE, 0x00))
    packet[-1] ^= 0x01
    board.write(bytes(packet))
    with pytest.raises(ScannerError, match="checksum"):
        driver._wait(sc.BUS_USB, sc.CMD_HANDSHAKE, None, 0.5)
    assert driver.handshake()


def test_an_unfinished_packet_does_not_block_later_ones(bench):
    driver, board = bench()
    board.write(encode_packet(sc.BUS_USB, sc.CMD_STATUS, 0x00)[:4])
    time.sleep(0.35)
    with pytest.raises(ScannerError, match="timed out"):
        driver._wait(sc.BUS_USB, sc.CMD_STATUS, None, 0.5)
    assert driver.handshake()


@pytest.mark.parametrize(
    "bus, command, sub, payload, error",
    [
        (sc.BUS_USB, 0x0E, 0x0B, b"", "invalid command"),  # upstream's bootstrap command
        (sc.BUS_USB, 0x0E, 0xF0, b"\x00", "invalid command"),  # upstream's EEPROM restore
        (sc.BUS_TCM, sc.CMD_MSG_TX, sc.TX_SINGLE, b"\x10", "invalid bus"),
        (sc.BUS_CCD, sc.CMD_MSG_TX, sc.TX_SINGLE, b"\x10", "invalid bus"),
        (sc.BUS_PCM, sc.CMD_MSG_TX, 0x04, b"\x10", "invalid sub-command"),  # repeat forever
        (sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_SCI_BUS, b"\xA1", "invalid payload"),  # TCM channel
        (sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_SCI_BUS, b"\x89", "invalid payload"),  # inverted logic
        (sc.BUS_USB, sc.CMD_SETTINGS, sc.SET_SCI_BUS, b"\x83", "invalid payload"),  # 125000 baud
    ],
)
def test_features_the_bridge_does_not_have_are_refused(bench, bus, command, sub, payload, error):
    driver, board = bench()
    board.write(encode_packet(bus, command, sub, payload))
    with pytest.raises(ScannerError, match=error):
        driver._wait(sc.BUS_PCM, sc.CMD_MSG_RX, None, 0.5)
    assert board.wire.seen == b""


# -- reading --------------------------------------------------------------------


def test_single_read_returns_echo_then_value(bench):
    driver, board = bench()
    reply = driver.send_sci(bytes.fromhex("28 00 20"))
    assert reply == bytes.fromhex("28 00 20") + bytes([board.pcm.eeprom[0x20]])
    assert driver.send_sci(bytes.fromhex("26 00 12 34"))[-1] == board.pcm.rom[0x1234]
    assert driver.send_sci(bytes.fromhex("15 00 40"))[-1] == board.pcm.rom[0x40]


def test_an_address_with_no_value_comes_back_as_echo_only(bench):
    driver, _ = bench()
    assert driver.send_sci(bytes.fromhex("28 7F 00")) == bytes.fromhex("28 7F 00")


def test_fault_list_is_passed_through_whole(bench):
    driver, board = bench()
    board.pcm.faults = [0x01, 0x2A, 0x33]
    faults = sci.parse_fault_list(driver.send_sci(bytes([sci.CMD_FAULT_LIST])))
    assert faults.codes == [0x01, 0x2A, 0x33] and faults.checksum_ok


def test_list_replies_arrive_in_order(bench):
    driver, board = bench()
    driver.set_interval(0)
    replies = driver.send_sci_list([bytes([0x28, 0x00, a]) for a in range(40)])
    assert [r[-1] for r in replies] == list(board.pcm.eeprom[:40])


def test_list_is_paced_by_the_interval(bench):
    driver, _ = bench()
    driver.set_interval(80)
    started = time.monotonic()
    assert len(driver.send_sci_list([bytes([0x28, 0x00, a]) for a in range(4)])) == 4
    assert time.monotonic() - started >= 0.22  # three gaps of 80 ms


def test_key_off_gives_no_answer(bench):
    driver, board = bench()
    board.pcm.key_on = False
    assert driver.send_sci(bytes.fromhex("28 00 20"), timeout=0.4) is None


def test_list_stops_when_the_computer_goes_silent_and_the_board_recovers(bench):
    driver, board = bench()
    driver.set_interval(0)
    board.pcm.die_after = board.pcm.exchanges + 3
    replies = driver.send_sci_list([bytes([0x28, 0x00, a]) for a in range(8)], timeout_each=0.4)
    assert len(replies) == 3
    board.pcm.die_after = None
    assert driver.send_sci(bytes.fromhex("28 00 00")) is not None


def test_high_speed_ram_table_and_back(bench):
    driver, board = bench()
    assert driver.send_sci(bytes([sci.CMD_HIGH_SPEED])) == bytes([sci.CMD_HIGH_SPEED])
    assert driver.high_speed
    data = driver.send_sci(bytes([0xF4, 0xFF]), timeout=3.0)
    assert data[0] == 0xF4 and len(data) == 1 + 2 * sci.RAM_TABLE_SIZE
    offsets, values = data[1::2], data[2::2]
    assert list(offsets) == list(range(sci.RAM_TABLE_SIZE))
    expected = board.pcm._high_speed(bytes([0xF4]) + bytes(range(sci.RAM_TABLE_SIZE)))[1:]
    assert bytes(values) == expected
    assert driver.send_sci(bytes.fromhex("F5 00 10"))[1::2] == bytes([0x00, 0x10])

    assert driver.send_sci(bytes([sci.CMD_LOW_SPEED])) == bytes([sci.CMD_LOW_SPEED])
    assert not driver.high_speed
    assert driver.send_sci(bytes.fromhex("28 00 20")) is not None
    board.close()
    assert [line for line in board.stderr.split("\n") if line] == [
        "BAUD 7812",  # start-up
        "BAUD 7812",  # link enabled
        "BAUD 62500",  # followed the computer to high speed
        "BAUD 7812",  # and back
    ]


# -- the firmware's own read-only guard --------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "27 00 10 55",  # write EEPROM
        "17",  # erase fault codes
        "23 01",  # reset adaptive memory
        "13 07",  # actuator test
        "19 40",  # idle speed control
        "2B",  # security seed
        "2C 12 34",  # security key
        "20",  # bootstrap flash read command
        "28 00 10 27",  # a write riding behind an allowed read
        "10 17",  # an erase riding behind the fault list request
        "26 00 00",  # allowed command, wrong length
        "F4 00",  # RAM table selector outside high-speed mode
    ],
)
def test_firmware_refuses_anything_but_reads_even_without_the_python_guard(bench, message):
    driver, board = bench()
    raw_single(driver, message)
    with pytest.raises(ScannerError, match="invalid payload"):
        driver._wait(sc.BUS_PCM, sc.CMD_MSG_RX, None, 0.5)
    assert board.wire.seen == b""


def test_firmware_checks_every_message_of_a_list_before_sending_any(bench):
    driver, board = bench()
    payload = bytes.fromhex("03  03 28 00 00  03 28 00 01  04 27 00 10 55")
    board.write(encode_packet(sc.BUS_PCM, sc.CMD_MSG_TX, sc.TX_LIST, payload))
    with pytest.raises(ScannerError, match="invalid payload"):
        driver._wait(sc.BUS_PCM, sc.CMD_MSG_RX, None, 0.5)
    assert board.wire.seen == b""


@pytest.mark.parametrize("message", ["26 00 00 00", "F4 10 FE", "FE 27", "F4 F0", "27 00 10 55"])
def test_firmware_guard_in_high_speed_mode(bench, message):
    driver, board = bench()
    driver.send_sci(bytes([sci.CMD_HIGH_SPEED]))
    seen_before = bytes(board.wire.seen)
    raw_single(driver, message)
    with pytest.raises(ScannerError, match="invalid payload"):
        driver._wait(sc.BUS_PCM, sc.CMD_MSG_RX, None, 0.5)
    assert bytes(board.wire.seen) == seen_before


def test_only_read_commands_ever_reach_the_wire_in_normal_use(bench):
    driver, board = bench()
    link = memory.MemoryLink(driver, interval_ms=0)
    link.start()
    link.alive()
    driver.send_sci(bytes([sci.CMD_FAULT_LIST]))
    link.read_all(sci.REGIONS["rom"], list(range(20)))
    assert board.wire.unexpected == b""
    assert board.pcm.forbidden_seen == []


# -- the memory scripts on top of the firmware -----------------------------------------


def test_rom_and_eeprom_dumps_match_the_memory(bench, tmp_path):
    driver, _ = bench()
    link = memory.MemoryLink(driver, interval_ms=0)
    link.start()
    rom_path, eeprom_path = tmp_path / "rom-pass1.bin", tmp_path / "eeprom-pass1.bin"
    assert memory.dump_region(link, sci.REGIONS["rom"], 0x1000, 0x300, rom_path).missing_count == 0
    assert rom_path.read_bytes() == golden_rom()[0x1000:0x1300]
    assert memory.dump_region(link, sci.REGIONS["eeprom"], 0, 0x200, eeprom_path).complete
    assert eeprom_path.read_bytes() == golden_eeprom()


def test_dump_resumes_after_the_key_is_turned_off(bench, tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "REPLY_TIMEOUT", 0.4)
    driver, board = bench()
    link = memory.MemoryLink(driver, interval_ms=0)
    link.start()
    path = tmp_path / "rom-pass1.bin"
    board.pcm.die_after = board.pcm.exchanges + 100
    with pytest.raises(memory.ComputerSilent):
        memory.dump_region(link, sci.REGIONS["rom"], 0, 0x180, path)
    assert memory.load_state(path).done == 100
    board.pcm.die_after = None
    assert memory.dump_region(link, sci.REGIONS["rom"], 0, 0x180, path).missing_count == 0
    assert path.read_bytes() == golden_rom()[:0x180]


def test_selftest_passes_with_the_loopback_jumper(bench, capsys):
    driver, _ = bench(pcm=None, configure=False, battery_mv=0, wire=WireLoopback)
    assert memcli.cmd_selftest(driver) == 0
    out = capsys.readouterr().out
    assert "tjdiag SCI bridge" in out and "[PASS] Microcontroller: ESP32" in out
    assert "[PASS] Firmware version: 1.0.0" in out and "Board is ready." in out


def test_selftest_fails_without_the_jumper(bench, capsys):
    driver, _ = bench(pcm=None, configure=False, battery_mv=0)
    assert memcli.cmd_selftest(driver) == 1
    assert "jumper" in capsys.readouterr().out


# -- the ESP32 sketch itself ---------------------------------------------------------


def test_sketch_type_checks_against_the_arduino_api_stand_in():
    """Not a real ESP32 compile: catches syntax and type errors in the wrapper."""
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("no C++ compiler available")
    stub = Path(__file__).resolve().parent / "arduino_stub"
    result = subprocess.run(
        [compiler, "-std=c++11", "-Wall", "-Wextra", "-Werror", "-fsyntax-only", "-x", "c++",
         "-I", str(stub), "-I", str(FIRMWARE), str(FIRMWARE / "esp32-sci-bridge.ino")],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_firmware_and_python_guards_allow_the_same_low_speed_commands():
    """The two read-only lists are kept in step by hand; this catches drift."""
    source = (FIRMWARE / "bridge_core.h").read_text()
    for command, length in sci._LOW_SPEED_LENGTHS.items():
        assert f"= 0x{command:02X}" in source, f"command {command:02X} missing from the firmware"
    assert set(sci._LOW_SPEED_LENGTHS) == set(MESSAGE_LENGTHS)

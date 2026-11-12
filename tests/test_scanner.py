import pytest

from tjdiag import scanner as sc
from tjdiag import sci
from tjdiag.scanner import PacketReader, Scanner, ScannerError, encode_packet, encode_scanner_packet
from tjdiag.scisim import SimMega, make_mem_sim


def opened(name="healthy"):
    sim = make_mem_sim(name)
    board = Scanner(sim, timeout=0.5)
    board.open(boot_wait=0.01)
    return board, sim


# -- framing: values worked out by hand from the firmware's rules -------------


def test_handshake_request_bytes():
    # 3D, length 0002, datacode 01, sub 00; checksum is the sum including 3D.
    assert encode_packet(sc.BUS_USB, sc.CMD_HANDSHAKE, 0x00) == bytes.fromhex("3D 00 02 01 00 40")


def test_engine_computer_message_bytes():
    # Bus 2 (engine computer), command 6 (transmit) -> datacode 26; sub 02 = single.
    packet = encode_packet(sc.BUS_PCM, sc.CMD_MSG_TX, sc.TX_SINGLE, bytes.fromhex("28 00 10"))
    assert packet == bytes.fromhex("3D 00 05 26 02 28 00 10 A2")


def test_scanner_packets_set_the_top_bit_and_carry_a_timestamp():
    raw = encode_scanner_packet(sc.BUS_PCM, sc.CMD_MSG_RX, sc.RX_LOW_SPEED, b"\x28\x00\x10\x5A", 0x01020304)
    assert raw[3] == 0xA7  # 0x80 | bus 2 << 4 | command 7
    assert raw[1:3] == bytes([0x00, 0x0A])  # datacode + sub + 4 timestamp + 4 payload
    (packet,) = PacketReader().feed(raw)
    assert packet.from_scanner and packet.bus == sc.BUS_PCM and packet.command == sc.CMD_MSG_RX
    assert packet.payload == b"\x28\x00\x10\x5A" and packet.timestamp_ms == 0x01020304


def test_laptop_packets_have_no_timestamp_to_strip():
    raw = encode_packet(sc.BUS_PCM, sc.CMD_MSG_TX, sc.TX_SINGLE, bytes.fromhex("26 00 00 00"))
    (packet,) = PacketReader().feed(raw)
    assert not packet.from_scanner and packet.payload == bytes.fromhex("26 00 00 00")


def test_reader_resynchronises_after_garbage_and_a_bad_checksum():
    good = encode_scanner_packet(sc.BUS_USB, sc.CMD_HANDSHAKE, 0, sc.HANDSHAKE_TEXT)
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    reader = PacketReader()
    packets = reader.feed(b"\x00\x7f" + bytes(bad) + good)
    assert [p.payload for p in packets] == [sc.HANDSHAKE_TEXT]
    assert reader.discarded > 0


def test_reader_handles_packets_split_across_reads():
    raw = encode_scanner_packet(sc.BUS_USB, sc.CMD_STATUS, 0, bytes(53))
    reader = PacketReader()
    assert reader.feed(raw[:4]) == []
    assert reader.feed(raw[4:20]) == []
    assert len(reader.feed(raw[20:])) == 1


# -- board ------------------------------------------------------------------------


def test_open_gets_past_startup_chatter():
    board, _ = opened()
    assert board.handshake()


def test_open_fails_clearly_when_nothing_answers():
    class Dead:
        def write(self, data):
            pass

        def read(self, timeout):
            return b""

        def close(self):
            pass

    with pytest.raises(ScannerError, match="handshake"):
        Scanner(Dead(), timeout=0.02).open(boot_wait=0.01, attempts=2)


def test_status_battery_and_firmware_version():
    board, _ = opened()
    status = board.status()
    assert status["is_mega2560"] and status["connected_to_vehicle"]
    assert status["battery_volts"] == 12.6
    assert board.battery_volts() == 12.6
    assert board.firmware_version() == "0.9.12"


def test_no_battery_sense_reads_as_zero():
    board, _ = opened("bench")
    assert board.battery_volts() == 0.0
    assert not board.status()["connected_to_vehicle"]


def test_board_error_packets_are_raised():
    class Erroring(SimMega):
        def _handle(self, packet):
            self._emit(sc.BUS_USB, sc.CMD_ERROR, 0x05, b"\xFF")

    board = Scanner(Erroring(None, boot_chatter=False), timeout=0.2)
    with pytest.raises(ScannerError, match="checksum"):
        board.status()


# -- what may be sent -----------------------------------------------------------


def test_only_listed_packet_types_can_be_sent():
    board, sim = opened()
    with pytest.raises(ScannerError, match="refusing"):
        board._send(sc.BUS_USB, 0x0E, 0x0B)  # firmware debug: bootstrap mode
    with pytest.raises(ScannerError, match="refusing"):
        board._send(sc.BUS_TCM, sc.CMD_MSG_TX, sc.TX_SINGLE, b"\x10")
    assert sim.unexpected == []


@pytest.mark.parametrize(
    "message",
    [
        "27 00 10 55",  # write EEPROM
        "17",  # erase fault codes
        "23 01",  # reset adaptive memory / erase EEPROM
        "13 07",  # actuator test
        "19 40",  # idle speed control
        "2B",  # security seed
        "2C 12 34",  # security key
        "F4 00",  # RAM table selector, but not in high-speed mode
    ],
)
def test_write_and_control_commands_never_reach_the_wire(message):
    board, sim = opened()
    board.configure_sci()
    with pytest.raises(sci.ForbiddenCommand):
        board.send_sci(bytes.fromhex(message))
    with pytest.raises(sci.ForbiddenCommand):
        board.send_sci_list([bytes.fromhex("28 00 00"), bytes.fromhex(message)])
    assert sim.sci_messages == 0
    assert sim.pcm.forbidden_seen == []


def test_ordinary_commands_are_refused_in_high_speed_mode():
    board, sim = opened()
    board.configure_sci()
    assert board.send_sci(bytes([sci.CMD_HIGH_SPEED])) == bytes([sci.CMD_HIGH_SPEED])
    assert board.high_speed
    with pytest.raises(sci.ForbiddenCommand):
        board.send_sci(bytes.fromhex("26 00 00 00"))
    assert board.send_sci(bytes([sci.CMD_LOW_SPEED])) == bytes([sci.CMD_LOW_SPEED])
    assert not board.high_speed
    assert board.send_sci(bytes.fromhex("28 00 20")) is not None


# -- messages -------------------------------------------------------------------


def test_single_message_returns_echo_then_value():
    board, sim = opened()
    board.configure_sci()
    reply = board.send_sci(bytes.fromhex("28 00 20"))
    assert reply == bytes.fromhex("28 00 20") + bytes([sim.pcm.eeprom[0x20]])


def test_single_message_with_no_answer_is_none():
    board, _ = opened("key-off")
    board.configure_sci()
    assert board.send_sci(bytes.fromhex("28 00 20"), timeout=0.05) is None


def test_nothing_is_sent_while_the_sci_link_is_disabled():
    board, _ = opened()
    assert board.send_sci(bytes.fromhex("28 00 20"), timeout=0.05) is None


def test_list_returns_replies_in_order():
    board, sim = opened()
    board.configure_sci()
    messages = [bytes([0x28, 0x00, a]) for a in range(5)]
    replies = board.send_sci_list(messages)
    assert [r[-1] for r in replies] == list(sim.pcm.eeprom[:5])


def test_one_entry_list_is_sent_as_a_single_message():
    # The firmware sends nothing for a list with one entry.
    board, sim = opened()
    board.configure_sci()
    assert len(board.send_sci_list([bytes.fromhex("28 00 00")])) == 1


def test_list_comes_back_short_when_the_computer_goes_silent():
    board, sim = opened()
    board.configure_sci()
    sim.pcm.die_after = 3
    replies = board.send_sci_list([bytes([0x28, 0x00, a]) for a in range(6)], timeout_each=0.05)
    assert len(replies) == 3
    sim.pcm.die_after = None
    assert board.send_sci(bytes.fromhex("28 00 00")) is not None  # board still usable


def test_oversized_list_is_refused_before_sending():
    board, sim = opened()
    board.configure_sci()
    with pytest.raises(ScannerError, match="too long"):
        board.send_sci_list([bytes.fromhex("26 00 00 00")] * 60)
    assert sim.sci_messages == 0

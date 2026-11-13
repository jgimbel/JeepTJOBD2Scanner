import pytest

from tjdiag import memory, sci
from tjdiag.scanner import Scanner
from tjdiag.scisim import SIM_EEPROM_SIZE, SIM_ROM_SIZE, SimMega, SimPcm, golden_eeprom, golden_rom, make_mem_sim

ROM = sci.REGIONS["rom"]
ROM16 = sci.REGIONS["rom16"]
EEPROM = sci.REGIONS["eeprom"]


def link_for(sim):
    board = Scanner(sim, timeout=0.5)
    board.open(boot_wait=0.01)
    link = memory.MemoryLink(board)
    link.start()
    return link


@pytest.fixture(autouse=True)
def fast_timeouts(monkeypatch):
    monkeypatch.setattr(memory, "REPLY_TIMEOUT", 0.05)


# -- sci helpers ------------------------------------------------------------------


def test_region_requests():
    assert ROM.request(0x012345) == bytes.fromhex("26 01 23 45")
    assert ROM16.request(0x8000) == bytes.fromhex("15 80 00")
    assert EEPROM.request(0x01FF) == bytes.fromhex("28 01 FF")
    with pytest.raises(ValueError):
        EEPROM.request(0x10000)


@pytest.mark.parametrize(
    "reply, expected",
    [
        ("26 01 23 45 9C", 0x9C),
        ("26 01 23 45", None),  # echo only, no value
        ("26 01 23 46 9C", None),  # echo of a different address
        ("26 01 23 45 9C 00", None),  # too long
        ("", None),
    ],
)
def test_a_value_is_only_accepted_with_a_matching_echo(reply, expected):
    assert ROM.value_from(0x012345, bytes.fromhex(reply)) == expected
    assert ROM.value_from(0x012345, None) is None


@pytest.mark.parametrize(
    "reply, codes, ok",
    [
        ("10 FE 0E", [], True),  # documented "no faults" reply
        ("10 FD FE 0B", [], True),  # documented alternative
        ("10 01 2A FE 39", [0x01, 0x2A], True),
        ("10 01 2A FE 00", [0x01, 0x2A], False),
        ("10 01", [], None),  # cut short
        ("", [], None),
    ],
)
def test_fault_list_parsing(reply, codes, ok):
    faults = sci.parse_fault_list(bytes.fromhex(reply))
    assert (faults.codes, faults.checksum_ok) == (codes, ok)


# -- probing ----------------------------------------------------------------------


def test_link_alive_only_with_the_key_on():
    assert link_for(make_mem_sim("healthy")).alive()
    assert not link_for(make_mem_sim("key-off")).alive()


def test_probe_finds_the_real_size_and_flags_mirrors():
    link = link_for(make_mem_sim("healthy"))
    result = memory.probe_region(link, ROM)
    assert result["supported"] and result["suggested_size"] == SIM_ROM_SIZE
    assert any(band["mirror"] for band in result["bands"])
    assert not any(band["mirror"] for band in result["bands"] if band["end"] <= SIM_ROM_SIZE)
    assert result["bytes_per_second"] > 0
    assert "repeats lower memory" in memory.format_probe(result)


def test_probe_finds_where_the_eeprom_stops_answering():
    result = memory.probe_region(link_for(make_mem_sim("healthy")), EEPROM)
    assert result["suggested_size"] == SIM_EEPROM_SIZE


def test_probe_reports_an_unsupported_command():
    link = link_for(make_mem_sim("no-rom24"))
    assert not memory.probe_region(link, ROM)["supported"]
    assert memory.probe_region(link, ROM16)["suggested_size"] == SIM_ROM_SIZE
    assert "not usable" in memory.format_probe(memory.probe_region(link, ROM))


def test_probe_does_not_suggest_trailing_empty_space():
    rom = bytearray(golden_rom())
    rom[0x2000:] = b"\xFF" * 0x2000
    sim = SimMega(SimPcm(rom, bytearray(golden_eeprom()), rom_beyond="silent"))
    assert memory.probe_region(link_for(sim), ROM)["suggested_size"] == 0x2000


# -- dumping ----------------------------------------------------------------------


def test_dump_reproduces_the_memory_exactly(tmp_path):
    link = link_for(make_mem_sim("healthy"))
    path = tmp_path / "rom-pass1.bin"
    state = memory.dump_region(link, ROM, 0, SIM_ROM_SIZE, path)
    assert state.complete and state.missing_count == 0
    assert path.read_bytes() == golden_rom()
    assert state.lowest_battery == 12.6


def test_dump_of_a_sub_range(tmp_path):
    link = link_for(make_mem_sim("healthy"))
    path = tmp_path / "part.bin"
    memory.dump_region(link, ROM, 0x1000, 0x123, path)
    assert path.read_bytes() == golden_rom()[0x1000:0x1123]


def test_dump_stops_and_resumes_when_the_computer_goes_silent(tmp_path):
    sim = make_mem_sim("healthy")
    link = link_for(sim)
    sim.pcm.die_after = sim.pcm.exchanges + 500
    path = tmp_path / "rom-pass1.bin"
    with pytest.raises(memory.ComputerSilent, match="key is still ON"):
        memory.dump_region(link, ROM, 0, SIM_ROM_SIZE, path)
    stopped = memory.load_state(path)
    assert not stopped.complete and stopped.done == 500
    assert stopped.missing_count == 0  # the silent stretch is not recorded as holes
    assert path.read_bytes()[:500] == golden_rom()[:500]

    sim.pcm.die_after = None
    before = sim.sci_messages
    state = memory.dump_region(link, ROM, 0, SIM_ROM_SIZE, path)
    # The addresses that went unanswered were read again, leaving no holes.
    assert state.complete and state.missing_count == 0
    assert sim.sci_messages - before < SIM_ROM_SIZE  # it did not start over
    assert path.read_bytes() == golden_rom()


def test_dump_refuses_to_start_on_a_low_battery(tmp_path):
    sim = make_mem_sim("lowbatt")
    link = link_for(sim)
    before = sim.sci_messages
    with pytest.raises(memory.LowBattery, match="11.2 V"):
        memory.dump_region(link, ROM, 0, 256, tmp_path / "a.bin")
    assert sim.sci_messages == before
    # With the guard switched off it proceeds.
    assert memory.dump_region(link, ROM, 0, 256, tmp_path / "b.bin", min_battery=None).complete


def test_dump_pauses_when_the_battery_sags_and_resumes_later(tmp_path):
    sim = make_mem_sim("healthy")
    sim.battery = [12.6, 12.4, 11.0]
    link = link_for(sim)
    path = tmp_path / "a.bin"
    with pytest.raises(memory.LowBattery):
        memory.dump_region(link, ROM, 0, 1024, path, battery_every=1)
    assert 0 < memory.load_state(path).done < 1024
    sim.battery = 13.2
    state = memory.dump_region(link, ROM, 0, 1024, path, battery_every=1)
    assert state.complete and state.lowest_battery == 11.0
    assert path.read_bytes() == golden_rom()[:1024]


def test_battery_guard_is_skipped_when_no_voltage_is_sensed(tmp_path):
    sim = SimMega(SimPcm(bytearray(golden_rom()), bytearray(golden_eeprom())), battery_volts=0.0)
    assert memory.dump_region(link_for(sim), ROM, 0, 128, tmp_path / "a.bin").complete


def test_resuming_with_different_parameters_is_refused(tmp_path):
    link = link_for(make_mem_sim("healthy"))
    path = tmp_path / "a.bin"
    memory.dump_region(link, ROM, 0, 128, path)
    with pytest.raises(ValueError, match="different dump"):
        memory.dump_region(link, ROM, 0, 256, path)


class GlitchyPcm(SimPcm):
    """Corrupts the echo of one address the first time, and never answers another."""

    glitch_once = 0x0155
    always_silent = 0x0200
    glitched = False

    def exchange(self, message):
        if message[0] == sci.CMD_ROM_24:
            address = int.from_bytes(message[1:], "big")
            if address == self.always_silent:
                return b""
            if address == self.glitch_once and not self.glitched:
                self.glitched = True
                reply = bytearray(super().exchange(message))
                reply[2] ^= 0x40
                return bytes(reply)
        return super().exchange(message)


def test_a_bad_echo_is_re_requested_and_a_dead_address_is_recorded(tmp_path):
    sim = SimMega(GlitchyPcm(bytearray(golden_rom()), bytearray(golden_eeprom())))
    link = link_for(sim)
    path = tmp_path / "a.bin"
    state = memory.dump_region(link, ROM, 0, 0x400, path)
    image = path.read_bytes()
    assert state.complete
    assert image[0x0155] == golden_rom()[0x0155]  # recovered by the retry
    assert state.missing == [[0x0200, 0x0201]]  # recorded, not guessed
    assert image[0x0201:0x0400] == golden_rom()[0x0201:0x0400]  # and it carried on


def test_existing_passes_are_listed_in_order(tmp_path):
    link = link_for(make_mem_sim("healthy"))
    for number in (1, 2):
        memory.dump_region(link, EEPROM, 0, 64, memory.pass_path(tmp_path, "eeprom", number))
    passes = memory.existing_passes(tmp_path, "eeprom")
    assert [p.name for p, _ in passes] == ["eeprom-pass1.bin", "eeprom-pass2.bin"]
    assert all(state.complete for _, state in passes)


def test_dump_past_the_end_of_a_memory_stops_with_advice(tmp_path):
    link = link_for(make_mem_sim("healthy"))
    path = tmp_path / "eeprom-pass1.bin"
    with pytest.raises(memory.RangeUnreadable, match="smaller --size"):
        memory.dump_region(link, EEPROM, 0, SIM_EEPROM_SIZE + 0x100, path)
    state = memory.load_state(path)
    assert state.done == SIM_EEPROM_SIZE and state.missing_count == 0
    assert path.read_bytes()[:SIM_EEPROM_SIZE] == golden_eeprom()

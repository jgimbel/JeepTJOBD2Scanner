"""Byte transports between the tool and an ELM327-type adapter.

A transport only has to move bytes. The serial transport covers USB adapters and
Bluetooth dongles that the operating system exposes as a serial port. The
simulator provides its own transport (see sim.py).
"""

from __future__ import annotations

import time
from typing import Protocol, TextIO


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, timeout: float) -> bytes:
        """Return whatever bytes are available, waiting up to `timeout` seconds.

        Returns b"" when nothing arrived in time.
        """
        ...

    def close(self) -> None: ...


class SerialTransport:
    """Serial port transport (USB adapter, or a paired Bluetooth dongle's port)."""

    def __init__(self, port: str, baud: int = 38400):
        try:
            import serial  # pyserial, imported lazily so the simulator needs no install
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "pyserial is not installed. Run: pip install pyserial"
            ) from exc
        self._serial = serial.Serial(port, baud, timeout=0.05)

    def write(self, data: bytes) -> None:
        self._serial.write(data)
        self._serial.flush()

    def read(self, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            waiting = self._serial.in_waiting
            chunk = self._serial.read(waiting or 1)
            if chunk:
                return chunk
            if time.monotonic() >= deadline:
                return b""

    def close(self) -> None:
        self._serial.close()


class LoggingTransport:
    """Wraps a transport and records every byte in both directions.

    The log is the ground truth when something parses unexpectedly on the real
    vehicle: it shows exactly what the adapter said.
    """

    def __init__(self, inner: Transport, log: TextIO, binary: bool = False):
        self._inner = inner
        self._log = log
        self._binary = binary
        self._start = time.monotonic()

    def _record(self, direction: str, data: bytes) -> None:
        elapsed = time.monotonic() - self._start
        if self._binary:
            text = data.hex(" ").upper()
        else:
            text = repr(data.decode("ascii", errors="replace"))
        self._log.write(f"{elapsed:9.3f} {direction} {text}\n")
        self._log.flush()

    def write(self, data: bytes) -> None:
        self._record(">>", data)
        self._inner.write(data)

    def read(self, timeout: float) -> bytes:
        data = self._inner.read(timeout)
        if data:
            self._record("<<", data)
        return data

    def close(self) -> None:
        self._inner.close()


def list_serial_ports() -> list[tuple[str, str]]:
    """Return (device, description) for every serial port the OS knows about."""
    try:
        from serial.tools import list_ports
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError("pyserial is not installed. Run: pip install pyserial") from exc
    return [(p.device, p.description) for p in list_ports.comports()]

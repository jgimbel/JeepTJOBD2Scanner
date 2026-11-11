import pytest

from tjdiag import obd
from tjdiag.elm import Elm327
from tjdiag.sim import make_sim


@pytest.fixture
def connect():
    """Return a function that builds a simulator and a connected driver for it."""

    def _connect(spec: str = "pcm", **adapter_options):
        sim = make_sim(spec)
        for name, value in adapter_options.items():
            setattr(sim, name, value)
        elm = Elm327(sim, timeout=1.0)
        protocol = obd.connect(elm)
        return elm, sim, protocol

    return _connect

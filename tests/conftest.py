"""Shared fixtures: the fake printer (rasterezpl.fakeprinter) on a free port, stopped after the test."""

from __future__ import annotations

import pytest

from rasterezpl.fakeprinter import FakePrinter, Profile


@pytest.fixture
def fake_printer():
    """``fake_printer(profile=None, **knobs) -> FakePrinter`` started on 127.0.0.1 at a free port; every fake made
    in the test is stopped at its end. ``fp.target`` is the ``tcp://`` string for ``--to`` or a registry record."""
    started: list[FakePrinter] = []

    def make(profile: Profile | None = None, **knobs) -> FakePrinter:
        fp = FakePrinter(profile or Profile(**knobs)).start()
        started.append(fp)
        return fp

    yield make
    for fp in started:
        fp.stop()

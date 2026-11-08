"""The Windows port (0.20.0), on every OS: the ``win:<queue>`` transport against a fake ``win32print`` (pywin32's
module, recorded call by call), the queue listing, the registry under %APPDATA%, the OS font folders, the USB
caveat, the pinned layout engine, and ``python -m rasterezpl``. No printer, no spooler."""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

import rasterezpl as rz
from rasterezpl import registry, text, transport
from rasterezpl.cli import main
from rasterezpl.compose import fonts_available
from rasterezpl.jobs import send_logged
from rasterezpl.media import offset_commands
from rasterezpl.registry import load
from rasterezpl.transport import TCP_CHUNK, SendError, deliver, status

PIL = pytest.importorskip("PIL")

REG = """
media:
  tiny:
    dpi: 100
    width_mm: 20
    length_mm: 10
    gap_mm: 2
    areas_in: [[0, 0, 0.16, 0.08], [0.4, 0.1, 0.16, 0.08]]
printers:
  winp:
    transport: "win:Panduit TDP43ME"
    media: tiny
    offset_in: [0.1, -0.02]
"""


class FakeWin32Print:
    """pywin32's win32print as the spooler shows it: one queue, RAW documents, chunked writes recorded."""

    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4

    class error(Exception):  # pywintypes.error
        pass

    def __init__(self, fail_after: int | None = None, open_fails: bool = False):
        self.calls: list = []
        self.writes: list[bytes] = []
        self.fail_after = fail_after
        self.open_fails = open_fails

    def GetDefaultPrinter(self):
        return "Panduit TDP43ME"

    def EnumPrinters(self, flags, name, level):
        self.calls.append(("EnumPrinters", flags, name, level))
        return [
            {
                "pPrinterName": "Panduit TDP43ME",
                "pPortName": "USB001",
                "pDriverName": "Panduit TDP43ME",
                "Status": 0,
                "cJobs": 1,
            },
            {
                "pPrinterName": "Godex G500",
                "pPortName": "192.168.1.50",
                "pDriverName": "Godex G500",
                "Status": 128,
                "cJobs": 0,
            },
        ]

    def OpenPrinter(self, name):
        if self.open_fails:
            raise self.error(1801, "OpenPrinter", "The printer name is invalid.")
        self.calls.append(("OpenPrinter", name))
        return 42

    def GetPrinter(self, h, level):
        self.calls.append(("GetPrinter", h, level))
        return {"Status": 0, "cJobs": 2}

    def StartDocPrinter(self, h, level, info):
        self.calls.append(("StartDocPrinter", h, level, tuple(info)))
        return 7

    def StartPagePrinter(self, h):
        self.calls.append(("StartPagePrinter", h))

    def WritePrinter(self, h, data):
        done = sum(len(w) for w in self.writes)
        if self.fail_after is not None and done + len(data) > self.fail_after:
            raise self.error(1722, "WritePrinter", "The RPC server is unavailable.")
        self.writes.append(bytes(data))
        self.calls.append(("WritePrinter", h, len(data)))
        return len(data)

    def EndPagePrinter(self, h):
        self.calls.append(("EndPagePrinter", h))

    def EndDocPrinter(self, h):
        self.calls.append(("EndDocPrinter", h))

    def AbortPrinter(self, h):
        self.calls.append(("AbortPrinter", h))

    def ClosePrinter(self, h):
        self.calls.append(("ClosePrinter", h))


@pytest.fixture
def wp(monkeypatch):
    fake = FakeWin32Print()
    monkeypatch.setitem(sys.modules, "win32print", fake)
    return fake


@pytest.fixture
def site(tmp_path):
    reg = tmp_path / "printers.yaml"
    reg.write_text(REG, encoding="utf-8")
    r = load(str(reg))
    job = tmp_path / "a.ezpl"
    job.write_bytes(rz.job(r.media["tiny"], [[None, None]] * 3))  # 6 labels, 3 blocks
    return {"reg": r, "regpath": reg, "job": job, "media": r.media["tiny"]}


def test_win_queue_takes_the_job_as_one_raw_document_in_chunks(wp, site):
    m = site["media"]
    data = rz.job(m, [[None, None]] * 40)
    data = data + b"\x00" * (3 * TCP_CHUNK + 11)  # past three chunks, so the chunking shows
    note, n = deliver(data, "win:Panduit TDP43ME", m, (0.1, -0.02))
    expected = offset_commands(m, (0.1, -0.02)) + data
    assert n == len(expected) and b"".join(wp.writes) == expected
    assert all(len(w) <= TCP_CHUNK for w in wp.writes) and len(wp.writes) == -(-len(expected) // TCP_CHUNK)
    assert note.startswith(f"spooled {n} bytes (40 labels) on Windows queue 'Panduit TDP43ME' as RAW")
    kinds = [c[0] for c in wp.calls]
    assert kinds[:3] == ["OpenPrinter", "StartDocPrinter", "StartPagePrinter"]
    assert kinds[-3:] == ["EndPagePrinter", "EndDocPrinter", "ClosePrinter"]
    assert set(kinds[3:-3]) == {"WritePrinter"} and "AbortPrinter" not in kinds
    assert wp.calls[1] == (
        "StartDocPrinter",
        42,
        1,
        ("rasterezpl job", None, "RAW"),
    )  # RAW: the bytes pass through


def test_a_spooler_failure_mid_document_aborts_it_and_names_the_bytes(monkeypatch, site, tmp_path):
    fake = FakeWin32Print(fail_after=TCP_CHUNK + 5)
    monkeypatch.setitem(sys.modules, "win32print", fake)
    m = site["media"]
    data = b"x" * (3 * TCP_CHUNK)
    with pytest.raises(SendError) as ei:
        deliver(data, "win:Panduit TDP43ME")
    e = ei.value
    assert e.bytes_sent == TCP_CHUNK == sum(len(w) for w in fake.writes)
    assert f"the spooler stopped taking data after {TCP_CHUNK} bytes of {len(data)}" in str(e)
    assert "RPC server is unavailable" in str(e) and "read the roll" in str(e)
    kinds = [c[0] for c in fake.calls]
    assert kinds[-2:] == ["AbortPrinter", "ClosePrinter"] and "EndDocPrinter" not in kinds
    # through the logged path: ok false, the bytes, the error
    log = tmp_path / "log.jsonl"
    fake.writes.clear()
    ok, note = send_logged(
        data,
        "win:Panduit TDP43ME",
        log,
        file="j.ezpl",
        labels="1-6",
        count=6,
        bookends=False,
        printer="winp",
        media=m,
    )
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert ok is False and entry["ok"] is False and entry["bytes_sent"] == TCP_CHUNK
    assert entry["transport"] == "win:Panduit TDP43ME" and "stopped taking data" in entry["error"]
    assert note.startswith("FAILED: ") and "resume with --labels" in note


def test_an_unknown_queue_is_a_clean_refusal_with_nothing_sent(monkeypatch):
    fake = FakeWin32Print(open_fails=True)
    monkeypatch.setitem(sys.modules, "win32print", fake)
    with pytest.raises(SendError) as ei:
        deliver(b"abc", "win:Nope")
    assert (
        ei.value.bytes_sent == 0 and "nothing sent" in str(ei.value) and "rasterezpl queues" in str(ei.value)
    )
    assert "printer name is invalid" in str(ei.value)
    assert status("win:Nope").startswith("win queue 'Nope': cannot open it")


def test_without_pywin32_the_message_names_the_extra(monkeypatch, site, capsys):
    monkeypatch.setitem(
        sys.modules, "win32print", None
    )  # import fails as it does off Windows without pywin32
    with pytest.raises(RuntimeError, match=r"rasterezpl\[windows\]"):
        deliver(b"abc", "win:Q")
    with pytest.raises(SystemExit) as ei:
        main(["--registry", str(site["regpath"]), "status", "--printer", "winp"])
    assert "pywin32" in str(ei.value) and "rasterezpl[windows]" in str(ei.value)


def test_status_over_a_queue_is_what_the_spooler_knows_and_never_ready(wp, site, capsys):
    st = status("win:Panduit TDP43ME")
    assert st == "win queue 'Panduit TDP43ME': no ~S,CHECK over a queue; spooler status 0, 2 jobs waiting"
    assert ("GetPrinter", 42, 2) in wp.calls and wp.calls[-1] == ("ClosePrinter", 42)
    # so `print --status` refuses, as it does for an lp: queue — the printer's own state is not readable here
    rc = main(
        ["--registry", str(site["regpath"]), "print", str(site["job"]), "--printer", "winp", "--status"]
    )
    out = capsys.readouterr().out
    assert rc == 2 and "not ready — nothing sent" in out and not wp.writes
    assert main(["--registry", str(site["regpath"]), "status", "--printer", "winp"]) == 1


def test_cli_print_to_a_queue_logs_the_queue_as_transport(wp, site, capsys):
    rc = main(
        [
            "--registry",
            str(site["regpath"]),
            "print",
            str(site["job"]),
            "--printer",
            "winp",
            "--labels",
            "1-2",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0 and "spooled " in out and "on Windows queue 'Panduit TDP43ME' as RAW" in out
    spooled = b"".join(wp.writes)
    assert spooled.startswith(b"^R10\r~Q-2\r") and rz.count_labels(spooled) == 1
    log = site["job"].parent / rz.jobs.LOG_NAME if hasattr(rz, "jobs") else None
    from rasterezpl.jobs import LOG_NAME

    entry = json.loads((site["job"].parent / LOG_NAME).read_text(encoding="utf-8").splitlines()[-1])
    assert (
        entry["transport"] == "win:Panduit TDP43ME"
        and entry["ok"] is True
        and entry["bytes_sent"] == len(spooled)
    )
    assert log is None or True


def test_queues_lists_windows_queues_or_cups_queues(wp, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    assert main(["queues"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("win:Panduit TDP43ME") and "port USB001" in out[0] and "status 0, OK" in out[0]
    assert out[0].endswith("(default)") and "1 job(s)" in out[0]
    assert out[1].startswith("win:Godex G500") and "error flag set" in out[1] and "(default)" not in out[1]
    assert wp.calls[-1] == ("EnumPrinters", 6, None, 2)  # local + connections, level 2
    # off Windows: lpstat's queues as lp: targets
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, capture_output, text):
        r = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == ["lpstat", "-p"]:
            r.stdout = (
                "printer tdp43me is idle.  enabled since Mon\nprinter office disabled since Tue -\n\treason\n"
            )
        elif cmd == ["lpstat", "-d"]:
            r.stdout = "system default destination: office\n"
        return r

    monkeypatch.setattr(transport.subprocess, "run", fake_run)
    assert main(["queues"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("lp:tdp43me") and "is idle." in out[0] and "(default)" not in out[0]
    assert out[1].startswith("lp:office") and out[1].endswith("(default)")

    def no_lpstat(cmd, capture_output, text):
        raise FileNotFoundError("lpstat")

    monkeypatch.setattr(transport.subprocess, "run", no_lpstat)
    assert main(["queues"]) == 0 and "no print queue on this machine" in capsys.readouterr().out


def test_usb_on_windows_points_at_the_queue(monkeypatch):
    core = types.ModuleType("usb.core")

    class NoBackendError(Exception):
        pass

    core.NoBackendError = NoBackendError  # type: ignore[attr-defined]

    def find(find_all=False):
        raise NoBackendError("No backend available")

    core.find = find  # type: ignore[attr-defined]
    util = types.ModuleType("usb.util")
    usb = types.ModuleType("usb")
    usb.core, usb.util = core, util  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "usb", usb)
    monkeypatch.setitem(sys.modules, "usb.core", core)
    monkeypatch.setitem(sys.modules, "usb.util", util)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RuntimeError) as ei:
        transport.list_usb_printers()
    msg = str(ei.value)
    assert (
        "no libusb backend" in msg
        and "Zadig" in msg
        and "win:<queue name>" in msg
        and "rasterezpl queues" in msg
    )
    with pytest.raises(SystemExit) as se:
        main(["usb"])
    assert "Zadig" in str(se.value)
    # and without pyusb at all, the same pointer
    monkeypatch.setitem(sys.modules, "usb", None)
    monkeypatch.setitem(sys.modules, "usb.core", None)
    with pytest.raises(RuntimeError, match=r"rasterezpl\[usb\].*win:<queue name>"):
        transport.list_usb_printers()


def test_registry_is_read_from_appdata_first(tmp_path, monkeypatch):
    monkeypatch.delenv("RASTEREZPL_PRINTERS", raising=False)
    appdata = tmp_path / "AppData" / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    home_reg = tmp_path / "home" / ".config" / "rasterezpl" / "printers.yaml"
    monkeypatch.setattr(registry, "DEFAULT_PATH", home_reg)
    assert registry.default_paths() == [appdata / "rasterezpl" / "printers.yaml", home_reg]
    assert registry.suggested_path() == appdata / "rasterezpl" / "printers.yaml"
    assert registry.registry_path() is None  # neither exists: presets only
    home_reg.parent.mkdir(parents=True)
    home_reg.write_text(REG, encoding="utf-8")
    assert registry.registry_path() == home_reg
    win_reg = appdata / "rasterezpl" / "printers.yaml"
    win_reg.parent.mkdir(parents=True)
    win_reg.write_text(REG.replace("winp:", "fromappdata:"), encoding="utf-8")
    assert registry.registry_path() == win_reg and list(load().printers) == ["fromappdata"]
    monkeypatch.setenv("RASTEREZPL_PRINTERS", str(home_reg))  # the env var still wins over both
    assert registry.registry_path() == home_reg
    monkeypatch.delenv("APPDATA")
    assert registry.default_paths() == [home_reg]


def test_printers_says_where_it_looked(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RASTEREZPL_PRINTERS", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(registry, "DEFAULT_PATH", tmp_path / "nohome.yaml")
    assert main(["printers"]) == 0
    out = capsys.readouterr().out
    assert "no registry file at" in out and "Roaming" in out and "put yours at" in out


def test_system_fonts_are_listed_and_resolve_by_their_own_names():
    dirs = text.font_dirs()
    assert all(d.is_dir() for d in dirs)
    faces = text.system_fonts()
    assert isinstance(faces, dict) and text.system_fonts() is faces  # computed once
    if not faces:
        pytest.skip("no OS fonts on this machine")
    name, path = next(iter(sorted(faces.items())))
    assert text.find_font(name) == path and text.find_font(name.upper()) == path
    listed = fonts_available()
    by_path = {e["path"]: e for e in listed}
    assert (
        listed[0]["bundled"] is True and path in by_path
    )  # listed — as a known candidate (Arial) or as system
    assert len(by_path) == len(listed)  # one entry per file
    assert all(
        e.get("system")
        for e in listed
        if e["path"] not in {text.find_font(n) for n in text.FONT_CANDIDATES} and not e.get("bundled")
    )
    # a bundled face is never shadowed by an installed one of the same name
    assert text.find_font("Inter") == text.bundled_fonts()["Inter"]


def test_fonts_are_laid_out_by_the_basic_engine_on_every_machine():
    from PIL import ImageFont

    inter = text.bundled_fonts()["Inter Bold"]
    f = text.load_font(inter, 100)
    assert f.layout_engine == ImageFont.Layout.BASIC
    # the number the CI matrix found 2026-11-08: raqm (where libfribidi is installed) says 1150.9 for this line
    assert f.getlength("IAD108.1 SC77 R6225/A") == 1162.0
    # render_text, the bookends and the ruler all go through load_font: the same raster whether or not fribidi is here
    img = text.render_text((1200, 130), ["IAD108.1 SC77 R6225/A"], inter, 100)
    assert img.getbbox() is not None


def test_module_entry_point_and_tolerant_console():
    out = subprocess.run([sys.executable, "-m", "rasterezpl", "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == f"rasterezpl {rz.__version__}"
    # a cp1252 console: the arrows and dots in the summaries print as ? instead of raising
    env = {"PYTHONIOENCODING": "cp1252"}
    import os

    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "from rasterezpl.cli import _tolerant_console; _tolerant_console(); print('a → b · c')",
        ],
        capture_output=True,
        env={**os.environ, **env},
    )
    assert r.returncode == 0 and r.stdout.strip() == b"a ? b \xb7 c"

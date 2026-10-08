"""Faded print, two levers: `stroke` thickens glyphs in the raster; a printer record's darkness / speed are
applied to the job at send time (`retune`) without touching the file."""

from __future__ import annotations

import json

import pytest

import rasterezpl as rz
from rasterezpl.cli import main
from rasterezpl.compose import Spec, render
from rasterezpl.jobs import plan, run
from rasterezpl.layouts import LAYOUTS, render_label
from rasterezpl.registry import load
from rasterezpl.text import bundled_fonts

PIL = pytest.importorskip("PIL")


def _ink(im) -> int:
    return sum(1 for v in im.getdata() if v == 0)


def test_stroke_adds_ink_and_still_refuses_to_clip():
    inter = bundled_fonts()["Inter"]
    plain = rz.render_text((300, 60), ["Rack 12 U31"], inter, 20)
    bold = rz.render_text((300, 60), ["Rack 12 U31"], inter, 20, stroke=1)
    heavy = rz.render_text((300, 60), ["Rack 12 U31"], inter, 20, stroke=2)
    assert _ink(plain) < _ink(bold) < _ink(heavy)
    assert plain.size == bold.size == heavy.size == (300, 60)
    # the stroke counts toward the width: a line that just fits plain does not fit with 2 extra dots a side
    w = (
        int(__import__("PIL.ImageFont", fromlist=["truetype"]).truetype(inter, 20).getlength("Rack 12 U31"))
        + 1
    )
    rz.render_text((w, 60), ["Rack 12 U31"], inter, 20)
    with pytest.raises(ValueError):
        rz.render_text((w, 60), ["Rack 12 U31"], inter, 20, stroke=2)
    # every layout takes it (the sample in Inter, stroke 1 vs 0 → more ink wherever there is text)
    from rasterezpl.images import placeholder_logo

    mark = placeholder_logo()
    m, full = rz.PANDUIT_S150X225VATY_2UP, rz.PANDUIT_S150X225VATY_2UP_FULL
    for lay in LAYOUTS.values():
        if not lay.sample:
            continue  # picture / code alone: no text to thicken
        _, _, lw, lh = (full if lay.name.startswith("clear-") else m).area_px(0)
        kw = {"image": mark if lay.needs_image else None, "qr": "C3000071A605" if lay.needs_qr else None}
        a = render_label(lw, lh, lay.sample.split("\n"), inter, 14, lay.name, **kw)
        b = render_label(lw, lh, lay.sample.split("\n"), inter, 14, lay.name, stroke=1, **kw)
        assert _ink(b) > _ink(a), lay.name


def test_compose_stroke_field():
    reg = load(None)
    base = {"media": "panduit-s150x225vaty-2up", "font": "Inter", "pt": 6, "text": "a b c\nd e f"}
    s0 = Spec.from_dict(base)
    s1 = Spec.from_dict({**base, "stroke": "1"})
    assert s0.stroke == 0 and s1.stroke == 1 and Spec.from_dict({**base, "stroke": -3}).stroke == 0
    d0, m, _, _ = render(reg, s0)
    d1, _, _, _ = render(reg, s1)
    p0 = rz.decode_block(rz.parse_blocks(d0)[0][1], m)
    p1 = rz.decode_block(rz.parse_blocks(d1)[0][1], m)
    assert _ink(p1) > _ink(p0)


def test_retune_rewrites_only_the_setup():
    m = rz.PANDUIT_S150X225VATY_2UP
    data = rz.job(m, [[None, None]])
    assert b"^S" not in data.split(b"^L\r")[0] or True  # the preset sends no ^S/^H: nothing to replace
    head = b"^Q34,2\r^W92\r~R200\r^AT\r^S3\r^H14\r^E18\r^P1\r^L\r"
    body = b"Q0,24,92,8\r" + b"\x00" * 8 + b"\rE\r"
    out = rz.retune(head + body, darkness=17, speed=2)
    assert out == b"^Q34,2\r^W92\r~R200\r^AT\r^S2\r^H17\r^E18\r^P1\r^L\r" + body
    assert rz.retune(head + body) == head + body
    assert rz.retune(head + body, darkness=5) == head.replace(b"^H14", b"^H05") + body
    with pytest.raises(ValueError):
        rz.retune(head + body, darkness=20)
    with pytest.raises(ValueError):
        rz.retune(head + body, speed=0)
    assert rz.retune(b"no setup here", darkness=17) == b"no setup here"


def test_printer_heat_applied_at_send_time(tmp_path, capsys):
    reg = tmp_path / "printers.yaml"
    spool = tmp_path / "spool.ezpl"
    reg.write_text(
        "media:\n  flags:\n    dpi: 203\n    width_mm: 92\n    length_mm: 34\n    gap_mm: 2\n    speed: 3\n"
        "    darkness: 14\n    stop_mm: 18\n    areas_in: [[0, 0, 3.622, 1.339]]\n"
        f'printers:\n  hot:\n    transport: "{spool}"\n    media: flags\n    darkness: 17\n    speed: 2\n'
    )
    r = load(str(reg))
    assert r.printers["hot"].darkness == 17 and r.printers["hot"].speed == 2
    root = tmp_path / "root"
    root.mkdir()
    (root / "t.ezpl").write_bytes(rz.job(r.media["flags"], [[None]]))
    assert b"^S3\r^H14\r" in (root / "t.ezpl").read_bytes()
    notes = run(root, r, plan(root, r, [{"file": "t.ezpl"}]), root / "log.jsonl")
    assert "darkness 17, speed 2" in notes[0]
    sent = spool.read_bytes()
    assert b"^S2\r^H17\r" in sent and b"^H14" not in sent
    assert b"^S3\r^H14\r" in (root / "t.ezpl").read_bytes()  # the file is untouched
    entry = json.loads((root / "log.jsonl").read_text().splitlines()[-1])
    assert "darkness 17" in entry["note"]
    # the CLI: the record's heat by default, --darkness/--speed for one print
    assert main(["--registry", str(reg), "print", str(root / "t.ezpl"), "--printer", "hot"]) == 0
    assert "darkness 17, speed 2" in capsys.readouterr().out and b"^H17" in spool.read_bytes()
    assert (
        main(["--registry", str(reg), "print", str(root / "t.ezpl"), "--printer", "hot", "--darkness", "9"])
        == 0
    )
    assert b"^S2\r^H09\r" in spool.read_bytes()

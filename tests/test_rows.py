"""The list of labels: a CSV / TSV file or a pasted sheet → one label per row. The parser's edge cases, the probe
the page calls, the recipe list, the CLI's --rows, and the sidecar carrying the list for a reload."""

from __future__ import annotations

import json
import threading

import pytest

import rasterezpl as rz
from rasterezpl.cli import main
from rasterezpl.compose import (
    Spec,
    list_composed,
    parse_rows,
    probe_rows,
    read_rows,
    sniff_delimiter,
)
from rasterezpl.registry import load
from rasterezpl.server import make_server
from test_compose import REG, _get, _post  # rootdir conftest-less: tests are importable as modules

PIL = pytest.importorskip("PIL")


def test_sniff_and_read_rows():
    assert sniff_delimiter("a\tb,c") == "\t"
    assert sniff_delimiter("a;b;c,d") == ";"
    assert sniff_delimiter("a,b") == "," and sniff_delimiter("single") == ","
    # BOM, CRLF, a semicolon sheet (a European Excel export), quoted cells, a blank row, a short row
    sheet = '﻿device;port\r\nsw1;"Eth1/1; a"\r\n;\r\nsw2\r\n'
    cols, rows = read_rows(sheet)
    assert cols == ["device", "port"]
    assert rows == [{"device": "sw1", "port": "Eth1/1; a"}, {"device": "sw2", "port": ""}]
    # no header: col1… as wide as the widest row, the first line is data
    cols, rows = read_rows("a,b\nc\n", header=False)
    assert cols == ["col1", "col2"] and rows == [{"col1": "a", "col2": "b"}, {"col1": "c", "col2": ""}]
    assert read_rows("   \n") == ([], [])
    with pytest.raises(ValueError):  # a header that repeats or blanks a name cannot be addressed
        read_rows("a,a\n1,2\n")
    with pytest.raises(ValueError):
        read_rows("a,\n1,2\n")


def test_parse_rows_template_and_defaults():
    assert parse_rows("name\nAlice\n\nBob\n", "") == ["Alice", "Bob"]  # blank rows skipped
    assert parse_rows("Alice\nBob\n", "", header=False) == ["Alice", "Bob"]  # a plain list, no header
    assert parse_rows("a,b\n1,2\n", "{b}-{a} #{n}") == ["2-1 #1"]
    assert parse_rows("a,b\n1,2\n", "{a}\\n{b}") == ["1\n2"]
    with pytest.raises(ValueError, match="columns: a, b"):
        parse_rows("a,b\n1,2\n", "{zz}")


def test_probe_rows():
    p = probe_rows("device,port\nsw1,e1\nsw2,e2\n", "{device} {port}")
    assert p == {"columns": ["device", "port"], "rows": 2, "labels": 2, "sample": ["sw1 e1", "sw2 e2"]}
    p = probe_rows("device,port\nsw1,e1\n", "{nope}")
    assert p["columns"] == ["device", "port"] and p["rows"] == 1 and "nope" in p["error"]
    assert probe_rows("", "") == {"columns": [], "rows": 0, "labels": 0, "sample": []}


def test_spec_from_dict_keeps_the_list():
    s = Spec.from_dict(
        {"media": "m", "font": "f", "pt": 3, "rows": "x\n1\n2", "template": "", "header": True}
    )
    assert s.labels == ["1", "2"] and s.rows == "x\n1\n2" and s.template == "" and s.header is True
    s = Spec.from_dict({"media": "m", "font": "f", "pt": 3, "rows": "1\n2", "header": False, "qr": "{col1}"})
    assert s.labels == ["1", "2"] and s.qrs == ["1", "2"] and s.header is False


@pytest.fixture
def site(tmp_path):
    reg = tmp_path / "printers.yaml"
    reg.write_text(REG.format(target=tmp_path / "spool.ezpl"))
    root = tmp_path / "root"
    root.mkdir()
    srv = make_server(root, "127.0.0.1", 0, str(reg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "root": root}
    srv.shutdown()
    srv.server_close()


def test_rows_probe_compose_sidecar_and_recipe_list(site):
    b, root = site["base"], site["root"]
    fonts = json.loads(_get(f"{b}/api/fonts")[2])
    if not fonts:
        pytest.skip("no font on this machine")
    font = fonts[0]["name"]
    assert json.loads(_get(f"{b}/api/composed")[2]) == []
    sheet = "﻿name;rack\r\nAlice;6222\r\n\r\nBob;6223\r\n"
    st, j = _post(f"{b}/api/rows", {"rows": sheet, "template": "{name}\\nR{rack}"})
    assert st == 200 and j == {
        "columns": ["name", "rack"],
        "rows": 2,
        "labels": 2,
        "sample": ["Alice\nR6222", "Bob\nR6223"],
    }
    st, j = _post(f"{b}/api/rows", {"rows": sheet, "template": "{x}"})
    assert st == 200 and "x" in j["error"] and j["columns"] == ["name", "rack"]
    spec = {
        "media": "tiny",
        "font": font,
        "pt": 3,
        "rows": sheet,
        "template": "{n}",  # the test media is 16×8 dots: the probe above rendered nothing, this prints
        "name": "dco",
        "fit": True,
    }
    st, c = _post(f"{b}/api/compose", spec)
    assert st == 200 and c["labels"] == 2 and c["blocks"] == 1
    side = json.loads(_get(f"{b}/api/spec?file={c['spec']}")[2])
    assert side["texts"] == ["1", "2"]
    assert side["rows"] == sheet and side["template"] == "{n}" and side["header"] is True
    # the recipe list names it; reloading its sidecar into a new spec gives the same labels
    lst = json.loads(_get(f"{b}/api/composed")[2])
    assert len(lst) == 1 and lst[0]["name"] == "dco" and lst[0]["input"] == "rows" and lst[0]["labels"] == 2
    assert lst[0]["spec"] == c["spec"] and lst[0]["file"] == c["file"] and lst[0]["layout"] == "plain"
    keep = {k: side[k] for k in ("media", "font", "pt", "rows", "header")}
    assert Spec.from_dict({**keep, "template": "{name}"}).labels == [
        "Alice",
        "Bob",
    ]  # the kept list, any template
    # a text composition is listed too, newest first
    st, c2 = _post(f"{b}/api/compose", {"media": "tiny", "font": font, "pt": 3, "text": "z", "name": "later"})
    assert st == 200
    lst = list_composed(root)
    assert [r["name"] for r in lst] == ["later", "dco"] and lst[0]["input"] == "text"
    assert list_composed(root, n=1)[0]["name"] == "later"
    # a no-header list through the API
    st, c3 = _post(
        f"{b}/api/compose",
        {"media": "tiny", "font": font, "pt": 3, "rows": "1\n2\n3", "header": False, "fit": True},
    )
    assert st == 200 and c3["labels"] == 3
    assert json.loads((root / c3["spec"]).read_text())["texts"] == ["1", "2", "3"]


def test_cli_text_rows(tmp_path, capsys):
    reg = tmp_path / "printers.yaml"
    reg.write_text(REG.format(target=tmp_path / "spool.ezpl"))
    font = rz.find_font("Arial", "DejaVu Sans")
    if font is None:
        pytest.skip("no font")
    csv_file = tmp_path / "dco.csv"
    csv_file.write_text("﻿name,rack\nAlice,6222\nBob,6223\n", encoding="utf-8")
    job = tmp_path / "job.ezpl"
    base = ["--registry", str(reg), "text", "--media", "tiny", "--font", font, "--pt", "4", "--fit"]
    assert main([*base, "--rows", str(csv_file), "--template", "{n}", "-o", str(job)]) == 0  # 16×8 dots
    assert "2 labels in 1 blocks" in capsys.readouterr().out
    assert rz.count_labels(job.read_bytes()) == 1
    plain = tmp_path / "plain.txt"
    plain.write_text("1\n2\n3\n")
    assert main([*base, "--rows", str(plain), "--no-header", "-o", str(job)]) == 0
    assert "3 labels in 2 blocks" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="not in the header"):
        main([*base, "--rows", str(csv_file), "--template", "{zz}", "-o", str(job)])
    with pytest.raises(SystemExit, match="not both, not neither"):
        main([*base, "-o", str(job)])
    with pytest.raises(SystemExit, match="not both, not neither"):
        main([*base, "a", "--rows", str(csv_file), "-o", str(job)])
    with pytest.raises(SystemExit):
        main([*base, "--rows", str(tmp_path / "missing.csv"), "-o", str(job)])


def test_preview_renders_only_the_shown_row(site, monkeypatch):
    """The live preview of a long list must not raster the whole list: one row's labels, the caption says of N."""
    import sys

    from rasterezpl.compose import preview_png

    cmod = sys.modules["rasterezpl.compose"]  # the package re-exports compose() under the module's name

    b = site["base"]
    fonts = json.loads(_get(f"{b}/api/fonts")[2])
    if not fonts:
        pytest.skip("no font on this machine")
    reg = load(str(site["root"].parent / "printers.yaml"))
    seen: list[list[str]] = []
    real = cmod.render

    def spy(reg_, spec, root=None):
        seen.append(list(spec.labels))
        return real(reg_, spec, root)

    monkeypatch.setattr(cmod, "render", spy)
    labels = [str(i) for i in range(1, 8)]  # 7 labels, 2 copies each = 14 texts = 7 rows on the two-up media
    spec = Spec(media="tiny", font=fonts[0]["name"], pt=3, labels=labels, copies=2, fit=True, qr="{n}")
    png = preview_png(reg, spec, site["root"], block=1)
    assert png.startswith(b"\x89PNG") and seen[-1] == ["1", "1"]
    preview_png(reg, spec, site["root"], block=4)
    assert seen[-1] == ["4", "4"]
    preview_png(reg, spec, site["root"], block=99)  # past the end: the last row
    assert seen[-1] == ["7", "7"]
    # {n} is already the running number in the rastered row, and a single area keeps one label per row
    three = Spec(media="tiny", font=fonts[0]["name"], pt=3, labels=["#{n}", "b", "c"], start=5, fit=True)
    preview_png(reg, three, site["root"], block=1)
    assert seen[-1] == ["#5", "b"]
    preview_png(reg, three, site["root"], block=2)
    assert seen[-1] == ["c"]
    one_area = Spec(media="tiny", font=fonts[0]["name"], pt=3, labels=["a", "b", "c"], area=1, fit=True)
    preview_png(reg, one_area, site["root"], block=3)
    assert seen[-1] == ["c"]
    # the spec given is not changed by the slicing
    assert spec.labels == labels and spec.copies == 2 and spec.qr == "{n}"

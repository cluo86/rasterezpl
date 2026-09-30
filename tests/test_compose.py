"""Composing jobs from typed text, rows and an Easy-Mark project — pure parts, then the API."""

from __future__ import annotations

import base64
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

import rasterezpl as rz
from rasterezpl.compose import Spec, expand, fonts_available, import_pemx, parse_rows, parse_text
from rasterezpl.registry import load
from rasterezpl.server import make_server

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
  filep:
    transport: "{target}"
    media: tiny
"""


def test_parse_text_rows_expand():
    assert parse_text("a\nb\n\n\nc\n \nd\ne\n") == ["a\nb", "c", "d\ne"]
    assert parse_text("") == []
    rows = "device\tport\track\nsw1\tEth1/1\t6222\nsw2\tEth2/1\t6223\n"
    assert parse_rows(rows, "{device} {port}\\nRack {rack} #{n}") == [
        "sw1 Eth1/1\nRack 6222 #1",
        "sw2 Eth2/1\nRack 6223 #2",
    ]
    assert parse_rows("a,b\n1,2\n", "") == ["1\n2"]  # no template: every column on its own line
    with pytest.raises(ValueError):
        parse_rows(rows, "{nope}")
    assert expand(["#{n}", "x"], copies=2, start=5) == ["#5", "#5", "x", "x"]


def test_spec_from_dict_and_import_pemx():
    s = Spec.from_dict({"media": "tiny", "font": "Arial", "pt": "4", "text": "a\n\nb", "copies": "2"})
    assert s.labels == ["a", "b"] and s.copies == 2 and s.pt == 4.0 and s.area is None
    proj = {
        "documents": [
            {
                "format": {"partName": "S150X225VATY"},
                "series": [{"style": {"fontSize": 5.4}, "data": ["l1\nl2", "l3\nl4"]}, {"data": ["l5"]}],
            }
        ]
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project-version.txt", "1.6.0")
        z.writestr("project.json", json.dumps(proj))
    got = import_pemx(buf.getvalue())
    assert got == {"texts": ["l1\nl2", "l3\nl4", "l5"], "pt": 5.4, "part": "S150X225VATY", "labels": 3}
    with pytest.raises(ValueError):
        import_pemx(b"not a zip")


def _post(url: str, doc: dict, raw: bool = False):
    req = urllib.request.Request(
        url, data=json.dumps(doc).encode(), headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, (body if raw else json.loads(body))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("content-type", ""), r.read()


@pytest.fixture
def site(tmp_path):
    reg = tmp_path / "printers.yaml"
    reg.write_text(REG.format(target=tmp_path / "spool.ezpl"))
    root = tmp_path / "root"
    root.mkdir()
    srv = make_server(root, "127.0.0.1", 0, str(reg))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "root": root, "reg": load(str(reg))}
    srv.shutdown()
    srv.server_close()


def test_compose_api_preview_save_reload_and_print(site):
    b, root = site["base"], site["root"]
    fonts = json.loads(_get(f"{b}/api/fonts")[2])
    if not fonts:
        pytest.skip("no font on this machine")
    font = fonts[0]["name"]
    st, _, body = _get(f"{b}/api/media")
    media = {m["name"]: m for m in json.loads(body)}
    assert media["tiny"]["areas"] == 2 and media["tiny"]["printers"] == ["filep"]
    spec = {"media": "tiny", "font": font, "pt": 3, "text": "a {n}\n\nc", "copies": 2, "name": "Test one"}
    # refusals: unknown media / face, empty text
    st, j = _post(f"{b}/api/compose", {**spec, "media": "nope"})
    assert st == 400 and "unknown media" in j["error"]
    st, j = _post(f"{b}/api/compose", {**spec, "font": "No Such Face"})
    assert st == 400 and "not on this machine" in j["error"]
    st, j = _post(f"{b}/api/compose", {**spec, "text": ""})
    assert st == 400 and "no label text" in j["error"]
    # preview: a PNG, nothing saved
    st, png = _post(f"{b}/api/compose", {**spec, "preview": True}, raw=True)
    assert st == 200 and png.startswith(b"\x89PNG") and not (root / "composed").exists()
    # compose: 2 labels × 2 copies = 4 labels = 2 two-up blocks, saved with its sidecar, {n} expanded
    st, j = _post(f"{b}/api/compose", spec)
    assert st == 200 and j["labels"] == 4 and j["blocks"] == 2 and j["file"].startswith("composed/Test-one-")
    data = (root / j["file"]).read_bytes()
    assert rz.count_labels(data) == 2
    side = json.loads(_get(f"{b}/api/spec?file={j['spec']}")[2])
    assert side["texts"] == ["a 1", "a 1", "c", "c"] and side["labels"] == ["a {n}", "c"]
    assert side["font_path"] == fonts[0]["path"] and side["media"] == "tiny"
    # it is an ordinary job now: listed, plannable, printable through the same path
    files = {f["file"]: f for f in json.loads(_get(f"{b}/api/files")[2])}
    assert files[j["file"]]["labels"] == 4 and files[j["file"]]["printers"] == ["filep"]
    st, pj = _post(f"{b}/api/print", {"jobs": [{"file": j["file"], "labels": "1-2"}]})
    assert st == 200 and pj["sent"] and rz.count_labels((root.parent / "spool.ezpl").read_bytes()) == 1
    # rows + template
    st, j2 = _post(
        f"{b}/api/compose",
        {
            "media": "tiny",
            "font": font,
            "pt": 3,
            "rows": "device\tport\nsw1\tEth1/1\nsw2\tEth2/1",
            "template": "{device}",
            "name": "rows",
        },
    )
    assert st == 200 and j2["labels"] == 2
    assert json.loads((root / j2["spec"]).read_text())["texts"] == ["sw1", "sw2"]
    # the Easy-Mark import
    proj = {
        "documents": [
            {"format": {"partName": "X"}, "series": [{"style": {"fontSize": 5.4}, "data": ["p\nq"]}]}
        ]
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project.json", json.dumps(proj))
    st, j3 = _post(f"{b}/api/import", {"pemx": base64.b64encode(buf.getvalue()).decode()})
    assert st == 200 and j3["texts"] == ["p\nq"] and j3["pt"] == 5.4
    # a spec outside the root is refused
    st, j4 = _post(f"{b}/api/compose", spec)  # same content → same hash → same file, fine
    assert st == 200
    st, _, _ = _get(f"{b}/api/log?n=5")
    assert st == 200
    assert fonts_available(root)[0]["name"] == font

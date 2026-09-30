"""The browser front end's API against a file-target printer — no printer, no browser: the server runs in a
thread on a free port and urllib is the client."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

import rasterezpl as rz
from rasterezpl.registry import load
from rasterezpl.server import LOG_NAME, make_server

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
    transport: "{tiny_target}"
    media: tiny
    offset_in: [0.1, -0.02]
    note: test
  pand:
    transport: "{pand_target}"
    media: panduit-s150x225vaty-2up
"""


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("content-type", ""), r.read()


def _post(url: str, doc: dict):
    req = urllib.request.Request(
        url, data=json.dumps(doc).encode(), headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture
def site(tmp_path):
    reg = tmp_path / "printers.yaml"
    tiny_target, pand_target = tmp_path / "spool-tiny.ezpl", tmp_path / "spool-pand.ezpl"
    reg.write_text(REG.format(tiny_target=tiny_target, pand_target=pand_target))
    r = load(str(reg))
    root = tmp_path / "root"
    (root / "jobs").mkdir(parents=True)
    (root / "jobs" / "a.ezpl").write_bytes(rz.job(r.media["tiny"], [[None, None], [None, None]]))  # 4 labels
    (root / "other.ezpl").write_bytes(rz.job(rz.PANDUIT_S150X225VATY_2UP, [[None, None]]))  # 2 labels
    (root / "note.txt").write_text("static file beside the jobs")
    (tmp_path / "outside.ezpl").write_bytes(rz.job(r.media["tiny"], [[None, None]]))
    srv = make_server(root, "127.0.0.1", 0, str(reg))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield {"base": base, "root": root, "tiny": tiny_target, "pand": pand_target, "tmp": tmp_path}
    srv.shutdown()
    srv.server_close()


def test_pages_printers_files_and_static(site):
    b = site["base"]
    st, ct, body = _get(f"{b}/rasterezpl/")
    assert st == 200 and "text/html" in ct and b"rasterezpl" in body
    st, _, body = _get(f"{b}/api/printers")
    prs = json.loads(body)
    assert [p["name"] for p in prs] == ["filep", "pand"]
    assert prs[0]["offset_in"] == [0.1, -0.02] and prs[0]["media_name"] == "tiny"
    st, _, body = _get(f"{b}/api/files")
    files = {f["file"]: f for f in json.loads(body)}
    assert files["jobs/a.ezpl"]["labels"] == 4 and files["jobs/a.ezpl"]["printers"] == ["filep"]
    assert files["other.ezpl"]["labels"] == 2 and files["other.ezpl"]["printers"] == ["pand"]
    st, _, body = _get(f"{b}/jobs/a.ezpl")  # the job files are served as static files
    assert st == 200 and body == (site["root"] / "jobs" / "a.ezpl").read_bytes()
    st, _, body = _get(f"{b}/note.txt")
    assert st == 200 and b"static file" in body
    st, ct, body = _get(f"{b}/api/proof?file=jobs/a.ezpl&label=3")
    assert st == 200 and ct == "image/png" and body.startswith(b"\x89PNG")


def test_plan_refusals_and_auto_printer(site):
    b = site["base"]
    st, j = _post(f"{b}/api/plan", {"jobs": [{"file": "jobs/a.ezpl", "labels": "2,9"}]})
    assert st == 200 and "outside 1–4" in j["plan"][0]["refused"]
    st, j = _post(f"{b}/api/plan", {"jobs": [{"file": "../outside.ezpl", "labels": "1"}]})
    assert "outside the served root" in j["plan"][0]["refused"]
    st, j = _post(f"{b}/api/plan", {"jobs": [{"file": "note.txt", "labels": "1"}]})
    assert "not a .ezpl" in j["plan"][0]["refused"]
    st, j = _post(f"{b}/api/plan", {"jobs": [{"file": "jobs/a.ezpl", "labels": "1-3", "printer": "pand"}]})
    assert "holds panduit-s150x225vaty-2up" in j["plan"][0]["refused"]
    st, j = _post(f"{b}/api/plan", {"jobs": [{"file": "jobs/a.ezpl", "labels": "x"}]})
    assert "print-dialog form" in j["plan"][0]["refused"]
    st, j = _post(
        f"{b}/api/plan", {"jobs": [{"file": "jobs/a.ezpl", "labels": "1-3"}, {"file": "other.ezpl"}]}
    )
    r0, r1 = j["plan"]
    assert r0["refused"] is None and r0["printer"] == "filep" and r0["count"] == 3 and r0["total"] == 4
    assert r1["refused"] is None and r1["printer"] == "pand" and r1["labels"] == "1-2" and r1["count"] == 2
    st, j = _post(f"{b}/api/plan", {"jobs": []})
    assert st == 400 and "jobs" in j["error"]
    st, j = _post(f"{b}/api/nothing", {"jobs": [{"file": "x"}]})
    assert st == 404


def test_print_dry_run_refused_and_real(site):
    b = site["base"]
    # dry run: the plan comes back, nothing is written
    st, j = _post(f"{b}/api/print", {"jobs": [{"file": "jobs/a.ezpl", "labels": "1-3"}], "dry_run": True})
    assert st == 200 and j["sent"] is False and j["notes"] == ["dry run — nothing sent"]
    assert not site["tiny"].exists()
    # one refused row stops the whole cart
    st, j = _post(
        f"{b}/api/print",
        {"jobs": [{"file": "jobs/a.ezpl", "labels": "1-3"}, {"file": "jobs/a.ezpl", "labels": "9"}]},
    )
    assert st == 409 and j["sent"] is False and "nothing sent" in j["notes"][0]
    assert not site["tiny"].exists()
    # the real thing, to the file targets: offset in front, labels 1-3 of a two-up = both blocks
    st, j = _post(
        f"{b}/api/print", {"jobs": [{"file": "jobs/a.ezpl", "labels": "1-3"}, {"file": "other.ezpl"}]}
    )
    assert st == 200 and j["sent"] is True and len(j["notes"]) == 2
    spool = site["tiny"].read_bytes()
    assert spool.startswith(b"^R10\r~Q-2\r") and rz.count_labels(spool) == 2
    assert rz.count_labels(site["pand"].read_bytes()) == 1
    log = site["root"] / LOG_NAME
    entries = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [e["file"] for e in entries] == ["jobs/a.ezpl", "other.ezpl"] and entries[0]["printer"] == "filep"
    st, _, body = _get(f"{b}/api/log?n=10")
    assert [e["count"] for e in json.loads(body)] == [3, 2]

"""Encode, parse, select, decode — pure functions, no printer."""

from __future__ import annotations

import pytest

import rasterezpl as rz

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

M2 = rz.Media("two-up", 100, 20.0, 10.0, 2.0, areas_in=((0.0, 0.0, 0.16, 0.08), (0.4, 0.1, 0.16, 0.08)))


def img(w: int, h: int, black: list[tuple[int, int]]):
    im = Image.new("L", (w, h), 255)
    for x, y in black:
        im.putpixel((x, y), 0)
    return im


def test_setup_commands_and_preset():
    m = rz.PANDUIT_S150X225VATY_2UP
    assert rz.setup_commands(m) == b"^Q57,3\r^W83\r~R200\r^AT\r^P1\r"
    assert m.area_px(0) == (38, 19, 450, 225) and m.area_px(1) == (488, 19, 450, 225)
    assert (m.width_px, m.length_px) == (975, 675) and m.labels_per_block == 2
    g = rz.Media(
        "g", 203, 92, 34, 2, speed=3, darkness=14, stop_mm=18, areas_in=((0, 0, 92 / 25.4, 34 / 25.4),)
    )
    assert rz.setup_commands(g) == b"^Q34,2\r^W92\r~R200\r^AT\r^S3\r^H14\r^E18\r^P1\r"
    assert g.area_px(0) == (0, 0, 735, 272)
    with pytest.raises(ValueError):
        rz.setup_commands(rz.Media("bad", 203, 92, 34, 2, darkness=20))


def test_bitmap_polarity_and_pattern_blocks():
    stride, h, rows = rz.bitmap_rows(img(16, 10, [(x, 0) for x in range(8)] + [(15, 9)]))
    assert (stride, h) == (2, 10) and rows[0] == b"\xff\x00" and rows[9] == b"\x00\x01"
    data = rz.pattern_blocks(img(16, 10, [(x, 0) for x in range(8)] + [(15, 9)]), 8, 19)
    assert data == b"Q8,19,2,8\r" + b"\xff\x00" + b"\x00\x00" * 7 + b"\r" + b"Q8,27,2,2\r\x00\x00\x00\x01\r"
    assert rz.pattern_blocks(img(16, 16, [(0, 15)]), 0, 0) == b"Q0,8,2,8\r" + b"\x00\x00" * 7 + b"\x80\x00\r"
    # x off the byte boundary: padded on the left, the content stays at x = 3
    assert rz.pattern_blocks(img(8, 1, [(0, 0)]), 3, 0) == b"Q0,0,2,1\r\x10\x00\r"


def test_label_block_rotates_into_the_printer_frame():
    m = rz.Media(
        "t", 100, 20.0, 10.0, 2.0, speed=3, darkness=14, stop_mm=18, areas_in=((0.0, 0.0, 0.16, 0.08),)
    )
    blk = rz.label_block(m, [img(16, 8, [(0, 0)])])
    assert blk.startswith(b"^Q10,2\r^W20\r~R200\r^AT\r^S3\r^H14\r^E18\r^P1\r^L\r") and blk.endswith(b"\rE\r")
    # 79 × 39 dot page: the 16 × 8 area at (0, 0) lands at (63, 31), padded 7 to the boundary 56 (3 bytes wide),
    # its black dot (0, 0) → (15 + 7, 7)
    assert blk.count(b"Q56,31,3,8\r" + b"\x00\x00\x00" * 7 + b"\x00\x00\x02\r") == 1
    with pytest.raises(ValueError):
        rz.label_block(m, [img(8, 8, [])])
    with pytest.raises(ValueError):
        rz.label_block(m, [img(16, 8, []), img(16, 8, [])])


def test_parse_select_decode_roundtrip():
    a = img(16, 8, [(0, 0), (13, 4)])
    b = img(16, 8, [(x, 1) for x in range(16)])
    tricky = img(16, 8, [(0, 2), (1, 2), (2, 2), (3, 2), (5, 2), (10, 2), (11, 2), (12, 2), (13, 2), (15, 2)])
    data = rz.job(M2, [[a, b], [tricky, None], [b, a]])
    assert rz.count_labels(data) == 3 and len(rz.parse_blocks(data)) == 3
    assert rz.select_blocks(data, [1]) == rz.first_labels(data, 1) == rz.label_block(M2, [a, b])
    assert rz.select_blocks(data, [1, 3]) == rz.label_block(M2, [a, b]) + rz.label_block(M2, [b, a])
    assert rz.parse_selection("1,3,5-6", 6) == [1, 3, 5, 6] and rz.parse_selection("2-2", 3) == [2]
    with pytest.raises(ValueError):
        rz.parse_selection("7", 6)
    page = rz.decode_block(rz.parse_blocks(data)[0][1], M2)
    assert page.size == (79, 39)
    rx, ry, aw, ah = M2.printer_rect(0)
    assert page.crop((rx, ry, rx + aw, ry + ah)).tobytes() == a.rotate(180).tobytes()
    only2 = rz.select_labels(data, "2", M2)
    pg = rz.decode_block(rz.parse_blocks(only2)[0][1], M2)
    assert pg.crop((rx, ry, rx + aw, ry + ah)).getextrema() == (255, 255)
    bx, by, bw, bh = M2.printer_rect(1)
    assert pg.crop((bx, by, bx + bw, by + bh)).tobytes() == b.rotate(180).tobytes()
    sel = rz.select_labels(data, "1-2,5", M2)
    assert rz.count_labels(sel) == 2 and sel.startswith(rz.label_block(M2, [a, b]))
    assert len(rz.decode_job(data, M2)) == 3


def test_blocks_outside_areas_catches_a_stale_frame():
    m = rz.PANDUIT_S150X225VATY_2UP
    good = rz.job(m, [[img(450, 225, [(0, 0), (449, 224)]), img(450, 225, [(10, 10)])]])
    assert rz.blocks_outside_areas(good, m) == []
    # the same stock drawn leading-edge-up (no rotation): blocks land at y 19… — outside the rotated areas
    unrotated = rz.Media("u", 300, m.width_mm, m.length_mm, m.gap_mm, rotate180=False, areas_in=m.areas_in)
    stale = rz.job(unrotated, [[img(450, 225, [(0, 0)]), None]])
    bad = rz.blocks_outside_areas(stale, m)
    assert bad and bad[0][0] == 1 and bad[0][2] == 19


def test_header_and_media_match():
    data = rz.job(M2, [[img(16, 8, []), None]])
    assert rz.header_of(data) == (10, 2, 20)
    assert rz.match_media(data, {"x": M2, "p": rz.PANDUIT_S150X225VATY_2UP})[0] == "x"
    # the same stock at two resolutions shares a header: a 300 dpi job's blocks overflow the 203 dpi page, so it
    # matches only the 300 preset; a small 203 dpi job fits both and stays ambiguous (name the media)
    both = {"p300": rz.PANDUIT_S150X225VATY_2UP, "p203": rz.PANDUIT_S150X225VATY_2UP_203}
    j300 = rz.job(rz.PANDUIT_S150X225VATY_2UP, [[img(450, 225, [(449, 0)]), None]])
    assert rz.matching_media(j300, both) == ["p300"]
    m203 = rz.PANDUIT_S150X225VATY_2UP_203
    j203 = rz.job(m203, [[img(*m203.area_px(0)[2:], [(0, 0)]), None]])
    assert rz.matching_media(j203, both) == ["p300", "p203"]
    with pytest.raises(ValueError):
        rz.match_media(j203, both)
    with pytest.raises(ValueError):
        rz.match_media(data, {"p": rz.PANDUIT_S150X225VATY_2UP})
    with pytest.raises(ValueError):
        rz.match_media(b"hello", {"x": M2})


def test_offsets_are_the_printers_not_the_files(tmp_path):
    m = rz.PANDUIT_S150X225VATY_2UP
    assert rz.offset_commands(m, (0.1, -0.02)) == b"^R30\r~Q-6\r"
    assert rz.offset_commands(m, (0.0, 0.05)) == b"~Q+15\r"
    with pytest.raises(ValueError):
        rz.offset_commands(m, (-0.045, 0.0))
    with pytest.raises(ValueError):
        rz.offset_commands(m, (0.0, 0.4))
    data = rz.job(M2, [[img(16, 8, [(0, 0)]), None]])
    out = tmp_path / "j.ezpl"
    note = rz.send(data, str(out), M2, (0.1, -0.02))
    assert "1 labels" in note and out.read_bytes() == b"^R10\r~Q-2\r" + data
    rz.send(data, str(out))
    assert out.read_bytes() == data


def test_render_text_refuses_to_clip_and_ruler_overlays():
    font = rz.find_font("Arial", "DejaVu Sans")
    if font is None:
        pytest.skip("no Arial / DejaVu Sans on this machine")
    m = rz.PANDUIT_S150X225VATY_2UP
    im = rz.render_text((450, 225), ["PP.EXAMPLE:1.ROOM.R6202/A.U31.S1.A1"] * 6, font, 22)
    assert im.size == (450, 225) and im.getextrema() == (0, 255)
    with pytest.raises(ValueError):
        rz.render_text((100, 225), ["PP.EXAMPLE:1.ROOM.R6202/A.U31.S1.A1"], font, 22)
    small = rz.render_text((300, 40), ["PP.EXAMPLE:1.ROOM.R6202/A.U31.S1.A1"], font, 22, fit=True)
    assert small.size == (300, 40) and small.getextrema() == (0, 255)
    up = rz.render_text((60, 24), ["A"], font, 14, align="left", margin=2)
    down = rz.render_text((60, 24), ["A"], font, 14, align="left", margin=2, rotate180=True)
    assert down.tobytes() == up.rotate(180).tobytes()
    data = rz.job(m, [[im, im]])
    page = rz.decode_block(rz.parse_blocks(data)[0][1], m)
    plain = rz.ruler_job(m, font)
    over = rz.ruler_job(m, font, page)
    assert rz.count_labels(plain) == 1 and plain.startswith(b"^Q57,3\r^W83\r") and b"\rQ0,0,122,8\r" in plain
    from PIL import ImageChops

    dec_over = rz.decode_block(rz.parse_blocks(over)[0][1], m)
    dec_plain = rz.decode_block(rz.parse_blocks(plain)[0][1], m)
    assert dec_over.tobytes() == ImageChops.darker(dec_plain, page).tobytes()

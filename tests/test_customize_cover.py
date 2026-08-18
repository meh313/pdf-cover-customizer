"""Tests for customize_cover.py, run against the two real sample brochures."""

from __future__ import annotations

import datetime as dt
import hashlib
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import customize_cover as cc  # noqa: E402

SAMPLES = sorted((ROOT / "samples").glob("*.pdf"))


@pytest.fixture(params=SAMPLES, ids=lambda p: p.stem)
def sample(request) -> Path:
    return request.param


def _page_digest(page: pymupdf.Page) -> str:
    return hashlib.sha1(page.get_pixmap(dpi=72).samples).hexdigest()


def _ink_bbox(
    page: pymupdf.Page, clip: pymupdf.Rect, threshold: int = 140, scale: int = 3
) -> tuple[float, float, float, float]:
    """Bounding box of the light pixels inside `clip`, in page points."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
    xs, ys = [], []
    for row in range(pix.height):
        for col in range(pix.width):
            if min(pix.pixel(col, row)[:3]) >= threshold:
                xs.append(col)
                ys.append(row)
    assert xs, "no ink found in clip"
    return (
        clip.x0 + min(xs) / scale,
        clip.y0 + min(ys) / scale,
        clip.x0 + (max(xs) + 1) / scale,
        clip.y0 + (max(ys) + 1) / scale,
    )


def _image_digests(doc: pymupdf.Document, page_no: int) -> list[str]:
    out = []
    for image in doc[page_no].get_images(full=True):
        data = doc.extract_image(image[0])
        out.append(hashlib.sha1(data["image"]).hexdigest())
    return sorted(out)


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_resolve_date_reformats_known_layouts():
    assert cc.resolve_date("2026-08-18") == "18/08/2026"
    assert cc.resolve_date("18/08/2026") == "18/08/2026"
    assert cc.resolve_date("18.08.2026") == "18/08/2026"
    assert cc.resolve_date("2026-08-18", "%d %B %Y").startswith("18 August")


def test_resolve_date_keeps_custom_text_verbatim():
    assert cc.resolve_date("Paris, le 18 aout 2026") == "Paris, le 18 aout 2026"


def test_resolve_date_defaults_to_today():
    assert cc.resolve_date(None) == dt.date.today().strftime("%d/%m/%Y")
    assert cc.resolve_date("   ") == dt.date.today().strftime("%d/%m/%Y")


def test_resolve_year_prefers_explicit_value():
    assert cc.resolve_year(2030, "18/08/2026") == "2030"
    assert cc.resolve_year("2030", "18/08/2026") == "2030"


def test_resolve_year_falls_back_to_the_date():
    assert cc.resolve_year(None, "18/08/2026") == "2026"
    assert cc.resolve_year(None, "no date here") == str(dt.date.today().year)


def test_resolve_year_rejects_junk():
    with pytest.raises(cc.CoverError):
        cc.resolve_year("deux mille", "18/08/2026")


def test_parse_color_accepts_hex_and_triples():
    assert cc._parse_color("#FFFFFF") == (1.0, 1.0, 1.0)
    assert cc._parse_color("0,0,0") == (0.0, 0.0, 0.0)
    r, g, b = cc._parse_color("255,128,0")
    assert (round(r, 3), round(g, 3), round(b, 3)) == (1.0, 0.502, 0.0)


def test_base14_mapping():
    assert cc._base14("Arial-BoldMT") == "hebo"
    assert cc._base14("ArialMT") == "helv"
    assert cc._base14("Arial-BoldItalicMT") == "hebi"


def test_default_output_path_sanitises_the_client():
    out = cc.default_output_path(Path("/tmp/brochure.pdf"), "A/B: Corp")
    assert out.name == "brochure - A_B_ Corp.pdf"


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def test_layout_detection(sample):
    doc = pymupdf.open(sample)
    layout = cc.detect_layout(doc[0])

    assert layout.band_rect.width > 300
    assert layout.free_height >= cc.MIN_FREE_HEIGHT
    assert layout.title_ink_bottom < layout.band_rect.y1
    # The title band is dark, so the title ink is near-white.
    assert min(layout.title_color) > 0.8

    assert layout.year is not None
    assert layout.year.text == "2026"
    assert layout.year.font_size > 40
    # Tight tracking: the advance is narrower than the glyph box.
    assert 0 < layout.year.advance < layout.year.font_size * 0.556
    doc.close()


# --------------------------------------------------------------------------
# End-to-end
# --------------------------------------------------------------------------


def test_cover_is_stamped(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(
        sample, "ArcelorMittal", output_path=out, date="2027-03-04", year=2027
    )

    doc = pymupdf.open(out)
    text = doc[0].get_text("text")
    assert "ArcelorMittal" in text
    assert "04/03/2027" in text
    assert "2027" in text
    assert "2026" not in text  # old year removed, not just painted over
    doc.close()


def test_client_sits_between_title_and_band_bottom(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, "Safran Tech", output_path=out, date="2026-08-18")

    source = pymupdf.open(sample)
    layout = cc.detect_layout(source[0])
    source.close()

    doc = pymupdf.open(out)
    hits = doc[0].search_for("Safran Tech")
    assert len(hits) == 1
    box = hits[0]
    assert box.y0 >= layout.title_ink_bottom
    assert box.y1 <= layout.band_rect.y1
    # Right-aligned on the title.
    assert abs(box.x1 - layout.title_right) < 2.0
    doc.close()


def test_date_sits_bottom_left(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, "Valeo", output_path=out, date="18/08/2026")

    doc = pymupdf.open(out)
    page = doc[0]
    hits = page.search_for("18/08/2026")
    assert len(hits) == 1
    box = hits[0]
    assert box.x0 < page.rect.width * 0.25
    assert box.y0 > page.rect.height * 0.90
    doc.close()


def test_new_year_lands_on_the_old_year(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, "Valeo", output_path=out, year=2031)

    source = pymupdf.open(sample)
    old = cc.detect_layout(source[0]).year
    source.close()

    doc = pymupdf.open(out)
    new = cc._find_year_span(doc[0])
    assert new is not None and new.text == "2031"

    # Same baseline, same glyph grid: identical font, size and tracking. The
    # reported line box differs, because the base-14 Helvetica substituted for
    # the (non-embedded) Arial declares different ascender/descender metrics.
    assert abs(new.baseline_y - old.baseline_y) < 0.1
    assert abs(new.font_size - old.font_size) < 0.1
    assert len(new.origins_x) == len(old.origins_x)
    for got, want in zip(new.origins_x, old.origins_x):
        assert abs(got - want) < 0.6
    doc.close()


def test_year_ink_stays_inside_the_diamond(tmp_path, sample):
    """The redrawn year must occupy the same pixels, not just the same box."""
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, "Valeo", output_path=out, year=2031)

    source = pymupdf.open(sample)
    box = cc.detect_layout(source[0]).year.rect
    clip = pymupdf.Rect(box.x0 - 12, box.y0 - 12, box.x1 + 12, box.y1 + 12)
    old_ink = _ink_bbox(source[0], clip)
    source.close()

    doc = pymupdf.open(out)
    new_ink = _ink_bbox(doc[0], clip)
    doc.close()

    # Digits are all the same width in Helvetica, so the block must line up.
    for got, want in zip(new_ink, old_ink):
        assert abs(got - want) <= 1.5


def test_rest_of_the_document_is_untouched(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, "Doctolib", output_path=out, date="18/08/2026")

    source = pymupdf.open(sample)
    result = pymupdf.open(out)
    assert result.page_count == source.page_count

    for page_no in range(1, source.page_count):
        assert source[page_no].get_text("text") == result[page_no].get_text("text")
        assert _image_digests(source, page_no) == _image_digests(result, page_no)
        assert _page_digest(source[page_no]) == _page_digest(result[page_no])

    source.close()
    result.close()


def test_page_one_artwork_survives(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, "Exotec", output_path=out, year=2026)

    source = pymupdf.open(sample)
    result = pymupdf.open(out)
    # Photos, logo and background must come through byte-identical, and the
    # vector shapes must all still be there.
    assert _image_digests(source, 0) == _image_digests(result, 0)
    assert len(result[0].get_drawings()) == len(source[0].get_drawings())
    source.close()
    result.close()


def test_source_file_is_never_modified(tmp_path, sample):
    before = hashlib.sha1(sample.read_bytes()).hexdigest()
    cc.customize_cover(sample, "Limagrain", output_path=tmp_path / "out.pdf")
    assert hashlib.sha1(sample.read_bytes()).hexdigest() == before


def test_keep_year_leaves_the_diamond_alone(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(
        sample, "Bertin", output_path=out, year=2029, update_year=False
    )
    doc = pymupdf.open(out)
    text = doc[0].get_text("text")
    assert "2026" in text
    assert "2029" not in text
    doc.close()


def test_long_client_name_is_shrunk_to_fit(tmp_path, sample):
    name = "Austrian Institute of Technology and Applied Sciences GmbH"
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, name, output_path=out)

    source = pymupdf.open(sample)
    layout = cc.detect_layout(source[0])
    source.close()

    doc = pymupdf.open(out)
    hits = doc[0].search_for(name)
    assert len(hits) == 1
    assert hits[0].x0 >= layout.text_left_limit
    assert hits[0].x1 <= layout.title_right + 0.5
    doc.close()


def test_accented_client_name(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, "Societe Generale Cote d'Azur", output_path=out)
    doc = pymupdf.open(out)
    assert "Societe Generale" in doc[0].get_text("text")
    doc.close()


def test_default_output_name(tmp_path, sample):
    work = tmp_path / sample.name
    work.write_bytes(sample.read_bytes())
    out = cc.customize_cover(work, "Enlaps")
    assert out.parent == work.parent
    assert out.name.endswith(" - Enlaps.pdf")
    assert out.is_file()


def test_refuses_to_overwrite_without_force(tmp_path, sample):
    out = tmp_path / "out.pdf"
    cc.customize_cover(sample, "XXII", output_path=out)
    with pytest.raises(cc.CoverError, match="already exists"):
        cc.customize_cover(sample, "XXII", output_path=out)
    cc.customize_cover(sample, "XXII", output_path=out, overwrite=True)


def test_refuses_to_overwrite_the_source(tmp_path, sample):
    with pytest.raises(cc.CoverError, match="overwrite the source"):
        cc.customize_cover(sample, "XXII", output_path=sample)


def test_refuses_an_empty_client(tmp_path, sample):
    with pytest.raises(cc.CoverError, match="client name is empty"):
        cc.customize_cover(sample, "   ", output_path=tmp_path / "out.pdf")


def test_missing_input(tmp_path):
    with pytest.raises(cc.CoverError, match="not found"):
        cc.customize_cover(tmp_path / "nope.pdf", "X", output_path=tmp_path / "o.pdf")


def test_refuses_an_already_stamped_pdf(tmp_path, sample):
    once = tmp_path / "once.pdf"
    cc.customize_cover(sample, "Ochy", output_path=once)
    with pytest.raises(cc.CoverError, match="already stamped"):
        cc.customize_cover(once, "Tracab", output_path=tmp_path / "twice.pdf")


def test_rejects_a_pdf_without_the_cover(tmp_path):
    plain = tmp_path / "plain.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(plain)
    doc.close()
    with pytest.raises(cc.CoverError, match="no cover title"):
        cc.customize_cover(plain, "X", output_path=tmp_path / "out.pdf")


def test_preview_png_is_written(tmp_path, sample):
    png = tmp_path / "preview.png"
    cc.customize_cover(
        sample, "Akanthas", output_path=tmp_path / "out.pdf", preview_path=png
    )
    assert png.is_file() and png.stat().st_size > 1000


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_happy_path(tmp_path, sample, capsys):
    out = tmp_path / "cli.pdf"
    code = cc.main(
        [str(sample), "--client", "Probayes", "--date", "2028-12-01", "-o", str(out)]
    )
    assert code == 0
    assert out.is_file()
    assert str(out) in capsys.readouterr().out

    doc = pymupdf.open(out)
    text = doc[0].get_text("text")
    assert "Probayes" in text and "01/12/2028" in text and "2028" in text
    doc.close()


def test_cli_date_prefix_and_format(tmp_path, sample):
    out = tmp_path / "cli.pdf"
    assert (
        cc.main(
            [
                str(sample),
                "-c",
                "Mycronic",
                "-d",
                "2026-08-18",
                "--date-format",
                "%d %b %Y",
                "--date-prefix",
                "Le ",
                "-o",
                str(out),
            ]
        )
        == 0
    )
    doc = pymupdf.open(out)
    assert "Le 18 Aug 2026" in doc[0].get_text("text")
    doc.close()


def test_cli_reports_errors(tmp_path, capsys):
    assert cc.main([str(tmp_path / "nope.pdf"), "-c", "X"]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_help_renders(capsys):
    """--help must not blow up on the '%' in the default date format."""
    with pytest.raises(SystemExit) as exit_info:
        cc.main(["--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "--date-format" in out and "%d/%m/%Y" in out

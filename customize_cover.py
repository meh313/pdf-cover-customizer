#!/usr/bin/env python3
"""Stamp client name, date and cover year onto page 1 of an Infoscribe references PDF.

The script only touches the first page. Every other page is copied through
untouched, and the original file is never modified in place.

Three things are written on the cover:

1. the client name, right under the cover title ("Our references / client
   testimonials", "Nos references clients", ...), inside the dark title band;
2. the run date (or any custom date string), bottom-left of the page;
3. the large year shown in the dark diamond, replaced by the requested year.

Geometry is auto-detected from the page itself (dark title band, title ink,
year text span), so the script keeps working if the template shifts slightly.
Every detected value can still be overridden from the command line.

Usage:
    python customize_cover.py input.pdf --client "ArcelorMittal"
    python customize_cover.py input.pdf -c "Valeo" -d 2026-09-30 -y 2027 -o out.pdf

Requires PyMuPDF (see requirements.txt).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import pymupdf

__version__ = "1.0.0"

# --------------------------------------------------------------------------
# Tunables. Ratios are relative to the detected geometry, not hard-coded
# points, so a slightly different export of the same template still works.
# --------------------------------------------------------------------------

#: Minimum font size (pt) for a text span to be considered part of the title.
TITLE_MIN_FONT_SIZE = 18.0

#: A fill is "dark" (candidate title band) below this relative luminance.
BAND_MAX_LUMINANCE = 0.30

#: Fraction of the free space under the title used as the client font size.
CLIENT_SIZE_RATIO = 0.38
CLIENT_SIZE_MIN = 9.0
CLIENT_SIZE_MAX = 20.0

#: Below this much free space under the title, refuse to write. Re-running the
#: script on an already-stamped PDF is the usual cause.
MIN_FREE_HEIGHT = 16.0

#: Cap-height / descender factors for Helvetica, used to centre the baseline.
CAP_HEIGHT_RATIO = 0.716
DESCENDER_RATIO = 0.212

#: Left inset inside the title band, as a fraction of the band width. The band
#: has a slanted left edge, so text must not start at the raw bounding box.
BAND_LEFT_INSET_RATIO = 0.07

#: Points trimmed off the band edges before scanning for title ink, so the
#: antialiased border between the band and the page background is not read as
#: ink and does not drag the detected title bottom down to the band bottom.
INK_SCAN_INSET = 1.5

#: Date block position, as a fraction of the page size.
DATE_LEFT_RATIO = 0.075
DATE_BASELINE_RATIO = 0.964
DATE_FONT_SIZE = 11.0

#: Infoscribe navy, used for the date when it sits on a light background.
BRAND_NAVY = (0.047, 0.184, 0.384)
LIGHT_INK = (0.945, 0.945, 0.945)

#: Date input formats accepted before falling back to "use the string as-is".
DATE_INPUT_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d %B %Y",
    "%d %b %Y",
)

DEFAULT_DATE_FORMAT = "%d/%m/%Y"

YEAR_RE = re.compile(r"^(1[5-9]|2[0-9])\d{2}$")


class CoverError(RuntimeError):
    """Raised when the cover cannot be located or written."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _luminance(rgb: Sequence[float]) -> float:
    r, g, b = rgb[0], rgb[1], rgb[2]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _int_to_rgb(value: int) -> tuple[float, float, float]:
    """Convert a PyMuPDF sRGB integer (0xRRGGBB) to a 0..1 triple."""
    return (
        ((value >> 16) & 0xFF) / 255.0,
        ((value >> 8) & 0xFF) / 255.0,
        (value & 0xFF) / 255.0,
    )


def _parse_color(text: str) -> tuple[float, float, float]:
    """Parse '#RRGGBB', 'RRGGBB' or 'r,g,b' (0..1 or 0..255) into a triple."""
    raw = text.strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", raw):
        return _int_to_rgb(int(raw, 16))
    parts = [p for p in re.split(r"[,\s]+", raw) if p]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"cannot read color {text!r}")
    values = [float(p) for p in parts]
    if any(v > 1.0 for v in values):
        values = [v / 255.0 for v in values]
    return (values[0], values[1], values[2])


def _base14(font_name: str) -> str:
    """Map an embedded font name onto the matching base-14 PyMuPDF alias.

    Helvetica is metrically identical to Arial, which is what this template
    uses, so the substitution is invisible.
    """
    lowered = font_name.lower()
    bold = "bold" in lowered or "black" in lowered or "heavy" in lowered
    italic = "italic" in lowered or "oblique" in lowered
    if bold and italic:
        return "hebi"
    if bold:
        return "hebo"
    if italic:
        return "heit"
    return "helv"


def _text_width(text: str, fontname: str, fontsize: float) -> float:
    return pymupdf.get_text_length(text, fontname=fontname, fontsize=fontsize)


# --------------------------------------------------------------------------
# Page-1 geometry detection
# --------------------------------------------------------------------------


@dataclass
class YearSpan:
    """The large year printed in the dark diamond."""

    text: str
    rect: pymupdf.Rect
    baseline_y: float
    font_size: float
    font_name: str
    color: tuple[float, float, float]
    origins_x: list[float] = field(default_factory=list)

    @property
    def advance(self) -> float:
        """Horizontal advance between two consecutive glyphs, in points.

        Derived from the real glyph origins so the negative character spacing
        used by the template (Tc = -1.92) is reproduced exactly.
        """
        if len(self.origins_x) >= 2:
            return (self.origins_x[-1] - self.origins_x[0]) / (len(self.origins_x) - 1)
        return _text_width("0", _base14(self.font_name), self.font_size)

    @property
    def last_glyph_advance(self) -> float:
        n = max(len(self.origins_x), 1)
        return self.rect.width - self.advance * (n - 1)


@dataclass
class CoverLayout:
    """Everything the stamping step needs to know about page 1."""

    page_rect: pymupdf.Rect
    band_rect: pymupdf.Rect
    title_right: float
    title_ink_bottom: float
    title_color: tuple[float, float, float]
    year: YearSpan | None

    @property
    def free_height(self) -> float:
        return self.band_rect.y1 - self.title_ink_bottom

    @property
    def text_left_limit(self) -> float:
        return self.band_rect.x0 + BAND_LEFT_INSET_RATIO * self.band_rect.width


def _title_spans(page: pymupdf.Page) -> list[dict]:
    """Large text spans sitting in the lower half of the cover."""
    mid_y = page.rect.y0 + page.rect.height * 0.45
    spans: list[dict] = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if span["size"] < TITLE_MIN_FONT_SIZE:
                    continue
                if not span["text"].strip():
                    continue
                if span["bbox"][1] < mid_y:
                    continue
                spans.append(span)
    return spans


def _find_band(page: pymupdf.Page, spans: Sequence[dict]) -> pymupdf.Rect:
    """Smallest dark filled shape enclosing the title text."""
    centres = [
        pymupdf.Point(
            (s["bbox"][0] + s["bbox"][2]) / 2, (s["bbox"][1] + s["bbox"][3]) / 2
        )
        for s in spans
    ]
    candidates: list[pymupdf.Rect] = []
    for drawing in page.get_drawings():
        fill = drawing.get("fill")
        if fill is None or _luminance(fill) > BAND_MAX_LUMINANCE:
            continue
        rect = pymupdf.Rect(drawing["rect"])
        if rect.width < page.rect.width * 0.3 or rect.height < 30:
            continue
        if not any(rect.contains(point) for point in centres):
            continue
        candidates.append(rect)
    if not candidates:
        raise CoverError("no dark title band found around the title on page 1")
    return min(candidates, key=lambda r: r.get_area())


def _ink_bottom(
    page: pymupdf.Page, clip: pymupdf.Rect, threshold: float = 0.55, scale: int = 3
) -> float | None:
    """Lowest y (points) inside `clip` that still carries light-coloured ink.

    The second title line is a rasterised image in this template, so its real
    glyph bottom cannot be read from the text layer. Rendering the band and
    looking for bright pixels handles text and image lines the same way.
    """
    clip = clip & page.rect
    if clip.is_empty:
        return None
    pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False)
    if pix.n < 3:
        return None
    cutoff = int(threshold * 255)
    samples = pix.samples
    stride, n = pix.stride, pix.n
    for row in range(pix.height - 1, -1, -1):
        base = row * stride
        for col in range(pix.width):
            off = base + col * n
            if (
                samples[off] >= cutoff
                and samples[off + 1] >= cutoff
                and samples[off + 2] >= cutoff
            ):
                return clip.y0 + (row + 1) / scale
    return None


def _find_year_span(page: pymupdf.Page) -> YearSpan | None:
    """The biggest text span on the page that reads like a year."""
    best: YearSpan | None = None
    for block in page.get_text("rawdict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                chars = span.get("chars", [])
                text = "".join(c["c"] for c in chars).strip()
                if not YEAR_RE.match(text):
                    continue
                candidate = YearSpan(
                    text=text,
                    rect=pymupdf.Rect(span["bbox"]),
                    baseline_y=chars[0]["origin"][1],
                    font_size=span["size"],
                    font_name=span["font"],
                    color=_int_to_rgb(span["color"]),
                    origins_x=[c["origin"][0] for c in chars],
                )
                if best is None or candidate.font_size > best.font_size:
                    best = candidate
    return best


def detect_layout(page: pymupdf.Page) -> CoverLayout:
    """Read page 1 and work out where the new content belongs."""
    spans = _title_spans(page)
    if not spans:
        raise CoverError(
            "no cover title found on page 1 - is this the references template?"
        )
    band = _find_band(page, spans) & page.rect
    title_right = max(s["bbox"][2] for s in spans)
    title_top = min(s["bbox"][1] for s in spans)
    title_color = _int_to_rgb(max(spans, key=lambda s: s["size"])["color"])

    scan = pymupdf.Rect(
        min(s["bbox"][0] for s in spans),
        title_top,
        band.x1 - INK_SCAN_INSET,
        band.y1 - INK_SCAN_INSET,
    )
    ink_bottom = _ink_bottom(page, scan)
    if ink_bottom is None or ink_bottom >= band.y1 - INK_SCAN_INSET:
        ink_bottom = max(s["bbox"][3] for s in spans)

    return CoverLayout(
        page_rect=pymupdf.Rect(page.rect),
        band_rect=band,
        title_right=title_right,
        title_ink_bottom=ink_bottom,
        title_color=title_color,
        year=_find_year_span(page),
    )


# --------------------------------------------------------------------------
# Stamping
# --------------------------------------------------------------------------


def _replace_year(page: pymupdf.Page, span: YearSpan, new_year: str) -> None:
    """Erase the old year and redraw the new one in the same place.

    The glyphs are placed one by one on the original advance grid, which keeps
    the template's tight letter spacing. Redaction removes the old characters
    from the content stream instead of merely covering them, so the old year
    does not survive in a text extraction.
    """
    if new_year == span.text:
        return

    page.add_redact_annot(span.rect + (-1, -1, 1, 1), fill=False)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )

    advance = span.advance
    width = advance * (len(new_year) - 1) + span.last_glyph_advance
    left = (span.rect.x0 + span.rect.x1) / 2 - width / 2
    fontname = _base14(span.font_name)
    for index, glyph in enumerate(new_year):
        page.insert_text(
            (left + index * advance, span.baseline_y),
            glyph,
            fontname=fontname,
            fontsize=span.font_size,
            color=span.color,
        )


def _fit_font_size(text: str, fontname: str, start: float, max_width: float) -> float:
    size = start
    while size > CLIENT_SIZE_MIN and _text_width(text, fontname, size) > max_width:
        size -= 0.25
    return size


def _draw_client(
    page: pymupdf.Page,
    layout: CoverLayout,
    client: str,
    *,
    font_size: float | None,
    bold: bool,
    color: tuple[float, float, float] | None,
) -> float:
    """Right-align the client name in the free space under the title."""
    fontname = "hebo" if bold else "helv"
    free = layout.free_height
    if free < MIN_FREE_HEIGHT:
        raise CoverError(
            f"only {free:.1f} pt left under the title (need {MIN_FREE_HEIGHT:.0f} pt) "
            "- this PDF looks like it was already stamped; start from the original"
        )

    size = font_size or min(
        max(free * CLIENT_SIZE_RATIO, CLIENT_SIZE_MIN), CLIENT_SIZE_MAX
    )
    max_width = layout.title_right - layout.text_left_limit
    size = _fit_font_size(client, fontname, size, max_width)

    cap = CAP_HEIGHT_RATIO * size
    baseline = layout.title_ink_bottom + (free - cap) / 2 + cap
    # Keep the descenders off the band edge if the name is unusually tall.
    baseline = min(baseline, layout.band_rect.y1 - DESCENDER_RATIO * size)

    width = _text_width(client, fontname, size)
    page.insert_text(
        (layout.title_right - width, baseline),
        client,
        fontname=fontname,
        fontsize=size,
        color=color or layout.title_color,
    )
    return size


def _draw_date(
    page: pymupdf.Page,
    layout: CoverLayout,
    date_text: str,
    *,
    font_size: float,
    bold: bool,
    color: tuple[float, float, float] | None,
) -> tuple[float, float]:
    """Write the date at the bottom-left, picking readable ink automatically."""
    fontname = "hebo" if bold else "helv"
    x = layout.page_rect.x0 + DATE_LEFT_RATIO * layout.page_rect.width
    baseline = layout.page_rect.y0 + DATE_BASELINE_RATIO * layout.page_rect.height

    if color is None:
        probe = pymupdf.Rect(x, baseline - font_size, x + 120, baseline + 4) & page.rect
        pix = page.get_pixmap(clip=probe, alpha=False)
        background = _luminance([c / 255.0 for c in pix.color_topusage()[1][:3]])
        color = BRAND_NAVY if background > 0.5 else LIGHT_INK

    page.insert_text(
        (x, baseline),
        date_text,
        fontname=fontname,
        fontsize=font_size,
        color=color,
    )
    return x, baseline


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def resolve_date(value: str | None, date_format: str = DEFAULT_DATE_FORMAT) -> str:
    """Turn the --date argument into the string printed on the cover.

    A recognised date is reformatted with `date_format`; anything else is kept
    verbatim, so callers can print "Paris, le 18 aout 2026" if they want to.
    """
    if value is None or not value.strip():
        return _dt.date.today().strftime(date_format)
    text = value.strip()
    for fmt in DATE_INPUT_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt).date().strftime(date_format)
        except ValueError:
            continue
    return text


def resolve_year(value: str | int | None, date_text: str) -> str:
    """Year to print in the diamond: explicit, else read off the date."""
    if value is not None:
        year = str(value).strip()
        if not year.isdigit():
            raise CoverError(f"year must be numeric, got {year!r}")
        return year
    match = re.search(r"\b(1[5-9]\d{2}|2[0-9]\d{2})\b", date_text)
    if match:
        return match.group(1)
    return str(_dt.date.today().year)


def default_output_path(input_path: Path, client: str) -> Path:
    """`<input> - <client>.pdf`, next to the input file."""
    safe = re.sub(r'[\\/:*?"<>|]+', "_", client).strip(" .") or "client"
    return input_path.with_name(f"{input_path.stem} - {safe}.pdf")


def customize_cover(
    input_path: str | Path,
    client: str,
    *,
    output_path: str | Path | None = None,
    date: str | None = None,
    date_format: str = DEFAULT_DATE_FORMAT,
    date_prefix: str = "",
    year: str | int | None = None,
    update_year: bool = True,
    client_font_size: float | None = None,
    client_bold: bool = True,
    client_color: tuple[float, float, float] | None = None,
    date_font_size: float = DATE_FONT_SIZE,
    date_bold: bool = False,
    date_color: tuple[float, float, float] | None = None,
    preview_path: str | Path | None = None,
    overwrite: bool = False,
    verbose: bool = False,
) -> Path:
    """Write a copy of `input_path` with the cover filled in. Returns the path."""
    src = Path(input_path).expanduser().resolve()
    if not src.is_file():
        raise CoverError(f"input PDF not found: {src}")
    if not client.strip():
        raise CoverError("client name is empty")

    out = (
        Path(output_path).expanduser().resolve()
        if output_path
        else default_output_path(src, client)
    )
    if out == src:
        raise CoverError("output would overwrite the source PDF - pass --output")
    if out.exists() and not overwrite:
        raise CoverError(f"{out.name} already exists - pass --force to replace it")

    date_text = date_prefix + resolve_date(date, date_format)
    year_text = resolve_year(year, date_text)

    doc = pymupdf.open(src)
    try:
        if doc.needs_pass:
            raise CoverError("encrypted PDF - decrypt it before running this script")
        if doc.page_count == 0:
            raise CoverError("PDF has no pages")

        page = doc[0]
        layout = detect_layout(page)
        if verbose:
            _report(layout, client, date_text, year_text)

        if update_year:
            if layout.year is None:
                print(
                    "warning: no year found on the cover, skipping the year update",
                    file=sys.stderr,
                )
            else:
                _replace_year(page, layout.year, year_text)

        used_size = _draw_client(
            page,
            layout,
            client.strip(),
            font_size=client_font_size,
            bold=client_bold,
            color=client_color,
        )
        _draw_date(
            page,
            layout,
            date_text,
            font_size=date_font_size,
            bold=date_bold,
            color=date_color,
        )
        if verbose:
            print(f"  client font size : {used_size:.2f} pt")

        out.parent.mkdir(parents=True, exist_ok=True)
        doc.save(out, garbage=4, deflate=True, clean=True)

        if preview_path:
            preview = Path(preview_path).expanduser().resolve()
            preview.parent.mkdir(parents=True, exist_ok=True)
            doc[0].get_pixmap(matrix=pymupdf.Matrix(2, 2)).save(preview)
    finally:
        doc.close()

    return out


def _report(layout: CoverLayout, client: str, date_text: str, year_text: str) -> None:
    print("detected cover geometry")
    print(f"  page             : {layout.page_rect}")
    print(f"  title band       : {layout.band_rect}")
    print(f"  title right edge : {layout.title_right:.2f}")
    print(f"  title ink bottom : {layout.title_ink_bottom:.2f}")
    print(f"  free height      : {layout.free_height:.2f} pt")
    if layout.year:
        print(
            f"  year span        : {layout.year.text!r} at {layout.year.rect} "
            f"({layout.year.font_name} {layout.year.font_size:.2f} pt, "
            f"advance {layout.year.advance:.2f} pt)"
        )
    else:
        print("  year span        : none")
    print(f"  client           : {client}")
    print(f"  date             : {date_text}")
    print(f"  year             : {year_text}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="customize_cover.py",
        description=(
            "Add the client name, a date and the cover year to page 1 of an "
            "Infoscribe references / testimonials PDF."
        ),
        epilog=(
            "examples:\n"
            '  python customize_cover.py brochure.pdf -c "ArcelorMittal"\n'
            '  python customize_cover.py brochure.pdf -c "Valeo" -d 30/09/2026\n'
            '  python customize_cover.py brochure.pdf -c "Safran Tech" -y 2027 '
            "-o out/safran.pdf\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pdf", type=Path, help="path to the PDF to customize")
    parser.add_argument(
        "-c", "--client", required=True, help="client name printed under the title"
    )
    parser.add_argument(
        "-d",
        "--date",
        help=(
            "date to print (default: today). A recognised date is reformatted "
            "with --date-format; any other text is printed verbatim."
        ),
    )
    parser.add_argument(
        "--date-format",
        default=DEFAULT_DATE_FORMAT,
        # argparse runs help strings through %-formatting, so '%' must be doubled.
        help="strftime format for parsed dates (default: "
        + DEFAULT_DATE_FORMAT.replace("%", "%%")
        + ")",
    )
    parser.add_argument(
        "--date-prefix", default="", help='text before the date, e.g. "Le "'
    )
    parser.add_argument(
        "-y",
        "--year",
        help="year shown in the diamond (default: the year of --date)",
    )
    parser.add_argument(
        "--keep-year", action="store_true", help="leave the cover year untouched"
    )
    parser.add_argument(
        "-o", "--output", type=Path, help='output PDF (default: "<input> - <client>.pdf")'
    )
    parser.add_argument(
        "-f", "--force", action="store_true", help="overwrite the output if it exists"
    )
    parser.add_argument(
        "--client-size", type=float, help="client font size in pt (default: automatic)"
    )
    parser.add_argument(
        "--client-regular",
        action="store_true",
        help="draw the client name in regular weight instead of bold",
    )
    parser.add_argument(
        "--client-color", type=_parse_color, help='client ink, e.g. "#F1F1F1"'
    )
    parser.add_argument(
        "--date-size", type=float, default=DATE_FONT_SIZE, help="date font size in pt"
    )
    parser.add_argument("--date-bold", action="store_true", help="draw the date in bold")
    parser.add_argument(
        "--date-color", type=_parse_color, help='date ink, e.g. "#0C2F62"'
    )
    parser.add_argument(
        "--preview", type=Path, help="also render page 1 to this PNG for a quick check"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print geometry")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        out = customize_cover(
            args.pdf,
            args.client,
            output_path=args.output,
            date=args.date,
            date_format=args.date_format,
            date_prefix=args.date_prefix,
            year=args.year,
            update_year=not args.keep_year,
            client_font_size=args.client_size,
            client_bold=not args.client_regular,
            client_color=args.client_color,
            date_font_size=args.date_size,
            date_bold=args.date_bold,
            date_color=args.date_color,
            preview_path=args.preview,
            overwrite=args.force,
            verbose=args.verbose,
        )
    except CoverError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

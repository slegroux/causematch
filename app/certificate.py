"""The certificate as a PDF: US Letter, the frozen signed snapshot, and a QR code to the
check link. Pure functions, no database and no request.

Text is set in Noto Sans (app/fonts, embedded as a subset). It covers Latin, Greek and
Cyrillic; a character the font has no glyph for (CJK, Armenian, Hangul...) is shown as
"?", decided from the font's own character map, so no name can raise.
"""

import re
import unicodedata
from datetime import datetime
from functools import cache
from pathlib import Path

import segno
from fontTools.ttLib import TTFont
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app import signing
from app.choices import minutes_label

QR_SIDE_MM = 36  # the symbol itself, quiet zone not counted
QR_QUIET_MODULES = 4
MARGIN = 20
LETTER = "Letter"
TOTAL_AND_QR_MM = 95  # room the last page needs for the total, the codes and the QR


def month_name(period):
    return datetime.strptime(period, "%Y-%m").strftime("%B %Y")


def ein_label(ein):
    digits = re.sub(r"\D", "", ein or "")
    return f"{digits[:2]}-{digits[2:]}" if len(digits) == 9 else (ein or "")


def long_date(iso):
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %-d, %Y")


def period_label(period):
    return "All hours" if period == "all" else month_name(period)


FONTS = Path(__file__).parent / "fonts"
FONT = "NotoSans"


@cache
def _glyphs():
    """The code points the font has a glyph for (Regular and Bold share a character set)."""
    return TTFont(FONTS / "NotoSans-Regular.ttf").getBestCmap()


def _plain(category):
    return category[0] in "LMNPS" or category == "Zs"


def pdf_text(value):
    """`value` with every character the font has no glyph for replaced by "?", and every
    one that is not a letter, mark, number, punctuation, symbol or space (control and
    format characters such as the bidirectional overrides, zero-width characters, line
    and paragraph separators)."""
    glyphs = _glyphs()
    return "".join(
        ch if ord(ch) in glyphs and _plain(unicodedata.category(ch)) else "?"
        for ch in str(value)
    )


def qr_matrix(url):
    """The QR code for `url` as rows of 0/1 (1 is dark): error correction M, no quiet zone
    (the renderer leaves one)."""
    return [list(row) for row in segno.make(url, error="m", micro=False).matrix]


def pdf_filename(snapshot):
    """causematch-hours-<nonprofit>-<period>.pdf, ASCII letters, digits and hyphens only."""
    slug = unicodedata.normalize("NFKD", snapshot["org"]["name"])
    slug = re.sub(r"[^a-z0-9]+", "-", slug.encode("ascii", "ignore").decode().lower())
    slug = slug.strip("-")[:40].strip("-") or "nonprofit"
    return f"causematch-hours-{slug}-{snapshot['period']}.pdf"


class _Page(FPDF):
    """FPDF with a footer on every page: the signature code and the page number."""

    code = ""

    def footer(self):
        self.set_y(-14)
        self.set_font(FONT, "", 8)
        self.set_text_color(90, 90, 90)
        self.cell(
            0,
            5,
            f"Signature code {self.code} - page {self.page_no()} of {{nb}}",
            align="C",
        )


def build_pdf(snapshot, cert_id, check_url, compress=True):
    """The certificate for `snapshot` (as stored and signed) with id `cert_id`, as PDF
    bytes. `check_url` is what the QR code encodes and what is printed and linked."""
    org = snapshot["org"]
    code = signing.code(signing.sign(snapshot, cert_id))
    pdf = _Page(unit="mm", format=LETTER)
    pdf.code = code
    pdf.alias_nb_pages()
    pdf.add_font(FONT, "", str(FONTS / "NotoSans-Regular.ttf"))
    pdf.add_font(FONT, "B", str(FONTS / "NotoSans-Bold.ttf"))
    pdf.set_compression(compress)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_auto_page_break(True, margin=MARGIN)
    pdf.set_title("Volunteer hours certificate")
    pdf.set_lang("en-US")
    pdf.set_producer("Causematch")
    pdf.set_creator("Causematch")
    pdf.add_page()
    width = pdf.w - 2 * MARGIN

    def line(text, size=11, style="", gap=1.5, link=None, color=(0, 0, 0)):
        pdf.set_font(FONT, style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(
            width,
            size * 0.45,
            pdf_text(text),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
            link=link,
        )
        pdf.ln(gap)

    line("Causematch", 10, "B", color=(90, 90, 90))
    line("Volunteer hours certificate", 22, "B", gap=4)
    line(snapshot["volunteer"], 16, "B", gap=1)
    line(f"volunteered with {org['name']}. The nonprofit confirmed these hours.")
    contact = [f"EIN {ein_label(org['ein'])}"] if org["ein"] else []
    if org["website"]:
        contact.append(org["website"])
    if contact:
        line(" - ".join(contact), 10, color=(90, 90, 90))
    line(period_label(snapshot["period"]), 12, "B", gap=3)

    pdf.set_font(FONT, "", 10)
    pdf.set_text_color(0, 0, 0)
    with pdf.table(
        col_widths=(35, 50, 15),
        text_align=("LEFT", "LEFT", "RIGHT"),
        line_height=6,
        width=width,
        borders_layout="HORIZONTAL_LINES",
    ) as table:
        table.row(("Date", "Role", "Hours"))
        for shift in snapshot["shifts"]:
            row = table.row()
            row.cell(pdf_text(long_date(shift["date"])))
            row.cell(pdf_text(shift["role"]))
            row.cell(pdf_text(minutes_label(shift["minutes"])))

    if pdf.will_page_break(TOTAL_AND_QR_MM):
        pdf.add_page()
    pdf.ln(4)
    line(f"Total: {minutes_label(snapshot['minutes'])}", 14, "B", gap=3)
    line(
        "Hours confirmed by " + ", ".join(snapshot["coordinators"]) + ".",
        10,
        color=(90, 90, 90),
    )
    line(f"Issued {long_date(snapshot['issued'])}", 10, color=(90, 90, 90))
    line(f"Signature code: {code}", 12, "B", gap=5)

    # The QR code: one filled square per dark module, four modules of white around it.
    matrix = qr_matrix(check_url)
    module = QR_SIDE_MM / len(matrix)
    quiet = QR_QUIET_MODULES * module
    if pdf.will_page_break(QR_SIDE_MM + 2 * quiet):
        pdf.add_page()
    left, top = pdf.l_margin + quiet, pdf.get_y() + quiet
    pdf.set_fill_color(0, 0, 0)
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                pdf.rect(left + c * module, top + r * module, module, module, "F")
    pdf.link(left, top, QR_SIDE_MM, QR_SIDE_MM, link=check_url)

    text_x = left + QR_SIDE_MM + quiet
    pdf.set_xy(text_x, top)
    pdf.set_font(FONT, "B", 10)
    pdf.set_text_color(0, 0, 0)
    pdf.multi_cell(
        pdf.w - MARGIN - text_x,
        5,
        pdf_text(check_url),
        new_x=XPos.LEFT,
        new_y=YPos.NEXT,
        link=check_url,
    )
    pdf.set_font(FONT, "", 10)
    pdf.multi_cell(
        pdf.w - MARGIN - text_x,
        5,
        "Anyone can check this certificate at the address above.",
    )
    return bytes(pdf.output())

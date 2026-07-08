"""
Form-output layer — fill the TTB F 5120.17 fillable PDF from computed figures.

The form is an AcroForm with named fields, so we fill by name:
  Part I  bulk    a1.N … f1.N        Part I bottled  a2.N … f2.N
  Part III spirits a3.N (proof gal)   Part IV grapes  a4.N (lbs) / concentrate d4.N (gal)
  cross-column totals  {part}.{line}Total
  header  MONTH, YEAR, OPERATED_BY, REGISTRY_NUMBER, EIN, Chk2 (version)

Values are strings (the form stores text). Gallon figures should already be rounded to
the tenth by the read-layer (27 CFR 24.281); we don't re-round here.
"""
import re
from collections import defaultdict

from pypdf import PdfReader, PdfWriter

_CELL = re.compile(r"^[a-f][1234]\.\d+$")
_TOTAL = re.compile(r"^[1234]\.\d+Total$")
_HEADER = ("MONTH", "YEAR", "OPERATED_BY", "REGISTRY_NUMBER", "EIN", "Chk2")

_PART3 = {"line_1_on_hand_beginning": "a3.1", "line_2_received": "a3.2", "line_4_total": "a3.4",
          "line_5_used": "a3.5", "line_9_on_hand_end": "a3.9", "line_10_total": "a3.10"}
_P4_GRAPE = {"line_1_on_hand_beginning": "a4.1", "line_2_received": "a4.2",
             "line_5_used_in_wine_production": "a4.5", "line_9_on_hand_end": "a4.9"}
_P4_CONC = {"line_1_on_hand_beginning": "d4.1", "line_2_received": "d4.2",
            "line_5_used_in_wine_production": "d4.5", "line_8_removed_destroyed": "d4.8",
            "line_9_on_hand_end": "d4.9"}


def _column_totals(part1):
    """Cross-column TOTAL fields for Part I: sum a..f per (section, line)."""
    agg = defaultdict(float)
    for k, v in part1.items():
        m = re.match(r"^[a-f]([12])\.(\d+)$", k)
        if m:
            agg[(m.group(1), m.group(2))] += float(v)
    return {f"{sec}.{line}Total": f"{tot:.2f}".rstrip("0").rstrip(".")
            for (sec, line), tot in agg.items()}


def build_field_values(part1, part3=None, part4=None, header=None):
    """Assemble the full {field_name: value} map for the form."""
    vals = {k: str(v) for k, v in part1.items()}
    vals.update(_column_totals(part1))
    if part3:
        vals.update({_PART3[k]: str(v) for k, v in part3.items() if k in _PART3})
        vals[f"3.4Total"] = str(part3.get("line_4_total", ""))
        vals[f"3.5Total"] = str(part3.get("line_5_used", ""))
        vals[f"3.10Total"] = str(part3.get("line_10_total", ""))
    if part4:
        vals.update({_P4_GRAPE[k]: str(v) for k, v in part4.get("grapes_lbs", {}).items() if k in _P4_GRAPE})
        vals.update({_P4_CONC[k]: str(v) for k, v in part4.get("concentrate_gal", {}).items() if k in _P4_CONC})
    if header:
        vals.update({k: str(v) for k, v in header.items()})
    return vals


def render_5120_17_pdf(template_path, output_path, *, part1, part3=None, part4=None, header=None):
    """Fill a blank 5120.17 template with the computed figures → fileable PDF.
    Clears any existing data cells in the template first, then writes the computed values."""
    reader = PdfReader(template_path)
    writer = PdfWriter()
    writer.append(reader)

    # clear existing data cells / totals so a reused template leaves no stale figures
    existing = reader.get_fields() or {}
    clear = {k: "" for k in existing if _CELL.match(k) or _TOTAL.match(k)}
    values = {**clear, **build_field_values(part1, part3, part4, header)}

    try:
        writer.set_need_appearances_writer(True)   # so viewers render the values
    except Exception:
        pass
    for page in writer.pages:
        writer.update_page_form_field_values(page, values, auto_regenerate=False)
    with open(output_path, "wb") as f:
        writer.write(f)
    return values


# ============================================================ 5000.24 excise
def _fdf(fields):
    def esc(s):
        return (str(s).replace("\\", "\\\\").replace("(", "\\(")
                .replace(")", "\\)").replace("\r", "\\r").replace("\n", "\\r"))
    body = "".join(f"<</T({esc(k)})/V({esc(v)})>>" for k, v in fields.items())
    return "%FDF-1.2\n1 0 obj<</FDF<</Fields[" + body + "]>>>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF"


def render_5000_24_pdf(template_path, output_path, *, net_tax, serial_number,
                       period_start, period_end, header=None, date_signed=None, title="Owner"):
    """Fill the TTB F 5000.24 excise return via pdftk (these PDFs trip pypdf's form
    reader). Line 10 (wine) carries the net tax — CBMA credit already applied by the
    excise engine — and flows to lines 17/19/21 and the payment amount."""
    import subprocess
    import tempfile
    net = f"{float(net_tax):.2f}"
    values = {
        "Serial_Number": serial_number, "Return_Covers": "PERIOD",
        "Beginning": period_start.strftime("%m/%d/%Y"),
        "Ending": period_end.strftime("%m/%d/%Y"),
        "Tax.10": net, "Tax.17": net, "Tax.18": "0.00",
        "Tax.19": net, "Tax.20": "0.00", "Tax.21": net,
        "Payment_Amount": net, "Title": title,
    }
    if date_signed:
        values["Date_On_Form"] = date_signed.strftime("%m/%d/%Y")
    if header:
        values.update(header)
    with tempfile.NamedTemporaryFile("w", suffix=".fdf", delete=False) as f:
        f.write(_fdf(values))
        fdf_path = f.name
    subprocess.run(["pdftk", template_path, "fill_form", fdf_path,
                    "output", output_path], check=True)
    return values


# ============================================================ CA Crush Report
def render_crush_report_pdf(rows, year, output_path, totals=None):
    """Generate a CA Grape Crush Report summary PDF from ca_crush_report() rows,
    grouped by pricing district with weighted price and Brix. (No CDFA fillable
    template was provided; this is a clean fileable-reference document + see CSV export.)"""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(output_path, pagesize=letter, topMargin=0.7 * inch)
    story = [Paragraph(f"California Grape Crush Report — {year}", styles["Title"]),
             Paragraph("St. Amant Winery · BW-CA-5526 · Districts 10 (Amador) & 11 (Lodi)",
                       styles["Normal"]), Spacer(1, 16)]

    header = ["District", "Variety", "Tons Crushed", "Purchased Tons", "Avg $/Ton", "Avg Brix"]
    data = [header]
    cur_district = None
    for r in rows:
        if r["district"] != cur_district:
            cur_district = r["district"]
        data.append([
            str(r["district"] or "—"), r["variety"], f"{r['tons']:.3f}",
            f"{r['purchased_tons']:.3f}" if r["purchased_tons"] else "—",
            f"${r['avg_price_per_ton']:.2f}" if r["avg_price_per_ton"] else "estate",
            f"{r['avg_brix']:.1f}" if r["avg_brix"] else "—",
        ])
    if totals:
        for dist, tons in sorted(totals["by_district"].items(), key=lambda kv: kv[0] or 0):
            data.append([f"District {dist}", "SUBTOTAL", f"{tons:.3f}", "", "", ""])
        data.append(["", "GRAND TOTAL", f"{totals['grand_total_tons']:.3f}", "", "", ""])

    t = Table(data, repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#95974E")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6EED4")]),
    ]))
    story.append(t)
    doc.build(story)
    return output_path


def crush_report_csv(rows, output_path):
    import csv
    with open(output_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["district", "variety", "tons", "purchased_tons", "avg_price_per_ton", "avg_brix"])
        for r in rows:
            w.writerow([r["district"], r["variety"], r["tons"], r["purchased_tons"],
                        r["avg_price_per_ton"] or "", r["avg_brix"] or ""])
    return output_path

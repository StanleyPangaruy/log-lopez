import calendar
import os
from datetime import date, datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ["TABLE_NAME"]
REPORTS_BUCKET = os.environ["REPORTS_BUCKET"]
table = dynamodb.Table(TABLE_NAME)

# The approving official is fixed for every employee's report.
MAYOR_NAME = "Hon. ISAIAS B. UBANA II, PhD"
MAYOR_TITLE = "Municipal Mayor"

LOGO_PATH = os.path.join(os.path.dirname(__file__), "lopez.png")


def _period_for_trigger(trigger: str):
    """Period for the two scheduled triggers only — on-demand generation
    (trigger "period") passes its own explicit start/end instead, since the
    caller picks which period to target."""
    today = date.today()
    if trigger == "day16":
        start = today.replace(day=1)
        end = today.replace(day=15)
    elif trigger == "day1":
        if today.month == 1:
            year, month = today.year - 1, 12
        else:
            year, month = today.year, today.month - 1
        last_day = calendar.monthrange(year, month)[1]
        start = date(year, month, 16)
        end = date(year, month, last_day)
    else:
        raise ValueError(f"Unknown trigger: {trigger!r}")
    return start, end


def _list_all_user_ids():
    user_ids = []
    scan_kwargs = {
        "FilterExpression": Attr("pk").begins_with("PROFILE#"),
        "ProjectionExpression": "userId",
    }
    while True:
        resp = table.scan(**scan_kwargs)
        user_ids.extend(item["userId"] for item in resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return user_ids


def _fetch_profile(user_id: str):
    return table.get_item(Key={"pk": f"PROFILE#{user_id}", "sk": "PROFILE"}).get("Item")


def _fetch_tasks(user_id: str, start: date, end: date):
    resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq(f"TASK#{user_id}")
            & Key("sk").between(f"{start.isoformat()}#", f"{end.isoformat()}#￿")
        )
    )
    return sorted(resp.get("Items", []), key=lambda i: i["sk"])


def _period_title(start: date, end: date) -> str:
    if start.month == end.month and start.year == end.year:
        return f"For the month of {start.strftime('%B')} {start.day:02d} – {end.day:02d}, {end.year}"
    return (
        f"For the month of {start.strftime('%B')} {start.day:02d}, {start.year} "
        f"– {end.strftime('%B')} {end.day:02d}, {end.year}"
    )



# Fixed task-row style — always 10pt, never shrunk to force a fit.
_ROW_FONT = 10
_ROW_LEADING = 13
_ROW_PAD = 3
_CELL_LR_PAD = 5

_MASTHEAD_GAP = 10
_TABLE_GAP = 16
_SUB_SPACE_AFTER = 10


def _rows_height(tasks, row_count, desc_col_width) -> float:
    measure_style = ParagraphStyle("measure", fontSize=_ROW_FONT, leading=_ROW_LEADING)
    total = 0.0
    for i in range(row_count):
        desc = tasks[i]["description"] if tasks else "&nbsp;"
        _, h = Paragraph(desc, measure_style).wrap(desc_col_width, 10000)
        total += max(h, _ROW_LEADING) + 2 * _ROW_PAD
    return total


def _build_pdf(start: date, end: date, tasks, employee_name: str, employee_position: str, out_path: str) -> None:
    row_count = max(len(tasks), 1)

    top_margin = 0.65 * inch
    bottom_margin = 0.65 * inch
    left_margin = 0.85 * inch
    right_margin = 0.85 * inch

    col_widths = [1.1 * inch, 1.1 * inch, 0.9 * inch, 3.0 * inch]
    desc_col_width = col_widths[3] - 2 * _CELL_LR_PAD

    # Fixed-size elements above and below the task table (measured generously
    # with headroom), used only to decide whether the NAME/POSITION column
    # can safely be merged into one spanned cell (see the SPAN note below).
    fixed_overhead = (
        0.8 * inch  # masthead logo/text block
        + _MASTHEAD_GAP
        + 26  # "ACCOMPLISHMENT REPORT" title
        + 24  # period subtitle
        + 0.22 * inch  # table header row
        + _TABLE_GAP
        + 70  # signature block (labels + gap + name/position lines)
    )
    available_for_rows = (LETTER[1] - top_margin - bottom_margin) - fixed_overhead
    fits_one_page = _rows_height(tasks, row_count, desc_col_width) <= available_for_rows

    styles = getSampleStyleSheet()
    header_style = ParagraphStyle("header", parent=styles["Normal"], alignment=TA_CENTER, fontSize=11, leading=14)
    title_style = ParagraphStyle("title", parent=styles["Heading1"], alignment=TA_CENTER, fontSize=15, spaceAfter=4)
    sub_style = ParagraphStyle(
        "sub", parent=styles["Normal"], alignment=TA_CENTER, fontSize=10.5, spaceAfter=_SUB_SPACE_AFTER
    )
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=_ROW_FONT, leading=_ROW_LEADING)
    label_style = ParagraphStyle("label", parent=styles["Normal"], fontSize=9, leading=12, fontName="Helvetica-Bold")

    doc = SimpleDocTemplate(
        out_path,
        pagesize=LETTER,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
        leftMargin=left_margin,
        rightMargin=right_margin,
    )

    # The masthead text is centered on the page like the title below it; the
    # seal is drawn separately (see _draw_letterhead) at a fixed position to
    # its left, rather than being part of the centered flow.
    story = [
        Paragraph("Republic of the Philippines", header_style),
        Paragraph("Province of Quezon", header_style),
        Paragraph("Municipality of Lopez", header_style),
        Paragraph("*****", header_style),
        Spacer(1, _MASTHEAD_GAP),
        Paragraph("ACCOMPLISHMENT REPORT", title_style),
        Paragraph(_period_title(start, end), sub_style),
    ]

    # One table row per task (rather than two <br/>-joined paragraphs) so a
    # wrapped description can never drift out of sync with its own date.
    data = [
        [
            Paragraph("NAME", label_style),
            Paragraph("POSITION", label_style),
            Paragraph("DATE", label_style),
            Paragraph("DESCRIPTION", label_style),
        ],
    ]
    # Merging NAME/POSITION into one spanned cell only holds up when we're
    # sure the table fits on one page — a spanned cell that has to split
    # across a page boundary can crash reportlab's layout engine. If it
    # doesn't fit, repeat the name/position on every row instead so the
    # table can still split safely rather than erroring out.
    for i in range(row_count):
        if fits_one_page:
            name_cell = Paragraph(employee_name, cell_style) if i == 0 else Paragraph("", cell_style)
            position_cell = Paragraph(employee_position, cell_style) if i == 0 else Paragraph("", cell_style)
        else:
            name_cell = Paragraph(employee_name, cell_style)
            position_cell = Paragraph(employee_position, cell_style)
        if tasks:
            date_str = datetime.strptime(tasks[i]["date"], "%Y-%m-%d").strftime("%-m/%-d/%Y")
            desc_str = tasks[i]["description"]
        else:
            date_str = "&nbsp;"
            desc_str = "&nbsp;"
        data.append([name_cell, position_cell, Paragraph(date_str, cell_style), Paragraph(desc_str, cell_style)])

    tbl = Table(data, colWidths=col_widths, rowHeights=[0.22 * inch] + [None] * row_count)
    style_commands = [
        ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("ALIGN", (0, 1), (1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, 0), 3),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
        ("TOPPADDING", (0, 1), (-1, -1), _ROW_PAD),
        ("BOTTOMPADDING", (0, 1), (-1, -1), _ROW_PAD),
        ("LEFTPADDING", (0, 0), (-1, -1), _CELL_LR_PAD),
        ("RIGHTPADDING", (0, 0), (-1, -1), _CELL_LR_PAD),
    ]
    if row_count > 1 and fits_one_page:
        style_commands.append(("SPAN", (0, 1), (0, row_count)))
        style_commands.append(("SPAN", (1, 1), (1, row_count)))
        style_commands.append(("VALIGN", (0, 1), (1, row_count), "MIDDLE"))
    tbl.setStyle(TableStyle(style_commands))
    story.append(tbl)
    story.append(Spacer(1, _TABLE_GAP))

    sig_data = [
        [Paragraph("Prepared by:", label_style), Paragraph("Noted by:", label_style)],
        [Spacer(1, 28), Spacer(1, 28)],
        [Paragraph(f"<b>{employee_name}</b>", cell_style), Paragraph(f"<b>{MAYOR_NAME}</b>", cell_style)],
        [Paragraph(employee_position, cell_style), Paragraph(MAYOR_TITLE, cell_style)],
    ]
    sig_tbl = Table(sig_data, colWidths=[3.05 * inch, 3.05 * inch])
    sig_tbl.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    # KeepTogether so the signature block never splits across a page boundary.
    story.append(KeepTogether([sig_tbl]))

    # Widest masthead line, to find where the centered text actually starts
    # so the seal can sit a fixed, modest gap to its left (not just guessed).
    masthead_lines = ["Republic of the Philippines", "Province of Quezon", "Municipality of Lopez", "*****"]
    widest_line = max(stringWidth(line, "Helvetica", 11) for line in masthead_lines)
    text_left_x = (LETTER[0] - widest_line) / 2

    def _draw_letterhead(canvas, doc_):
        logo_size = 0.8 * inch
        gap = 18
        x = text_left_x - gap - logo_size
        y = LETTER[1] - doc_.topMargin - logo_size + 0.08 * inch
        canvas.drawImage(LOGO_PATH, x, y, width=logo_size, height=logo_size, preserveAspectRatio=True, mask="auto")

    doc.build(story, onFirstPage=_draw_letterhead)


def _generate_for_user(user_id: str, start: date, end: date):
    profile = _fetch_profile(user_id)
    if not profile:
        print(f"Skipping {user_id}: no profile on file")
        return None

    tasks = _fetch_tasks(user_id, start, end)
    out_path = f"/tmp/{user_id}_{start.isoformat()}_to_{end.isoformat()}.pdf"
    _build_pdf(start, end, tasks, profile["name"], profile["designation"], out_path)

    key = f"{user_id}/{start.isoformat()}_to_{end.isoformat()}.pdf"
    s3.upload_file(out_path, REPORTS_BUCKET, key, ExtraArgs={"ContentType": "application/pdf"})
    return key


def handler(event, context):
    trigger = event.get("trigger", "day16")
    if trigger == "period":
        start = date.fromisoformat(event["start"])
        end = date.fromisoformat(event["end"])
    else:
        start, end = _period_for_trigger(trigger)

    user_id = event.get("userId")
    user_ids = [user_id] if user_id else _list_all_user_ids()

    generated = [key for uid in user_ids if (key := _generate_for_user(uid, start, end))]

    return {
        "statusCode": 200,
        "body": f"Generated {len(generated)} report(s) for {start.isoformat()}–{end.isoformat()}",
    }

import calendar
import os
from datetime import date, datetime

import boto3
from boto3.dynamodb.conditions import Key
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

TABLE_NAME = os.environ["TABLE_NAME"]
REPORTS_BUCKET = os.environ["REPORTS_BUCKET"]
table = dynamodb.Table(TABLE_NAME)

# Fixed reporting details (confirmed with the account owner).
EMPLOYEE_NAME = "STANLEY JOHN O. PANGARUY"
EMPLOYEE_POSITION = "IT TECHNICAL STAFF"
MAYOR_NAME = "Hon. ISAIAS B. UBANA II, PhD"
MAYOR_TITLE = "Municipal Mayor"


def _period_for_trigger(trigger: str):
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
    elif trigger == "now":
        # On-demand: the period the caller is currently in (mirrors tasks_api._current_period).
        if today.day <= 15:
            start = today.replace(day=1)
            end = today.replace(day=15)
        else:
            last_day = calendar.monthrange(today.year, today.month)[1]
            start = today.replace(day=16)
            end = today.replace(day=last_day)
    else:
        raise ValueError(f"Unknown trigger: {trigger!r}")
    return start, end


def _fetch_tasks(start: date, end: date):
    resp = table.query(
        KeyConditionExpression=(
            Key("pk").eq("TASK")
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


def _build_pdf(start: date, end: date, tasks, out_path: str) -> None:
    styles = getSampleStyleSheet()
    header_style = ParagraphStyle("header", parent=styles["Normal"], alignment=TA_CENTER, fontSize=11, leading=14)
    title_style = ParagraphStyle("title", parent=styles["Heading1"], alignment=TA_CENTER, fontSize=15, spaceAfter=4)
    sub_style = ParagraphStyle("sub", parent=styles["Normal"], alignment=TA_CENTER, fontSize=10.5, spaceAfter=18)
    cell_style = ParagraphStyle("cell", parent=styles["Normal"], fontSize=10, leading=13)
    label_style = ParagraphStyle("label", parent=styles["Normal"], fontSize=9, leading=12, fontName="Helvetica-Bold")

    doc = SimpleDocTemplate(
        out_path,
        pagesize=LETTER,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
    )

    story = [
        Paragraph("Republic of the Philippines", header_style),
        Paragraph("Province of Quezon", header_style),
        Paragraph("Municipality of Lopez", header_style),
        Paragraph("*****", header_style),
        Spacer(1, 14),
        Paragraph("ACCOMPLISHMENT REPORT", title_style),
        Paragraph(_period_title(start, end), sub_style),
    ]

    date_lines = "<br/>".join(
        datetime.strptime(t["date"], "%Y-%m-%d").strftime("%-m/%-d/%Y") for t in tasks
    ) or "&nbsp;"
    desc_lines = "<br/>".join(t["description"] for t in tasks) or "&nbsp;"

    data = [
        [
            Paragraph("NAME", label_style),
            Paragraph("POSITION", label_style),
            Paragraph("DATE", label_style),
            Paragraph("DESCRIPTION", label_style),
        ],
        [
            Paragraph(EMPLOYEE_NAME, cell_style),
            Paragraph(EMPLOYEE_POSITION, cell_style),
            Paragraph(date_lines, cell_style),
            Paragraph(desc_lines, cell_style),
        ],
    ]
    col_widths = [1.1 * inch, 1.1 * inch, 0.9 * inch, 3.0 * inch]
    tbl = Table(data, colWidths=col_widths, rowHeights=[0.3 * inch, None])
    tbl.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.75, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("ALIGN", (0, 0), (-1, 0), "CENTER"),
                ("VALIGN", (0, 1), (-1, 1), "TOP"),
                ("ALIGN", (0, 1), (1, 1), "CENTER"),
                ("TOPPADDING", (0, 1), (-1, 1), 10),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(tbl)
    story.append(Spacer(1, 48))

    sig_data = [
        [Paragraph("Prepared by:", label_style), Paragraph("Noted by:", label_style)],
        [Spacer(1, 28), Spacer(1, 28)],
        [Paragraph(f"<b>{EMPLOYEE_NAME}</b>", cell_style), Paragraph(f"<b>{MAYOR_NAME}</b>", cell_style)],
        [Paragraph(EMPLOYEE_POSITION, cell_style), Paragraph(MAYOR_TITLE, cell_style)],
    ]
    sig_tbl = Table(sig_data, colWidths=[3.05 * inch, 3.05 * inch])
    sig_tbl.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(sig_tbl)

    doc.build(story)


def handler(event, context):
    trigger = event.get("trigger", "day16")
    start, end = _period_for_trigger(trigger)
    tasks = _fetch_tasks(start, end)

    out_path = f"/tmp/{start.isoformat()}_to_{end.isoformat()}.pdf"
    _build_pdf(start, end, tasks, out_path)

    key = f"{start.isoformat()}_to_{end.isoformat()}.pdf"
    s3.upload_file(out_path, REPORTS_BUCKET, key, ExtraArgs={"ContentType": "application/pdf"})

    return {"statusCode": 200, "body": f"Report generated: {key} ({len(tasks)} tasks)"}

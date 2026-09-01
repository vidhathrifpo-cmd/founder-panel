import io
import os
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_RIGHT, TA_CENTER

from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = FastAPI(title="Vidhathri Invoice PDF Service")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "")
DRIVE_ACCOUNTING_FOLDER_ID = os.environ.get("DRIVE_ACCOUNTING_FOLDER_ID", "")

NAVY = colors.HexColor("#1F3B57")
GREY = colors.HexColor("#F2F2F2")


class LineItem(BaseModel):
    sku: str
    name: str
    quantity: float
    unitPrice: float
    gstRate: float
    gstAmount: float
    lineTotal: float
    lineType: str


class BillTo(BaseModel):
    name: str
    address: Optional[str] = None
    phone: Optional[str] = None
    gstin: Optional[str] = None


class InvoiceRequest(BaseModel):
    saleId: str
    invoiceNumber: str
    invoiceDate: str
    voucherType: str
    billTo: BillTo
    lineItems: List[LineItem]
    subtotal: float
    gstTotal: float
    grandTotal: float


class InvoiceResponse(BaseModel):
    invoiceNumber: str
    fileUrl: str
    driveFileId: str


def render_invoice_pdf(inv: InvoiceRequest) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm,
                             leftMargin=18 * mm, rightMargin=18 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("Title", parent=styles["Heading1"], textColor=NAVY, fontSize=18)
    normal = styles["Normal"]
    right = ParagraphStyle("Right", parent=normal, alignment=TA_RIGHT)
    center = ParagraphStyle("Center", parent=normal, alignment=TA_CENTER)

    story = []
    story.append(Paragraph("VIDHATHRI FARMERS PRODUCER COMPANY LIMITED", title_style))
    story.append(Paragraph("Kota, Brahmavara Taluk, Udupi District, Karnataka — 576221", normal))
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph(f"<b>INVOICE</b>", ParagraphStyle("InvHead", parent=styles["Heading2"], textColor=NAVY)))

    meta_table = Table([
        ["Invoice No.:", inv.invoiceNumber, "Date:", inv.invoiceDate],
    ], colWidths=[70 * mm, 55 * mm, 25 * mm, None])
    meta_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("FONTNAME", (0, 0), (0, 0), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, 0), "Helvetica-Bold"),
    ]))
    story.append(meta_table)
    story.append(Spacer(1, 6 * mm))

    bill_to_lines = [f"<b>Bill To:</b> {inv.billTo.name}"]
    if inv.billTo.address:
        bill_to_lines.append(inv.billTo.address)
    if inv.billTo.phone:
        bill_to_lines.append(f"Phone: {inv.billTo.phone}")
    if inv.billTo.gstin:
        bill_to_lines.append(f"GSTIN: {inv.billTo.gstin}")
    story.append(Paragraph("<br/>".join(bill_to_lines), normal))
    story.append(Spacer(1, 8 * mm))

    header = ["SKU", "Item", "Qty", "Rate", "GST%", "GST Amt", "Total"]
    rows = [header]
    for li in inv.lineItems:
        label = li.name if li.lineType == "NORMAL" else f"{li.name} ({li.lineType})"
        rows.append([
            li.sku, label, f"{li.quantity:g}", f"{li.unitPrice:,.2f}",
            f"{li.gstRate:g}%", f"{li.gstAmount:,.2f}", f"{li.lineTotal:,.2f}",
        ])
    rows.append(["", "", "", "", "", "Subtotal", f"{inv.subtotal:,.2f}"])
    rows.append(["", "", "", "", "", "GST Total", f"{inv.gstTotal:,.2f}"])
    rows.append(["", "", "", "", "", "GRAND TOTAL", f"{inv.grandTotal:,.2f}"])

    items_table = Table(rows, colWidths=[22 * mm, 55 * mm, 15 * mm, 22 * mm, 15 * mm, 25 * mm, 25 * mm])
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, len(inv.lineItems)), 0.5, colors.grey),
        ("BACKGROUND", (0, -1), (-1, -1), GREY),
        ("FONTNAME", (5, -3), (6, -1), "Helvetica-Bold"),
        ("LINEABOVE", (5, -3), (-1, -3), 0.5, colors.grey),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph("This is a system-generated invoice.", ParagraphStyle("Footer", parent=normal, fontSize=8, textColor=colors.grey)))

    doc.build(story)
    return buf.getvalue()


def find_or_create_folder(drive, name: str, parent_id: str) -> str:
    query = (
        f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    resp = drive.files().list(q=query, fields="files(id, name)", pageSize=1).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    created = drive.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
    ).execute()
    return created["id"]


def get_voucher_month_folder(drive, voucher_type: str, invoice_date: str) -> str:
    dt = datetime.strptime(invoice_date, "%Y-%m-%d")
    voucher_folder_id = find_or_create_folder(drive, voucher_type, DRIVE_ACCOUNTING_FOLDER_ID)
    year_folder_id = find_or_create_folder(drive, f"{dt.year:04d}", voucher_folder_id)
    month_folder_id = find_or_create_folder(drive, f"{dt.month:02d}", year_folder_id)
    return month_folder_id


def upload_to_drive(pdf_bytes: bytes, filename: str, voucher_type: str, invoice_date: str) -> tuple[str, str]:
    if not (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REFRESH_TOKEN and DRIVE_ACCOUNTING_FOLDER_ID):
        raise RuntimeError("Google OAuth credentials / DRIVE_ACCOUNTING_FOLDER_ID not configured")

    creds = UserCredentials(
        None,
        refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    drive = build("drive", "v3", credentials=creds)

    target_folder_id = get_voucher_month_folder(drive, voucher_type, invoice_date)

    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
    file_metadata = {"name": filename, "parents": [target_folder_id]}
    created = drive.files().create(body=file_metadata, media_body=media, fields="id, webViewLink").execute()
    return created["id"], created["webViewLink"]


@app.post("/generate-invoice", response_model=InvoiceResponse)
def generate_invoice(inv: InvoiceRequest):
    try:
        pdf_bytes = render_invoice_pdf(inv)
        filename = f"{inv.invoiceNumber.replace('/', '_')}.pdf"
        file_id, url = upload_to_drive(pdf_bytes, filename, inv.voucherType, inv.invoiceDate)
        return InvoiceResponse(invoiceNumber=inv.invoiceNumber, fileUrl=url, driveFileId=file_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

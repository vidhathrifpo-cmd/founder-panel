"""
invoice_pdf_service

Standalone service that takes an invoice payload (from the invoice_generator
TS script), renders a PDF using reportlab, uploads it to a shared Google
Drive folder, and returns the file's shareable URL.

Runs independently of the DB-querying logic in index.ts -- invoice_generator
POSTs the already-assembled payload here; this service knows nothing about
Supabase. Keeps the "what to invoice" and "how to render/store a PDF"
concerns fully separate, per the modular-scripts approach.

Run locally:    uvicorn main:app --host 0.0.0.0 --port 8000
Env required:
    GOOGLE_SERVICE_ACCOUNT_JSON   -- path to a service account key file
    DRIVE_INVOICES_FOLDER_ID      -- ID of the shared Drive folder to upload into
"""

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

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = FastAPI(title="Vidhathri Invoice PDF Service")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
DRIVE_SALES_FOLDER_ID = os.environ.get("DRIVE_SALES_FOLDER_ID", "")  # the "1.Sales" folder under Accounting

NAVY = colors.HexColor("#1F3B57")
GREY = colors.HexColor("#F2F2F2")


# ---------- Request schema (mirrors InvoicePayload from index.ts) ----------

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
    billTo: BillTo
    lineItems: List[LineItem]
    subtotal: float
    gstTotal: float
    grandTotal: float


class InvoiceResponse(BaseModel):
    invoiceNumber: str
    fileUrl: str
    driveFileId: str


# ---------- PDF rendering ----------

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
    story.append(Paragraph("Kota, Brahmavara Taluk, Udupi District, Karnataka \u2014 576221", normal))
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

    # Line items table
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


# ---------- Drive upload ----------
#
# Target layout: Accounting/1.Sales/{YYYY}/{MM}/invoice.pdf
# Month folders are found-or-created on the fly so no manual setup is needed
# each month. Only the Sales branch is used here -- Purchase/Payment folders
# are for future scripts (purchase recording, payment reconciliation), not
# this service.

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


def get_sales_month_folder(drive, invoice_date: str) -> str:
    dt = datetime.strptime(invoice_date, "%Y-%m-%d")
    year_folder_id = find_or_create_folder(drive, f"{dt.year:04d}", DRIVE_SALES_FOLDER_ID)
    month_folder_id = find_or_create_folder(drive, f"{dt.month:02d}", year_folder_id)
    return month_folder_id


def upload_to_drive(pdf_bytes: bytes, filename: str, invoice_date: str) -> tuple[str, str]:
    if not SERVICE_ACCOUNT_FILE or not DRIVE_SALES_FOLDER_ID:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON / DRIVE_SALES_FOLDER_ID not configured")

    creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    drive = build("drive", "v3", credentials=creds)

    target_folder_id = get_sales_month_folder(drive, invoice_date)

    media = MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False)
    file_metadata = {"name": filename, "parents": [target_folder_id]}
    created = drive.files().create(body=file_metadata, media_body=media, fields="id, webViewLink").execute()
    return created["id"], created["webViewLink"]


# ---------- Endpoint ----------

@app.post("/generate-invoice", response_model=InvoiceResponse)
def generate_invoice(inv: InvoiceRequest):
    try:
        pdf_bytes = render_invoice_pdf(inv)
        filename = f"{inv.invoiceNumber.replace('/', '_')}.pdf"
        file_id, url = upload_to_drive(pdf_bytes, filename, inv.invoiceDate)
        return InvoiceResponse(invoiceNumber=inv.invoiceNumber, fileUrl=url, driveFileId=file_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}

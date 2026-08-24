"""
Invoice/email HTML, and a PDF invoice builder via reportlab. Same visual
structure as the original _build_invoice_html/_build_invoice_pdf/
_send_subscription_email, but every color and label that was a literal
string is now read from a BrandingConfig - so a second product gets its own
look without touching this file.
"""
from __future__ import annotations
import datetime
import io

from dodman_billing.config import BrandingConfig


def fmt_invoice_date(val: str) -> str:
    if not val:
        return "\u2014"
    try:
        dt = datetime.datetime.fromisoformat(val.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y")
    except Exception:
        try:
            return datetime.datetime.strptime(val[:10], "%Y-%m-%d").strftime("%d %b %Y")
        except Exception:
            return val[:10] if len(val) >= 10 else val


def build_invoice_html(branding: BrandingConfig, *, invoice_ref: str, user_email: str, plan: str,
                        amount_minor: int, currency: str, paid_at: str,
                        period_start: str, period_end: str) -> str:
    amount_display = f"{currency} {amount_minor / 100:,.2f}"
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%d %B %Y")
    plan_display = plan.title() if plan else "\u2014"
    paid_at_display = fmt_invoice_date(paid_at) if paid_at else "\u2014"
    ps_display = fmt_invoice_date(period_start)
    pe_display = fmt_invoice_date(period_end)

    b = branding
    S_BODY = f"font-family:{b.font_family};background:{b.bg_color};color:{b.text_color};margin:0;padding:40px;"
    S_CARD = f"max-width:600px;margin:0 auto;background:{b.card_color};border:1px solid {b.border_color};border-radius:12px;overflow:hidden;"
    S_HDR = f"background:{b.header_color};padding:28px 32px;border-bottom:1px solid {b.border_color};"
    S_BRAND = f"font-size:11px;letter-spacing:.3em;text-transform:uppercase;color:{b.brand_color};margin:0 0 6px 0;"
    S_TITLE = "font-size:22px;font-weight:700;color:#ffffff;margin:0;"
    S_BODYD = "padding:28px 32px;"
    S_AMT = f"font-size:26px;font-weight:700;color:{b.accent_color};text-align:center;padding:16px 0 24px;"
    S_ROW = f"display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid {b.header_color};font-size:13px;"
    S_ROW_L = "display:flex;justify-content:space-between;padding:10px 0;font-size:13px;"
    S_LBL = f"color:{b.muted_color};"
    S_VAL = f"color:{b.text_color};text-align:right;"
    S_MONO = f"color:{b.text_color};text-align:right;font-family:{b.font_family};font-size:12px;"
    S_BADGE = (f"display:inline-block;background:rgba(16,185,129,.15);border:1px solid rgba(16,185,129,.4);"
               f"color:{b.accent_color};font-size:10px;padding:3px 10px;border-radius:4px;"
               f"letter-spacing:.08em;text-transform:uppercase;")
    S_FTR = f"padding:18px 32px;border-top:1px solid {b.border_color};font-size:11px;color:#3a3a44;text-align:center;"

    def row(label: str, value: str, last: bool = False, mono_val: bool = False) -> str:
        val_style = S_MONO if mono_val else S_VAL
        return f'<div style="{S_ROW_L if last else S_ROW}"><span style="{S_LBL}">{label}</span><span style="{val_style}">{value}</span></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Invoice {invoice_ref[:12]}</title></head>
<body style="{S_BODY}">
<div style="{S_CARD}">
  <div style="{S_HDR}">
    <p style="{S_BRAND}">{b.company_name}</p>
    <p style="{S_TITLE}">Invoice</p>
  </div>
  <div style="{S_BODYD}">
    <div style="{S_AMT}">{amount_display}</div>
    {row("Reference", invoice_ref, mono_val=True)}
    {row("Status", f'<span style="{S_BADGE}">Paid</span>')}
    {row("Billed to", user_email)}
    {row("Plan", plan_display)}
    {row("Paid on", paid_at_display)}
    {row("Period start", ps_display)}
    {row("Period end", pe_display)}
    {row("Invoice date", now_str, last=True)}
  </div>
  <div style="{S_FTR}">{b.company_name} &middot; {b.product_display_name} &middot; {b.support_email}</div>
</div>
</body>
</html>"""


def build_invoice_pdf(branding: BrandingConfig, *, invoice_ref: str, user_email: str, plan: str,
                       amount_minor: int, currency: str, paid_at: str,
                       period_start: str, period_end: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib.styles import ParagraphStyle

    b = branding
    buf = io.BytesIO()

    C_BG, C_CARD, C_BORDER = colors.HexColor(b.bg_color), colors.HexColor(b.card_color), colors.HexColor(b.border_color)
    C_HDR_BG, C_BRAND = colors.HexColor(b.header_color), colors.HexColor(b.brand_color)
    C_WHITE, C_MUTED, C_BODY, C_ACCENT = colors.HexColor("#ffffff"), colors.HexColor(b.muted_color), colors.HexColor(b.text_color), colors.HexColor(b.accent_color)
    C_ROW_ALT = colors.HexColor("#0d0d10")
    C_BADGE_BG, C_BADGE_BDR = colors.HexColor("#0d2e22"), colors.HexColor("#1a5c3a")

    def ps(name, **kw):
        defaults = dict(fontName="Courier", fontSize=10, textColor=C_BODY, leading=14, spaceAfter=0)
        defaults.update(kw)
        return ParagraphStyle(name, **defaults)

    S_BRAND_ST = ps("brand", fontSize=8, textColor=C_BRAND, spaceAfter=2)
    S_TITLE_ST = ps("title", fontSize=20, textColor=C_WHITE, fontName="Courier-Bold")
    S_AMOUNT = ps("amount", fontSize=26, textColor=C_ACCENT, fontName="Courier-Bold", alignment=TA_CENTER, spaceAfter=4)
    S_LABEL = ps("label", fontSize=10, textColor=C_MUTED, alignment=0)
    S_VALUE = ps("value", fontSize=10, textColor=C_BODY, alignment=2)
    S_MONO = ps("mono", fontSize=9, textColor=C_BODY, alignment=2)
    S_BADGE = ps("badge", fontSize=8, textColor=C_ACCENT, alignment=2, fontName="Courier-Bold")
    S_FOOTER = ps("footer", fontSize=8, textColor=C_MUTED, alignment=TA_CENTER)

    amount_display = f"{currency} {amount_minor / 100:,.2f}"
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%d %B %Y")
    plan_display = plan.title() if plan else "\u2014"
    paid_at_display = fmt_invoice_date(paid_at)
    ps_display = fmt_invoice_date(period_start)
    pe_display = fmt_invoice_date(period_end)

    W, H = A4
    LEFT = RIGHT = TOP = BOT = 20 * mm
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=LEFT, rightMargin=RIGHT, topMargin=TOP, bottomMargin=BOT)
    card_w = W - LEFT - RIGHT

    def _draw_bg(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, W, H, fill=1, stroke=0)
        card_x, card_y = LEFT - 6 * mm, BOT - 6 * mm
        card_w2, card_h2 = W - LEFT - RIGHT + 12 * mm, H - TOP - BOT + 12 * mm
        canvas.setFillColor(C_CARD)
        canvas.setStrokeColor(C_BORDER)
        canvas.roundRect(card_x, card_y, card_w2, card_h2, 6, fill=1, stroke=1)
        hdr_h = 22 * mm
        canvas.setFillColor(C_HDR_BG)
        canvas.roundRect(card_x, H - TOP - hdr_h + 6 * mm, card_w2, hdr_h + 6 * mm, 6, fill=1, stroke=0)
        canvas.rect(card_x, H - TOP - hdr_h, card_w2, 6 * mm + 1, fill=1, stroke=0)
        canvas.setStrokeColor(C_BORDER)
        canvas.setLineWidth(0.5)
        canvas.line(card_x, H - TOP - hdr_h, card_x + card_w2, H - TOP - hdr_h)
        canvas.restoreState()

    story = [Spacer(1, 2 * mm), Paragraph(b.company_name.upper(), S_BRAND_ST), Paragraph("Invoice", S_TITLE_ST),
             Spacer(1, 10 * mm), Paragraph(amount_display, S_AMOUNT), Spacer(1, 4 * mm),
             HRFlowable(width="100%", thickness=0.5, color=C_BORDER), Spacer(1, 2 * mm)]

    rows = [
        ("Reference", Paragraph(invoice_ref, S_MONO)),
        ("Status", Paragraph("PAID", S_BADGE)),
        ("Billed to", Paragraph(user_email, S_VALUE)),
        ("Plan", Paragraph(plan_display, S_VALUE)),
        ("Paid on", Paragraph(paid_at_display, S_VALUE)),
        ("Period start", Paragraph(ps_display, S_VALUE)),
        ("Period end", Paragraph(pe_display, S_VALUE)),
        ("Invoice date", Paragraph(now_str, S_VALUE)),
    ]
    table_data = [[Paragraph(label, S_LABEL), val] for label, val in rows]
    col_w = [card_w * 0.38, card_w * 0.62]
    tbl = Table(table_data, colWidths=col_w, repeatRows=0)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), C_CARD),
        ("BACKGROUND", (0, 1), (-1, 1), C_ROW_ALT),
        ("BACKGROUND", (0, 3), (-1, 3), C_ROW_ALT),
        ("BACKGROUND", (0, 5), (-1, 5), C_ROW_ALT),
        ("BACKGROUND", (0, 7), (-1, 7), C_ROW_ALT),
        ("BACKGROUND", (1, 1), (1, 1), C_BADGE_BG),
        ("BOX", (1, 1), (1, 1), 0.5, C_BADGE_BDR),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, C_BORDER),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(tbl)
    story += [Spacer(1, 6 * mm), HRFlowable(width="100%", thickness=0.5, color=C_BORDER), Spacer(1, 3 * mm),
              Paragraph(f"{b.company_name} &middot; {b.product_display_name} &middot; {b.support_email}", S_FOOTER)]

    doc.build(story, onFirstPage=_draw_bg, onLaterPages=_draw_bg)
    return buf.getvalue()


def build_lifecycle_email_html(branding: BrandingConfig, *, heading: str, body: str,
                                cta_text: str = "", cta_url: str = "") -> str:
    b = branding
    cta_block = ""
    if cta_text and cta_url:
        cta_block = (f'<div style="text-align:center;margin:24px 0;">'
                     f'<a href="{cta_url}" style="background:{b.accent_color};color:{b.bg_color};font-weight:700;'
                     f'font-size:12px;letter-spacing:.08em;text-transform:uppercase;padding:11px 28px;'
                     f'border-radius:6px;text-decoration:none;">{cta_text}</a></div>')
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<style>
  body{{font-family:{b.font_family};background:{b.bg_color};color:{b.text_color};margin:0;padding:40px;}}
  .card{{max-width:560px;margin:0 auto;background:{b.card_color};border:1px solid {b.border_color};border-radius:12px;overflow:hidden;}}
  .hdr{{background:{b.header_color};padding:24px 28px;border-bottom:1px solid {b.border_color};}}
  .brand{{font-size:10px;letter-spacing:.3em;text-transform:uppercase;color:{b.brand_color};margin:0 0 4px;}}
  .heading{{font-size:18px;font-weight:700;color:#fff;margin:0;}}
  .body{{padding:24px 28px;font-size:13px;line-height:1.7;color:{b.muted_color};}}
  .footer{{padding:16px 28px;border-top:1px solid {b.border_color};font-size:10px;color:#3a3a44;text-align:center;}}
</style>
</head><body>
<div class="card">
  <div class="hdr"><p class="brand">{b.company_name}</p><p class="heading">{heading}</p></div>
  <div class="body">{body}{cta_block}</div>
  <div class="footer">{b.company_name} &middot; {b.product_display_name} &middot; Questions? Reply to this email.</div>
</div>
</body></html>"""

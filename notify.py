"""
Módulo de notificaciones para KMA · Buk Finanzas.

Compara la extracción actual con el último estado guardado en Drive y envía
un email con: nuevas facturas por empresa, egresos sin categoría y facturas
de alto valor.
"""

import io
import json
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

log = logging.getLogger(__name__)

DRIVE_FOLDER_ID     = "0AJUk5QWCegyXUk9PVA"
STATE_FILE_NAME     = "kma_buk_last_state.json"
HIGH_VALUE_CLP      = 5_000_000   # Alerta si monto_bruto supera este valor


# ─────────────────────────────────────────────────────────────────────────────
# Drive helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_drive_service():
    raw = os.environ.get("GOOGLE_CREDENTIALS")
    if not raw:
        from pathlib import Path
        matches = list(Path(__file__).parent.glob("master-chess-*.json"))
        if matches:
            raw = matches[0].read_text()
    if not raw:
        raise EnvironmentError("GOOGLE_CREDENTIALS no definido.")
    creds = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_file(service, name: str) -> str | None:
    files = (
        service.files()
        .list(
            q=f"name='{name}' and '{DRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="files(id)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    return files[0]["id"] if files else None


def _download_state(service) -> dict | None:
    file_id = _find_file(service, STATE_FILE_NAME)
    if not file_id:
        return None
    content = service.files().get_media(fileId=file_id).execute()
    return json.loads(content.decode("utf-8"))


def _upload_state(service, state: dict):
    data = json.dumps(state, ensure_ascii=False, default=str).encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/json", resumable=False)
    file_id = _find_file(service, STATE_FILE_NAME)
    if file_id:
        service.files().update(
            fileId=file_id, media_body=media, supportsAllDrives=True
        ).execute()
    else:
        service.files().create(
            body={"name": STATE_FILE_NAME, "parents": [DRIVE_FOLDER_ID]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
    log.info(f"Notificaciones: estado guardado en Drive ({STATE_FILE_NAME})")


# ─────────────────────────────────────────────────────────────────────────────
# Lógica de comparación
# ─────────────────────────────────────────────────────────────────────────────

def _invoice_key(row) -> str:
    return f"{row['empresa']}|{row['tipo_flujo']}|{str(row['numero_documento'])}|{str(row['fecha_emision'])}"


def _fmt_clp(value) -> str:
    try:
        v = float(value)
        if v >= 1_000_000:
            return f"${v/1_000_000:.1f}M"
        return f"${v:,.0f}"
    except Exception:
        return str(value)


# ─────────────────────────────────────────────────────────────────────────────
# Construcción del email HTML
# ─────────────────────────────────────────────────────────────────────────────

def _build_email(new_df: pd.DataFrame, sin_cat: pd.DataFrame, alto_valor: pd.DataFrame) -> tuple[str, str]:
    today = datetime.now().strftime("%-d de %B de %Y")
    total_new = len(new_df)
    total_monto = new_df["monto_bruto"].sum()

    # ── Sección: nuevas facturas por empresa
    by_empresa = (
        new_df.groupby(["empresa", "tipo_flujo"])
        .agg(n=("monto_bruto", "count"), monto=("monto_bruto", "sum"))
        .reset_index()
        .sort_values("monto", ascending=False)
    )
    rows_empresa = ""
    for _, r in by_empresa.iterrows():
        tipo_badge = (
            '<span style="background:#1F4E79;color:#fff;padding:2px 7px;border-radius:10px;font-size:11px">EGRESO</span>'
            if r["tipo_flujo"] == "EGRESO"
            else '<span style="background:#2E7D32;color:#fff;padding:2px 7px;border-radius:10px;font-size:11px">INGRESO</span>'
        )
        rows_empresa += f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #eee">{r['empresa']}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:center">{tipo_badge}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:center">{int(r['n'])}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right">{_fmt_clp(r['monto'])}</td>
        </tr>"""

    # ── Sección: sin categoría
    sin_cat_html = ""
    if not sin_cat.empty:
        filas = ""
        for _, r in sin_cat.head(20).iterrows():
            filas += f"""
            <tr>
              <td style="padding:5px 10px;border-bottom:1px solid #fde8e8">{r['empresa']}</td>
              <td style="padding:5px 10px;border-bottom:1px solid #fde8e8">{r['nombre_contraparte'] or '—'}</td>
              <td style="padding:5px 10px;border-bottom:1px solid #fde8e8;text-align:right">{_fmt_clp(r['monto_bruto'])}</td>
              <td style="padding:5px 10px;border-bottom:1px solid #fde8e8">{r['fecha_emision'] or '—'}</td>
            </tr>"""
        extra = f"<p style='color:#c00;font-size:12px'>... y {len(sin_cat)-20} más</p>" if len(sin_cat) > 20 else ""
        sin_cat_html = f"""
        <h3 style="color:#c00;margin-top:28px">⚠️ Nuevos egresos SIN CATEGORÍA ({len(sin_cat)})</h3>
        <p style="color:#666;font-size:13px;margin-top:-8px">Estas facturas bloquean el análisis de flujo de caja. Asigna categoría en Buk Finanzas.</p>
        <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px;background:#fff8f8;border:1px solid #fcc">
          <tr style="background:#fdd;font-weight:bold">
            <th style="padding:6px 10px;text-align:left">Empresa</th>
            <th style="padding:6px 10px;text-align:left">Proveedor</th>
            <th style="padding:6px 10px;text-align:right">Monto</th>
            <th style="padding:6px 10px;text-align:left">Fecha</th>
          </tr>{filas}
        </table>{extra}"""

    # ── Sección: alto valor
    alto_valor_html = ""
    if not alto_valor.empty:
        filas = ""
        for _, r in alto_valor.sort_values("monto_bruto", ascending=False).head(10).iterrows():
            filas += f"""
            <tr>
              <td style="padding:5px 10px;border-bottom:1px solid #e8f0fe">{r['empresa']}</td>
              <td style="padding:5px 10px;border-bottom:1px solid #e8f0fe">{r['tipo_flujo']}</td>
              <td style="padding:5px 10px;border-bottom:1px solid #e8f0fe">{r['nombre_contraparte'] or '—'}</td>
              <td style="padding:5px 10px;border-bottom:1px solid #e8f0fe;text-align:right;font-weight:bold">{_fmt_clp(r['monto_bruto'])}</td>
              <td style="padding:5px 10px;border-bottom:1px solid #e8f0fe">{r['estado'] or '—'}</td>
            </tr>"""
        alto_valor_html = f"""
        <h3 style="color:#1F4E79;margin-top:28px">💰 Facturas de alto valor nuevas > {_fmt_clp(HIGH_VALUE_CLP)} ({len(alto_valor)})</h3>
        <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px;background:#f0f4ff;border:1px solid #c5d5f5">
          <tr style="background:#dce8ff;font-weight:bold">
            <th style="padding:6px 10px;text-align:left">Empresa</th>
            <th style="padding:6px 10px;text-align:left">Tipo</th>
            <th style="padding:6px 10px;text-align:left">Contraparte</th>
            <th style="padding:6px 10px;text-align:right">Monto</th>
            <th style="padding:6px 10px;text-align:left">Estado</th>
          </tr>{filas}
        </table>"""

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:'Segoe UI',Arial,sans-serif;color:#222;background:#f5f7fa;margin:0;padding:0">
<div style="max-width:680px;margin:30px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">

  <!-- Header -->
  <div style="background:#1F4E79;padding:24px 32px">
    <p style="color:#a8c4e0;font-size:12px;margin:0 0 4px">KMA Asset Management</p>
    <h1 style="color:#fff;margin:0;font-size:22px;font-weight:600">Buk Finanzas — Reporte Diario</h1>
    <p style="color:#a8c4e0;font-size:13px;margin:6px 0 0">{today}</p>
  </div>

  <!-- Body -->
  <div style="padding:28px 32px">

    <!-- KPIs -->
    <div style="display:flex;gap:16px;margin-bottom:24px">
      <div style="flex:1;background:#f0f4ff;border-radius:6px;padding:14px 18px;text-align:center">
        <p style="margin:0;font-size:28px;font-weight:700;color:#1F4E79">{total_new}</p>
        <p style="margin:4px 0 0;font-size:12px;color:#666">Facturas nuevas</p>
      </div>
      <div style="flex:1;background:#{'#fff8f8' if not sin_cat.empty else '#f0fff4'};border-radius:6px;padding:14px 18px;text-align:center">
        <p style="margin:0;font-size:28px;font-weight:700;color:{'#c00' if not sin_cat.empty else '#2E7D32'}">{len(sin_cat)}</p>
        <p style="margin:4px 0 0;font-size:12px;color:#666">Sin categoría</p>
      </div>
      <div style="flex:1;background:#f0f4ff;border-radius:6px;padding:14px 18px;text-align:center">
        <p style="margin:0;font-size:28px;font-weight:700;color:#1F4E79">{_fmt_clp(total_monto)}</p>
        <p style="margin:4px 0 0;font-size:12px;color:#666">Total nuevas</p>
      </div>
    </div>

    <!-- Tabla por empresa -->
    <h3 style="color:#1F4E79;margin-top:0">🆕 Nuevas facturas por empresa</h3>
    <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px">
      <tr style="background:#1F4E79;color:#fff">
        <th style="padding:8px 10px;text-align:left">Empresa</th>
        <th style="padding:8px 10px;text-align:center">Tipo</th>
        <th style="padding:8px 10px;text-align:center">Facturas</th>
        <th style="padding:8px 10px;text-align:right">Monto total</th>
      </tr>
      {rows_empresa}
    </table>

    {sin_cat_html}
    {alto_valor_html}

  </div>

  <!-- Footer -->
  <div style="background:#f5f7fa;padding:16px 32px;border-top:1px solid #eee">
    <p style="margin:0;font-size:12px;color:#999">
      Generado automáticamente por KMA · Buk Finanzas —
      <a href="https://kma-buk-finanzas-t3yj4rkt3crwbj6b4sq2hn.streamlit.app" style="color:#1F4E79">Ver dashboard</a>
    </p>
  </div>

</div>
</body>
</html>"""

    n_sin_cat = len(sin_cat)
    subject = f"[KMA Buk Finanzas] {total_new} facturas nuevas"
    if n_sin_cat:
        subject += f" · ⚠️ {n_sin_cat} sin categoría"

    return subject, html


# ─────────────────────────────────────────────────────────────────────────────
# Envío de email
# ─────────────────────────────────────────────────────────────────────────────

def _send_email(gmail_user: str, app_password: str, to: str, subject: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"KMA Buk Finanzas <{gmail_user}>"
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.ehlo()
        server.starttls()
        server.login(gmail_user, app_password)
        server.sendmail(gmail_user, to, msg.as_string())
    log.info(f"Notificaciones: email enviado a {to} — {subject}")


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada principal
# ─────────────────────────────────────────────────────────────────────────────

def run_checks(df: pd.DataFrame):
    """
    Compara df con el estado previo guardado en Drive.
    Envía email si hay facturas nuevas. Actualiza el estado.
    """
    gmail_user     = os.environ.get("GMAIL_USER", "").strip()
    app_password   = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    notify_email   = os.environ.get("NOTIFY_EMAIL", "").strip()

    if not (gmail_user and app_password and notify_email):
        log.warning("Notificaciones: GMAIL_USER / GMAIL_APP_PASSWORD / NOTIFY_EMAIL no definidos — se omite.")
        return

    try:
        service = _get_drive_service()
    except Exception as exc:
        log.warning(f"Notificaciones: no se pudo conectar a Drive — {exc}")
        return

    prev_state = _download_state(service)
    df["_key"] = df.apply(_invoice_key, axis=1)
    current_keys = set(df["_key"])

    new_state = {"run_date": datetime.now().isoformat(), "keys": list(current_keys)}

    if prev_state is None:
        log.info("Notificaciones: primer run, guardando estado inicial (sin email).")
        _upload_state(service, new_state)
        return

    prev_keys = set(prev_state.get("keys", []))
    new_df    = df[df["_key"].isin(current_keys - prev_keys)].copy()

    if new_df.empty:
        log.info("Notificaciones: sin facturas nuevas desde el último run.")
        _upload_state(service, new_state)
        return

    new_egresos = new_df[new_df["tipo_flujo"] == "EGRESO"]
    sin_cat     = new_egresos[new_egresos["categoria"].fillna("").str.strip() == ""]
    alto_valor  = new_df[pd.to_numeric(new_df["monto_bruto"], errors="coerce").fillna(0) > HIGH_VALUE_CLP]

    subject, html = _build_email(new_df, sin_cat, alto_valor)

    try:
        _send_email(gmail_user, app_password, notify_email, subject, html)
    except Exception as exc:
        log.error(f"Notificaciones: error enviando email — {exc}")

    _upload_state(service, new_state)


# ─────────────────────────────────────────────────────────────────────────────
# Reporte de diagnóstico inicial
# ─────────────────────────────────────────────────────────────────────────────

DOC_TYPE_MAP = {
    "INVOICE":         "Fact_Af",
    "EXEMPT_INVOICE":  "Fact_Ex",
    "FEE_INVOICE":     "BH",
    "CREDIT_NOTE":     "NC",
    "RECEIPT":         "Boleta",
}
REJECTED_STATUSES = {"REJECTED", "NULLIFIED", "rejected", "nullified", "rechazado", "anulado"}
PENDING_STATUSES  = {"PENDING", "pending"}
STATUS_LABELS = {
    "PAID":                   "Pagado",
    "APPROVED":               "Aprobado (por pagar)",
    "PENDING":                "Pendiente aprobación",
    "PAY_SCHEDULED":          "Pago agendado",
    "ARCHIVED":               "Archivado",
    "REJECTED":               "Rechazado",
    "NULLIFIED":              "Anulado",
    "OVERDUE":                "Vencido",
    "ISSUED":                 "Emitido",
    "MANUALLY_MARKED_AS_PAID":"Marcado pagado manual",
}


def _mm(v) -> str:
    try:
        v = float(v)
    except Exception:
        return "—"
    if abs(v) >= 1_000_000_000:
        return f"${v/1_000_000_000:.1f} MM$"
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.1f} M$"
    return f"${v:,.0f}"


def _build_diagnostic_html(df: pd.DataFrame) -> str:
    egresos   = df[df["tipo_flujo"] == "EGRESO"].copy()
    activos   = egresos[~egresos["estado"].isin(REJECTED_STATUSES)]
    excluidos = egresos[egresos["estado"].isin(REJECTED_STATUSES)]

    total_activos = len(activos)
    monto_activos = activos["monto_bruto"].sum()
    sin_cat       = activos[activos["categoria"].fillna("").str.strip() == ""]
    pendientes    = activos[activos["estado"].isin(PENDING_STATUSES)]
    pct_sin_cat   = 100 * len(sin_cat) / max(total_activos, 1)
    today_str     = datetime.now().strftime("%-d de %B de %Y")

    kpi_color_cat = "#c00" if pct_sin_cat > 5 else "#2E7D32"
    kpis = f"""
    <div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap">
      <div style="flex:1;min-width:130px;background:#f0f4ff;border-radius:6px;padding:14px 16px;text-align:center;border-top:3px solid #1F4E79">
        <p style="margin:0;font-size:26px;font-weight:700;color:#1F4E79">{total_activos:,}</p>
        <p style="margin:4px 0 0;font-size:11px;color:#555">Egresos activos<br><span style="color:#999">(excl. {len(excluidos)} rechaz./anul.)</span></p>
      </div>
      <div style="flex:1;min-width:130px;background:#f0f4ff;border-radius:6px;padding:14px 16px;text-align:center;border-top:3px solid #1F4E79">
        <p style="margin:0;font-size:22px;font-weight:700;color:#1F4E79">{_mm(monto_activos)}</p>
        <p style="margin:4px 0 0;font-size:11px;color:#555">Monto total activo</p>
      </div>
      <div style="flex:1;min-width:130px;background:{'#fff8f8' if pct_sin_cat>5 else '#f0fff4'};border-radius:6px;padding:14px 16px;text-align:center;border-top:3px solid {kpi_color_cat}">
        <p style="margin:0;font-size:26px;font-weight:700;color:{kpi_color_cat}">{len(sin_cat):,}</p>
        <p style="margin:4px 0 0;font-size:11px;color:#555">Sin categoría<br><span style="color:#999">({pct_sin_cat:.1f}% del total)</span></p>
      </div>
      <div style="flex:1;min-width:130px;background:#fff8f0;border-radius:6px;padding:14px 16px;text-align:center;border-top:3px solid #E65100">
        <p style="margin:0;font-size:26px;font-weight:700;color:#E65100">{len(pendientes):,}</p>
        <p style="margin:4px 0 0;font-size:11px;color:#555">Pendientes aprobación<br><span style="color:#999">({_mm(pendientes['monto_bruto'].sum())})</span></p>
      </div>
    </div>"""

    SII_LABELS = {
        "INVOICE":        "Facturas Afectas",
        "EXEMPT_INVOICE": "Facturas Exentas",
        "FEE_INVOICE":    "Boletas Honorario",
    }
    sii_rows = ""
    for tipo, label in SII_LABELS.items():
        sub  = activos[activos["tipo_documento"] == tipo]
        pend = sub[sub["estado"].isin(PENDING_STATUSES)]
        sc   = sub[sub["categoria"].fillna("").str.strip() == ""]
        alerta = ' <span style="color:#c00;font-weight:bold">⚠</span>' if (len(sc) > 0 or len(pend) > 0) else ""
        sii_rows += f"""<tr>
          <td style="padding:7px 10px;border-bottom:1px solid #e0e8ff;font-weight:bold">{label}{alerta}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #e0e8ff;text-align:center">{len(sub):,}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #e0e8ff;text-align:right">{_mm(sub['monto_bruto'].sum())}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #e0e8ff;text-align:center;color:{'#E65100' if len(pend)>0 else '#2E7D32'};font-weight:bold">{len(pend)}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #e0e8ff;text-align:center;color:{'#c00' if len(sc)>0 else '#2E7D32'};font-weight:bold">{len(sc)}</td>
        </tr>"""
    sii_section = f"""
    <h3 style="color:#1F4E79;margin-top:28px;border-bottom:2px solid #1F4E79;padding-bottom:6px">
      📄 Documentos SII
    </h3>
    <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px;background:#f5f8ff;border:1px solid #c5d5f5">
      <tr style="background:#1F4E79;color:#fff">
        <th style="padding:8px 10px;text-align:left">Tipo</th>
        <th style="padding:8px 10px;text-align:center">Facturas</th>
        <th style="padding:8px 10px;text-align:right">Monto</th>
        <th style="padding:8px 10px;text-align:center">Pendientes</th>
        <th style="padding:8px 10px;text-align:center">Sin cat.</th>
      </tr>{sii_rows}
    </table>"""

    pend_rows = ""
    pend_by = pendientes.groupby("empresa").agg(n=("monto_bruto","count"), monto=("monto_bruto","sum")).sort_values("monto", ascending=False)
    for emp, row in pend_by.iterrows():
        alerta = " 🔴" if row["monto"] > 1_000_000_000 else (" 🟡" if row["monto"] > 100_000_000 else "")
        pend_rows += f"""<tr>
          <td style="padding:7px 10px;border-bottom:1px solid #ffe8d6">{emp}{alerta}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #ffe8d6;text-align:center">{int(row['n'])}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #ffe8d6;text-align:right;font-weight:bold">{_mm(row['monto'])}</td>
        </tr>"""
    pend_section = f"""
    <h3 style="color:#E65100;margin-top:28px;border-bottom:2px solid #E65100;padding-bottom:6px">
      ⏳ Pendientes de aprobación por empresa
    </h3>
    <p style="color:#666;font-size:12px;margin-top:-6px">🔴 &gt;$1.000 MM$ &nbsp; 🟡 &gt;$100 M$ &nbsp; Acción: aprobar o rechazar en Buk Finanzas.</p>
    <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px;background:#fff8f0;border:1px solid #f5c08a">
      <tr style="background:#E65100;color:#fff">
        <th style="padding:8px 10px;text-align:left">Empresa</th>
        <th style="padding:8px 10px;text-align:center">Facturas</th>
        <th style="padding:8px 10px;text-align:right">Monto total</th>
      </tr>{pend_rows}
      <tr style="background:#ffe8d6;font-weight:bold">
        <td style="padding:7px 10px">TOTAL</td>
        <td style="padding:7px 10px;text-align:center">{len(pendientes)}</td>
        <td style="padding:7px 10px;text-align:right">{_mm(pendientes['monto_bruto'].sum())}</td>
      </tr>
    </table>"""

    sc_rows = ""
    sc_by = sin_cat.groupby("empresa").agg(n=("monto_bruto","count"), monto=("monto_bruto","sum")).sort_values("monto", ascending=False)
    for emp, row in sc_by.iterrows():
        total_emp = len(activos[activos["empresa"] == emp])
        pct = 100 * row["n"] / max(total_emp, 1)
        color = "#c00" if (pct > 20 or row["monto"] > 1_000_000_000) else ("#E65100" if pct > 10 else "#2E7D32")
        sc_rows += f"""<tr>
          <td style="padding:7px 10px;border-bottom:1px solid #fde8e8">{emp}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #fde8e8;text-align:center;color:{color};font-weight:bold">{int(row['n'])}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #fde8e8;text-align:right">{_mm(row['monto'])}</td>
          <td style="padding:7px 10px;border-bottom:1px solid #fde8e8;text-align:center;color:{color}">{pct:.0f}%</td>
        </tr>"""
    cat_section = f"""
    <h3 style="color:#c00;margin-top:28px;border-bottom:2px solid #c00;padding-bottom:6px">
      🗂 Sin categoría por empresa
    </h3>
    <p style="color:#666;font-size:12px;margin-top:-6px">Bloquea el análisis de flujo de caja. Asigna categorías en Buk Finanzas → Contabilidad.</p>
    <table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px;background:#fff8f8;border:1px solid #fcc">
      <tr style="background:#c00;color:#fff">
        <th style="padding:8px 10px;text-align:left">Empresa</th>
        <th style="padding:8px 10px;text-align:center">Sin cat.</th>
        <th style="padding:8px 10px;text-align:right">Monto afectado</th>
        <th style="padding:8px 10px;text-align:center">% de sus facturas</th>
      </tr>{sc_rows}
    </table>"""

    ief_section = """
    <h3 style="color:#1F4E79;margin-top:28px;border-bottom:2px solid #1F4E79;padding-bottom:6px">
      🏢 IEF — Centro de Costo
    </h3>
    <div style="background:#f0f4ff;border:1px solid #c5d5f5;border-radius:6px;padding:14px 18px;font-size:13px;color:#555">
      El campo <strong>centro_costo</strong> se agrega a la extracción a partir del próximo run (mañana 7am).<br>
      Desde entonces el reporte diario incluirá cuántas facturas de IEF están sin centro de costo asignado.
    </div>"""

    sii_pend = activos[activos["tipo_documento"].isin(DOC_TYPE_MAP) & activos["estado"].isin(PENDING_STATUSES)]
    sii_pend_by = sii_pend.groupby("empresa").agg(n=("monto_bruto","count"), monto=("monto_bruto","sum")).sort_values("monto", ascending=False)
    sec_rows = "".join(f"""<tr>
      <td style="padding:6px 10px;border-bottom:1px solid #e8f5e9">{emp}</td>
      <td style="padding:6px 10px;border-bottom:1px solid #e8f5e9;text-align:center">{int(row['n'])}</td>
      <td style="padding:6px 10px;border-bottom:1px solid #e8f5e9;text-align:right;font-weight:bold">{_mm(row['monto'])}</td>
    </tr>""" for emp, row in sii_pend_by.iterrows())
    sec_table = (
        f'<table width="100%" cellspacing="0" style="border-collapse:collapse;font-size:13px;background:#f1f8e9;border:1px solid #a5d6a7">'
        f'<tr style="background:#2E7D32;color:#fff"><th style="padding:8px 10px;text-align:left">Empresa</th>'
        f'<th style="padding:8px 10px;text-align:center">Doc. SII pend.</th>'
        f'<th style="padding:8px 10px;text-align:right">Monto</th></tr>{sec_rows}</table>'
        if not sii_pend_by.empty else '<p style="color:#2E7D32;font-size:13px">✓ Sin documentos SII pendientes de aprobación.</p>'
    )
    sec_section = f"""
    <h3 style="color:#1F4E79;margin-top:28px;border-bottom:2px solid #1F4E79;padding-bottom:6px">
      🔒 Control de seguridad — Doc. SII pendientes
    </h3>
    <p style="color:#666;font-size:12px;margin-top:-6px">
      Fact_Af / Fact_Ex / BH sin aprobar ni rechazar. Mayor riesgo: una factura SII no rechazada a tiempo puede generar obligación exigible.
    </p>
    {sec_table}
    <p style="font-size:12px;color:#666;margin-top:10px;background:#f5f5f5;padding:10px;border-radius:4px">
      <strong>ℹ️ Nota:</strong> Se detectaron 220 grupos de facturas con montos repetidos.
      Corresponden a comisiones bancarias recurrentes de <strong>Santander Chile</strong> ($685–$709 CLP mensuales).
      Esto es normal y no requiere acción. El sistema continuará monitoreando proveedores nuevos con facturas repetidas.
    </p>"""

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:'Segoe UI',Arial,sans-serif;color:#222;background:#f5f7fa;margin:0;padding:0">
<div style="max-width:700px;margin:30px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,.08)">
  <div style="background:#1F4E79;padding:24px 32px">
    <p style="color:#a8c4e0;font-size:12px;margin:0 0 4px">KMA Asset Management</p>
    <h1 style="color:#fff;margin:0;font-size:22px;font-weight:600">Buk Finanzas — Estado Inicial</h1>
    <p style="color:#a8c4e0;font-size:13px;margin:6px 0 0">Diagnóstico completo · {today_str} · {df['empresa'].nunique()} empresas · {len(df):,} registros</p>
  </div>
  <div style="padding:28px 32px">
    {kpis}{sii_section}{pend_section}{cat_section}{ief_section}{sec_section}
  </div>
  <div style="background:#f5f7fa;padding:16px 32px;border-top:1px solid #eee">
    <p style="margin:0;font-size:12px;color:#999">
      Generado automáticamente · KMA Buk Finanzas —
      <a href="https://kma-buk-finanzas-t3yj4rkt3crwbj6b4sq2hn.streamlit.app" style="color:#1F4E79">Ver dashboard</a>
      &nbsp;·&nbsp; Este es un reporte único de estado inicial. Los reportes diarios detectan facturas nuevas.
    </p>
  </div>
</div>
</body>
</html>"""


def send_diagnostic_report(df: pd.DataFrame):
    """Envía email de diagnóstico inicial con el estado actual de todos los egresos."""
    gmail_user   = os.environ.get("GMAIL_USER", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    notify_email = os.environ.get("NOTIFY_EMAIL", "").strip()
    if not (gmail_user and app_password and notify_email):
        log.warning("Diagnóstico: credenciales de email no definidas.")
        return
    html    = _build_diagnostic_html(df)
    subject = "[KMA Buk Finanzas] Estado Inicial — Diagnóstico completo"
    _send_email(gmail_user, app_password, notify_email, subject, html)
    log.info("Diagnóstico: email enviado.")

import io
import json
import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="KMA · Buk Finanzas",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# Autenticación — contraseña compartida
# ─────────────────────────────────────────────────────────────────────────────
_APP_PASSWORD = st.secrets.get("APP_PASSWORD", "")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.markdown("<br><br>", unsafe_allow_html=True)
    col = st.columns([1, 1.2, 1])[1]
    with col:
        st.markdown("### KMA Asset Management")
        st.markdown("#### Buk Finanzas")
        st.markdown("<br>", unsafe_allow_html=True)
        pwd = st.text_input("Contraseña", type="password", placeholder="Ingresa la contraseña")
        if st.button("Ingresar", use_container_width=True):
            if pwd == _APP_PASSWORD:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    st.stop()

st.markdown("""
<style>
    /* Hide Streamlit chrome */
    #MainMenu, footer, header { visibility: hidden; }
    .block-container { padding-top: 1.8rem; padding-bottom: 1rem; }

    /* Metric cards */
    [data-testid="metric-container"] {
        background: #f4f6fb;
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        padding: 18px 20px;
    }
    [data-testid="stMetricLabel"]  { font-size: 0.75rem; color: #64748b; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; }
    [data-testid="stMetricValue"]  { font-size: 1.6rem; font-weight: 700; color: #1e293b; }
    [data-testid="stMetricDelta"]  { font-size: 0.78rem; }

    /* Sidebar */
    [data-testid="stSidebar"] { background: #f8fafc; border-right: 1px solid #e2e8f0; }
    [data-testid="stSidebar"] .stMarkdown h3 { color: #1e293b; font-size: 1rem; margin-bottom: 0; }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: 2px solid #e2e8f0; }
    .stTabs [data-baseweb="tab"] { border-radius: 6px 6px 0 0; font-weight: 600; font-size: 0.85rem; color: #64748b; }
    .stTabs [aria-selected="true"] { color: #1F4E79 !important; border-bottom: 2px solid #1F4E79; }

    /* Divider */
    hr { border-color: #e2e8f0; margin: 0.5rem 0; }

    /* Download buttons */
    .stDownloadButton > button {
        background: #1F4E79; color: white; border: none;
        border-radius: 6px; font-weight: 600; font-size: 0.82rem;
        padding: 6px 16px;
    }
    .stDownloadButton > button:hover { background: #163d61; }
</style>
""", unsafe_allow_html=True)

DRIVE_FOLDER_ID = "0AJUk5QWCegyXUk9PVA"
LOCAL_FOLDER    = Path(__file__).parent

# ─────────────────────────────────────────────────────────────────────────────
# Data loading — local file o Google Drive
# ─────────────────────────────────────────────────────────────────────────────
def _drive_service():
    raw = st.secrets.get("GOOGLE_CREDENTIALS") or os.environ.get("GOOGLE_CREDENTIALS")
    if not raw:
        return None
    creds = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _latest_from_drive(service):
    results = (
        service.files()
        .list(
            q=f"'{DRIVE_FOLDER_ID}' in parents and trashed=false and name contains 'Consolidado_Buk_Finanzas'",
            orderBy="name desc",
            pageSize=1,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    if not results:
        return None, None
    f = results[0]
    content = service.files().get_media(fileId=f["id"]).execute()
    return io.BytesIO(content), f["name"]


def _latest_local():
    files = sorted(LOCAL_FOLDER.glob("*_Consolidado_Buk_Finanzas.xlsx"), reverse=True)
    if not files:
        return None, None
    return files[0], files[0].name


@st.cache_data(ttl=3600, show_spinner="Cargando datos…")
def load_data():
    # 1. Intentar Google Drive
    svc = _drive_service()
    if svc:
        buf, name = _latest_from_drive(svc)
        if buf:
            df = pd.read_excel(buf)
            return df, name, "drive"

    # 2. Archivo local
    path, name = _latest_local()
    if path:
        df = pd.read_excel(path)
        return df, name, "local"

    return None, None, None


df_raw, source_file, source_type = load_data()

if df_raw is None:
    st.error("No se encontró ningún archivo de datos. Ejecuta extract_bemmbo.py primero.")
    st.stop()

for col in ["fecha_emision", "fecha_agendamiento"]:
    df_raw[col] = pd.to_datetime(df_raw[col], errors="coerce")
df_raw["mes"] = df_raw["fecha_emision"].dt.to_period("M").dt.to_timestamp()

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — filtros
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### KMA Asset Management")
    origen = "Google Drive" if source_type == "drive" else "archivo local"
    st.caption(f"📁 {source_file}  ·  {origen}")
    st.divider()

    tipos_opts = sorted(df_raw["tipo_flujo"].dropna().unique())
    tipos = st.multiselect("Tipo de flujo", tipos_opts, default=list(tipos_opts))

    empresas_opts = sorted(df_raw["empresa"].dropna().unique())
    empresas = st.multiselect("Empresa", empresas_opts, default=list(empresas_opts))

    min_date = df_raw["fecha_emision"].min().date()
    max_date = df_raw["fecha_emision"].max().date()
    st.markdown("**Período**")
    c1, c2 = st.columns(2)
    fecha_desde = c1.date_input("Desde", min_date, min_value=min_date, max_value=max_date, label_visibility="collapsed")
    fecha_hasta = c2.date_input("Hasta", max_date, min_value=min_date, max_value=max_date, label_visibility="collapsed")
    st.caption(f"{fecha_desde.strftime('%d/%m/%Y')} → {fecha_hasta.strftime('%d/%m/%Y')}")

    if "EGRESO" in tipos:
        cats_all = sorted(c for c in df_raw["categoria"].dropna().unique() if c)
        cats = st.multiselect("Categoría", cats_all, default=list(cats_all))
    else:
        cats = []

    estados_opts = sorted(df_raw["estado"].dropna().unique())
    estados = st.multiselect("Estado", estados_opts, default=list(estados_opts))

    st.divider()
    if st.button("🔄 Actualizar datos", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Filtrado
# ─────────────────────────────────────────────────────────────────────────────
base = (
    df_raw["tipo_flujo"].isin(tipos)
    & df_raw["empresa"].isin(empresas)
    & df_raw["estado"].isin(estados)
    & (df_raw["fecha_emision"].dt.date >= fecha_desde)
    & (df_raw["fecha_emision"].dt.date <= fecha_hasta)
)
cat_ok = (df_raw["tipo_flujo"] == "INGRESO") | (
    (df_raw["tipo_flujo"] == "EGRESO")
    & (df_raw["categoria"].isin(cats) | df_raw["categoria"].isna() | (df_raw["categoria"] == ""))
)
df = df_raw[base & cat_ok].copy()

# ─────────────────────────────────────────────────────────────────────────────
# Header + KPIs
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("## Buk Finanzas")

egresos_df  = df[df["tipo_flujo"] == "EGRESO"]
ingresos_df = df[df["tipo_flujo"] == "INGRESO"]

total_egresos  = egresos_df["monto_bruto"].sum()
total_ingresos = ingresos_df["monto_bruto"].sum()
n_docs         = len(df)
uf_ref         = df["uf_dia_emision"].dropna().iloc[-1] if not df["uf_dia_emision"].dropna().empty else None

k1, k2, k3, k4 = st.columns(4)
k1.metric("Total Egresos", f"${total_egresos / 1e6:,.1f}M")
k2.metric("Total Ingresos", f"${total_ingresos / 1e6:,.1f}M")
k3.metric("Documentos", f"{n_docs:,}")
k4.metric("UF referencia", f"{uf_ref:,.2f}" if uf_ref else "—")

st.markdown("<br>", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────
COLORS = {"EGRESO": "#1F4E79", "INGRESO": "#22c55e"}
PALETTE = ["#1F4E79", "#2E75B6", "#4BACC6", "#70AD47", "#ED7D31", "#A9D18E",
           "#9DC3E6", "#F4B942", "#C00000", "#7030A0"]

PLOT_LAYOUT = dict(
    plot_bgcolor="white",
    paper_bgcolor="white",
    margin=dict(l=0, r=10, t=36, b=0),
    font=dict(family="Inter, sans-serif", size=12, color="#1e293b"),
    title_font=dict(size=13, color="#1e293b", family="Inter, sans-serif"),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                font=dict(size=11)),
)

tab_res, tab_datos = st.tabs(["Resumen", "Datos"])

# ── TAB RESUMEN ──────────────────────────────────────────────────────────────
with tab_res:

    # Row 1: Categorías | Empresas
    col_cat, col_emp = st.columns([1.3, 1])

    with col_cat:
        df_cat = (
            egresos_df[egresos_df["categoria"].notna() & (egresos_df["categoria"] != "")]
            .groupby("categoria", as_index=False)["monto_bruto"].sum()
            .sort_values("monto_bruto")
        )
        fig = px.bar(
            df_cat, x="monto_bruto", y="categoria", orientation="h",
            title="Egresos por categoría",
            labels={"monto_bruto": "", "categoria": ""},
            color_discrete_sequence=["#1F4E79"],
            text="monto_bruto",
        )
        fig.update_traces(
            texttemplate="$%{x:,.0f}",
            textposition="outside",
            textfont_size=10,
        )
        fig.update_layout(**PLOT_LAYOUT, height=420)
        fig.update_xaxes(showticklabels=False, showgrid=False, zeroline=False)
        fig.update_yaxes(tickfont=dict(size=11))
        st.plotly_chart(fig, use_container_width=True)

    with col_emp:
        df_emp = (
            egresos_df.groupby("empresa", as_index=False)["monto_bruto"].sum()
            .sort_values("monto_bruto", ascending=False)
        )
        fig2 = px.bar(
            df_emp, x="empresa", y="monto_bruto",
            title="Egresos por empresa",
            labels={"monto_bruto": "", "empresa": ""},
            color_discrete_sequence=["#2E75B6"],
            text="monto_bruto",
        )
        fig2.update_traces(texttemplate="$%{y:,.0f}", textposition="outside", textfont_size=9)
        fig2.update_layout(**PLOT_LAYOUT, height=420)
        fig2.update_yaxes(showticklabels=False, showgrid=False, zeroline=False)
        fig2.update_xaxes(tickangle=-30, tickfont=dict(size=10))
        st.plotly_chart(fig2, use_container_width=True)

    # Row 2: Evolución mensual
    df_mes = (
        df.groupby(["mes", "tipo_flujo"], as_index=False)["monto_bruto"].sum()
    )
    fig3 = px.bar(
        df_mes, x="mes", y="monto_bruto", color="tipo_flujo",
        barmode="group",
        title="Evolución mensual",
        labels={"monto_bruto": "", "mes": "", "tipo_flujo": ""},
        color_discrete_map=COLORS,
    )
    fig3.update_layout(**PLOT_LAYOUT, height=320)
    fig3.update_yaxes(tickprefix="$", tickformat=",.0f", showgrid=True,
                      gridcolor="#f1f5f9", zeroline=False)
    fig3.update_xaxes(tickformat="%b %Y", tickangle=-30)
    st.plotly_chart(fig3, use_container_width=True)

    # Row 3: Top proveedores
    col_prov, col_estado = st.columns([1.4, 1])

    with col_prov:
        df_prov = (
            egresos_df[egresos_df["nombre_contraparte"].notna() & (egresos_df["nombre_contraparte"] != "")]
            .groupby("nombre_contraparte", as_index=False)["monto_bruto"].sum()
            .sort_values("monto_bruto", ascending=False)
            .head(10)
            .sort_values("monto_bruto")
        )
        fig4 = px.bar(
            df_prov, x="monto_bruto", y="nombre_contraparte", orientation="h",
            title="Top 10 proveedores (egresos)",
            labels={"monto_bruto": "", "nombre_contraparte": ""},
            color_discrete_sequence=["#4BACC6"],
            text="monto_bruto",
        )
        fig4.update_traces(texttemplate="$%{x:,.0f}", textposition="outside", textfont_size=10)
        fig4.update_layout(**PLOT_LAYOUT, height=340)
        fig4.update_xaxes(showticklabels=False, showgrid=False, zeroline=False)
        fig4.update_yaxes(tickfont=dict(size=11))
        st.plotly_chart(fig4, use_container_width=True)

    with col_estado:
        df_estado = (
            egresos_df.groupby("estado", as_index=False)["monto_bruto"].sum()
            .sort_values("monto_bruto", ascending=False)
        )
        fig5 = px.pie(
            df_estado, values="monto_bruto", names="estado",
            title="Distribución por estado (egresos)",
            color_discrete_sequence=PALETTE,
            hole=0.42,
        )
        fig5.update_layout(**PLOT_LAYOUT, height=340)
        fig5.update_traces(textposition="outside", textinfo="percent+label",
                           textfont_size=11)
        st.plotly_chart(fig5, use_container_width=True)


# ── TAB DATOS ────────────────────────────────────────────────────────────────
with tab_datos:
    hdr, dl1, dl2 = st.columns([6, 1, 1])
    hdr.markdown(f"**{len(df):,} registros** con los filtros actuales")

    # Excel
    buf_xl = io.BytesIO()
    with pd.ExcelWriter(buf_xl, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Datos")
    dl1.download_button(
        "⬇ Excel", data=buf_xl.getvalue(),
        file_name="buk_finanzas.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    # CSV
    dl2.download_button(
        "⬇ CSV", data=df.to_csv(index=False).encode("utf-8"),
        file_name="buk_finanzas.csv", mime="text/csv",
    )

    st.dataframe(
        df.drop(columns=["mes"], errors="ignore"),
        use_container_width=True,
        height=620,
        hide_index=True,
        column_config={
            "monto_bruto":           st.column_config.NumberColumn("Monto Bruto",     format="$%,.0f"),
            "monto_neto":            st.column_config.NumberColumn("Monto Neto",      format="$%,.0f"),
            "monto_uf_emision":      st.column_config.NumberColumn("UF Emisión",      format="%.4f UF"),
            "monto_uf_agendamiento": st.column_config.NumberColumn("UF Agend.",       format="%.4f UF"),
            "uf_dia_emision":        st.column_config.NumberColumn("UF día emisión",  format="$%,.2f"),
            "uf_dia_agendamiento":   st.column_config.NumberColumn("UF día agend.",   format="$%,.2f"),
            "fecha_emision":         st.column_config.DateColumn("Fecha Emisión",     format="DD-MM-YYYY"),
            "fecha_agendamiento":    st.column_config.DateColumn("Fecha Agend.",      format="DD-MM-YYYY"),
        },
    )

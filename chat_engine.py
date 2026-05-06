from __future__ import annotations

import pandas as pd
import streamlit as st
import google.generativeai as genai
from google.api_core import exceptions as google_exc

# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────
MODEL_NAME      = "gemini-2.5-flash"   # fallback: gemini-2.0-flash
FORBIDDEN_COLS  = {"rut_contraparte", "numero_documento"}
MAX_ROWS_PROMPT = 60
MAX_HIST_TURNS  = 8

_GEN_CONFIG = genai.types.GenerationConfig(temperature=0.2, max_output_tokens=1024)

_SYSTEM_PROMPT = """\
Eres un asistente financiero de KMA Asset Management, holding inmobiliario chileno.
Trabajas exclusivamente con datos de Buk Finanzas (egresos e ingresos del holding).

Reglas:
1. Responde SOLO con base en el CONTEXTO provisto. Si falta información, dilo: \
"No cuento con ese dato en el período o filtros actuales."
2. Nunca inventes montos, proveedores, fechas ni categorías.
3. Formatea montos como $X.XXX.XXX CLP o X,XXXX UF.
4. Respuestas concisas (3–5 oraciones) salvo que se pida análisis extenso.
5. Idioma: español de Chile.
6. Si piden RUT, número de documento o folio, responde: \
"Por política de privacidad (Ley 21.719) no proceso identificadores únicos."
7. El contexto refleja los filtros activos del dashboard (empresa, fechas, categoría, etc.).\
"""


# ─────────────────────────────────────────────────────────────────────────────
# Cliente Gemini (cacheado por sesión)
# ─────────────────────────────────────────────────────────────────────────────
@st.cache_resource
def _get_model():
    api_key = st.secrets.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    genai.configure(api_key=api_key)
    return genai.GenerativeModel(
        model_name=MODEL_NAME,
        generation_config=_GEN_CONFIG,
        system_instruction=_SYSTEM_PROMPT,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sanitización
# ─────────────────────────────────────────────────────────────────────────────
def sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    return df.drop(columns=[c for c in FORBIDDEN_COLS if c in df.columns])


# ─────────────────────────────────────────────────────────────────────────────
# Construcción de contexto
# ─────────────────────────────────────────────────────────────────────────────
def _clp(n: float) -> str:
    return f"${n:,.0f} CLP"


def build_context(df: pd.DataFrame, question: str) -> str:
    df = sanitize_df(df)
    if df.empty:
        return "<CONTEXTO>Sin datos para los filtros actuales.</CONTEXTO>"

    egresos  = df[df["tipo_flujo"] == "EGRESO"]
    ingresos = df[df["tipo_flujo"] == "INGRESO"]

    f_min = df["fecha_emision"].min()
    f_max = df["fecha_emision"].max()
    f_min_s = f_min.strftime("%d-%m-%Y") if pd.notna(f_min) else "N/A"
    f_max_s = f_max.strftime("%d-%m-%Y") if pd.notna(f_max) else "N/A"

    total_eg  = egresos["monto_bruto"].sum()  if not egresos.empty  else 0
    total_ing = ingresos["monto_bruto"].sum() if not ingresos.empty else 0

    empresas   = ", ".join(sorted(df["empresa"].dropna().unique()))
    cats_uniq  = sorted(df["categoria"].dropna().replace("", pd.NA).dropna().unique())
    categorias = ", ".join(cats_uniq) if cats_uniq else "(sin categorías)"

    lines = [
        "<CONTEXTO>",
        f"Período: {f_min_s} — {f_max_s}",
        f"Registros totales: {len(df):,}",
        f"Total egresos: {_clp(total_eg)}",
        f"Total ingresos: {_clp(total_ing)}",
        f"Empresas en vista: {empresas}",
        f"Categorías presentes: {categorias}",
        "",
        "## Top 10 categorías por egreso",
    ]

    if not egresos.empty:
        top_cat = (
            egresos[egresos["categoria"].notna() & (egresos["categoria"] != "")]
            .groupby("categoria")["monto_bruto"].sum()
            .sort_values(ascending=False)
            .head(10)
        )
        for cat, val in top_cat.items():
            lines.append(f"  {cat}: {_clp(val)}")

    lines += ["", "## Top 10 proveedores por egreso"]
    if not egresos.empty:
        top_prov = (
            egresos[egresos["nombre_contraparte"].notna() & (egresos["nombre_contraparte"] != "")]
            .groupby("nombre_contraparte")["monto_bruto"].sum()
            .sort_values(ascending=False)
            .head(10)
        )
        for prov, val in top_prov.items():
            lines.append(f"  {prov}: {_clp(val)}")

    lines += ["", "## Totales mensuales"]
    df_mes = (
        df.groupby(["mes", "tipo_flujo"])["monto_bruto"]
        .sum()
        .unstack(fill_value=0)
        .reset_index()
    )
    df_mes["mes"] = pd.to_datetime(df_mes["mes"]).dt.strftime("%m-%Y")
    for _, row in df_mes.iterrows():
        eg  = _clp(row.get("EGRESO",  0))
        ing = _clp(row.get("INGRESO", 0))
        lines.append(f"  {row['mes']}: Egresos {eg} | Ingresos {ing}")

    lines += ["", "## Totales por empresa"]
    df_emp = (
        df.groupby(["empresa", "tipo_flujo"])["monto_bruto"]
        .sum()
        .unstack(fill_value=0)
        .reset_index()
    )
    for _, row in df_emp.iterrows():
        eg  = _clp(row.get("EGRESO",  0))
        ing = _clp(row.get("INGRESO", 0))
        lines.append(f"  {row['empresa']}: Egresos {eg} | Ingresos {ing}")

    lines += ["", "## Distribución por estado (egresos)"]
    if not egresos.empty:
        dist_est = egresos.groupby("estado")["monto_bruto"].sum().sort_values(ascending=False)
        for est, val in dist_est.items():
            lines.append(f"  {est}: {_clp(val)}")

    # Subset por keyword
    q_lower   = question.lower()
    kw_cols   = ["nombre_contraparte", "categoria", "empresa", "centro_costo", "tipo_documento"]
    mask      = pd.Series(False, index=df.index)
    for col in kw_cols:
        if col in df.columns:
            mask |= df[col].astype(str).str.lower().str.contains(q_lower, na=False)

    if mask.any():
        subset    = df[mask].head(MAX_ROWS_PROMPT)
        show_cols = [c for c in subset.columns if c != "mes"]
        total_kw  = mask.sum()
        lines += [
            "",
            f"## Detalle relevante ({total_kw:,} filas, mostrando primeras {len(subset)})",
            subset[show_cols].to_csv(index=False, date_format="%d-%m-%Y"),
        ]

    lines.append("</CONTEXTO>")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Función pública
# ─────────────────────────────────────────────────────────────────────────────
def ask(question: str, df_filtered: pd.DataFrame, history: list[dict]) -> str:
    model = _get_model()
    if model is None:
        return (
            "Configura **GEMINI_API_KEY** en `.streamlit/secrets.toml` para activar el chat. "
            "Obtén tu clave gratuita en https://aistudio.google.com/apikey"
        )

    context = build_context(df_filtered, question)

    recent   = history[-(MAX_HIST_TURNS * 2):]
    hist_txt = ""
    if recent:
        parts = [
            f"{'Usuario' if m['role'] == 'user' else 'Asistente'}: {m['content']}"
            for m in recent
        ]
        hist_txt = "\n[HISTORIAL RECIENTE]\n" + "\n".join(parts) + "\n[/HISTORIAL]\n"

    full_prompt = f"{context}{hist_txt}\n[PREGUNTA]\n{question}"

    try:
        response = model.generate_content(full_prompt)
        return response.text
    except google_exc.ResourceExhausted:
        return "Has alcanzado el límite gratuito de Gemini (15 req/min). Espera un momento e intenta de nuevo."
    except google_exc.PermissionDenied:
        return "GEMINI_API_KEY inválida o sin permisos. Verifica en .streamlit/secrets.toml."
    except Exception as e:
        return f"Error de conexión con Gemini. Intenta de nuevo. ({type(e).__name__})"

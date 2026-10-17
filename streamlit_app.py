import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date, datetime
from io import BytesIO

import pandas as pd
import pdfplumber
import streamlit as st

# ======================= CONFIGURACIÓN =======================
DB_PATH = "conciliacion.db"
USUARIOS = {"socio1": "clave1", "socio2": "clave2"}  # Cambiar. Recomendado: st.secrets
TOLERANCIA = 0.05  # diferencia máxima aceptada en pesos (tope permitido)

st.set_page_config(
    page_title="Liquidaciones y Conciliación - Castillo de Oro",
    page_icon="💳",
    layout="wide",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS facturas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT, tipo TEXT, punto_venta TEXT, numero TEXT,
    cuit_emisor TEXT, razon_social TEXT,
    neto REAL, iva REAL, total REAL, cae TEXT,
    cargado_por TEXT, fecha_carga TEXT,
    UNIQUE (tipo, punto_venta, numero, cuit_emisor)
);
CREATE TABLE IF NOT EXISTS liquidaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plataforma TEXT, banco TEXT, nro_lote TEXT,
    fecha_liquidacion TEXT, periodo_desde TEXT, periodo_hasta TEXT,
    bruto REAL, comision REAL, costo_financiero REAL,
    iva_comision REAL, retenciones REAL,
    neto_esperado REAL, neto_acreditado REAL, fecha_acreditacion TEXT,
    diferencia REAL, estado TEXT, notas TEXT,
    cargado_por TEXT, fecha_carga TEXT,
    ultima_modif_por TEXT, ultima_modif TEXT
);
CREATE TABLE IF NOT EXISTS liquidacion_factura (
    liquidacion_id INTEGER,
    factura_id INTEGER,
    PRIMARY KEY (liquidacion_id, factura_id)
);
CREATE TABLE IF NOT EXISTS actividad (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    momento TEXT, usuario TEXT, accion TEXT, detalle TEXT
);
"""


# ======================= BASE DE DATOS =======================
def init_db():
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(SCHEMA)
        con.commit()


def ejecutar(sql, params=()):
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        cur = con.execute(sql, params)
        con.commit()
        return cur.lastrowid


def consultar(sql, params=()):
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        return pd.read_sql_query(sql, con, params=params)


def ahora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def registrar(accion, detalle):
    ejecutar(
        "INSERT INTO actividad (momento, usuario, accion, detalle) VALUES (?,?,?,?)",
        (ahora(), st.session_state["usuario"], accion, detalle),
    )


# ======================= UTILIDADES =======================
def fmt(x):
    """Formato argentino: $ 1.234,56"""
    s = f"{float(x or 0):,.2f}"
    return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def num(x):
    """Convierte '1.234,56', '1234.56' o números a float."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("$", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def txt(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return str(x).strip().split(".0")[0] if str(x).endswith(".0") else str(x).strip()


def sin_acentos(t):
    return "".join(
        c for c in unicodedata.normalize("NFD", str(t))
        if unicodedata.category(c) != "Mn"
    ).lower()


def buscar_col(cols, *palabras):
    for c in cols:
        if all(p in c for p in palabras):
            return c
    return None


def calcular(bruto, com, cf, iva_com, ret, neto_acred, fecha_acred):
    """Devuelve (neto_esperado, diferencia, estado)."""
    esperado = (bruto or 0) - (com or 0) - (cf or 0) - (iva_com or 0) - (ret or 0)
    if not fecha_acred:
        return round(esperado, 2), None, "Pendiente de acreditación"
    dif = (neto_acred or 0) - esperado
    estado = "Conciliada" if abs(dif) <= TOLERANCIA else "Con diferencia"
    return round(esperado, 2), round(dif, 2), estado


# ======================= LECTURA DE PDF DE LIQUIDACIONES =======================
PATRONES = {
    "f_bruto": [r"total presentado", r"bruto", r"importe total", r"total ventas", r"venta total", r"total cupones"],
    "f_com": [r"arancel", r"comision"],
    "f_cf": [r"interes", r"financiacion", r"costo financiero", r"cargo financiero"],
    "f_iva": [r"iva 21", r"iva 10", r"iva s/ comision", r"iva comision"],
    "f_ret": [r"retencion", r"percepcion", r"sirtac", r"iibb", r"ingresos brutos", r"bienes durables", r"per\. ?iva"],
    "f_neto": [r"neto (liquidado|a acreditar|acreditado|a pagar|a cobrar)", r"total recibido", r"importe neto"],
}
NUM_RE = re.compile(r"-?\$?\s?\d{1,3}(?:\.\d{3})*,\d{2}")
FECHA_RE = re.compile(r"\b(\d{2})[/.-](\d{2})[/.-](\d{4})\b")
PLAT_CLAVES = {
    "naranja": "Naranja X", "banco naci": "Banco Nación", "mercado pago": "Mercado Pago",
    "payway": "Payway", "fiserv": "Fiserv", "+pagos": "+Pagos", "credicoop": "Banco Credicoop",
    "galicia": "Banco Galicia", "santander": "Banco Santander", "bbva": "BBVA", "macro": "Banco Macro",
}


def extraer_liquidacion_pdf(archivo):
    """Lee el PDF y devuelve (datos para el formulario, avisos). Los valores se revisan antes de guardar."""
    archivo.seek(0)
    with pdfplumber.open(archivo) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if not texto.strip():
        raise ValueError("El PDF no tiene texto seleccionable (puede ser escaneado).")

    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    datos, avisos = {}, []

    for campo, patrones in PATRONES.items():
        montos = []
        for linea in lineas:
            ll = sin_acentos(linea)
            if any(re.search(p, ll) for p in patrones):
                nums = NUM_RE.findall(linea)
                if nums:
                    montos.append(abs(num(nums[-1])))
        if montos:
            datos[campo] = montos[0] if campo in ("f_bruto", "f_neto") else round(sum(montos), 2)
        else:
            avisos.append(f"No se encontró el importe de '{campo}'. Complétalo a mano.")

    m = FECHA_RE.search(texto)
    if m:
        try:
            datos["f_fliq"] = date(int(m[3]), int(m[2]), int(m[1]))
        except ValueError:
            pass

    m = re.search(r"(?:liquidacion|lote|operacion|comprobante)\w*\.?\s*(?:n[º°o.]|nro\.?|numero)?\s*:?\s*(\d[\d-]{3,})", sin_acentos(texto))
    if m:
        datos["f_lote"] = m[1]

    tl = sin_acentos(texto)
    for clave, nombre in PLAT_CLAVES.items():
        if clave in tl:
            datos["f_plat"] = nombre
            break

    # Control de coherencia: bruto - deducciones debe dar el neto informado
    if {"f_bruto", "f_neto"} <= datos.keys():
        deducc = sum(datos.get(k, 0) for k in ("f_com", "f_cf", "f_iva", "f_ret"))
        dif = datos["f_bruto"] - deducc - datos["f_neto"]
        if abs(dif) > TOLERANCIA:
            avisos.append(f"El PDF no cierra: bruto − deducciones − neto da {fmt(dif)}. Revisa los importes.")

    return datos, avisos


def selector_rango(key):
    hoy = date.today()
    c1, c2 = st.columns(2)
    d = c1.date_input("Desde", value=date(hoy.year, 1, 1), key=f"{key}_d")
    h = c2.date_input("Hasta", value=hoy, key=f"{key}_h")
    return d.isoformat(), h.isoformat()


# ======================= IMPORTACIÓN AFIP =======================
def normalizar_afip(df):
    df.columns = [sin_acentos(c).strip() for c in df.columns]
    cols = list(df.columns)
    return {
        "fecha": buscar_col(cols, "fecha"),
        "tipo": buscar_col(cols, "tipo", "comprobante"),
        "pv": buscar_col(cols, "punto"),
        "numero": buscar_col(cols, "numero", "desde") or buscar_col(cols, "numero"),
        "cuit": buscar_col(cols, "nro", "doc"),
        "razon": buscar_col(cols, "denominacion") or buscar_col(cols, "razon"),
        "neto": buscar_col(cols, "neto", "gravado") or buscar_col(cols, "neto"),
        "iva": buscar_col(cols, "iva"),
        "total": buscar_col(cols, "total"),
        "cae": buscar_col(cols, "autoriz") or buscar_col(cols, "cae"),
    }


def importar_facturas(df_raw, usuario):
    df = df_raw.copy()
    m = normalizar_afip(df)
    faltan = [k for k in ("fecha", "tipo", "numero", "total") if not m[k]]
    if faltan:
        raise ValueError(f"No se encontraron las columnas: {faltan}. Verificar el archivo de AFIP.")

    def get(fila, k):
        return fila[m[k]] if m[k] else None

    nuevas = 0
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        for _, r in df.iterrows():
            fecha = pd.to_datetime(get(r, "fecha"), dayfirst=True, errors="coerce")
            if pd.isna(fecha):
                continue
            cur = con.execute(
                """INSERT OR IGNORE INTO facturas
                   (fecha, tipo, punto_venta, numero, cuit_emisor, razon_social,
                    neto, iva, total, cae, cargado_por, fecha_carga)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fecha.date().isoformat(),
                    txt(get(r, "tipo")),
                    txt(get(r, "pv")),
                    txt(get(r, "numero")),
                    txt(get(r, "cuit")),
                    txt(get(r, "razon")),
                    num(get(r, "neto")),
                    num(get(r, "iva")),
                    num(get(r, "total")),
                    txt(get(r, "cae")),
                    usuario,
                    ahora(),
                ),
            )
            nuevas += cur.rowcount
        con.commit()
    return nuevas, len(df)


# ======================= LOGIN =======================
init_db()

if "usuario" not in st.session_state:
    st.session_state["usuario"] = None

if not st.session_state["usuario"]:
    st.title("💳 Liquidaciones y Conciliación")
    with st.form("login"):
        u = st.selectbox("Usuario", list(USUARIOS))
        p = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Ingresar", type="primary"):
            if USUARIOS[u] == p:
                st.session_state["usuario"] = u
                registrar("Ingreso", "Sesión iniciada")
                st.rerun()
            else:
                st.error("Contraseña incorrecta.")
    st.stop()

usuario = st.session_state["usuario"]

# ======================= SIDEBAR =======================
st.sidebar.markdown(f"👤 **{usuario}**")
if st.sidebar.button("🚪 Salir"):
    registrar("Salida", "Sesión cerrada")
    st.session_state["usuario"] = None
    st.rerun()


@st.fragment(run_every=15)
def panel_actividad_lateral():
    st.sidebar.markdown("#### 🕘 Últimos movimientos")
    df = consultar(
        "SELECT momento, usuario, accion, detalle FROM actividad ORDER BY id DESC LIMIT 8"
    )
    for r in df.itertuples():
        st.sidebar.caption(f"{r.momento[11:16]} · **{r.usuario}** · {r.accion}: {r.detalle}")


panel_actividad_lateral()

# ======================= TITULO Y PESTAÑAS =======================
st.title("💳 Liquidaciones y Conciliación")
t1, t2, t3, t4, t5 = st.tabs([
    "📥 Facturas AFIP",
    "💳 Liquidaciones",
    "🔗 Conciliación",
    "📊 Informes",
    "🕘 Actividad",
])

# ---------- 1. FACTURAS AFIP ----------
with t1:
    st.subheader("Importar facturas emitidas desde AFIP")
    st.caption("En AFIP: Mis Comprobantes → Emitidos → Exportar (CSV o XLSX).")
    archivo = st.file_uploader("Archivo de AFIP", type=["csv", "xlsx"], key="up_afip")
    if archivo and st.button("Importar facturas", type="primary"):
        try:
            if archivo.name.lower().endswith("xlsx"):
                df = pd.read_excel(archivo, dtype=str)
            else:
                df = pd.read_csv(archivo, sep=None, engine="python", dtype=str)
            nuevas, total = importar_facturas(df, usuario)
            registrar("Importación AFIP", f"{nuevas} facturas nuevas de {total} en {archivo.name}")
            st.success(f"Se incorporaron {nuevas} facturas nuevas. Las duplicadas se omitieron.")
        except Exception as e:
            st.error(f"No se pudo importar: {e}")

    st.markdown("#### Facturas cargadas")
    d, h = selector_rango("fact")
    facturas = consultar(
        """SELECT fecha, tipo, punto_venta, numero, razon_social, neto, iva, total, cargado_por
           FROM facturas WHERE fecha BETWEEN ? AND ? ORDER BY fecha DESC""",
        (d, h),
    )
    st.caption(f"{len(facturas)} facturas en el rango | Total: {fmt(facturas.total.sum())}")
    st.dataframe(facturas, hide_index=True)

# ---------- 2. LIQUIDACIONES ----------
with t2:
    st.subheader("Registrar liquidación")
    facs = consultar("SELECT id, fecha, tipo, punto_venta, numero, total FROM facturas ORDER BY fecha DESC")
    opciones_fac = {
        int(r.id): f"{r.fecha} | {r.tipo} {r.punto_venta}-{r.numero} | {fmt(r.total)}"
        for r in facs.itertuples()
    }

    st.markdown("##### 1. Leer PDF de la liquidación")
    st.caption("Sube el PDF de cualquier plataforma o banco. Los campos se completan solos y tú los revisas antes de guardar.")
    pdf_liq = st.file_uploader("PDF de liquidación", type=["pdf"], key="up_pdf_liq")
    if pdf_liq is not None and st.button("Leer PDF y completar campos"):
        try:
            datos, avisos = extraer_liquidacion_pdf(pdf_liq)
            st.session_state.update(datos)
            registrar("Lectura PDF", f"Liquidación leída desde {pdf_liq.name}")
            st.success("Campos completados. Revisa los valores antes de guardar.")
            for a in avisos:
                st.warning(a)
        except Exception as e:
            st.error(f"No se pudo leer el PDF: {e}")

    st.markdown("##### 2. Revisar y guardar")
    with st.form("form_liq", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        plat = c1.text_input("Plataforma / tarjeta / POS (escribir libremente)", key="f_plat")
        banco = c2.text_input("Banco / cuenta de destino", key="f_banco")
        lote = c2.text_input("Nro. de lote / liquidación", key="f_lote")
        f_liq = c3.date_input("Fecha de liquidación", value=None, key="f_fliq")
        p_desde = c3.date_input("Período desde", value=date.today())
        p_hasta = c1.date_input("Período hasta", value=date.today())
        bruto = c2.number_input("Bruto liquidado", min_value=0.0, step=100.0, format="%.2f", key="f_bruto")
        com = c3.number_input("Comisiones / arancel", min_value=0.0, step=100.0, format="%.2f", key="f_com")
        cf = c1.number_input("Costo financiero / cuotas", min_value=0.0, step=100.0, format="%.2f", key="f_cf")
        iva_com = c2.number_input("IVA s/ comisiones", min_value=0.0, step=100.0, format="%.2f", key="f_iva")
        ret = c3.number_input("Retenciones / percepciones", min_value=0.0, step=100.0, format="%.2f", key="f_ret")
        neto_acred = c1.number_input("Neto acreditado (si ya llegó)", min_value=0.0, step=100.0, format="%.2f", key="f_neto")
        f_acred = c2.date_input("Fecha de acreditación (si ya llegó)", value=None)
        notas = st.text_area("Notas")
        vinc = st.multiselect(
            "Facturas relacionadas",
            options=list(opciones_fac),
            format_func=lambda i: opciones_fac[i],
        )
        guardar = st.form_submit_button("Guardar liquidación", type="primary")

    if guardar:
        f_liq = f_liq or date.today()
        if bruto <= 0:
            st.error("El bruto liquidado debe ser mayor a cero.")
        else:
            plataforma = plat.strip() or "Sin especificar"
            f_acred_iso = f_acred.isoformat() if f_acred else None
            esperado, dif, estado = calcular(bruto, com, cf, iva_com, ret, neto_acred, f_acred_iso)
            liq_id = ejecutar(
                """INSERT INTO liquidaciones
                   (plataforma, banco, nro_lote, fecha_liquidacion, periodo_desde, periodo_hasta,
                    bruto, comision, costo_financiero, iva_comision, retenciones,
                    neto_esperado, neto_acreditado, fecha_acreditacion, diferencia, estado, notas,
                    cargado_por, fecha_carga, ultima_modif_por, ultima_modif)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    plataforma, banco, lote, f_liq.isoformat(),
                    p_desde.isoformat(), p_hasta.isoformat(),
                    bruto, com, cf, iva_com, ret,
                    esperado, neto_acred if f_acred_iso else None, f_acred_iso, dif, estado, notas,
                    usuario, ahora(), usuario, ahora(),
                ),
            )
            for fid in vinc:
                ejecutar(
                    "INSERT OR IGNORE INTO liquidacion_factura (liquidacion_id, factura_id) VALUES (?,?)",
                    (liq_id, fid),
                )
            registrar("Liquidación", f"#{liq_id} {plataforma} por {fmt(bruto)} ({estado})")
            st.success(f"Liquidación #{liq_id} guardada. Estado: {estado}.")

    st.markdown("#### Liquidaciones registradas")
    d2, h2 = selector_rango("liq")
    busq = st.text_input("Buscar por plataforma, banco o lote")
    liqs = consultar(
        """SELECT id, plataforma, banco, nro_lote, fecha_liquidacion, bruto, comision,
                  costo_financiero, iva_comision, retenciones, neto_esperado,
                  neto_acreditado, fecha_acreditacion, diferencia, estado, cargado_por
           FROM liquidaciones WHERE fecha_liquidacion BETWEEN ? AND ? ORDER BY fecha_liquidacion DESC""",
        (d2, h2),
    )
    if busq:
        mask = liqs.apply(
            lambda c: c.astype(str).str.contains(busq, case=False, na=False)
        ).any(axis=1)
        liqs = liqs[mask]
    st.dataframe(liqs, hide_index=True)

# ---------- 3. CONCILIACIÓN ----------
with t3:
    st.subheader("Registrar acreditación bancaria")
    pend = consultar(
        """SELECT id, plataforma, nro_lote, neto_esperado FROM liquidaciones
           WHERE fecha_acreditacion IS NULL ORDER BY fecha_liquidacion"""
    )
    if pend.empty:
        st.info("No hay liquidaciones pendientes de acreditación.")
    else:
        etiquetas = {
            int(r.id): f"#{int(r.id)} · {r.plataforma} · lote {r.nro_lote or '-'} · esperado {fmt(r.neto_esperado)}"
            for r in pend.itertuples()
        }
        idsel = st.selectbox("Liquidación pendiente", list(etiquetas), format_func=etiquetas.get)
        with st.form("form_acred"):
            n_acred = st.number_input("Neto acreditado en banco", min_value=0.0, step=100.0, format="%.2f")
            f_ac = st.date_input("Fecha de acreditación", value=date.today())
            ok = st.form_submit_button("Registrar acreditación", type="primary")
        if ok:
            liq = consultar("SELECT * FROM liquidaciones WHERE id=?", (idsel,)).iloc[0]
            esperado, dif, estado = calcular(
                liq.bruto, liq.comision, liq.costo_financiero,
                liq.iva_comision, liq.retenciones, n_acred, f_ac.isoformat(),
            )
            ejecutar(
                """UPDATE liquidaciones SET neto_acreditado=?, fecha_acreditacion=?,
                   neto_esperado=?, diferencia=?, estado=?, ultima_modif_por=?, ultima_modif=?
                   WHERE id=?""",
                (n_acred, f_ac.isoformat(), esperado, dif, estado, usuario, ahora(), int(idsel)),
            )
            registrar("Acreditación", f"#{idsel}: acreditado {fmt(n_acred)} ({estado})")
            st.success(f"Acreditación registrada. Estado: {estado}.")

    st.subheader("Vincular facturas a una liquidación")
    todas = consultar("SELECT id, plataforma, nro_lote, bruto FROM liquidaciones ORDER BY id DESC")
    sin = consultar(
        """SELECT id, fecha, tipo, punto_venta, numero, total FROM facturas
           WHERE id NOT IN (SELECT factura_id FROM liquidacion_factura) ORDER BY fecha DESC"""
    )
    if todas.empty or sin.empty:
        st.info("Se necesitan liquidaciones y facturas sin vincular.")
    else:
        liq_sel = st.selectbox(
            "Liquidación",
            todas.id.tolist(),
            format_func=lambda i: f"#{i} · {todas.set_index('id').loc[i, 'plataforma']} · {fmt(todas.set_index('id').loc[i, 'bruto'])}",
        )
        fac_sel = st.multiselect(
            "Facturas sin vincular",
            sin.id.tolist(),
            format_func=lambda i: f"{sin.set_index('id').loc[i, 'fecha']} | {sin.set_index('id').loc[i, 'numero']} | {fmt(sin.set_index('id').loc[i, 'total'])}",
        )
        if st.button("Vincular facturas"):
            for fid in fac_sel:
                ejecutar(
                    "INSERT OR IGNORE INTO liquidacion_factura (liquidacion_id, factura_id) VALUES (?,?)",
                    (int(liq_sel), int(fid)),
                )
            registrar("Vinculación", f"Liquidación #{liq_sel} ← {len(fac_sel)} factura(s)")
            st.success("Facturas vinculadas.")

    st.subheader("Control: bruto liquidado vs. facturas vinculadas")
    control = consultar(
        """SELECT l.id, l.plataforma, l.fecha_liquidacion, l.bruto,
                  COALESCE(SUM(f.total), 0) AS facturado,
                  l.bruto - COALESCE(SUM(f.total), 0) AS diferencia_bruto
           FROM liquidaciones l
           LEFT JOIN liquidacion_factura lf ON lf.liquidacion_id = l.id
           LEFT JOIN facturas f ON f.id = lf.factura_id
           GROUP BY l.id ORDER BY l.fecha_liquidacion DESC"""
    )
    st.dataframe(control, hide_index=True)

# ---------- 4. INFORMES ----------
with t4:
    st.subheader("Informes para la toma de decisiones")
    d3, h3 = selector_rango("inf")
    plats_db = consultar("SELECT DISTINCT plataforma FROM liquidaciones ORDER BY plataforma")
    plats = st.multiselect("Plataformas (vacío = todas)", plats_db.plataforma.tolist())

    liq = consultar("SELECT * FROM liquidaciones WHERE fecha_liquidacion BETWEEN ? AND ?", (d3, h3))
    if plats:
        liq = liq[liq.plataforma.isin(plats)]

    if liq.empty:
        st.info("No hay liquidaciones en el rango seleccionado.")
    else:
        liq["deducciones"] = liq.comision + liq.costo_financiero + liq.iva_comision + liq.retenciones
        pendientes = liq[liq.fecha_acreditacion.isna()]

        k1, k2, k3, k4, k5 = st.columns(5)
        k1.metric("Bruto liquidado", fmt(liq.bruto.sum()))
        k2.metric("Deducciones", fmt(liq.deducciones.sum()))
        k3.metric("Neto acreditado", fmt(liq.neto_acreditado.sum()))
        k4.metric("Pendiente de acreditar", fmt(pendientes.neto_esperado.sum()))
        tasa = (liq.deducciones.sum() / liq.bruto.sum() * 100) if liq.bruto.sum() else 0
        k5.metric("Costo efectivo promedio", f"{tasa:,.2f} %".replace(",", "X").replace(".", ",").replace("X", "."))

        resumen = (
            liq.groupby("plataforma")
            .agg(
                liquidaciones=("id", "count"),
                bruto=("bruto", "sum"),
                deducciones=("deducciones", "sum"),
                neto_esperado=("neto_esperado", "sum"),
                neto_acreditado=("neto_acreditado", "sum"),
            )
            .reset_index()
        )
        resumen["costo_efectivo_%"] = (resumen.deducciones / resumen.bruto * 100).round(2)

        st.markdown("#### Resumen por plataforma")
        st.dataframe(resumen, hide_index=True)
        st.bar_chart(resumen.set_index("plataforma")[["bruto", "neto_acreditado"]])

        st.markdown("#### Detalle de deducciones")
        ded = liq.groupby("plataforma")[["comision", "costo_financiero", "iva_comision", "retenciones"]].sum().reset_index()
        st.dataframe(ded, hide_index=True)

        st.markdown("#### Fondos en tránsito (pendientes de acreditación)")
        st.dataframe(
            pendientes[["id", "plataforma", "nro_lote", "fecha_liquidacion", "neto_esperado"]],
            hide_index=True,
        )

        st.markdown("#### Facturas sin liquidar en el período")
        sin_liq = consultar(
            """SELECT fecha, tipo, punto_venta, numero, razon_social, total FROM facturas
               WHERE fecha BETWEEN ? AND ?
               AND id NOT IN (SELECT factura_id FROM liquidacion_factura)
               ORDER BY fecha""",
            (d3, h3),
        )
        st.dataframe(sin_liq, hide_index=True)

        buf = BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            resumen.to_excel(w, sheet_name="Resumen", index=False)
            ded.to_excel(w, sheet_name="Deducciones", index=False)
            liq.drop(columns=["deducciones"]).to_excel(w, sheet_name="Detalle", index=False)
            pendientes.to_excel(w, sheet_name="Pendientes", index=False)
            sin_liq.to_excel(w, sheet_name="Facturas sin liquidar", index=False)

        st.download_button(
            "📥 Descargar informe en Excel",
            data=buf.getvalue(),
            file_name=f"informe_liquidaciones_{d3}_{h3}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )
        registrar("Informe", f"Generó informe {d3} al {h3}")

# ---------- 5. ACTIVIDAD ----------
with t5:
    st.subheader("Actividad del equipo (se actualiza sola cada 15 segundos)")

    @st.fragment(run_every=15)
    def tabla_actividad():
        df = consultar(
            "SELECT momento, usuario, accion, detalle FROM actividad ORDER BY id DESC LIMIT 200"
        )
        st.dataframe(df, hide_index=True)

    tabla_actividad()

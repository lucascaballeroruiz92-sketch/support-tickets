import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import date, datetime
from io import BytesIO
from itertools import combinations

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
CREATE TABLE IF NOT EXISTS liquidacion_impuestos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    liquidacion_id INTEGER,
    impuesto TEXT,
    monto REAL
);
CREATE TABLE IF NOT EXISTS liquidacion_filas (
    liquidacion_id INTEGER,
    fecha TEXT,
    monto REAL
);
CREATE TABLE IF NOT EXISTS liquidacion_comprobantes (
    liquidacion_id INTEGER,
    pv TEXT,
    numero TEXT
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
COMPROBANTE_RE = re.compile(r"\b(\d{4,5})\s?-\s?(\d{6,8})\b")
PLAT_CLAVES = {
    "naranja": "Naranja X", "banco naci": "Banco Nación", "mercado pago": "Mercado Pago",
    "payway": "Payway", "fiserv": "Fiserv", "+pagos": "+Pagos", "credicoop": "Banco Credicoop",
    "galicia": "Banco Galicia", "santander": "Banco Santander", "bbva": "BBVA", "macro": "Banco Macro",
}


def facturas_en_texto(texto, bruto):
    """Busca facturas de AFIP que coincidan con el PDF.
    1) Por número de comprobante que aparezca en el PDF (punto de venta - número).
    2) Si no aparece, por monto: factura sin liquidar con total igual al bruto."""
    ids = set()
    for pv, nro in COMPROBANTE_RE.findall(texto):
        f = consultar(
            """SELECT id FROM facturas
               WHERE CAST(punto_venta AS INTEGER)=? AND CAST(numero AS INTEGER)=?""",
            (int(pv), int(nro)),
        )
        ids.update(f.id.tolist())
    if not ids and bruto:
        f = consultar(
            """SELECT id FROM facturas
               WHERE ABS(total - ?) <= ?
                 AND id NOT IN (SELECT factura_id FROM liquidacion_factura)""",
            (float(bruto), TOLERANCIA),
        )
        ids.update(f.id.tolist())
    return sorted(int(i) for i in ids)


FILA_FECHA_MONTO_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\b.*?(\d{1,3}(?:\.\d{3})*,\d{2})")


def filas_por_fecha(texto):
    """Lee las filas del PDF que empiezan con fecha de compra y traen un importe.
    Devuelve [{'fecha': date, 'monto': float}]. Una fila = una compra o un grupo del día."""
    filas = []
    for linea in texto.splitlines():
        m = FILA_FECHA_MONTO_RE.match(linea.strip())
        if not m:
            continue
        try:
            f = datetime.strptime(m[1], "%d/%m/%Y").date()
        except ValueError:
            continue
        filas.append({"fecha": f, "monto": num(m[2])})
    return filas


def buscar_combinacion(cands, objetivo):
    """Busca 1 a 4 facturas cuya suma coincida con el objetivo (tolerancia TOLERANCIA)."""
    for k in range(1, min(4, len(cands)) + 1):
        for combo in combinations(cands, k):
            if abs(sum(c[2] for c in combo) - objetivo) <= TOLERANCIA:
                return combo
    return None


def cruzar_por_dia(filas):
    """Por cada fila: facturas de AFIP con fecha ±1 día cuya suma coincida con el monto.
    Estados: Conciliado / Revisar (hay facturas pero no cierran) / Posnet sin factura."""
    ids = set()
    totales = {"Conciliado": 0.0, "Revisar": 0.0, "Posnet sin factura": 0.0}
    for fila in filas:
        f = fila["fecha"].isoformat()
        df = consultar(
            """SELECT id, fecha, total FROM facturas
               WHERE fecha BETWEEN date(?, '-1 day') AND date(?, '+1 day')
                 AND id NOT IN (SELECT factura_id FROM liquidacion_factura)
               ORDER BY ABS(julianday(fecha) - julianday(?))""",
            (f, f, f),
        )
        cands = [(int(r.id), r.fecha, float(r.total)) for r in df.itertuples()][:12]
        combo = buscar_combinacion(cands, fila["monto"]) if cands else None
        if combo:
            ids.update(c[0] for c in combo)
            estado = "Conciliado"
        elif cands:
            estado = "Revisar"
        else:
            estado = "Posnet sin factura"
        totales[estado] += fila["monto"]
    textos = [
        f"Cruce por día: conciliado {fmt(totales['Conciliado'])}",
        f"posnet sin factura {fmt(totales['Posnet sin factura'])}",
        f"a revisar {fmt(totales['Revisar'])}",
    ]
    return sorted(ids), textos


def extraer_liquidacion_pdf(archivo):
    """Lee el PDF y devuelve (datos, avisos). Incluye las facturas detectadas en 'f_facturas'."""
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
            avisos.append(f"No se encontró el importe de '{campo}'.")

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

    # Solo se guardan las filas y comprobantes; el cruce con AFIP se hace
    # cuando la factura ya está importada (ver vincular_liquidacion).
    datos["f_filas"] = filas_por_fecha(texto)
    datos["f_comprobantes"] = COMPROBANTE_RE.findall(texto)
    datos["f_facturas"] = []

    # Control de coherencia: bruto - deducciones debe dar el neto informado
    if {"f_bruto", "f_neto"} <= datos.keys():
        deducc = sum(datos.get(k, 0) for k in ("f_com", "f_cf", "f_iva", "f_ret"))
        dif = datos["f_bruto"] - deducc - datos["f_neto"]
        if abs(dif) > TOLERANCIA:
            avisos.append(f"El PDF no cierra: bruto − deducciones − neto da {fmt(dif)}. Revisa los importes.")

    return datos, avisos


def vincular_liquidacion(liq_id):
    """Vincula facturas ya importadas de AFIP a una liquidación guardada.
    1) Por número de comprobante leído del PDF. 2) Si no hay, por día y monto."""
    ids = set()
    comp = consultar("SELECT pv, numero FROM liquidacion_comprobantes WHERE liquidacion_id=?", (liq_id,))
    for r in comp.itertuples():
        f = consultar(
            """SELECT id FROM facturas
               WHERE CAST(punto_venta AS INTEGER)=? AND CAST(numero AS INTEGER)=?
                 AND id NOT IN (SELECT factura_id FROM liquidacion_factura)""",
            (int(r.pv), int(r.numero)),
        )
        ids.update(f.id.tolist())
    if not ids:
        filas_db = consultar("SELECT fecha, monto FROM liquidacion_filas WHERE liquidacion_id=?", (liq_id,))
        if not filas_db.empty:
            filas = [
                {"fecha": datetime.strptime(r.fecha, "%Y-%m-%d").date(), "monto": float(r.monto)}
                for r in filas_db.itertuples()
            ]
            ids_dia, _ = cruzar_por_dia(filas)
            ids.update(ids_dia)
    for fid in ids:
        ejecutar(
            "INSERT OR IGNORE INTO liquidacion_factura (liquidacion_id, factura_id) VALUES (?,?)",
            (liq_id, int(fid)),
        )
    return len(ids)


def vincular_pendientes(usuario):
    """Intenta vincular todas las liquidaciones que todavía no tienen factura."""
    pend = consultar(
        "SELECT id FROM liquidaciones WHERE id NOT IN (SELECT liquidacion_id FROM liquidacion_factura)"
    )
    total = 0
    for liq_id in pend.id.tolist():
        n = vincular_liquidacion(int(liq_id))
        if n:
            total += n
            registrar("Vinculación automática", f"Liquidación #{liq_id}: {n} factura(s) vinculadas")
    return total


def guardar_liquidacion_auto(datos, usuario, nombre_pdf):
    """Crea la liquidación a partir de los datos del PDF y vincula las facturas detectadas."""
    f_liq = datos.get("f_fliq") or date.today()
    bruto = datos.get("f_bruto", 0.0)
    com = datos.get("f_com", 0.0)
    cf = datos.get("f_cf", 0.0)
    iva = datos.get("f_iva", 0.0)
    ret = datos.get("f_ret", 0.0)
    esperado, _, estado = calcular(bruto, com, cf, iva, ret, None, None)
    plataforma = datos.get("f_plat", "Sin especificar")
    lote = datos.get("f_lote", "")
    nota = f"Cargado desde PDF: {nombre_pdf}."
    if not datos.get("f_facturas"):
        nota += " Pendiente de vincular con AFIP."
    if datos.get("f_neto"):
        nota += f" Neto según PDF: {fmt(datos['f_neto'])}."
    liq_id = ejecutar(
        """INSERT INTO liquidaciones
           (plataforma, banco, nro_lote, fecha_liquidacion, periodo_desde, periodo_hasta,
            bruto, comision, costo_financiero, iva_comision, retenciones,
            neto_esperado, neto_acreditado, fecha_acreditacion, diferencia, estado, notas,
            cargado_por, fecha_carga, ultima_modif_por, ultima_modif)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            plataforma, "", lote, f_liq.isoformat(), f_liq.isoformat(), f_liq.isoformat(),
            bruto, com, cf, iva, ret,
            esperado, None, None, None, estado, nota,
            usuario, ahora(), usuario, ahora(),
        ),
    )
    for f in datos.get("f_filas", []):
        ejecutar(
            "INSERT INTO liquidacion_filas (liquidacion_id, fecha, monto) VALUES (?,?,?)",
            (liq_id, f["fecha"].isoformat(), f["monto"]),
        )
    for pv, nro in datos.get("f_comprobantes", []):
        ejecutar(
            "INSERT INTO liquidacion_comprobantes (liquidacion_id, pv, numero) VALUES (?,?,?)",
            (liq_id, pv, nro),
        )
    # La liquidación ya quedó guardada. La vinculación con AFIP es un paso aparte
    # que no debe impedir el guardado.
    registrar("Liquidación automática", f"#{liq_id} {plataforma} desde {nombre_pdf}")
    try:
        n_vinc = vincular_liquidacion(liq_id)
    except Exception:
        n_vinc = 0
    return liq_id, estado, n_vinc


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
    st.title("Castillo de Oro")
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


# ======================= MENÚ ACORDEÓN EN EL LATERAL =======================
SUBMENUS = {
    "📊 Ventas": ["Carga de Registro de Ventas"],
    "💰 Tesorería": ["Liquidaciones", "Conciliación Bancaria"],
    "📈 Contabilidad": [
        "Facturas vs neto final",
        "Facturas impagas tras cruce bancario",
        "Impuestos para DDJJ",
        "Resumen general",
    ],
}

if "bloque_abierto" not in st.session_state:
    st.session_state["bloque_abierto"] = "📊 Ventas"


def alternar_bloque(nombre):
    actual = st.session_state["bloque_abierto"]
    st.session_state["bloque_abierto"] = None if actual == nombre else nombre


st.sidebar.markdown("---")
st.sidebar.subheader("📌 Módulos")
for nombre, opciones in SUBMENUS.items():
    abierto = st.session_state["bloque_abierto"] == nombre
    st.sidebar.button(
        ("▾ " if abierto else "▸ ") + nombre,
        key=f"btn_{nombre}",
        on_click=alternar_bloque,
        args=(nombre,),
        use_container_width=True,
    )
    if abierto:
        st.sidebar.radio(
            "Submenú:",
            opciones,
            key=f"sub_{nombre}",
            label_visibility="collapsed",
        )

st.title("Castillo de Oro")

bloque = st.session_state["bloque_abierto"]
if bloque is None:
    st.info("Haga clic en un módulo del menú lateral para desplegar sus opciones.")
    st.stop()

if bloque == "📊 Ventas":
    vista = SUBMENUS[bloque][0]
else:
    vista = st.session_state.get(f"sub_{bloque}", SUBMENUS[bloque][0])

# ---------- 1. VENTAS: CARGA DE REGISTRO ----------
if vista == "Carga de Registro de Ventas":
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
            n_vinc_afip = vincular_pendientes(usuario)
            registrar("Importación AFIP", f"{nuevas} facturas nuevas de {total} en {archivo.name}")
            st.success(
                f"Se incorporaron {nuevas} facturas nuevas. Facturas vinculadas automáticamente "
                f"con liquidaciones pendientes: {n_vinc_afip}."
            )
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


def fl(x):
    return 0.0 if pd.isna(x) else float(x)


def vista_planilla():
    st.subheader("Planilla de liquidaciones (hoja 2026)")
    st.caption(
        "Se completa sola al subir el PDF de cada liquidación en el formulario de abajo. "
        "Es solo de lectura: no se edita a mano."
    )
    base = consultar("""SELECT id, fecha_liquidacion, plataforma, nro_lote, bruto, comision,
               costo_financiero, iva_comision, retenciones, fecha_acreditacion, neto_acreditado
        FROM liquidaciones ORDER BY fecha_liquidacion, id""")
    facs = consultar("""SELECT lf.liquidacion_id AS id,
               GROUP_CONCAT(f.tipo || ' ' || f.punto_venta || '-' || f.numero, ', ') AS facturas,
               SUM(f.total) AS monto_fac
        FROM liquidacion_factura lf JOIN facturas f ON f.id = lf.factura_id
        GROUP BY lf.liquidacion_id""")
    if base.empty:
        st.info("Todavía no hay liquidaciones. Sube un PDF abajo para empezar.")
        return
    imp = consultar("""SELECT liquidacion_id AS id, impuesto, SUM(monto) AS monto
        FROM liquidacion_impuestos GROUP BY liquidacion_id, impuesto""")
    df = base.merge(facs, on="id", how="left")
    if not imp.empty:
        imp_p = imp.pivot(index="id", columns="impuesto", values="monto").reset_index()
        df = df.merge(imp_p, on="id", how="left")
    for col in ["IVA 21%", "IVA 10,5%", "IVA 27%", "SIRTAC / IIBB", "Percepciones", "Impuesto débitos y créditos"]:
        if col not in df.columns:
            df[col] = 0.0
        df[col] = df[col].fillna(0.0)
    df["facturas"] = df["facturas"].fillna("Sin factura vinculada")
    df["monto_fac"] = df["monto_fac"].fillna(0.0)
    df["ded"] = df.comision.fillna(0) + df.costo_financiero.fillna(0) + df.iva_comision.fillna(0) + df.retenciones.fillna(0)
    df["esperado"] = df.bruto.fillna(0) - df.ded
    df["control"] = (df.neto_acreditado - df.esperado).where(df.fecha_acreditacion.notna())
    df["fecha_liquidacion"] = pd.to_datetime(df.fecha_liquidacion, errors="coerce").dt.date
    df["fecha_acreditacion"] = pd.to_datetime(df.fecha_acreditacion, errors="coerce").dt.date

    renombre = {
        "id": "ID", "fecha_liquidacion": "FECHA LIQ.", "plataforma": "PLATAFORMA",
        "nro_lote": "N° LIQUIDACION", "facturas": "FACTURAS", "monto_fac": "MONTO FAC.",
        "bruto": "TT PRESENTADO", "comision": "ARANCEL", "costo_financiero": "FINANCIACION",
        "iva_comision": "IVA", "IVA 21%": "IVA 21%", "IVA 10,5%": "IVA 10,5%", "IVA 27%": "IVA 27%", "SIRTAC / IIBB": "SIRTAC / IIBB", "Percepciones": "PERCEPCIONES", "Impuesto débitos y créditos": "DÉB. Y CRÉD.", "retenciones": "PER./SIRTAC/OTROS", "ded": "TT DEDUCCIONES",
        "esperado": "ACREDITACION ESPERADA", "fecha_acreditacion": "FECHA ACRED.",
        "neto_acreditado": "ACREDITADO BANCO", "control": "CONTROL",
    }
    vista_df = df.rename(columns=renombre)[list(renombre.values())]

    dinero = st.column_config.NumberColumn(format="$ %.2f")
    st.dataframe(
        vista_df,
        hide_index=True,
        use_container_width=True,
        column_config={
            "FECHA LIQ.": st.column_config.DateColumn("FECHA LIQ.", format="DD/MM/YYYY"),
            "FECHA ACRED.": st.column_config.DateColumn("FECHA ACRED.", format="DD/MM/YYYY"),
            "TT PRESENTADO": dinero, "ARANCEL": dinero, "FINANCIACION": dinero,
            "IVA": dinero, "IVA 21%": dinero, "IVA 10,5%": dinero, "IVA 27%": dinero,
            "SIRTAC / IIBB": dinero, "PERCEPCIONES": dinero, "DÉB. Y CRÉD.": dinero,
            "PER./SIRTAC/OTROS": dinero, "ACREDITADO BANCO": dinero,
            "MONTO FAC.": dinero, "TT DEDUCCIONES": dinero,
            "ACREDITACION ESPERADA": dinero, "CONTROL": dinero,
        },
    )

    buf_p = BytesIO()
    with pd.ExcelWriter(buf_p, engine="openpyxl") as w:
        vista_df.to_excel(w, sheet_name="2026", index=False)
    st.download_button(
        "📥 Descargar planilla en Excel",
        data=buf_p.getvalue(),
        file_name="planilla_liquidaciones_2026.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------- 2. TESORERÍA: LIQUIDACIONES ----------
if vista == "Liquidaciones":
    sub_liq = st.radio(
        "Sección de Liquidaciones:",
        ["📊 Planilla (Excel)", "📤 Cargar liquidación"],
        horizontal=True,
        key="sub_liq",
    )

if vista == "Liquidaciones" and sub_liq == "📊 Planilla (Excel)":
    vista_planilla()

if vista == "Liquidaciones" and sub_liq == "📤 Cargar liquidación":
    st.subheader("1. Subir PDF de la liquidación (automático)")
    st.caption(
        "Sube el PDF de la liquidación. La app lee los importes, busca la factura de AFIP "
        "(por número de comprobante o por monto) y guarda la liquidación ya vinculada."
    )
    pdf_liq = st.file_uploader("PDF de liquidación", type=["pdf"], key="up_pdf_liq")
    if pdf_liq is not None and st.button("Procesar PDF y vincular factura", type="primary"):
        try:
            datos, avisos = extraer_liquidacion_pdf(pdf_liq)
            lote = datos.get("f_lote", "")
            ya = consultar(
                "SELECT id FROM liquidaciones WHERE nro_lote = ? AND nro_lote != ''", (lote,)
            ) if lote else pd.DataFrame()
            if not datos.get("f_bruto"):
                st.error("No se encontró el bruto en el PDF. Usa la carga manual de abajo.")
            elif not ya.empty:
                st.warning(f"Ya existe una liquidación con el lote {lote} (#{int(ya.id.iloc[0])}). No se duplicó.")
            else:
                liq_id, estado, n_vinc = guardar_liquidacion_auto(datos, usuario, pdf_liq.name)
                st.success(
                    f"Liquidación #{liq_id} guardada. Facturas vinculadas: "
                    f"{n_vinc}. Estado: {estado}."
                )
                for a in avisos:
                    st.warning(a)
        except Exception as e:
            import traceback
            st.error(f"No se pudo procesar el PDF: {e}")
            st.code(traceback.format_exc())

    if st.button("Re-intentar vincular liquidaciones pendientes"):
        n_pend = vincular_pendientes(usuario)
        st.success(f"Facturas vinculadas: {n_pend}.")

    st.subheader("2. Carga manual (solo si el PDF no se pudo leer)")
    facs = consultar("SELECT id, fecha, tipo, punto_venta, numero, total FROM facturas ORDER BY fecha DESC")
    opciones_fac = {
        int(r.id): f"{r.fecha} | {r.tipo} {r.punto_venta}-{r.numero} | {fmt(r.total)}"
        for r in facs.itertuples()
    }
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
        st.markdown("**Desglose de impuestos (para declaraciones juradas)**")
        i1, i2, i3 = st.columns(3)
        imp_iva21 = i1.number_input("IVA 21 %", min_value=0.0, step=100.0, format="%.2f", key="imp_iva21")
        imp_iva105 = i2.number_input("IVA 10,5 %", min_value=0.0, step=100.0, format="%.2f", key="imp_iva105")
        imp_iva27 = i3.number_input("IVA 27 %", min_value=0.0, step=100.0, format="%.2f", key="imp_iva27")
        imp_sirtac = i1.number_input("SIRTAC / IIBB", min_value=0.0, step=100.0, format="%.2f", key="imp_sirtac")
        imp_perc = i2.number_input("Percepciones (IVA, IIBB, otras)", min_value=0.0, step=100.0, format="%.2f", key="imp_perc")
        imp_debcred = i3.number_input("Impuesto débitos y créditos", min_value=0.0, step=100.0, format="%.2f", key="imp_debcred")
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
            for nombre_imp, monto_imp in [
                ("IVA 21%", imp_iva21), ("IVA 10,5%", imp_iva105), ("IVA 27%", imp_iva27),
                ("SIRTAC / IIBB", imp_sirtac), ("Percepciones", imp_perc),
                ("Impuesto débitos y créditos", imp_debcred),
            ]:
                if monto_imp:
                    ejecutar(
                        "INSERT INTO liquidacion_impuestos (liquidacion_id, impuesto, monto) VALUES (?,?,?)",
                        (liq_id, nombre_imp, monto_imp),
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

# ---------- 3. TESORERÍA: CONCILIACIÓN ----------
if vista == "Conciliación Bancaria":
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

# ---------- 4. CONTABILIDAD: INFORMES ----------
# Informe 1: facturas vs neto final (después de impuestos, intereses y tasas)
if vista == "Facturas vs neto final":
    st.subheader("Facturas vs. lo que quedó después de impuestos, intereses y tasas")
    d4, h4 = selector_rango("r1")
    r1 = consultar(
        """SELECT f.fecha, f.tipo, f.punto_venta, f.numero, f.razon_social, f.total AS facturado,
                  l.id AS liquidacion_id, l.plataforma, l.bruto, l.comision, l.costo_financiero,
                  l.iva_comision, l.retenciones, l.neto_esperado, l.neto_acreditado, l.estado,
                  (SELECT COALESCE(SUM(f2.total),0) FROM liquidacion_factura lf2
                     JOIN facturas f2 ON f2.id = lf2.factura_id
                     WHERE lf2.liquidacion_id = l.id) AS total_liq_facturas
           FROM facturas f
           JOIN liquidacion_factura lf ON lf.factura_id = f.id
           JOIN liquidaciones l ON l.id = lf.liquidacion_id
           WHERE f.fecha BETWEEN ? AND ?
           ORDER BY f.fecha""",
        (d4, h4),
    )
    if r1.empty:
        st.info("No hay facturas vinculadas a liquidaciones en el rango.")
    else:
        r1["peso"] = r1.facturado / r1.total_liq_facturas
        r1["deducciones"] = r1.peso * (r1.comision + r1.costo_financiero + r1.iva_comision + r1.retenciones)
        r1["neto_final"] = r1.peso * r1.neto_acreditado.fillna(r1.neto_esperado)
        r1["factura"] = r1.tipo + " " + r1.punto_venta.astype(str) + "-" + r1.numero.astype(str)
        m1, m2, m3 = st.columns(3)
        m1.metric("Facturado", fmt(r1.facturado.sum()))
        m2.metric("Descuentos (impuestos, intereses, tasas)", fmt(r1.deducciones.sum()))
        m3.metric("Neto final recibido", fmt(r1.neto_final.sum()))
        tabla1 = r1[["fecha", "factura", "razon_social", "plataforma", "facturado", "deducciones", "neto_final", "estado"]]
        st.dataframe(tabla1, hide_index=True)

# Informe 2: facturas impagas después del cruce con el banco
if vista == "Facturas impagas tras cruce bancario":
    st.subheader("Facturas sin acreditación bancaria confirmada")
    d5, h5 = selector_rango("r2")
    r2 = consultar(
        """SELECT f.fecha, f.tipo, f.punto_venta, f.numero, f.razon_social, f.total
           FROM facturas f
           WHERE f.fecha BETWEEN ? AND ?
             AND f.id NOT IN (
                 SELECT lf.factura_id FROM liquidacion_factura lf
                 JOIN liquidaciones l ON l.id = lf.liquidacion_id
                 WHERE l.fecha_acreditacion IS NOT NULL)
           ORDER BY f.fecha""",
        (d5, h5),
    )
    st.metric("Total impago", fmt(r2.total.sum() if not r2.empty else 0))
    if r2.empty:
        st.success("No hay facturas impagas en el rango.")
    else:
        st.dataframe(r2, hide_index=True)

# Informe 3: impuestos por liquidación, para declaraciones juradas
if vista == "Impuestos para DDJJ":
    st.subheader("Impuestos retenidos por liquidación (para DDJJ)")
    d6, h6 = selector_rango("r3")
    det = consultar(
        """SELECT l.id AS nro_liquidacion, l.fecha_liquidacion, l.plataforma, l.nro_lote,
                  COALESCE((SELECT GROUP_CONCAT(f.tipo || ' ' || f.punto_venta || '-' || f.numero, ', ')
                            FROM liquidacion_factura lf JOIN facturas f ON f.id = lf.factura_id
                            WHERE lf.liquidacion_id = l.id), '') AS facturas,
                  li.impuesto, li.monto
           FROM liquidacion_impuestos li
           JOIN liquidaciones l ON l.id = li.liquidacion_id
           WHERE l.fecha_liquidacion BETWEEN ? AND ?
           ORDER BY l.fecha_liquidacion, l.id""",
        (d6, h6),
    )
    if det.empty:
        st.info("No hay impuestos desglosados en el rango. Cárgalos en el formulario de Liquidaciones.")
    else:
        st.metric("Total impuestos recaudados", fmt(det.monto.sum()))
        st.markdown("#### Total por tipo de impuesto")
        por_tipo = det.groupby("impuesto", as_index=False).monto.sum().sort_values("monto", ascending=False)
        st.dataframe(por_tipo, hide_index=True)
        st.markdown("#### Detalle por liquidación")
        det["facturas"] = det.facturas.fillna("")
        piv = det.pivot_table(
            index=["nro_liquidacion", "fecha_liquidacion", "plataforma", "nro_lote", "facturas"],
            columns="impuesto", values="monto", aggfunc="sum", fill_value=0,
        ).reset_index()
        st.dataframe(piv, hide_index=True)
        buf3 = BytesIO()
        with pd.ExcelWriter(buf3, engine="openpyxl") as w:
            por_tipo.to_excel(w, sheet_name="Total por impuesto", index=False)
            piv.to_excel(w, sheet_name="Detalle por liquidacion", index=False)
        st.download_button(
            "📥 Descargar impuestos para DDJJ",
            data=buf3.getvalue(),
            file_name=f"impuestos_ddjj_{d6}_{h6}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary",
        )

# Informe general (resumen anterior)
if vista == "Resumen general":
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

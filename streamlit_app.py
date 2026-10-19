import re
import sqlite3
import traceback
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
TOLERANCIA = 0.05  # diferencia máxima aceptada en pesos

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
    liquidacion_id INTEGER, fecha TEXT, monto REAL
);
CREATE TABLE IF NOT EXISTS liquidacion_comprobantes (
    liquidacion_id INTEGER, pv TEXT, numero TEXT
);
CREATE TABLE IF NOT EXISTS liquidacion_pdf (
    liquidacion_id INTEGER PRIMARY KEY, neto_pdf REAL
);
CREATE TABLE IF NOT EXISTS extractos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archivo TEXT, banco TEXT, periodo_desde TEXT, periodo_hasta TEXT,
    cargado_por TEXT, fecha_carga TEXT
);
CREATE TABLE IF NOT EXISTS extracto_movimientos (
    extracto_id INTEGER, fecha TEXT, descripcion TEXT, monto REAL
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


# ======================= FORMATO DE NÚMEROS EN TABLAS =======================
# Muestra los importes como 1.234.567,89 (punto para miles, coma para centavos).
# Se aplica a todas las tablas de la app sin tocar cada llamada.
if not hasattr(st, "_df_original"):
    st._df_original = st.dataframe


def _numero_ar(x):
    if pd.isna(x):
        return ""
    s = f"{x:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _dataframe_ar(data, *args, **kwargs):
    kwargs.pop("column_config", None)
    if isinstance(data, pd.DataFrame):
        data = data.copy()
        for c in data.columns:
            if pd.api.types.is_float_dtype(data[c]):
                data[c] = data[c].map(_numero_ar)
    return st._df_original(data, *args, **kwargs)


st.dataframe = _dataframe_ar


def sin_acentos(t):
    return "".join(
        c for c in unicodedata.normalize("NFD", str(t))
        if unicodedata.category(c) != "Mn"
    ).lower()


def calcular(bruto, com, cf, iva_com, ret, neto_acred, fecha_acred):
    """Devuelve (neto_esperado, diferencia, estado) a partir de bruto y deducciones."""
    esperado = (bruto or 0) - (com or 0) - (cf or 0) - (iva_com or 0) - (ret or 0)
    if not fecha_acred:
        return round(esperado, 2), None, "Pendiente de acreditación"
    dif = (neto_acred or 0) - esperado
    estado = "Conciliada" if abs(dif) <= TOLERANCIA else "Con diferencia"
    return round(esperado, 2), round(dif, 2), estado


def selector_rango(key):
    hoy = date.today()
    c1, c2 = st.columns(2)
    d = c1.date_input("Desde", value=date(hoy.year, 1, 1), key=f"{key}_d")
    h = c2.date_input("Hasta", value=hoy, key=f"{key}_h")
    return d.isoformat(), h.isoformat()


# ======================= LECTURA DE PDF DE LIQUIDACIONES =======================
NUM_RE = re.compile(r"-?\$?\s?\d{1,3}(?:\.\d{3})*,\d{2}")
FECHA_RE = re.compile(r"\b(\d{2})[/.-](\d{2})[/.-](\d{4})\b")
FILA_FECHA_MONTO_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\b.*?(\d{1,3}(?:\.\d{3})*,\d{2})")
# Comprobante de factura: solo punto de venta y número (el tipo de letra no se usa)
COMP_RE = re.compile(r"(?:comprobante|factura|cbte)[^\n\d]{0,30}(\d{4,5})\s?-\s?(\d{6,8})", re.I)
PLAT_CLAVES = {
    "naranja": "Naranja X", "banco naci": "Banco Nación", "mercado pago": "Mercado Pago",
    "payway": "Payway", "fiserv": "Fiserv", "+pagos": "+Pagos", "credicoop": "Banco Credicoop",
    "galicia": "Banco Galicia", "santander": "Banco Santander", "bbva": "BBVA", "macro": "Banco Macro",
}


def importe_en(linea, primero=False):
    nums = NUM_RE.findall(linea)
    if not nums:
        return None
    return num(nums[0] if primero else nums[-1])


def tasa_iva(s):
    """Extrae la alícuota de una línea de IVA (21, 10.5 o 27)."""
    m = re.search(r"iva\D*(\d+(?:[.,]\d+)?)", s)
    return float(m[1].replace(",", ".")) if m else None


def filas_por_fecha(texto):
    """Filas del PDF que empiezan con fecha de compra y traen un importe."""
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


def extraer_liquidacion_pdf(archivo):
    """Lee el PDF de la liquidación y separa los impuestos por alícuota y tipo."""
    archivo.seek(0)
    with pdfplumber.open(archivo) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if not texto.strip():
        raise ValueError("El PDF no tiene texto seleccionable (puede ser escaneado).")

    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    ll = [sin_acentos(l) for l in lineas]
    datos, avisos = {}, []

    def sumar_si(cond):
        total, hallado = 0.0, False
        for l, s in zip(lineas, ll):
            if cond(s):
                v = importe_en(l)
                if v is not None:
                    total += abs(v)
                    hallado = True
        return round(total, 2) if hallado else 0.0

    # Bruto: primera fila 'Totales'
    bruto = None
    for l, s in zip(lineas, ll):
        if re.match(r"^totales\b", s):
            bruto = importe_en(l, primero=True)
            if bruto is not None:
                break
    if bruto is None:
        for i, s in enumerate(ll):
            if re.search(r"total presentado|importe total|total ventas|venta total|total cupones", s):
                bruto = importe_en(lineas[i])
                break
    if bruto is not None:
        datos["f_bruto"] = abs(bruto)
    else:
        avisos.append("No se encontró el importe de 'bruto'.")

    arancel = sumar_si(lambda s: s.startswith("arancel") or s.startswith("comision"))
    bonif = sumar_si(lambda s: s.startswith("bonificacion"))
    datos["f_com"] = round(arancel - bonif, 2)
    datos["f_cf"] = sumar_si(lambda s: re.match(r"(interes|financiacion|costo financiero|cargo financiero)", s) is not None)

    iva21 = sumar_si(lambda s: s.startswith("iva") and tasa_iva(s) == 21.0)
    iva105 = sumar_si(lambda s: s.startswith("iva") and tasa_iva(s) == 10.5)
    iva27 = sumar_si(lambda s: s.startswith("iva") and tasa_iva(s) == 27.0)
    perc = sumar_si(lambda s: "percepcion" in s)
    sirtac = sumar_si(lambda s: re.search(r"sirtac|iibb|ingresos brutos", s) is not None)
    debcred = sumar_si(lambda s: re.search(r"debito|credito|impuesto al cheque|ley 25413", s) is not None
                       and not s.startswith("total"))

    datos["f_iva"] = round(iva21 + iva105 + iva27, 2)
    datos["f_ret"] = round(perc + sirtac + debcred, 2)
    datos["f_desglose"] = {
        "IVA 21%": iva21, "IVA 10,5%": iva105, "IVA 27%": iva27,
        "Percepciones": perc, "SIRTAC / IIBB": sirtac, "Débitos y créditos": debcred,
    }

    neto = None
    for i, s in enumerate(ll):
        if re.search(r"^neto liquidado|total recibido|neto a (pagar|acreditar|cobrar)", s):
            neto = importe_en(lineas[i])
            if neto is None and i + 1 < len(lineas):
                neto = importe_en(lineas[i + 1])
            if neto is not None:
                break
    if neto is not None:
        datos["f_neto"] = abs(neto)
    else:
        avisos.append("No se encontró el neto liquidado.")

    m = re.search(r"Fecha de Pago\s*:?\s*(\d{2}/\d{2}/\d{4})", texto, re.I)
    fs = m.group(1) if m else None
    if not fs:
        m = FECHA_RE.search(texto)
        fs = m.group(0) if m else None
    if fs:
        try:
            d_, mo_, a_ = re.split(r"[/.-]", fs)
            datos["f_fliq"] = date(int(a_), int(mo_), int(d_))
        except (ValueError, TypeError):
            pass

    m = re.search(r"Tipo y N\S*\s*:?\s*([A-Za-z]-\d{4}-\d+)", texto, re.I)
    if m:
        datos["f_lote"] = m.group(1)

    tl = sin_acentos(texto)
    for clave, nombre in PLAT_CLAVES.items():
        if clave in tl:
            datos["f_plat"] = nombre
            break

    m = COMP_RE.search(texto)
    datos["f_comprobantes"] = [(m[1], m[2])] if m else []
    datos["f_filas"] = filas_por_fecha(texto)

    if {"f_bruto", "f_neto"} <= datos.keys():
        deducc = datos["f_com"] + datos["f_cf"] + datos["f_iva"] + datos["f_ret"]
        dif = datos["f_bruto"] - deducc - datos["f_neto"]
        if abs(dif) > TOLERANCIA:
            avisos.append(f"El PDF no cierra: bruto − deducciones − neto da {fmt(dif)}. Revisa los importes.")

    datos["f_texto"] = texto
    return datos, avisos


def guardar_liquidacion_pdf(datos, usuario, nombre_pdf):
    """Guarda la liquidación tal cual está en el PDF. No vincula facturas (eso es otro paso)."""
    f_liq = datos.get("f_fliq") or date.today()
    bruto = datos.get("f_bruto", 0.0)
    com = datos.get("f_com", 0.0)
    cf = datos.get("f_cf", 0.0)
    iva = datos.get("f_iva", 0.0)
    ret = datos.get("f_ret", 0.0)
    esperado, _, estado = calcular(bruto, com, cf, iva, ret, None, None)
    plataforma = datos.get("f_plat", "Sin especificar")
    lote = datos.get("f_lote", "")
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
            esperado, None, None, None, estado, f"Cargado desde PDF: {nombre_pdf}.",
            usuario, ahora(), usuario, ahora(),
        ),
    )
    ejecutar("INSERT OR REPLACE INTO liquidacion_pdf (liquidacion_id, neto_pdf) VALUES (?,?)",
             (liq_id, datos.get("f_neto")))
    for nombre_imp, monto in datos.get("f_desglose", {}).items():
        if monto:
            ejecutar("INSERT INTO liquidacion_impuestos (liquidacion_id, impuesto, monto) VALUES (?,?,?)",
                     (liq_id, nombre_imp, monto))
    for f in datos.get("f_filas", []):
        ejecutar("INSERT INTO liquidacion_filas (liquidacion_id, fecha, monto) VALUES (?,?,?)",
                 (liq_id, f["fecha"].isoformat(), f["monto"]))
    for pv, nro in datos.get("f_comprobantes", []):
        ejecutar("INSERT INTO liquidacion_comprobantes (liquidacion_id, pv, numero) VALUES (?,?,?)",
                 (liq_id, pv, nro))
    registrar("Liquidación cargada", f"#{liq_id} {plataforma} desde {nombre_pdf}")
    return liq_id, estado


def cantidad_vinculadas(liq_id):
    """Cuántas facturas AFIP están vinculadas a la liquidación."""
    df = consultar("SELECT COUNT(*) AS n FROM liquidacion_factura WHERE liquidacion_id=?", (int(liq_id),))
    return int(df.n.iloc[0])


def eliminar_liquidacion(liq_id, usuario):
    """Borra una liquidación y sus datos asociados. No permite borrar si tiene facturas vinculadas."""
    if cantidad_vinculadas(liq_id) > 0:
        return False, "La liquidación tiene comprobantes vinculados y no se puede borrar."
    liq_id = int(liq_id)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        info = con.execute("SELECT plataforma, nro_lote FROM liquidaciones WHERE id=?", (liq_id,)).fetchone()
        for tabla in ("liquidacion_impuestos", "liquidacion_filas", "liquidacion_comprobantes",
                      "liquidacion_pdf", "liquidacion_factura"):
            con.execute(f"DELETE FROM {tabla} WHERE liquidacion_id=?", (liq_id,))
        con.execute("DELETE FROM liquidaciones WHERE id=?", (liq_id,))
        con.commit()
    detalle = f"#{liq_id} {info[0]} {info[1] or ''}" if info else f"#{liq_id}"
    registrar("Liquidación eliminada", detalle)
    return True, f"Liquidación {detalle} eliminada."


def borrar_todos_los_datos():
    """Vacía todas las tablas de datos: liquidaciones, facturas AFIP, extractos y actividad."""
    tablas = ("liquidacion_impuestos", "liquidacion_filas", "liquidacion_comprobantes",
              "liquidacion_pdf", "liquidacion_factura", "liquidaciones",
              "extracto_movimientos", "extractos", "facturas", "actividad")
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        for tabla in tablas:
            con.execute(f"DELETE FROM {tabla}")
        con.execute("DELETE FROM sqlite_sequence")
        con.commit()


def borrar_todas_liquidaciones(usuario):
    """Borra todas las liquidaciones y sus datos asociados. No toca facturas AFIP ni extractos."""
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        n = con.execute("SELECT COUNT(*) FROM liquidaciones").fetchone()[0]
        for tabla in ("liquidacion_impuestos", "liquidacion_filas", "liquidacion_comprobantes",
                      "liquidacion_pdf", "liquidacion_factura", "liquidaciones"):
            con.execute(f"DELETE FROM {tabla}")
        con.commit()
    registrar("Borrado total de liquidaciones", f"{n} liquidaciones eliminadas por {usuario}")
    return n


# ======================= VINCULACIÓN CON AFIP =======================
def cruzar_por_dia(filas):
    """Por cada fila del PDF: facturas con fecha ±1 día cuya suma coincida con el monto."""
    ids = set()
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
        for k in range(1, min(4, len(cands)) + 1):
            encontrado = None
            for combo in combinations(cands, k):
                if abs(sum(c[2] for c in combo) - fila["monto"]) <= TOLERANCIA:
                    encontrado = combo
                    break
            if encontrado:
                ids.update(c[0] for c in encontrado)
                break
    return sorted(ids)


def vincular_liquidacion(liq_id):
    """1) Por número de comprobante del PDF. 2) Si no hay, por día y monto."""
    ids = set()
    comp = consultar("SELECT pv, numero FROM liquidacion_comprobantes WHERE liquidacion_id=?", (liq_id,))
    for r in comp.itertuples():
        f = consultar(
            """SELECT id FROM facturas
               WHERE CAST(punto_venta AS INTEGER)=? AND CAST(numero AS INTEGER)=?
                 AND id NOT IN (SELECT factura_id FROM liquidacion_factura)""",
            (int(r.pv), int(r.numero)),
        )
        ids.update(int(x) for x in f.id.tolist())
    if not ids:
        filas_db = consultar("SELECT fecha, monto FROM liquidacion_filas WHERE liquidacion_id=?", (liq_id,))
        if not filas_db.empty:
            filas = [
                {"fecha": datetime.strptime(r.fecha, "%Y-%m-%d").date(), "monto": float(r.monto)}
                for r in filas_db.itertuples()
            ]
            ids.update(cruzar_por_dia(filas))
    for fid in ids:
        ejecutar("INSERT OR IGNORE INTO liquidacion_factura (liquidacion_id, factura_id) VALUES (?,?)",
                 (liq_id, int(fid)))
    return len(ids)


def vincular_pendientes():
    pend = consultar(
        """SELECT id, plataforma, nro_lote FROM liquidaciones
           WHERE id NOT IN (SELECT liquidacion_id FROM liquidacion_factura)
           ORDER BY fecha_liquidacion, id"""
    )
    resultados = []
    for r in pend.itertuples():
        n = vincular_liquidacion(int(r.id))
        resultados.append({"Liquidación #": int(r.id), "Plataforma": r.plataforma,
                           "Lote": r.nro_lote, "Facturas vinculadas": n})
        if n:
            registrar("Vinculación", f"Liquidación #{r.id}: {n} factura(s)")
    return resultados


# ======================= IMPORTACIÓN AFIP =======================
def leer_afip(archivo):
    """Lee el export de AFIP saltando la fila de título hasta encontrar 'Fecha'."""
    archivo.seek(0)
    if archivo.name.lower().endswith("xlsx"):
        raw = pd.read_excel(archivo, header=None, dtype=str)
    else:
        raw = pd.read_csv(archivo, header=None, sep=None, engine="python", dtype=str)
    inicio = next(
        i for i, fila in raw.iterrows()
        if any(isinstance(x, str) and sin_acentos(x).strip() == "fecha" for x in fila)
    )
    df = raw.iloc[inicio + 1:].copy()
    df.columns = [str(x).strip() for x in raw.iloc[inicio]]
    return df.dropna(how="all")


def normalizar_afip(df):
    cols = {sin_acentos(c).strip(): c for c in df.columns}
    return {
        "fecha": cols.get("fecha"),
        "tipo": cols.get("tipo"),
        "pv": cols.get("punto de venta"),
        "numero": cols.get("numero desde"),
        "cuit": cols.get("nro. doc. receptor"),
        "razon": cols.get("denominacion receptor"),
        "neto": cols.get("neto gravado total"),
        "iva": cols.get("total iva"),
        "total": cols.get("imp. total"),
        "cae": cols.get("cod. autorizacion"),
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
                    txt(get(r, "tipo")), txt(get(r, "pv")), txt(get(r, "numero")),
                    txt(get(r, "cuit")), txt(get(r, "razon")),
                    num(get(r, "neto")), num(get(r, "iva")), num(get(r, "total")),
                    txt(get(r, "cae")), usuario, ahora(),
                ),
            )
            nuevas += cur.rowcount
        con.commit()
    return nuevas, len(df)


# ======================= EXTRACTOS BANCARIOS =======================
BANCOS_EXT = {
    "banco naci": "Banco Nación", "galicia": "Banco Galicia", "santander": "Banco Santander",
    "bbva": "BBVA", "macro": "Banco Macro", "credicoop": "Banco Credicoop", "ciudad": "Banco Ciudad",
    "provincia": "Banco Provincia", "icbc": "ICBC", "brubank": "Brubank", "naranja": "Naranja X",
    "mercado pago": "Mercado Pago",
}


def texto_de_archivo(archivo):
    archivo.seek(0)
    nombre = archivo.name.lower()
    if nombre.endswith(".pdf"):
        with pdfplumber.open(archivo) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    if nombre.endswith(".xlsx"):
        df = pd.read_excel(archivo, header=None, dtype=str)
        return "\n".join(" | ".join(str(x) for x in fila if pd.notna(x)) for fila in df.values.tolist())
    return archivo.read().decode("utf-8-sig", errors="ignore")


def movimientos_de_texto(texto):
    movs = []
    for linea in texto.splitlines():
        f = FECHA_RE.search(linea)
        if not f:
            continue
        montos = NUM_RE.findall(linea)
        if not montos:
            continue
        try:
            fecha = date(int(f[3]), int(f[2]), int(f[1]))
        except ValueError:
            continue
        movs.append({"fecha": fecha, "descripcion": linea.strip()[:200], "monto": num(montos[-1])})
    return movs


def guardar_extracto(archivo, usuario):
    texto = texto_de_archivo(archivo)
    movs = movimientos_de_texto(texto)
    tl = sin_acentos(texto)
    banco = next((n for k, n in BANCOS_EXT.items() if k in tl), "No identificado")
    desde = min((m["fecha"] for m in movs), default=None)
    hasta = max((m["fecha"] for m in movs), default=None)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        cur = con.execute(
            """INSERT INTO extractos (archivo, banco, periodo_desde, periodo_hasta, cargado_por, fecha_carga)
               VALUES (?,?,?,?,?,?)""",
            (archivo.name, banco,
             desde.isoformat() if desde else None, hasta.isoformat() if hasta else None,
             usuario, ahora()),
        )
        ext_id = cur.lastrowid
        con.executemany(
            "INSERT INTO extracto_movimientos (extracto_id, fecha, descripcion, monto) VALUES (?,?,?,?)",
            [(ext_id, m["fecha"].isoformat(), m["descripcion"], m["monto"]) for m in movs],
        )
        con.commit()
    registrar("Extracto cargado", f"{archivo.name} ({banco}, {len(movs)} movimientos)")
    return len(movs), banco


def conciliar(usuario):
    """Para cada liquidación: factura − deducciones debe coincidir con un crédito del extracto,
    y ese crédito debe coincidir con el neto de la liquidación."""
    liqs = consultar(
        """SELECT l.id, l.plataforma, l.nro_lote, l.fecha_liquidacion, l.bruto,
               l.comision, l.costo_financiero, l.iva_comision, l.retenciones,
               (SELECT COALESCE(SUM(f.total),0) FROM liquidacion_factura lf
                  JOIN facturas f ON f.id = lf.factura_id WHERE lf.liquidacion_id = l.id) AS facturado,
               (SELECT COUNT(*) FROM liquidacion_factura lf WHERE lf.liquidacion_id = l.id) AS n_fac,
               (SELECT p.neto_pdf FROM liquidacion_pdf p WHERE p.liquidacion_id = l.id) AS neto_pdf
           FROM liquidaciones l ORDER BY l.fecha_liquidacion, l.id"""
    )
    movs = consultar("SELECT rowid AS mid, fecha, descripcion, monto FROM extracto_movimientos")
    usados, filas = set(), []
    for L in liqs.itertuples():
        ded = sum(float(x or 0) for x in (L.comision, L.costo_financiero, L.iva_comision, L.retenciones))
        base = float(L.facturado) if L.n_fac > 0 else float(L.bruto or 0)
        factura_menos_ded = round(base - ded, 2)
        f_pago = date.fromisoformat(L.fecha_liquidacion) if L.fecha_liquidacion else None
        acred, f_ac, estado = None, None, "Sin acreditación en extracto"
        ventana = [
            m for m in movs.itertuples()
            if m.mid not in usados and (f_pago is None or abs((date.fromisoformat(m.fecha) - f_pago).days) <= 7)
        ]
        ok = next((m for m in ventana if abs(abs(m.monto) - factura_menos_ded) <= TOLERANCIA), None)
        if ok is not None:
            usados.add(ok.mid)
            acred, f_ac, estado = float(ok.monto), ok.fecha, "Conciliada"
        elif L.neto_pdf:
            ok2 = next((m for m in ventana if abs(abs(m.monto) - float(L.neto_pdf)) <= TOLERANCIA), None)
            if ok2 is not None:
                usados.add(ok2.mid)
                acred, f_ac, estado = float(ok2.monto), ok2.fecha, "Con diferencia"
        dif = round(abs(acred) - factura_menos_ded, 2) if acred is not None else None
        ejecutar(
            """UPDATE liquidaciones SET neto_acreditado=?, fecha_acreditacion=?, diferencia=?, estado=?,
               ultima_modif_por=?, ultima_modif=? WHERE id=?""",
            (acred, f_ac, dif, estado, usuario, ahora(), int(L.id)),
        )
        filas.append({
            "Liquidación #": int(L.id), "Plataforma": L.plataforma, "Lote": L.nro_lote,
            "Fecha pago": L.fecha_liquidacion, "Facturado": base, "Deducciones": ded,
            "Factura − deducciones": factura_menos_ded,
            "Neto según PDF": float(L.neto_pdf) if L.neto_pdf else None,
            "Acreditado en extracto": acred,
            "Dif. vs factura − ded.": dif,
            "Estado": estado,
        })
    registrar("Conciliación", f"Conciliadas {len(filas)} liquidaciones")
    return pd.DataFrame(filas)


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

# ======================= SIDEBAR Y MENÚ =======================
st.sidebar.markdown(f"👤 **{usuario}**")
if st.sidebar.button("🚪 Salir"):
    registrar("Salida", "Sesión cerrada")
    st.session_state["usuario"] = None
    st.rerun()

SUBMENUS = {
    "📊 Ventas": ["Carga de Registro de Ventas"],
    "💰 Tesorería": {
        "Liquidaciones": ["Carga de liquidación", "Vinculación de comprobantes", "Planilla Excel"],
        "Extractos Bancarios": ["Carga de extractos", "Conciliación"],
    },
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
    if abierto and isinstance(opciones, dict):
        seccion = st.sidebar.radio("Submenú:", list(opciones), key=f"sec_{nombre}")
        st.sidebar.radio("Opción:", opciones[seccion], key=f"hoja_{seccion}")
    elif abierto:
        st.sidebar.radio("Submenú:", opciones, key=f"sub_{nombre}", label_visibility="collapsed")

with st.sidebar.expander("⚠️ Reiniciar datos de prueba"):
    st.caption("Borra liquidaciones, facturas AFIP, extractos y actividad. No se puede deshacer.")
    conf_todo = st.checkbox("Entiendo que se borrará todo", key="chk_reset_todo")
    if st.button("Borrar todos los datos", disabled=not conf_todo, key="btn_reset_todo"):
        borrar_todos_los_datos()
        st.session_state.pop("resultado_conc", None)
        st.rerun()

st.title("Castillo de Oro")

bloque = st.session_state["bloque_abierto"]
if bloque is None:
    st.info("Haga clic en un módulo del menú lateral para desplegar sus opciones.")
    st.stop()

hoja = None
if bloque == "💰 Tesorería":
    vista = st.session_state.get(f"sec_{bloque}", "Liquidaciones")
    hoja = st.session_state.get(f"hoja_{vista}", SUBMENUS[bloque][vista][0])
elif bloque == "📊 Ventas":
    vista = SUBMENUS[bloque][0]
else:
    vista = st.session_state.get(f"sub_{bloque}", SUBMENUS[bloque][0])


# ======================= 1. VENTAS =======================
def vista_ventas():
    st.subheader("Importar facturas emitidas desde AFIP")
    st.caption("En AFIP: Mis Comprobantes → Emitidos → Exportar (CSV o XLSX).")
    archivo = st.file_uploader("Archivo de AFIP", type=["csv", "xlsx"], key="up_afip")
    if archivo and st.button("Importar facturas", type="primary"):
        try:
            df = leer_afip(archivo)
            nuevas, total = importar_facturas(df, usuario)
            registrar("Importación AFIP", f"{nuevas} facturas nuevas de {total} en {archivo.name}")
            st.success(f"Se incorporaron {nuevas} facturas nuevas de {total} filas.")
        except Exception as e:
            st.error(f"No se pudo importar: {e}")

    st.markdown("#### Facturas cargadas")
    d, h = selector_rango("fact")
    facturas = consultar(
        """SELECT fecha, punto_venta, numero, razon_social, neto, iva, total, cargado_por
           FROM facturas WHERE fecha BETWEEN ? AND ? ORDER BY fecha DESC""",
        (d, h),
    )
    st.caption(f"{len(facturas)} facturas en el rango | Total: {fmt(facturas.total.sum())}")
    st.dataframe(facturas, hide_index=True)


# ======================= 2. TESORERÍA: LIQUIDACIONES =======================
def liq_carga():
    st.subheader("Carga de liquidación")
    st.caption("Sube el PDF de la liquidación y pulsa Cargar. Se guarda tal como viene en el PDF.")
    pdf_liq = st.file_uploader("PDF de liquidación", type=["pdf"], key="up_pdf_liq")
    if pdf_liq is not None and st.button("Cargar", type="primary"):
        try:
            datos, avisos = extraer_liquidacion_pdf(pdf_liq)
            lote = datos.get("f_lote", "")
            ya = consultar("SELECT id FROM liquidaciones WHERE nro_lote = ? AND nro_lote != ''", (lote,)) if lote else pd.DataFrame()
            if not datos.get("f_bruto"):
                st.error("No se encontró el bruto en el PDF. No se guardó nada.")
                with st.expander("Texto leído del PDF"):
                    st.text(datos.get("f_texto", ""))
            elif not ya.empty:
                st.warning(f"Ya existe la liquidación {lote} (#{int(ya.id.iloc[0])}). No se duplicó.")
            else:
                liq_id, estado = guardar_liquidacion_pdf(datos, usuario, pdf_liq.name)
                st.success(f"Liquidación #{liq_id} cargada. Estado: {estado}.")
                for a in avisos:
                    st.warning(a)
        except Exception as e:
            st.error(f"No se pudo procesar el PDF: {e}")
            st.code(traceback.format_exc())

    st.markdown("#### Liquidaciones cargadas")
    d2, h2 = selector_rango("liq")
    liqs = consultar(
        """SELECT id, plataforma, nro_lote, fecha_liquidacion, bruto, comision,
                  costo_financiero, iva_comision, retenciones, estado
           FROM liquidaciones WHERE fecha_liquidacion BETWEEN ? AND ?
           ORDER BY fecha_liquidacion DESC""",
        (d2, h2),
    )
    st.dataframe(liqs, hide_index=True)

    st.markdown("#### Borrar todas las liquidaciones")
    st.caption("Borra todas las liquidaciones cargadas, con sus impuestos, filas, comprobantes y vinculaciones. No borra facturas AFIP ni extractos.")
    confirmar = st.checkbox("Entiendo que esta acción no se puede deshacer", key="chk_borrar_todo")
    if st.button("🗑️ Borrar todas las liquidaciones", type="primary", disabled=not confirmar, key="btn_borrar_todo"):
        n = borrar_todas_liquidaciones(usuario)
        st.success(f"Se borraron {n} liquidaciones.")
        st.rerun()


def guardar_vinculacion_manual(liq_id, factura_ids, usuario):
    """Reemplaza las facturas vinculadas a una liquidación por la selección manual."""
    liq_id = int(liq_id)
    with closing(sqlite3.connect(DB_PATH, timeout=30)) as con:
        con.execute("DELETE FROM liquidacion_factura WHERE liquidacion_id=?", (liq_id,))
        con.executemany(
            "INSERT INTO liquidacion_factura (liquidacion_id, factura_id) VALUES (?,?)",
            [(liq_id, int(f)) for f in factura_ids],
        )
        con.commit()
    registrar("Vinculación manual", f"Liquidación #{liq_id}: {len(factura_ids)} factura(s)")


def liq_vinculacion():
    st.subheader("Vinculación de comprobantes")
    st.markdown("#### Vincular manualmente")
    st.caption("Elegí una liquidación y marcá las facturas AFIP que le corresponden. Comparalas con el bruto.")
    liqs = consultar(
        "SELECT id, plataforma, nro_lote, fecha_liquidacion, bruto FROM liquidaciones ORDER BY fecha_liquidacion DESC, id DESC"
    )
    if liqs.empty:
        st.info("No hay liquidaciones cargadas.")
    else:
        opc = {
            f"#{int(r.id)} - {r.plataforma} - {r.nro_lote or 's/lote'} - {r.fecha_liquidacion}": r
            for r in liqs.itertuples()
        }
        eleg = st.selectbox("Liquidación", list(opc), key="vinc_liq_sel")
        L = opc[eleg]
        liq_id = int(L.id)
        bruto = float(L.bruto or 0)
        otras = consultar("SELECT factura_id FROM liquidacion_factura WHERE liquidacion_id != ?", (liq_id,))
        facts = consultar(
            """SELECT id, fecha, tipo, punto_venta, numero, razon_social, total FROM facturas
               ORDER BY fecha DESC"""
        )
        facts = facts[~facts.id.isin(otras.factura_id.tolist())]
        actuales = consultar("SELECT factura_id FROM liquidacion_factura WHERE liquidacion_id=?", (liq_id,)).factura_id.tolist()
        etiquetas = {
            int(r.id): f"{r.fecha} | {r.tipo} {r.punto_venta}-{r.numero} | {r.razon_social} | {fmt(r.total)}"
            for r in facts.itertuples()
        }
        c1, c2 = st.columns(2)
        c1.metric("Bruto de la liquidación", fmt(bruto))
        sel = st.multiselect(
            "Facturas AFIP",
            options=list(etiquetas),
            default=[int(x) for x in actuales if int(x) in etiquetas],
            format_func=lambda i: etiquetas[i],
            key=f"vinc_fac_{liq_id}",
        )
        total_sel = float(facts[facts.id.isin(sel)].total.sum()) if sel else 0.0
        dif = round(bruto - total_sel, 2)
        c2.metric("Total de facturas seleccionadas", fmt(total_sel))
        if sel and abs(dif) <= TOLERANCIA:
            st.success("El total de facturas coincide con el bruto de la liquidación.")
        elif sel:
            st.warning(f"Diferencia bruto − facturas: {fmt(dif)}")
        if st.button("Guardar vinculación", type="primary", key=f"btn_vinc_{liq_id}"):
            guardar_vinculacion_manual(liq_id, sel, usuario)
            st.success(f"Liquidación #{liq_id} vinculada con {len(sel)} factura(s).")
            st.rerun()
    st.markdown("#### Comprobantes vinculados")
    vinc = consultar(
        """SELECT l.id AS liquidacion, l.plataforma, l.nro_lote, f.fecha, f.tipo,
                  f.punto_venta, f.numero, f.razon_social, f.total
           FROM liquidacion_factura lf
           JOIN liquidaciones l ON l.id = lf.liquidacion_id
           JOIN facturas f ON f.id = lf.factura_id
           ORDER BY l.id, f.fecha"""
    )
    st.dataframe(vinc, hide_index=True)

    st.markdown("#### Eliminar liquidación")
    st.caption("Solo se pueden eliminar liquidaciones que no tengan comprobantes vinculados.")
    todas = consultar("SELECT id, plataforma, nro_lote, fecha_liquidacion FROM liquidaciones ORDER BY fecha_liquidacion DESC, id DESC")
    if todas.empty:
        st.info("No hay liquidaciones cargadas.")
    else:
        opciones = {
            f"#{int(r.id)} - {r.plataforma} - {r.nro_lote or 's/lote'} - {r.fecha_liquidacion}": int(r.id)
            for r in todas.itertuples()
        }
        elegida = st.selectbox("Liquidación", list(opciones), key="del_liq_sel")
        liq_sel = opciones[elegida]
        n_vinc = cantidad_vinculadas(liq_sel)
        if n_vinc > 0:
            st.warning(f"No se puede eliminar: tiene {n_vinc} comprobante(s) vinculado(s).")
        else:
            st.info("Esta liquidación no tiene comprobantes vinculados y puede eliminarse.")
            if st.button("🗑️ Eliminar liquidación", type="primary", key="btn_del_liq"):
                ok, msg = eliminar_liquidacion(liq_sel, usuario)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)


def liq_planilla():
    st.subheader("Planilla de liquidaciones por comprobante")
    st.caption("Una fila por factura. Las deducciones de cada liquidación se reparten según el peso de la factura.")
    df = consultar(
        """SELECT l.id AS liq, l.plataforma, l.nro_lote, l.fecha_liquidacion, l.fecha_acreditacion,
                  l.neto_acreditado, l.comision, l.costo_financiero, l.iva_comision, l.retenciones,
                  f.fecha AS fecha_fac, f.tipo, f.punto_venta, f.numero, f.razon_social, f.total,
                  (SELECT SUM(f2.total) FROM liquidacion_factura lf2 JOIN facturas f2 ON f2.id = lf2.factura_id
                    WHERE lf2.liquidacion_id = l.id) AS tot_liq
           FROM liquidacion_factura lf
           JOIN liquidaciones l ON l.id = lf.liquidacion_id
           JOIN facturas f ON f.id = lf.factura_id
           ORDER BY f.fecha, l.id"""
    )
    if df.empty:
        st.info("Todavía no hay comprobantes vinculados. Usa 'Vincular factura' primero.")
        return
    imp = consultar("SELECT liquidacion_id, impuesto, SUM(monto) AS monto FROM liquidacion_impuestos GROUP BY liquidacion_id, impuesto")
    imp_p = imp.pivot(index="liquidacion_id", columns="impuesto", values="monto").fillna(0) if not imp.empty else pd.DataFrame()
    peso = df.total / df.tot_liq
    out = pd.DataFrame({
        "FECHA FAC.": pd.to_datetime(df.fecha_fac).dt.strftime("%d/%m/%Y"),
        "COMPROBANTE": df.tipo + " " + df.punto_venta.astype(str) + "-" + df.numero.astype(str),
        "CLIENTE": df.razon_social,
        "MONTO FACTURA": df.total,
        "LIQ. N°": df.liq,
        "PLATAFORMA": df.plataforma,
        "ARANCEL": df.comision * peso,
        "INTERÉS / FINANCIACIÓN": df.costo_financiero * peso,
    })
    for col in ["IVA 21%", "IVA 10,5%", "IVA 27%"]:
        out[col] = (df.liq.map(imp_p[col]) if (not imp_p.empty and col in imp_p) else 0) * peso
    for col, etiqueta in [("Percepciones", "PERCEPCIONES"), ("SIRTAC / IIBB", "SIRTAC / IIBB"),
                          ("Impuesto débitos y créditos", "DÉB. Y CRÉD.")]:
        out[etiqueta] = (df.liq.map(imp_p[col]) if (not imp_p.empty and col in imp_p) else 0) * peso
    out["TOTAL DEDUCCIONES"] = (df.comision + df.costo_financiero + df.iva_comision + df.retenciones) * peso
    out["NETO PARA ESTA VENTA"] = df.total - out["TOTAL DEDUCCIONES"]
    out["FECHA ACRED."] = df.fecha_acreditacion
    out["ACREDITADO BANCO"] = df.neto_acreditado * peso
    st.dataframe(out.round(2), hide_index=True, use_container_width=True)
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        out.round(2).to_excel(w, sheet_name="2026", index=False)
    st.download_button(
        "📥 Descargar planilla en Excel", data=buf.getvalue(),
        file_name="planilla_liquidaciones.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ======================= 2b. TESORERÍA: EXTRACTOS =======================
def ext_carga():
    st.subheader("Carga de extractos")
    archivo = st.file_uploader("Extracto bancario (PDF, CSV o XLSX)", type=["pdf", "csv", "xlsx"], key="up_ext")
    if archivo is not None and st.button("Guardar", type="primary"):
        try:
            n, banco = guardar_extracto(archivo, usuario)
            st.success(f"Extracto guardado: {n} movimientos detectados ({banco}).")
        except Exception as e:
            st.error(f"No se pudo guardar el extracto: {e}")
    st.markdown("#### Extractos guardados")
    exts = consultar(
        """SELECT e.id, e.archivo, e.banco, e.periodo_desde, e.periodo_hasta,
                  (SELECT COUNT(*) FROM extracto_movimientos m WHERE m.extracto_id = e.id) AS movimientos
           FROM extractos e ORDER BY e.id DESC"""
    )
    st.dataframe(exts, hide_index=True)


def ext_conciliacion():
    st.subheader("Conciliación")
    st.caption("Por cada liquidación: factura − deducciones debe coincidir con lo acreditado en el extracto.")
    if st.button("Conciliar", type="primary"):
        st.session_state["resultado_conc"] = conciliar(usuario)
    if "resultado_conc" in st.session_state:
        res = st.session_state["resultado_conc"]
        if res.empty:
            st.info("No hay liquidaciones para conciliar.")
        else:
            st.dataframe(res.round(2), hide_index=True, use_container_width=True)


def vista_liquidaciones(sub):
    st.markdown("## 💰 Tesorería - Liquidaciones")
    if sub == "Carga de liquidación":
        liq_carga()
    elif sub == "Vinculación de comprobantes":
        liq_vinculacion()
    else:
        liq_planilla()


def vista_extractos(sub):
    st.markdown("## 💰 Tesorería - Extractos Bancarios")
    if sub == "Carga de extractos":
        ext_carga()
    else:
        ext_conciliacion()


# ======================= 3. CONTABILIDAD =======================
def vista_contabilidad(nombre):
    st.markdown(f"## 📈 Contabilidad - {nombre}")
    if nombre == "Facturas vs neto final":
        d4, h4 = selector_rango("r1")
        r1 = consultar(
            """SELECT f.fecha, f.tipo, f.punto_venta, f.numero, f.razon_social, f.total AS facturado,
                      l.plataforma, l.comision, l.costo_financiero, l.iva_comision, l.retenciones,
                      l.neto_acreditado, l.neto_esperado, l.estado,
                      (SELECT COALESCE(SUM(f2.total),0) FROM liquidacion_factura lf2
                         JOIN facturas f2 ON f2.id = lf2.factura_id WHERE lf2.liquidacion_id = l.id) AS tot
               FROM facturas f
               JOIN liquidacion_factura lf ON lf.factura_id = f.id
               JOIN liquidaciones l ON l.id = lf.liquidacion_id
               WHERE f.fecha BETWEEN ? AND ? ORDER BY f.fecha""",
            (d4, h4),
        )
        if r1.empty:
            st.info("No hay facturas vinculadas en el rango.")
        else:
            p = r1.facturado / r1.tot
            r1["descuentos"] = p * (r1.comision + r1.costo_financiero + r1.iva_comision + r1.retenciones)
            r1["neto_final"] = p * r1.neto_acreditado.fillna(r1.neto_esperado)
            r1["factura"] = r1.tipo + " " + r1.punto_venta.astype(str) + "-" + r1.numero.astype(str)
            m1, m2, m3 = st.columns(3)
            m1.metric("Facturado", fmt(r1.facturado.sum()))
            m2.metric("Descuentos", fmt(r1.descuentos.sum()))
            m3.metric("Neto final", fmt(r1.neto_final.sum()))
            st.dataframe(r1[["fecha", "factura", "razon_social", "plataforma", "facturado", "descuentos", "neto_final", "estado"]], hide_index=True)
    elif nombre == "Facturas impagas tras cruce bancario":
        d5, h5 = selector_rango("r2")
        r2 = consultar(
            """SELECT f.fecha, f.tipo, f.punto_venta, f.numero, f.razon_social, f.total FROM facturas f
               WHERE f.fecha BETWEEN ? AND ? AND f.id NOT IN (
                 SELECT lf.factura_id FROM liquidacion_factura lf
                 JOIN liquidaciones l ON l.id = lf.liquidacion_id WHERE l.estado = 'Conciliada')
               ORDER BY f.fecha""",
            (d5, h5),
        )
        st.metric("Total impago", fmt(r2.total.sum() if not r2.empty else 0))
        st.dataframe(r2, hide_index=True)
    elif nombre == "Impuestos para DDJJ":
        d6, h6 = selector_rango("r3")
        det = consultar(
            """SELECT l.id AS liquidacion, l.fecha_liquidacion, l.plataforma, li.impuesto, li.monto
               FROM liquidacion_impuestos li JOIN liquidaciones l ON l.id = li.liquidacion_id
               WHERE l.fecha_liquidacion BETWEEN ? AND ? ORDER BY l.fecha_liquidacion""",
            (d6, h6),
        )
        if det.empty:
            st.info("No hay impuestos desglosados en el rango.")
        else:
            st.metric("Total impuestos", fmt(det.monto.sum()))
            por_tipo = det.groupby("impuesto", as_index=False).monto.sum()
            st.dataframe(por_tipo, hide_index=True)
            st.dataframe(det.pivot_table(index=["liquidacion", "fecha_liquidacion", "plataforma"],
                                         columns="impuesto", values="monto", aggfunc="sum", fill_value=0).reset_index(),
                         hide_index=True)
    else:
        d3, h3 = selector_rango("inf")
        liq = consultar("SELECT * FROM liquidaciones WHERE fecha_liquidacion BETWEEN ? AND ?", (d3, h3))
        if liq.empty:
            st.info("No hay liquidaciones en el rango.")
        else:
            liq["deducciones"] = liq.comision + liq.costo_financiero + liq.iva_comision + liq.retenciones
            k1, k2, k3 = st.columns(3)
            k1.metric("Bruto", fmt(liq.bruto.sum()))
            k2.metric("Deducciones", fmt(liq.deducciones.sum()))
            k3.metric("Neto acreditado", fmt(liq.neto_acreditado.sum()))
            st.dataframe(liq.groupby("plataforma")[["bruto", "deducciones", "neto_acreditado"]].sum().reset_index(), hide_index=True)


if vista == "Carga de Registro de Ventas":
    vista_ventas()
elif vista == "Liquidaciones":
    vista_liquidaciones(hoja)
elif vista == "Extractos Bancarios":
    vista_extractos(hoja)
elif bloque == "📈 Contabilidad":
    vista_contabilidad(vista)

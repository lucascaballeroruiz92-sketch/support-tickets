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

# Nombres de los impuestos (se usan igual en la lectura, la base y la planilla)
IVA21, IVA105, IVA27 = "IVA 21%", "IVA 10,5%", "IVA 27%"
PERC, SIRTAC, BIEN, DEB = "Percepciones", "SIRTAC / IIBB", "Bienes durables (cuenta y orden)", "Débitos y créditos"
COM, INT = "Arancel / comisión", "Interés / financiación"
CATEGORIAS = [COM, INT, IVA21, IVA105, IVA27, PERC, SIRTAC, BIEN, DEB]
# Plataformas que informan comisión e interés con IVA 21 % incluido
IVA_INCLUIDO = {"mercadopago"}


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
    """Convierte '1.234,56', '1,234.56', '1234.56', '$ 369.000' o números a float."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("$", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")   # 1.234,56
        else:
            s = s.replace(",", "")                     # 1,234.56
    elif "," in s:
        s = s.replace(".", "").replace(",", ".")       # 1234,56
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")                         # 369.000 (miles)
    try:
        return float(s)
    except ValueError:
        return 0.0


def txt(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return ""
    return str(x).strip().split(".0")[0] if str(x).endswith(".0") else str(x).strip()


# ======================= FORMATO DE NÚMEROS EN TABLAS =======================
# Muestra los importes como 1.234.567,89 en todas las tablas de la app.
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
# Importes: "1.189.000,00", "$ 369.000", "$11,897.80" (cuando el signo o el $ acompañan el número,
# o cuando tienen centavos). Evita tomar códigos como "576.002" de los encabezados.
NUM_RE = re.compile(
    r"-?\$?\s?\d{1,3}(?:[.,]\d{3})*[.,]\d{2}(?!\d)"
    r"|-?\$\s?\d{1,3}(?:[.,]\d{3})*(?!\d)"
)
FECHA_RE = re.compile(r"\b(\d{2})[/.-](\d{2})[/.-](\d{4})\b")
FILA_FECHA_MONTO_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})\b.*?(\d{1,3}(?:\.\d{3})*,\d{2})")
# Comprobante de factura: solo punto de venta y número (el tipo de letra no se usa)
COMP_RE = re.compile(r"(?:comprobante|factura|cbte)[^\n\d]{0,30}(\d{4,5})\s?-\s?(\d{6,8})", re.I)
NEG_RE = re.compile(r"devol|bonif|reversa|nota de credito")
PLAT_CLAVES = [
    ("mercado pago", "mercadopago", "Mercado Pago"),
    ("naranja", "naranja", "Naranja X"),
    ("bienes durables", "bna", "Banco Nación"),
    ("banco naci", "bna", "Banco Nación"),
    ("payway", "payway", "Payway / Credicoop"),
    ("credicoop", "payway", "Payway / Credicoop"),
    ("fiserv", "generica", "Fiserv"),
    ("+pagos", "generica", "+Pagos"),
]


def importe_en(linea, primero=False):
    nums = NUM_RE.findall(linea)
    if not nums:
        return None
    return num(nums[0] if primero else nums[-1])


def tasa_iva(s):
    """Alícuota de una línea de IVA (21, 10.5 o 27)."""
    m = re.search(r"iva\D*(\d+(?:[.,]\d+)?)\s*%", s)
    return float(m[1].replace(",", ".")) if m else None


def detectar_plataforma(texto):
    tl = sin_acentos(texto)
    for patron, clave, nombre in PLAT_CLAVES:
        if patron in tl:
            return clave, nombre
    return "generica", "Sin especificar"


def clasificar(s):
    """Concepto contable de una línea (minúsculas, sin acentos). None si no interesa."""
    if re.match(r"^(totales?|neto|importe|fecha|tipo|cuenta|detalle|alicuota|a pagar|glosario)\b", s):
        return None
    if re.match(r"^\d{2}/\d{2}/\d{4}", s):  # filas de cupones: se leen aparte
        return None
    if "bienes durables" in s:
        return BIEN
    if re.search(r"sirtac|iibb|ingresos brutos", s):
        return SIRTAC
    if "percepcion" in s:
        return PERC
    if re.search(r"impuesto al cheque|ley 25413|debitos y creditos|impuesto a los debitos", s):
        return DEB
    if re.search(r"\biva\b", s):
        return {21.0: IVA21, 10.5: IVA105, 27.0: IVA27}.get(tasa_iva(s), IVA21)
    if re.search(r"interes|financ", s):
        return INT
    if re.search(r"arancel|comision|bonif|cargo|devol", s):
        return COM
    if "cuota" in s:
        return INT
    return None


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
    """Lee el PDF, detecta la plataforma y separa cada concepto por alícuota y tipo."""
    archivo.seek(0)
    with pdfplumber.open(archivo) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if not texto.strip():
        raise ValueError("El PDF no tiene texto seleccionable (puede ser escaneado).")

    clave, plataforma = detectar_plataforma(texto)
    avisos = []
    if clave == "generica":
        avisos.append("Plataforma no reconocida: se aplicaron reglas genéricas. Revisa el resultado.")

    # Solo se analiza la parte de liquidación: se corta en el glosario
    todas = [l.strip() for l in texto.splitlines() if l.strip()]
    corte = next((i for i, l in enumerate(todas) if sin_acentos(l).startswith("glosario")), len(todas))
    lineas = todas[:corte]
    ll = [sin_acentos(l) for l in lineas]

    acum = {c: 0.0 for c in CATEGORIAS}
    for l, s in zip(lineas, ll):
        cat = clasificar(s)
        if cat is None:
            continue
        v = importe_en(l)
        if v is None:
            continue
        v = -abs(v) if NEG_RE.search(s) else abs(v)
        if clave in IVA_INCLUIDO and cat in (COM, INT):
            base = round(v / 1.21, 2)
            acum[IVA21] += round(v - base, 2)
            v = base
        acum[cat] += v

    # Bruto: fila 'Totales' (primer importe) o rótulos alternativos
    bruto = None
    for l, s in zip(lineas, ll):
        if re.match(r"^totales\b", s):
            bruto = importe_en(l, primero=True)
            if bruto is not None:
                break
    if bruto is None:
        for l, s in zip(lineas, ll):
            if re.search(r"^cobro\b|total presentado|monto bruto|importe total|total ventas|total cupones|venta en", s):
                bruto = importe_en(l, primero=True)
                if bruto is not None:
                    break

    # Neto: rótulo con importe en la misma línea o en la siguiente
    neto = None
    for i, s in enumerate(ll):
        if re.search(r"neto liquidado|neto acreditado|total recibido|neto a (pagar|acreditar|cobrar)", s):
            neto = importe_en(lineas[i])
            if neto is None and i + 1 < len(lineas):
                neto = importe_en(lineas[i + 1])
            if neto is not None:
                break

    # Fecha de pago
    fecha_pago = None
    m = re.search(r"Fecha de Pago\s*:?\s*(\d{2}/\d{2}/\d{4})", texto, re.I)
    fs = m.group(1) if m else (FECHA_RE.search(texto).group(0) if FECHA_RE.search(texto) else None)
    if fs:
        try:
            fecha_pago = datetime.strptime(fs.replace("-", "/").replace(".", "/"), "%d/%m/%Y").date()
        except ValueError:
            pass

    # Número de liquidación / lote
    lote = ""
    m = re.search(r"Tipo y N\S*\s*:?\s*([A-Za-z]-\d{4}-\d+)", texto, re.I) or re.search(
        r"(?:liquidacion|liq\.?|lote|operacion)\s*(?:n[º°o.]?|nro\.?)?\s*:?\s*(\d[\w-]{3,})",
        sin_acentos(texto))
    if m:
        lote = m.group(1)

    iva_total = acum[IVA21] + acum[IVA105] + acum[IVA27]
    ret_total = acum[PERC] + acum[SIRTAC] + acum[BIEN] + acum[DEB]
    m = COMP_RE.search(texto)

    datos = {
        "f_plat": plataforma,
        "f_bruto": abs(bruto) if bruto is not None else None,
        "f_com": round(acum[COM], 2),          # puede ser negativo (bonificaciones)
        "f_cf": round(acum[INT], 2),
        "f_iva": round(iva_total, 2),
        "f_ret": round(ret_total, 2),
        "f_neto": abs(neto) if neto is not None else None,
        "f_fliq": fecha_pago,
        "f_lote": lote,
        "f_desglose": {
            IVA21: round(acum[IVA21], 2), IVA105: round(acum[IVA105], 2), IVA27: round(acum[IVA27], 2),
            PERC: round(acum[PERC], 2), SIRTAC: round(acum[SIRTAC], 2),
            BIEN: round(acum[BIEN], 2), DEB: round(acum[DEB], 2),
        },
        "f_comprobantes": [(m[1], m[2])] if m else [],
        "f_filas": filas_por_fecha(texto),
        "f_texto": texto,
    }

    if datos["f_bruto"] is None:
        avisos.append("No se encontró el importe de 'bruto'.")
    if datos["f_neto"] is None:
        avisos.append("No se encontró el neto liquidado.")
    if datos["f_bruto"] is not None and datos["f_neto"] is not None:
        deducc = datos["f_com"] + datos["f_cf"] + datos["f_iva"] + datos["f_ret"]
        dif = datos["f_bruto"] - deducc - datos["f_neto"]
        if abs(dif) > TOLERANCIA:
            avisos.append(f"El PDF no cierra: bruto − deducciones − neto da {fmt(dif)}. Revisa los importes.")
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


# ======================= VINCULACIÓN CON AFIP =======================
def cruzar_por_dia(filas):
    """Por cada fila del PDF: facturas con fecha ±1 día cuya suma (1 a 4) coincida con el monto."""
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
    """Por liquidación: factura − deducciones debe coincidir con un crédito del extracto,
    y ese crédito con el neto que informa el PDF."""
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
                st.success(f"Liquidación #{liq_id} cargada ({datos['f_plat']}). Estado: {estado}.")
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


def liq_vinculacion():
    st.subheader("Vinculación de comprobantes")
    st.caption("Cruza las liquidaciones cargadas con las facturas de AFIP ya importadas.")
    if st.button("Vincular factura", type="primary"):
        res = vincular_pendientes()
        if res:
            st.dataframe(pd.DataFrame(res), hide_index=True)
        else:
            st.info("No hay liquidaciones pendientes de vincular.")
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


def liq_planilla():
    st.subheader("Planilla de liquidaciones por comprobante")
    st.caption(
        "Una fila por factura. Los importes de cada liquidación se reparten entre sus facturas "
        "en proporción al monto de cada una. Bienes durables aparece en negativo (descuento)."
    )
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

    imp = consultar("SELECT liquidacion_id, impuesto, SUM(monto) AS monto FROM liquidacion_impuestos "
                    "GROUP BY liquidacion_id, impuesto")
    imp_p = imp.pivot(index="liquidacion_id", columns="impuesto", values="monto") if not imp.empty else

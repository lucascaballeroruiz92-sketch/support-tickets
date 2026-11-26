import os
import re
import html
import traceback
import unicodedata
from datetime import date, datetime
from io import BytesIO

import pandas as pd
import pdfplumber
import streamlit as st
from sqlalchemy import create_engine

USUARIOS = {"socio1": "clave1", "socio2": "clave2"}  # Cambiar. Recomendado: st.secrets
ADMIN = "socio1"
TOL = 0.05

st.set_page_config(page_title="Castillo de Oro - Liquidaciones", page_icon="💳", layout="wide")

SCHEMA = """
CREATE TABLE IF NOT EXISTS facturas (id SERIAL PRIMARY KEY, fecha TEXT, tipo TEXT, punto_venta TEXT, numero TEXT,
    cuit_emisor TEXT, razon_social TEXT, neto DOUBLE PRECISION, iva DOUBLE PRECISION, total DOUBLE PRECISION,
    cae TEXT, cargado_por TEXT, fecha_carga TEXT, UNIQUE (tipo, punto_venta, numero, cuit_emisor));
CREATE TABLE IF NOT EXISTS liquidaciones (id SERIAL PRIMARY KEY, plataforma TEXT, banco TEXT, nro_lote TEXT,
    fecha_liquidacion TEXT, periodo_desde TEXT, periodo_hasta TEXT, bruto DOUBLE PRECISION, comision DOUBLE PRECISION,
    costo_financiero DOUBLE PRECISION, iva_comision DOUBLE PRECISION, retenciones DOUBLE PRECISION,
    neto_esperado DOUBLE PRECISION, neto_acreditado DOUBLE PRECISION, fecha_acreditacion TEXT,
    diferencia DOUBLE PRECISION, estado TEXT, notas TEXT, cargado_por TEXT, fecha_carga TEXT,
    ultima_modif_por TEXT, ultima_modif TEXT);
CREATE TABLE IF NOT EXISTS liquidacion_factura (liquidacion_id INTEGER, factura_id INTEGER, monto DOUBLE PRECISION,
    PRIMARY KEY (liquidacion_id, factura_id));
CREATE TABLE IF NOT EXISTS liquidacion_impuestos (id SERIAL PRIMARY KEY, liquidacion_id INTEGER, impuesto TEXT, monto DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS liquidacion_filas (liquidacion_id INTEGER, fecha TEXT, monto DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS liquidacion_comprobantes (liquidacion_id INTEGER, pv TEXT, numero TEXT);
CREATE TABLE IF NOT EXISTS liquidacion_pdf (liquidacion_id INTEGER PRIMARY KEY, neto_pdf DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS extractos (id SERIAL PRIMARY KEY, archivo TEXT, banco TEXT, periodo_desde TEXT,
    periodo_hasta TEXT, cargado_por TEXT, fecha_carga TEXT);
CREATE TABLE IF NOT EXISTS extracto_movimientos (id SERIAL PRIMARY KEY, extracto_id INTEGER, fecha TEXT,
    descripcion TEXT, monto DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS cobros_directos (id SERIAL PRIMARY KEY, factura_id INTEGER, medio TEXT, fecha TEXT,
    monto DOUBLE PRECISION, cargado_por TEXT, fecha_carga TEXT);
CREATE TABLE IF NOT EXISTS nota_credito_factura (id SERIAL PRIMARY KEY, nc_id INTEGER, factura_id INTEGER, monto DOUBLE PRECISION, fecha TEXT, cargado_por TEXT, fecha_carga TEXT);
CREATE TABLE IF NOT EXISTS actividad (id SERIAL PRIMARY KEY, momento TEXT, usuario TEXT, accion TEXT, detalle TEXT);
"""
LIQ_HIJAS = ("liquidacion_impuestos", "liquidacion_filas", "liquidacion_comprobantes",
             "liquidacion_pdf", "liquidacion_factura")
MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6,
         "jul": 7, "ago": 8, "sep": 9, "oct": 10, "nov": 11, "dic": 12}


# ======================= BASE DE DATOS =======================
@st.cache_resource
def get_engine():
    try:
        url = st.secrets["DATABASE_URL"]
    except Exception:
        url = os.environ.get("DATABASE_URL")
    if not url:
        st.error("Falta configurar DATABASE_URL en Streamlit Secrets.")
        st.stop()
    url = re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url)
    return create_engine(url, pool_pre_ping=True)


def q(sql, params=()):
    """Lectura. Los placeholders se escriben con '?'."""
    with get_engine().connect() as con:
        return pd.read_sql_query(sql.replace("?", "%s"), con.connection, params=tuple(params))


def x(sql, params=(), devolver_id=False):
    """Escritura. Con devolver_id retorna el id generado; si no, filas afectadas."""
    sql = sql.replace("?", "%s")
    with get_engine().begin() as con:
        if devolver_id:
            return con.exec_driver_sql(sql + " RETURNING id", tuple(params)).scalar()
        return con.exec_driver_sql(sql, tuple(params)).rowcount


def init_db():
    with get_engine().begin() as con:
        for sent in SCHEMA.split(";"):
            if sent.strip():
                con.exec_driver_sql(sent)


def ahora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def registrar(accion, detalle):
    x("INSERT INTO actividad (momento, usuario, accion, detalle) VALUES (?,?,?,?)",
      (ahora(), st.session_state.get("usuario"), accion, detalle))


# ======================= UTILIDADES =======================
def fmt(v):
    """Formato argentino: $ 1.234,56"""
    return "$ " + numar(float(v or 0))


def numar(v):
    if pd.isna(v):
        return ""
    return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def tabla(df):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(numar)
    st.dataframe(d, hide_index=True, use_container_width=True)


def num(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("$", "").replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def txt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return re.sub(r"\.0$", "", str(v).strip())


def sin_acentos(t):
    return "".join(c for c in unicodedata.normalize("NFD", str(t)) if unicodedata.category(c) != "Mn").lower()


def rango(key):
    hoy = date.today()
    c1, c2 = st.columns(2)
    d = c1.date_input("Desde", value=date(hoy.year, 1, 1), key=f"{key}_d")
    h = c2.date_input("Hasta", value=hoy, key=f"{key}_h")
    return d.isoformat(), h.isoformat()


def etiqueta_liq(r):
    return f"#{int(r.id)} - {r.plataforma} - {r.nro_lote or 's/lote'} - {r.fecha_liquidacion}"


def es_nc(tipo):
    """Nota de crédito: se reconoce por el tipo de comprobante de AFIP."""
    t = sin_acentos(tipo)
    return "credito" in t or t.strip().startswith("nc")


def mask_nc(df):
    return pd.Series([es_nc(t) for t in df.tipo], index=df.index, dtype=bool)


def solo_nc(df):
    return df[mask_nc(df)]


def sin_nc(df):
    return df[~mask_nc(df)]


def aplicado_nc(nid):
    """Monto de una nota de crédito ya aplicado a facturas."""
    return float(q("SELECT COALESCE(SUM(monto),0) AS m FROM nota_credito_factura WHERE nc_id=?", (nid,)).m.iloc[0])


def aplicado_factura(fid):
    """Monto que las notas de crédito restaron a una factura."""
    return float(q("SELECT COALESCE(SUM(monto),0) AS m FROM nota_credito_factura WHERE factura_id=?", (fid,)).m.iloc[0])


def saldo_factura(fid, total):
    """Total menos liquidaciones, cobros directos y notas de crédito aplicadas."""
    a = q("SELECT COALESCE(SUM(COALESCE(monto,0)),0) AS m FROM liquidacion_factura WHERE factura_id=?", (fid,))
    b = q("SELECT COALESCE(SUM(monto),0) AS m FROM cobros_directos WHERE factura_id=?", (fid,))
    return round(float(total) - float(a.m.iloc[0]) - float(b.m.iloc[0]) - aplicado_factura(fid), 2)


# ======================= LECTURA DE PDF =======================
NUM_RE = re.compile(r"-?\$?\s?\d{1,3}(?:\.\d{3})*,\d{2}")
FECHA_RE = re.compile(r"\b(\d{2})[/.-](\d{2})[/.-](\d{4})\b")
FILA_RE = re.compile(r"^(\d{2}/\d{2}/\d{4}).*?(\d{1,3}(?:\.\d{3})*,\d{2})")
COMP_RE = re.compile(r"(?:comprobante|factura|cbte)[^\n\d]{0,30}(\d{4,5})\s?-\s?(\d{6,8})", re.I)
PLATS = {"naranja": "Naranja X", "nacion servicios": "Banco Nación", "banco naci": "Banco Nación",
         "mercado pago": "Mercado Pago", "payway": "Payway", "fiserv": "Fiserv", "+pagos": "+Pagos",
         "credicoop": "Payway", "galicia": "Banco Galicia", "santander": "Banco Santander",
         "bbva": "BBVA", "macro": "Banco Macro"}


def leer_pdf(archivo):
    archivo.seek(0)
    with pdfplumber.open(archivo) as pdf:
        texto = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if not texto.strip():
        raise ValueError("El PDF no tiene texto seleccionable (puede ser escaneado).")
    return texto


def importe_en(linea):
    n = NUM_RE.findall(linea)
    return num(n[-1]) if n else None


def importe_bna(linea):
    """Formato Banco Nación / Naranja: 1,234.56 o -2,689.85"""
    n = re.findall(r"-?\d{1,3}(?:,\d{3})*\.\d{2}", linea)
    return float(n[-1].replace(",", "")) if n else None


def tasa_iva(s):
    m = re.search(r"iva\D*(\d+(?:[.,]\d+)?)", s)
    return float(m[1].replace(",", ".")) if m else None


def nro_liquidacion(lineas):
    plano = re.sub(r"\d{2}-\d{8}-\d", " ", " ".join(lineas))
    m = (re.search(r"(?<![\d-])(\d{6,})\D{0,3}nro\.?\s*de\s*liquidaci", plano, re.I)
         or re.search(r"nro\.?\s*de\s*liquidaci\w*\W{0,40}?(?<![\d-])(\d{8,16})(?![\d-])", plano, re.I))
    return m[1] if m else None


def parsear(texto):
    """Extrae bruto, deducciones por tipo, neto, fecha y número de liquidación del PDF."""
    L = [re.sub(r"^[#\s]+", "", l).strip() for l in texto.splitlines()]
    L = [l for l in L if l]
    S = [sin_acentos(l) for l in L]
    tl = sin_acentos(texto)
    av = []

    def suma(cond, f=importe_en, signo=False):
        t = 0.0
        for l, s in zip(L, S):
            if cond(s):
                v = f(l)
                if v is not None:
                    t += v if signo else abs(v)
        return round(t, 2)

    d = {"f_texto": texto, "f_comprobantes": [], "f_fliq": None, "f_lote": None, "f_filas": []}
    for l in L:
        m = FILA_RE.match(l)
        if m:
            try:
                d["f_filas"].append({"fecha": datetime.strptime(m[1], "%d/%m/%Y").date(), "monto": num(m[2])})
            except ValueError:
                pass
    bruto = round(sum(f["monto"] for f in d["f_filas"]), 2) if d["f_filas"] else None
    if not bruto:
        cands = []
        for i, s in enumerate(S):
            if re.match(r"^totales\b", s):
                for l in L[i:i + 4]:
                    cands += [num(v) for v in NUM_RE.findall(l)]
        bruto = max(cands) if cands else None
    if not bruto:
        i = next((i for i, s in enumerate(S) if re.search(
            r"total presentado|importe total|total ventas|venta total|total cupones", s)), None)
        bruto = importe_en(L[i]) if i is not None else None
    d["f_bruto"] = abs(bruto) if bruto else 0.0

    # Deducciones genéricas (Naranja, Fiserv, +Pagos, etc.)
    bonif = suma(lambda s: s.startswith("bonificacion"))
    d["f_com"] = suma(lambda s: s.startswith(("arancel", "comision")))
    # La bonificación de Naranja reduce los intereses (costo financiero), no la comisión
    d["f_cf"] = round(suma(lambda s: re.match(r"(interes|financiacion|costo financiero|cargo financiero)", s)) - bonif, 2)
    desg = {}
    for t, nombre in ((21.0, "IVA 21%"), (10.5, "IVA 10,5%"), (27.0, "IVA 27%")):
        desg[nombre] = suma(lambda s, t=t: s.startswith("iva") and tasa_iva(s) == t)
    perc = suma(lambda s: "percepcion" in s)
    sirtac = suma(lambda s: re.search(r"sirtac|iibb|ingresos brutos", s))
    deb = suma(lambda s: re.search(r"debito|credito|impuesto al cheque|ley 25413", s) and not s.startswith("total"))
    desg.update({"Percepción en IVA": perc, "SIRTAC / IIBB": sirtac, "Débitos y créditos": deb})
    d["f_desglose"] = desg
    d["f_iva"] = round(sum(v for k, v in desg.items() if k.startswith("IVA")), 2)
    d["f_ret"] = round(perc + sirtac + deb, 2)

    neto = None
    for i, s in enumerate(S):
        if re.search(r"^neto liquidado|total recibido|neto a (pagar|acreditar|cobrar)", s):
            neto = next((importe_en(L[j]) for j in (i, i + 1, i - 1)
                         if 0 <= j < len(L) and importe_en(L[j]) is not None), None)
            if neto is not None:
                break
    d["f_neto"] = abs(neto) if neto is not None else None

    # Fecha de liquidación = Fecha de Emisión (nunca la de pago)
    fs = None
    for i, l in enumerate(L):
        if "fecha de emision" in S[i]:
            fs = FECHA_RE.search(l) or (FECHA_RE.search(L[i + 1]) if i + 1 < len(L) else None)
            if fs:
                break
    if not fs:
        fs = next((FECHA_RE.search(l) for l, s in zip(L, S) if "pago" not in s and FECHA_RE.search(l)), None)
    if fs:
        d["f_fliq"] = date(int(fs[3]), int(fs[2]), int(fs[1]))

    m = re.search(r"Tipo y N\S*\s*:?\s*([A-Za-z]-\d{4}-\d+)", texto, re.I)
    d["f_lote"] = nro_liquidacion(L) or (m[1] if m else None)
    d["f_plat"] = next((n for k, n in PLATS.items() if k in tl), None)
    m = COMP_RE.search(texto)
    d["f_comprobantes"] = [(m[1], m[2])] if m else []

    # Liquidación Banco Nación (signos y formato 1,234.56)
    if "total presentado" in tl or (re.search(r"nro\.?\s*de\s*liquidaci", tl) and "neto percibido" in tl):
        def sg(cond):
            return suma(cond, importe_bna, signo=True)

        bienes = sg(lambda s: s.startswith("bienes durables"))
        sirt_b = sg(lambda s: "sirtac" in s or s.startswith("ret iibb"))
        iva_b = sg(lambda s: s.startswith(("iva 21", "devolucion iva")) or "iva gene" in s)
        d["f_bruto"] = sg(lambda s: s.startswith("total presentado"))
        d["f_com"] = sg(lambda s: re.match(r"(devolucion )?arancel", s))
        d["f_cf"] = sg(lambda s: "cargo fin" in s and "iva" not in s)
        d["f_iva"] = iva_b
        d["f_ret"] = round(sirt_b + bienes, 2)
        d["f_desglose"] = {"IVA 21%": iva_b, "SIRTAC / IIBB": sirt_b, "Bienes Durables": bienes}
        d["f_neto"] = sg(lambda s: "neto percibido" in s) or d["f_neto"]
        mf = re.search(r"Consumos Cuotas\s*(\d{2}/\d{2}/\d{4})", texto)
        if mf:
            d["f_filas"] = [{"fecha": datetime.strptime(mf[1], "%d/%m/%Y").date(), "monto": d["f_bruto"]}]
        ml = re.search(r"(?<![\d-])(\d{6,})\s*Nro\.?\s*de\s*Liquidaci", texto, re.I)
        if ml:
            d["f_lote"] = ml[1]

    # Mercado Pago (PDF): comisión y costo financiero llevan IVA 21% incluido
    if "mercado pago" in tl and any(k in tl for k in ("cargo de mercado pago", "recargo de mercado pago",
                                                      "total recibido", "total de la operacion")):
        def mp(patron):
            for l, s in zip(L, S):
                if re.search(patron, s):
                    m_ = re.findall(r"\$\s?(\d{1,3}(?:\.\d{3})*(?:,\d{2})?)", l)
                    if m_:
                        return float(m_[-1].replace(".", "").replace(",", "."))
            return 0.0

        cobro = mp(r"^cobro")
        sirt = mp(r"sirtac|sircupa|ingresos brutos")
        cf_t = mp(r"^intereses por cuotas")
        com_t = mp(r"^(cargo|recargo) de mercado pago")
        com_n, cf_n = round(com_t / 1.21, 2), round(cf_t / 1.21, 2)
        iva_mp = round((com_t - com_n) + (cf_t - cf_n), 2)
        d.update(f_bruto=cobro, f_com=com_n, f_cf=cf_n, f_iva=iva_mp, f_ret=round(sirt, 2),
                 f_desglose={"IVA 21%": iva_mp, "SIRTAC / IIBB": sirt},
                 f_neto=mp(r"^total recibido") or mp(r"^total de la operacion"), f_plat="Mercado Pago")
        mm = re.search(r"movimiento\s+([A-Za-z0-9]{10,})", texto, re.I)
        if mm:
            d["f_lote"] = mm[1]
        sa = sin_acentos(texto)
        ma = re.search(r"total recibido el\s*(\d{1,2})/([a-z]{3})", sa) or re.search(r"(?m)^(\d{1,2})/([a-z]{3})\s", sa)
        mg = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2})\b,?\s*\d{1,2}:\d{2}", texto)
        anio = 2000 + int(mg[3]) if mg else date.today().year
        if ma and ma[2] in MESES:
            d["f_fliq"] = date(anio, MESES[ma[2]], int(ma[1]))
        elif mg:
            d["f_fliq"] = date(anio, int(mg[2]), int(mg[1]))
        d["f_filas"] = [{"fecha": d["f_fliq"], "monto": cobro}] if d["f_fliq"] else []

    # Resumen Diario - Payway (Credicoop). La percepción de IVA NO es IVA crédito.
    if "resumen diario de liquidaciones" in tl:
        def cred(p):
            m_ = re.search(p, texto, re.I)
            return num(m_[1]) if m_ else 0.0

        bruto_c = cred(r"\d+\s+Venta Tj\.?\s*D[eé]bito\s*\$\s*([\d.]+,\d{2})")
        iva_c = cred(r"IVA\s+21[.,]00\s?%\s*\$\s*([\d.]+,\d{2})")
        perc_c = cred(r"IVA\s+RG\s*\d+\s+\d+[.,]\d+\s?%\s*\$\s*([\d.]+,\d{2})")
        sirt_c = cred(r"SIRTAC[\s\S]{0,30}?\$\s*([\d.]+,\d{2})")
        pm = re.search(r"IVA\s+RG\s*\d+\s+(\d+[.,]\d+)\s?%", texto)
        pct = pm[1].replace(".", ",") if pm else "3,00"
        d.update(f_bruto=bruto_c,
                 f_com=cred(r"Arancel Tj\.?\s*D[eé]bito[\s\S]{0,40}?\$\s*([\d.]+,\d{2})"),
                 f_cf=0.0, f_iva=iva_c, f_ret=round(perc_c + sirt_c, 2),
                 f_desglose={"IVA 21%": iva_c, f"Percepción en IVA RG 2408 ({pct}%)": perc_c, "SIRTAC / IIBB": sirt_c},
                 f_neto=cred(r"SE ACREDITO EN:[\s\S]*?\$\s*([\d.]+,\d{2})") or None, f_plat="Payway")
        d["f_filas"] = [{"fecha": d["f_fliq"], "monto": bruto_c}] if d["f_fliq"] else []
        ml = re.search(r"Liq\.\s*N[°º]\s*(\d+)", texto)
        if ml:
            d["f_lote"] = ml[1]

    if d["f_bruto"]:
        calc = round(d["f_bruto"] - (d["f_com"] + d["f_cf"] + d["f_iva"] + d["f_ret"]), 2)
        if d.get("f_neto"):
            if abs(d["f_neto"] - calc) > TOL:
                av.append(f"El neto del PDF ({fmt(d['f_neto'])}) no coincide con bruto − deducciones ({fmt(calc)}). Se usa el del PDF.")
        else:
            d["f_neto"] = calc
    else:
        av.append("No se encontró el importe de 'bruto'.")
    return d, av


def guardar_liquidacion(d, origen, usuario):
    """Guarda una liquidación con los datos de d (PDF o CSV). No vincula facturas."""
    fliq = d.get("f_fliq") or date.today()
    bruto = d["f_bruto"]
    com, cf, iva, ret = (abs(d[k]) for k in ("f_com", "f_cf", "f_iva", "f_ret"))
    esperado = round(bruto - com - cf - iva - ret, 2)
    plataforma = d.get("f_plat") or "Sin especificar"
    liq_id = x("""INSERT INTO liquidaciones (plataforma, banco, nro_lote, fecha_liquidacion, periodo_desde,
                  periodo_hasta, bruto, comision, costo_financiero, iva_comision, retenciones, neto_esperado,
                  estado, notas, cargado_por, fecha_carga, ultima_modif_por, ultima_modif)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (plataforma, "", d.get("f_lote") or "", fliq.isoformat(), fliq.isoformat(), fliq.isoformat(),
                bruto, com, cf, iva, ret, esperado, "Pendiente de acreditación", f"Cargado desde: {origen}.",
                usuario, ahora(), usuario, ahora()), devolver_id=True)
    x("INSERT INTO liquidacion_pdf (liquidacion_id, neto_pdf) VALUES (?,?)", (liq_id, d.get("f_neto")))
    for nombre, monto in (d.get("f_desglose") or {}).items():
        if monto:
            x("INSERT INTO liquidacion_impuestos (liquidacion_id, impuesto, monto) VALUES (?,?,?)",
              (liq_id, nombre, monto))
    for f in d.get("f_filas", []):
        x("INSERT INTO liquidacion_filas (liquidacion_id, fecha, monto) VALUES (?,?,?)",
          (liq_id, f["fecha"].isoformat(), f["monto"]))
    for pv, nro in d.get("f_comprobantes", []):
        x("INSERT INTO liquidacion_comprobantes (liquidacion_id, pv, numero) VALUES (?,?,?)", (liq_id, pv, nro))
    registrar("Liquidación cargada", f"#{liq_id} {plataforma} desde {origen}")
    return liq_id


def eliminar_liquidacion(liq_id):
    n = int(q("SELECT COUNT(*) AS n FROM liquidacion_factura WHERE liquidacion_id=?", (liq_id,)).n.iloc[0])
    if n > 0:
        return False, "La liquidación tiene comprobantes vinculados. Desvinculala primero."
    for t in LIQ_HIJAS:
        x(f"DELETE FROM {t} WHERE liquidacion_id=?", (liq_id,))
    x("DELETE FROM liquidaciones WHERE id=?", (liq_id,))
    registrar("Liquidación eliminada", f"#{liq_id}")
    return True, f"Liquidación #{liq_id} eliminada."


def borrar_todas_liquidaciones():
    n = int(q("SELECT COUNT(*) AS n FROM liquidaciones").n.iloc[0])
    for t in LIQ_HIJAS + ("liquidaciones",):
        x(f"DELETE FROM {t}")
    registrar("Borrado total de liquidaciones", f"{n} eliminadas")
    return n


def borrar_todos_los_datos():
    with get_engine().begin() as con:
        con.exec_driver_sql(
            "TRUNCATE liquidacion_impuestos, liquidacion_filas, liquidacion_comprobantes, liquidacion_pdf, "
            "liquidacion_factura, liquidaciones, extracto_movimientos, extractos, facturas, cobros_directos, nota_credito_factura, "
            "actividad RESTART IDENTITY")


# ======================= CSV MERCADO PAGO =======================
def fecha_csv(v):
    s = str(v or "").strip()[:10]
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            pass
    return None


def cargar_csv_mp(archivo, usuario):
    """Una liquidación por operación de cobro (SOURCE_ID). Omite salidas de dinero y duplicados."""
    archivo.seek(0)
    raw = archivo.read()
    t = raw.decode("utf-8-sig", errors="ignore")
    df = pd.read_csv(BytesIO(raw), dtype=str, sep=";" if t.count(";") > t.count(",") else ",")
    df.columns = [c.strip() for c in df.columns]
    creadas = omitidas = 0
    for _, r in df.iterrows():
        sid = txt(r.get("SOURCE_ID"))
        bruto = num(r.get("TRANSACTION_AMOUNT"))
        ttipo = txt(r.get("TRANSACTION_TYPE")).upper()
        if not sid or bruto <= 0 or txt(r.get("PAYMENT_METHOD_TYPE")) not in ("credit_card", "debit_card"):
            continue
        if ttipo and not any(k in ttipo for k in ("SETTLEMENT", "LIQUID", "APPROV")):
            continue
        if not q("SELECT id FROM liquidaciones WHERE nro_lote=?", (sid,)).empty:
            omitidas += 1
            continue
        com = abs(num(r.get("MKP_FEE_AMOUNT")))
        cf = abs(num(r.get("FINANCING_FEE_AMOUNT")))
        ret = abs(num(r.get("TAXES_AMOUNT")))
        com_n, cf_n = round(com / 1.21, 2), round(cf / 1.21, 2)
        iva = round((com - com_n) + (cf - cf_n), 2)
        neto = num(r.get("SETTLEMENT_NET_AMOUNT")) or round(bruto - com_n - cf_n - iva - ret, 2)
        fl = fecha_csv(r.get("SETTLEMENT_DATE")) or date.today()
        guardar_liquidacion({
            "f_bruto": bruto, "f_com": com_n, "f_cf": cf_n, "f_iva": iva, "f_ret": ret, "f_neto": neto,
            "f_fliq": fl, "f_lote": sid, "f_plat": "Mercado Pago",
            "f_filas": [{"fecha": fl, "monto": bruto}],
            "f_desglose": {"IVA 21%": iva, "SIRTAC / IIBB": ret},
        }, "CSV Mercado Pago", usuario)
        creadas += 1
    registrar("Carga CSV Mercado Pago", f"{creadas} creadas, {omitidas} ya existentes")
    return creadas, omitidas


# ======================= VENTAS Y AFIP =======================
COLS_AFIP = {"fecha": "fecha", "tipo": "tipo", "pv": "punto de venta", "numero": "numero desde",
             "cuit": "nro. doc. receptor", "razon": "denominacion receptor", "neto": "neto gravado total",
             "iva": "total iva", "total": "imp. total", "cae": "cod. autorizacion"}


def leer_afip(archivo):
    """Lee el export de AFIP saltando el título hasta la fila 'Fecha'."""
    archivo.seek(0)
    if archivo.name.lower().endswith("xlsx"):
        raw = pd.read_excel(archivo, header=None, dtype=str)
    else:
        raw = pd.read_csv(archivo, header=None, sep=None, engine="python", dtype=str)
    ini = next(i for i, f in raw.iterrows()
               if any(isinstance(v, str) and sin_acentos(v).strip() == "fecha" for v in f))
    df = raw.iloc[ini + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[ini]]
    return df.dropna(how="all")


def importar_facturas(archivo, usuario):
    df = leer_afip(archivo)
    cols = {sin_acentos(c).strip(): c for c in df.columns}
    m = {k: cols.get(v) for k, v in COLS_AFIP.items()}
    if not all(m[k] for k in ("fecha", "tipo", "numero", "total")):
        raise ValueError("No se encontraron las columnas esperadas. Verificar el archivo de AFIP.")
    nuevas = 0
    for _, r in df.iterrows():
        g = lambda k: r[m[k]] if m[k] else None
        f = pd.to_datetime(g("fecha"), dayfirst=True, errors="coerce")
        if pd.isna(f):
            continue
        nuevas += x("""INSERT INTO facturas (fecha, tipo, punto_venta, numero, cuit_emisor, razon_social, neto, iva,
                       total, cae, cargado_por, fecha_carga) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (tipo, punto_venta, numero, cuit_emisor) DO NOTHING""",
                    (f.date().isoformat(), txt(g("tipo")), txt(g("pv")), txt(g("numero")), txt(g("cuit")),
                     txt(g("razon")), num(g("neto")), num(g("iva")), abs(num(g("total"))), txt(g("cae")),
                     usuario, ahora()))
    registrar("Importación AFIP", f"{nuevas} facturas nuevas de {len(df)} en {archivo.name}")
    return nuevas, len(df)


def guardar_venta_manual(fecha, cliente, total, usuario):
    """Venta sin factura AFIP (tipo MANUAL), para poder vincularla a una liquidación."""
    fid = x("""INSERT INTO facturas (fecha, tipo, punto_venta, numero, cuit_emisor, razon_social, neto, iva, total,
               cae, cargado_por, fecha_carga) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (fecha.isoformat(), "MANUAL", "0", "pendiente", "", cliente or "Venta sin factura",
             round(total, 2), 0.0, round(total, 2), "", usuario, ahora()), devolver_id=True)
    x("UPDATE facturas SET numero=? WHERE id=?", (f"{fid:08d}", fid))
    registrar("Venta manual", f"{fmt(total)} {cliente or ''} ({fecha.isoformat()})")


def vista_ventas(usuario):
    st.subheader("Importar facturas emitidas desde AFIP")
    st.caption("En AFIP: Mis Comprobantes → Emitidos → Exportar (CSV o XLSX).")
    archivo = st.file_uploader("Archivo de AFIP", type=["csv", "xlsx"], key="up_afip")
    if archivo and st.button("Importar facturas", type="primary"):
        try:
            nuevas, total = importar_facturas(archivo, usuario)
            st.success(f"Se incorporaron {nuevas} facturas nuevas de {total} filas.")
        except Exception as e:
            st.error(f"No se pudo importar: {e}")

    st.markdown("#### Cargar venta sin factura")
    with st.form("form_venta_manual", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        f_v = c1.date_input("Fecha de la venta", value=date.today())
        cli = c2.text_input("Cliente / detalle (opcional)")
        monto = c3.number_input("Monto total", min_value=0.0, step=100.0, format="%.2f")
        if st.form_submit_button("Guardar venta", type="primary"):
            if monto <= 0:
                st.error("El monto debe ser mayor a cero.")
            else:
                guardar_venta_manual(f_v, cli.strip(), float(monto), usuario)
                st.success(f"Venta manual cargada por {fmt(monto)}.")

    st.markdown("#### Facturas cargadas")
    d, h = rango("fact")
    fac = q("""SELECT fecha, tipo, punto_venta, numero, razon_social, neto, iva, total, cargado_por
               FROM facturas WHERE fecha BETWEEN ? AND ? ORDER BY fecha DESC""", (d, h))
    st.caption(f"{len(fac)} comprobantes en el rango | Total neto (descontando notas de crédito): " + fmt(sum(-float(t) if es_nc(tp) else float(t) for tp, t in zip(fac.tipo, fac.total))))
    tabla(fac)

    st.markdown("#### Eliminar comprobante")
    todas = q("SELECT id, fecha, tipo, punto_venta, numero, razon_social, total FROM facturas ORDER BY fecha DESC, id DESC")
    if todas.empty:
        st.info("No hay comprobantes cargados.")
        return
    ops = {f"{r.fecha} | {r.tipo} {r.punto_venta}-{r.numero} | {r.razon_social} | {fmt(r.total)}": int(r.id)
           for r in todas.itertuples()}
    el = st.selectbox("Comprobante a eliminar", list(ops), key="del_fac_sel")
    fid = ops[el]
    if int(q("SELECT COUNT(*) AS n FROM liquidacion_factura WHERE factura_id=?", (fid,)).n.iloc[0]) + int(q("SELECT COUNT(*) AS n FROM nota_credito_factura WHERE factura_id=? OR nc_id=?", (fid, fid)).n.iloc[0]) > 0:
        st.warning("No se puede eliminar: está vinculado a una liquidación o a una nota de crédito. Desvinculalo primero.")
    elif st.button("🗑️ Eliminar comprobante"):
        x("DELETE FROM cobros_directos WHERE factura_id=?", (fid,))
        x("DELETE FROM facturas WHERE id=?", (fid,))
        registrar("Comprobante eliminado", f"Factura #{fid} {el}")
        st.rerun()


# ======================= VENTAS: NOTAS DE CRÉDITO =======================
def vista_notas_credito(usuario):
    st.subheader("Notas de crédito")
    st.caption("Una nota de crédito resta a una factura, total o parcialmente. Se importan desde AFIP en 'Registro de ventas'.")
    todas = q("SELECT id, fecha, tipo, punto_venta, numero, razon_social, total FROM facturas ORDER BY fecha DESC, id DESC")
    ncs = solo_nc(todas).copy()
    if ncs.empty:
        st.info("No hay notas de crédito cargadas. Importalas desde AFIP en Registro de ventas.")
    else:
        ncs["aplicado"] = [aplicado_nc(int(i)) for i in ncs.id]
        ncs["saldo"] = [round(float(t) - a, 2) for t, a in zip(ncs.total, ncs.aplicado)]
        st.markdown("#### Notas de crédito cargadas")
        tabla(pd.DataFrame({
            "Fecha": ncs.fecha,
            "Comprobante": ncs.tipo + " " + ncs.punto_venta.astype(str) + "-" + ncs.numero.astype(str),
            "Razón social": ncs.razon_social, "Total NC": ncs.total,
            "Aplicado": ncs.aplicado, "Saldo disponible": ncs.saldo}))

        st.markdown("#### Aplicar nota de crédito a una factura")
        disp = ncs[ncs.saldo > TOL]
        facs = sin_nc(todas).copy()
        if disp.empty:
            st.info("Todas las notas de crédito ya están aplicadas.")
        elif facs.empty:
            st.info("No hay facturas cargadas para aplicar la nota de crédito.")
        else:
            facs["saldo"] = [saldo_factura(int(i), t) for i, t in zip(facs.id, facs.total)]
            facs = facs[facs.saldo > TOL]
            if facs.empty:
                st.info("No hay facturas con saldo pendiente para aplicar una nota de crédito.")
            else:
                ops_nc = {f"{r.fecha} | {r.tipo} {r.punto_venta}-{r.numero} | {r.razon_social} | Saldo NC {fmt(r.saldo)}": int(r.id)
                          for r in disp.itertuples()}
                fac_ops = {int(r.id): f"{r.fecha} | {r.tipo} {r.punto_venta}-{r.numero} | {r.razon_social} | Saldo {fmt(r.saldo)}"
                           for r in facs.itertuples()}
                c1, c2, c3 = st.columns(3)
                nc_el = c1.selectbox("Nota de crédito", list(ops_nc), key="nc_sel")
                fid = c2.selectbox("Factura a la que se aplica", list(fac_ops), format_func=lambda i: fac_ops[i], key="nc_fac")
                nid = ops_nc[nc_el]
                maximo = round(min(float(disp[disp.id == nid].saldo.iloc[0]), float(facs[facs.id == fid].saldo.iloc[0])), 2)
                monto = c3.number_input("Monto a aplicar (total o parcial)", min_value=0.0, max_value=maximo,
                                        value=maximo, step=100.0, format="%.2f", key=f"nc_monto_{nid}_{fid}")
                if st.button("Aplicar nota de crédito", type="primary"):
                    if monto <= 0:
                        st.error("El monto debe ser mayor a cero.")
                    else:
                        x("INSERT INTO nota_credito_factura (nc_id, factura_id, monto, fecha, cargado_por, fecha_carga) VALUES (?,?,?,?,?,?)",
                          (nid, int(fid), round(float(monto), 2), date.today().isoformat(), usuario, ahora()))
                        registrar("Nota de crédito aplicada", f"NC #{nid} a factura #{int(fid)}: {fmt(monto)}")
                        st.rerun()

    st.markdown("#### Notas de crédito aplicadas")
    aplic = q("""SELECT n.id, n.fecha, n.monto, nc.tipo AS nc_tipo, nc.punto_venta AS nc_pv, nc.numero AS nc_num,
                 f.tipo, f.punto_venta, f.numero, f.razon_social
                 FROM nota_credito_factura n JOIN facturas nc ON nc.id = n.nc_id JOIN facturas f ON f.id = n.factura_id
                 ORDER BY n.id DESC""")
    if aplic.empty:
        st.info("Todavía no hay notas de crédito aplicadas.")
        return
    tabla(pd.DataFrame({
        "Fecha": aplic.fecha,
        "Nota de crédito": aplic.nc_tipo + " " + aplic.nc_pv.astype(str) + "-" + aplic.nc_num.astype(str),
        "Factura": aplic.tipo + " " + aplic.punto_venta.astype(str) + "-" + aplic.numero.astype(str),
        "Razón social": aplic.razon_social, "Monto aplicado": aplic.monto}))
    ops_d = {f"{r.fecha} | NC {r.nc_tipo} {r.nc_pv}-{r.nc_num} → {r.tipo} {r.punto_venta}-{r.numero} | {fmt(r.monto)}": int(r.id)
             for r in aplic.itertuples()}
    el = st.selectbox("Aplicación a quitar", list(ops_d), key="nc_desv_sel")
    if st.button("Quitar aplicación"):
        x("DELETE FROM nota_credito_factura WHERE id=?", (ops_d[el],))
        registrar("Nota de crédito desvinculada", f"Aplicación #{ops_d[el]}")
        st.rerun()


# ======================= TESORERÍA: LIQUIDACIONES =======================
def liq_carga(usuario):
    st.subheader("Carga de liquidación")
    st.caption("Subí el PDF de la liquidación y pulsá Cargar. Se guarda tal como viene en el PDF.")
    pdf_liq = st.file_uploader("PDF de liquidación", type=["pdf"], key="up_pdf_liq")
    if pdf_liq is not None and st.button("Cargar PDF", type="primary"):
        try:
            d, avisos = parsear(leer_pdf(pdf_liq))
            if not d["f_bruto"]:
                st.error("No se encontró el bruto en el PDF. No se guardó nada.")
                st.text(d["f_texto"][:3000])
            elif d.get("f_lote") and not q("SELECT id FROM liquidaciones WHERE nro_lote=?", (d["f_lote"],)).empty:
                st.warning(f"Ya existe la liquidación {d['f_lote']}. No se duplicó.")
            else:
                liq_id = guardar_liquidacion(d, pdf_liq.name, usuario)
                st.success(f"Liquidación Nº {d.get('f_lote') or 's/n'} (#{liq_id}) cargada.")
                for a in avisos:
                    st.warning(a)
                if not d.get("f_lote"):
                    st.warning("No se detectó el número de liquidación en el PDF.")
        except Exception as e:
            st.error(f"No se pudo procesar el PDF: {e}")
            st.code(traceback.format_exc())

    st.markdown("#### CSV de Mercado Pago (una liquidación por operación)")
    csv_mp = st.file_uploader("CSV de Mercado Pago", type=["csv", "txt"], key="up_csv_mp")
    if csv_mp is not None and st.button("Cargar CSV"):
        try:
            c_, o_ = cargar_csv_mp(csv_mp, usuario)
            st.success(f"CSV procesado: {c_} liquidaciones creadas, {o_} ya existentes.")
        except Exception as e:
            st.error(f"No se pudo procesar el CSV: {e}")
            st.code(traceback.format_exc())

    st.markdown("#### Liquidaciones cargadas")
    d2, h2 = rango("liq")
    liqs = q("""SELECT id, plataforma, nro_lote, fecha_liquidacion, bruto, comision, costo_financiero,
                iva_comision, retenciones, estado FROM liquidaciones
                WHERE fecha_liquidacion BETWEEN ? AND ? ORDER BY fecha_liquidacion DESC""", (d2, h2))
    tabla(liqs.drop(columns=["id"]).rename(columns={"nro_lote": "Nº liquidación"}))

    st.markdown("#### Eliminar liquidación")
    st.caption("Solo se pueden eliminar liquidaciones sin comprobantes vinculados.")
    todas = q("SELECT id, plataforma, nro_lote, fecha_liquidacion FROM liquidaciones ORDER BY fecha_liquidacion DESC, id DESC")
    if not todas.empty:
        ops = {etiqueta_liq(r): int(r.id) for r in todas.itertuples()}
        el = st.selectbox("Liquidación a eliminar", list(ops), key="carga_del_sel")
        if st.button("Eliminar liquidación"):
            ok, msg = eliminar_liquidacion(ops[el])
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()

    st.markdown("#### Borrar todas las liquidaciones")
    st.caption("Borra liquidaciones con sus impuestos, filas y vinculaciones. No borra facturas AFIP ni extractos.")
    conf = st.checkbox("Entiendo que esta acción no se puede deshacer", key="chk_borrar_liq")
    if st.button("🗑️ Borrar todas las liquidaciones", type="primary", disabled=not conf):
        st.success(f"Se borraron {borrar_todas_liquidaciones()} liquidaciones.")
        st.rerun()


def cobros_directos_ui(usuario):
    st.markdown("#### Cobros directos (efectivo o transferencia)")
    st.caption("Registrá la parte de una factura cobrada sin tarjeta, para que no figure como impaga.")
    facts = q("SELECT id, fecha, tipo, punto_venta, numero, razon_social, total FROM facturas ORDER BY fecha DESC")
    facts = sin_nc(facts)
    if facts.empty:
        st.info("No hay facturas cargadas.")
        return
    etiq = {int(r.id): f"{r.fecha} | {r.tipo} {r.punto_venta}-{r.numero} | {r.razon_social} | Total {fmt(r.total)}"
            for r in facts.itertuples()}
    with st.form("form_cobro_directo", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns(4)
        fid = c1.selectbox("Factura", list(etiq), format_func=lambda i: etiq[i])
        medio = c2.selectbox("Medio", ["Efectivo", "Transferencia", "Otro"])
        fecha_c = c3.date_input("Fecha del cobro", value=date.today())
        monto = c4.number_input("Monto cobrado", min_value=0.0, step=100.0, format="%.2f")
        if st.form_submit_button("Registrar cobro", type="primary"):
            total = float(facts[facts.id == fid].iloc[0].total)
            saldo = saldo_factura(int(fid), total)
            if monto <= 0:
                st.error("El monto debe ser mayor a cero.")
            elif monto > saldo + TOL:
                st.error(f"El monto supera el saldo pendiente de la factura ({fmt(saldo)}).")
            else:
                x("INSERT INTO cobros_directos (factura_id, medio, fecha, monto, cargado_por, fecha_carga) VALUES (?,?,?,?,?,?)",
                  (int(fid), medio, fecha_c.isoformat(), round(float(monto), 2), usuario, ahora()))
                registrar("Cobro directo", f"Factura #{int(fid)}: {medio} {fmt(monto)}")
                st.success("Cobro registrado.")

    cobros = q("""SELECT c.id, c.fecha, c.medio, c.monto, f.tipo, f.punto_venta, f.numero, f.razon_social
                  FROM cobros_directos c JOIN facturas f ON f.id = c.factura_id ORDER BY c.fecha DESC, c.id DESC""")
    if cobros.empty:
        return
    tabla(cobros.drop(columns=["id"]).rename(columns={"fecha": "Fecha", "medio": "Medio", "monto": "Monto",
                                                       "tipo": "Tipo", "punto_venta": "PV", "numero": "Número",
                                                       "razon_social": "Razón social"}))
    ops = {f"{r.fecha} | {r.medio} | {fmt(r.monto)} | {r.tipo} {r.punto_venta}-{r.numero}": int(r.id)
           for r in cobros.itertuples()}
    el = st.selectbox("Cobro directo a desvincular", list(ops), key="desv_cd_sel")
    if st.button("Desvincular cobro directo"):
        x("DELETE FROM cobros_directos WHERE id=?", (ops[el],))
        registrar("Desvinculación cobro directo", f"Cobro #{ops[el]}")
        st.rerun()


def vinculacion_manual(L, usuario):
    liq_id, bruto = int(L.id), float(L.bruto or 0)
    facts = q("SELECT id, fecha, tipo, punto_venta, numero, razon_social, total FROM facturas ORDER BY fecha DESC")
    facts = sin_nc(facts).copy()
    facts["saldo"] = [saldo_factura(int(i), t) for i, t in zip(facts.id, facts.total)]
    facts = facts[facts.saldo > TOL]
    etiq = {int(r.id): f"{r.fecha} | {r.tipo} {r.punto_venta}-{r.numero} | {r.razon_social} | Total {fmt(r.total)} | Saldo {fmt(r.saldo)}"
            for r in facts.itertuples()}
    st.metric("Bruto de la liquidación", fmt(bruto))
    sel = st.multiselect("Facturas AFIP / ventas manuales", list(etiq), format_func=lambda i: etiq[i],
                         key=f"vinc_fac_{liq_id}")
    st.caption("Indicá cuánto de cada factura se cobró con esta liquidación. El resto puede ser efectivo o transferencia.")
    imps = {}
    for fid in sel:
        fila = facts[facts.id == fid].iloc[0]
        saldo = float(fila.saldo)
        imps[fid] = st.number_input(
            f"A imputar de {fila.tipo} {fila.punto_venta}-{fila.numero} (saldo disponible {fmt(saldo)})",
            min_value=0.0, max_value=saldo, value=float(min(saldo, fila.total)), step=100.0,
            format="%.2f", key=f"imp_{liq_id}_{fid}")
    total_imp = round(sum(imps.values()), 2)
    dif = round(bruto - total_imp, 2)
    st.metric("Total imputado", fmt(total_imp))
    if sel and abs(dif) <= TOL:
        st.success("El total imputado coincide con el bruto de la liquidación.")
    elif sel:
        st.warning(f"Diferencia bruto − imputado: {fmt(dif)}. Si parte se cobró en efectivo o transferencia, es normal.")
    if st.button("Guardar vinculación", type="primary", disabled=not sel):
        x("DELETE FROM liquidacion_factura WHERE liquidacion_id=?", (liq_id,))
        for fid in sel:
            x("INSERT INTO liquidacion_factura (liquidacion_id, factura_id, monto) VALUES (?,?,?)",
              (liq_id, int(fid), round(imps[fid], 2)))
        registrar("Vinculación manual", f"Liquidación #{liq_id}: {len(sel)} factura(s)")
        st.rerun()


def liq_vinculacion(usuario):
    st.subheader("Vinculación de comprobantes")
    st.markdown("#### Vincular una liquidación pendiente")
    pend = q("""SELECT id, plataforma, nro_lote, fecha_liquidacion, bruto FROM liquidaciones
                WHERE id NOT IN (SELECT liquidacion_id FROM liquidacion_factura)
                ORDER BY fecha_liquidacion DESC, id DESC""")
    if pend.empty:
        st.info("No hay liquidaciones pendientes de vincular.")
    else:
        opc = {etiqueta_liq(r): r for r in pend.itertuples()}
        eleg = st.selectbox("Liquidación", list(opc), key="vinc_liq_sel")
        vinculacion_manual(opc[eleg], usuario)

    cobros_directos_ui(usuario)

    st.markdown("#### Comprobantes vinculados")
    tabla(q("""SELECT l.id AS liquidacion, l.plataforma, l.nro_lote, f.fecha, f.tipo, f.punto_venta, f.numero,
               f.razon_social, f.total, lf.monto FROM liquidacion_factura lf
               JOIN liquidaciones l ON l.id = lf.liquidacion_id JOIN facturas f ON f.id = lf.factura_id
               ORDER BY l.id, f.fecha"""))

    con_vinc = q("""SELECT DISTINCT l.id, l.plataforma, l.nro_lote, l.fecha_liquidacion FROM liquidaciones l
                    JOIN liquidacion_factura lf ON lf.liquidacion_id = l.id ORDER BY l.id DESC""")
    if not con_vinc.empty:
        st.markdown("#### Desvincular liquidación")
        st.caption("Quita las facturas vinculadas para que la liquidación vuelva a quedar pendiente.")
        ops = {etiqueta_liq(r): int(r.id) for r in con_vinc.itertuples()}
        el = st.selectbox("Liquidación a desvincular", list(ops), key="desv_sel")
        if st.button("Desvincular liquidación"):
            x("DELETE FROM liquidacion_factura WHERE liquidacion_id=?", (ops[el],))
            registrar("Desvinculación", f"Liquidación #{ops[el]}")
            st.rerun()


def excel_bytes(df, hoja="2026"):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, sheet_name=hoja, index=False)
    return buf.getvalue()


def armar_planilla(desde=None, hasta=None, plataforma=None):
    """Una fila por comprobante vinculado, con deducciones prorrateadas. Devuelve vacío si no hay."""
    where, params = ["1=1"], []
    if desde:
        where.append("l.fecha_liquidacion BETWEEN ? AND ?")
        params += [desde, hasta]
    if plataforma:
        where.append("l.plataforma = ?")
        params.append(plataforma)
    df = q(f"""SELECT l.id AS liq, l.plataforma, l.nro_lote AS lote, l.fecha_acreditacion, l.neto_acreditado,
              l.comision, l.costo_financiero, l.iva_comision, l.retenciones,
              f.fecha AS fecha_fac, f.tipo, f.punto_venta, f.numero, f.razon_social, f.total,
              COALESCE(lf.monto, f.total) AS imp
              FROM liquidacion_factura lf JOIN liquidaciones l ON l.id = lf.liquidacion_id
              JOIN facturas f ON f.id = lf.factura_id WHERE {' AND '.join(where)}
              ORDER BY f.fecha, l.id""", params)
    if df.empty:
        return df
    df["peso"] = df.imp / df.groupby("liq")["imp"].transform("sum")
    imp = q("SELECT liquidacion_id AS liq, impuesto, SUM(monto) AS m FROM liquidacion_impuestos GROUP BY 1, 2")
    piv = imp.pivot(index="liq", columns="impuesto", values="m").fillna(0) if not imp.empty else pd.DataFrame()
    out = pd.DataFrame({
        "FECHA FAC.": pd.to_datetime(df.fecha_fac).dt.strftime("%d/%m/%Y"),
        "COMPROBANTE": df.tipo + " " + df.punto_venta.astype(str) + "-" + df.numero.astype(str),
        "CLIENTE": df.razon_social,
        "MONTO FACTURA": df.total,
        "MONTO IMPUTADO": df.imp,
        "LIQ. N°": df.lote,
        "PLATAFORMA": df.plataforma,
        "ARANCEL": df.comision * df.peso,
        "INTERÉS / FINANCIACIÓN": df.costo_financiero.abs() * df.peso,
    })
    for col in piv.columns:
        out[col.upper()] = df.liq.map(piv[col]).fillna(0).values * df.peso.values
    out["TOTAL DEDUCCIONES"] = (df.comision + df.costo_financiero + df.iva_comision + df.retenciones) * df.peso
    out["NETO PARA ESTA VENTA"] = df.imp - out["TOTAL DEDUCCIONES"]
    out["FECHA ACRED."] = df.fecha_acreditacion
    out["ACREDITADO BANCO"] = df.neto_acreditado * df.peso
    return out.round(2)


def liq_planilla():
    st.subheader("Planilla de liquidaciones por comprobante")
    st.caption("Una fila por factura. Las deducciones de cada liquidación se reparten según el peso de la factura.")
    out = armar_planilla()
    if out.empty:
        st.info("Todavía no hay comprobantes vinculados.")
        return
    tabla(out)
    st.download_button("📥 Descargar planilla en Excel", data=excel_bytes(out), file_name="planilla_liquidaciones.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ======================= TESORERÍA: EXTRACTOS =======================
BANCOS_EXT = {"banco naci": "Banco Nación", "galicia": "Banco Galicia", "santander": "Banco Santander",
              "bbva": "BBVA", "macro": "Banco Macro", "credicoop": "Banco Credicoop", "ciudad": "Banco Ciudad",
              "provincia": "Banco Provincia", "icbc": "ICBC", "brubank": "Brubank", "naranja": "Naranja X",
              "mercado pago": "Mercado Pago"}


def texto_de_archivo(archivo):
    archivo.seek(0)
    nombre = archivo.name.lower()
    if nombre.endswith(".pdf"):
        with pdfplumber.open(archivo) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    if nombre.endswith(".xlsx"):
        df = pd.read_excel(archivo, header=None, dtype=str)
        return "\n".join(" | ".join(str(v) for v in f if pd.notna(v)) for f in df.values.tolist())
    return archivo.read().decode("utf-8-sig", errors="ignore")


def guardar_extracto(archivo, usuario):
    texto = texto_de_archivo(archivo)
    movs = []
    for linea in texto.splitlines():
        f = FECHA_RE.search(linea)
        montos = NUM_RE.findall(linea)
        if not f or not montos:
            continue
        try:
            movs.append({"fecha": date(int(f[3]), int(f[2]), int(f[1])),
                         "descripcion": linea.strip()[:200], "monto": num(montos[-1])})
        except ValueError:
            continue
    tl = sin_acentos(texto)
    banco = next((n for k, n in BANCOS_EXT.items() if k in tl), "No identificado")
    desde = min((m["fecha"] for m in movs), default=None)
    hasta = max((m["fecha"] for m in movs), default=None)
    ext_id = x("INSERT INTO extractos (archivo, banco, periodo_desde, periodo_hasta, cargado_por, fecha_carga) VALUES (?,?,?,?,?,?)",
               (archivo.name, banco, desde.isoformat() if desde else None,
                hasta.isoformat() if hasta else None, usuario, ahora()), devolver_id=True)
    for m in movs:
        x("INSERT INTO extracto_movimientos (extracto_id, fecha, descripcion, monto) VALUES (?,?,?,?)",
          (ext_id, m["fecha"].isoformat(), m["descripcion"], m["monto"]))
    registrar("Extracto cargado", f"{archivo.name} ({banco}, {len(movs)} movimientos)")
    return len(movs), banco


def conciliar(usuario):
    """Factura − deducciones debe coincidir con un crédito del extracto, cercano a la fecha de pago."""
    liqs = q("""SELECT l.id, l.plataforma, l.nro_lote, l.fecha_liquidacion, l.bruto, l.comision,
                l.costo_financiero, l.iva_comision, l.retenciones,
                (SELECT COALESCE(SUM(COALESCE(lf.monto, f.total)),0) FROM liquidacion_factura lf
                   JOIN facturas f ON f.id = lf.factura_id WHERE lf.liquidacion_id = l.id) AS facturado,
                (SELECT COUNT(*) FROM liquidacion_factura lf WHERE lf.liquidacion_id = l.id) AS n_fac,
                (SELECT p.neto_pdf FROM liquidacion_pdf p WHERE p.liquidacion_id = l.id) AS neto_pdf
                FROM liquidaciones l ORDER BY l.fecha_liquidacion, l.id""")
    movs = q("SELECT id, fecha, monto FROM extracto_movimientos")
    usados, filas = set(), []
    for L in liqs.itertuples():
        ded = sum(0.0 if pd.isna(v) else float(v) for v in (L.comision, L.costo_financiero, L.iva_comision, L.retenciones))
        base = float(L.facturado) if L.n_fac > 0 else float(L.bruto or 0)
        esperado = round(base - ded, 2)
        f_pago = date.fromisoformat(L.fecha_liquidacion) if L.fecha_liquidacion else None
        ventana = [m for m in movs.itertuples() if m.id not in usados and
                   (f_pago is None or abs((date.fromisoformat(m.fecha) - f_pago).days) <= 7)]
        ok = None
        if pd.notna(L.neto_pdf):
            ok = next((m for m in ventana if abs(abs(m.monto) - float(L.neto_pdf)) <= TOL), None)
        if ok is None:
            ok = next((m for m in ventana if abs(abs(m.monto) - esperado) <= TOL), None)
        acred = f_ac = None
        estado = "Sin acreditación en extracto"
        if ok is not None:
            usados.add(ok.id)
            acred, f_ac, estado = float(ok.monto), ok.fecha, "Conciliada"
        dif = round(abs(acred) - esperado, 2) if acred is not None else None
        x("""UPDATE liquidaciones SET neto_acreditado=?, fecha_acreditacion=?, diferencia=?, estado=?,
             ultima_modif_por=?, ultima_modif=? WHERE id=?""",
          (acred, f_ac, dif, estado, usuario, ahora(), int(L.id)))
        filas.append({"Liquidación #": int(L.id), "Plataforma": L.plataforma, "Lote": L.nro_lote,
                      "Fecha pago": L.fecha_liquidacion, "Facturado": base, "Deducciones": ded,
                      "Factura − deducciones": esperado,
                      "Neto según PDF": float(L.neto_pdf) if pd.notna(L.neto_pdf) else None,
                      "Acreditado en extracto": acred, "Dif.": dif, "Estado": estado})
    registrar("Conciliación", f"{len(filas)} liquidaciones")
    return pd.DataFrame(filas)


def vista_extractos(op, usuario):
    if op == "Carga de extractos":
        st.subheader("Carga de extractos")
        archivo = st.file_uploader("Extracto bancario (PDF, CSV o XLSX)", type=["pdf", "csv", "xlsx"], key="up_ext")
        if archivo is not None and st.button("Guardar extracto", type="primary"):
            try:
                n, banco = guardar_extracto(archivo, usuario)
                st.success(f"Extracto guardado: {n} movimientos detectados ({banco}).")
            except Exception as e:
                st.error(f"No se pudo guardar el extracto: {e}")
        st.markdown("#### Extractos guardados")
        tabla(q("""SELECT e.id, e.archivo, e.banco, e.periodo_desde, e.periodo_hasta,
                   (SELECT COUNT(*) FROM extracto_movimientos m WHERE m.extracto_id = e.id) AS movimientos
                   FROM extractos e ORDER BY e.id DESC""").drop(columns=["id"]))
    else:
        st.subheader("Conciliación")
        st.caption("Por cada liquidación: factura − deducciones debe coincidir con lo acreditado en el extracto.")
        if st.button("Conciliar", type="primary"):
            st.session_state["resultado_conc"] = conciliar(usuario)
        if "resultado_conc" in st.session_state:
            res = st.session_state["resultado_conc"]
            if res.empty:
                st.info("No hay liquidaciones para conciliar.")
            else:
                tabla(res)


# ======================= PDF DE REPORTES =======================
def anchos_tabla(pdf, d):
    """Ancho de cada columna según el contenido, para que quepan los datos completos."""
    largos = []
    for j, c in enumerate(d.columns):
        vals = d.iloc[:, j].tolist()
        m = max([len(str(c))] + [len(fmt(v)) if isinstance(v, float) else len(str(v)) for v in vals])
        largos.append(min(max(m, 6), 40))
    s = sum(largos)
    return [273.0 * l / s for l in largos]


def filas_pdf(pdf, vals, anchos, bold=False):
    """Dibuja una fila con ajuste de texto: ninguna celda se corta."""
    lh = 4.2
    pdf.set_font("Helvetica", "B" if bold else "", 8)
    lineas = []
    for v, w in zip(vals, anchos):
        cur, out = "", []
        for p in str(v).split():
            cand = f"{cur} {p}".strip()
            if not cur or pdf.get_string_width(cand) <= w - 2:
                cur = cand
            else:
                out.append(cur)
                cur = p
        out.append(cur)
        lineas.append(out)
    alto = max(len(l) for l in lineas) * lh + 2
    if pdf.get_y() + alto > pdf.h - pdf.b_margin:
        pdf.add_page()
    y0, x0 = pdf.get_y(), pdf.l_margin
    x = x0
    for ls, w in zip(lineas, anchos):
        pdf.rect(x, y0, w, alto)
        for k, linea in enumerate(ls):
            pdf.set_xy(x + 1, y0 + 1 + k * lh)
            pdf.cell(w - 2, lh, linea)
        x += w
    pdf.set_xy(x0, y0 + alto)
def pdf_reporte(titulo, secciones, tachar=None):
    from fpdf import FPDF

    def t(v):
        if isinstance(v, float):
            return "" if pd.isna(v) else fmt(v)
        return str(v).replace("−", "-").encode("latin-1", "replace").decode("latin-1")

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, t(titulo), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, t(f"Castillo de Oro - generado el {ahora()}"), new_x="LMARGIN", new_y="NEXT")
    for sub, d in secciones:
        if sub:
            pdf.ln(4)
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 8, t(sub), new_x="LMARGIN", new_y="NEXT")
        anchos = anchos_tabla(pdf, d)
        filas_pdf(pdf, [t(c) for c in d.columns], anchos, bold=True)
        for _, fila in d.iterrows():
            filas_pdf(pdf, [t(v) for v in fila.tolist()], anchos)
    return bytes(pdf.output())


def boton_pdf(titulo, secciones, tachar=None):
    st.download_button("📄 Descargar en PDF", data=pdf_reporte(titulo, secciones, tachar),
                       file_name=f"{titulo.replace(' ', '_')}.pdf", mime="application/pdf", key=f"pdf_{titulo}")


# ======================= CONTABILIDAD =======================
def vista_contabilidad(op):
    if op == "Facturas impagas":
        d, h = rango("imp")
        r2 = q("""SELECT f.id, f.fecha, f.tipo, f.punto_venta, f.numero, f.razon_social, f.total,
                  (SELECT COUNT(*) FROM liquidacion_factura lf WHERE lf.factura_id = f.id) AS vinc,
                  (SELECT string_agg(DISTINCT c.medio, ', ') FROM cobros_directos c WHERE c.factura_id = f.id) AS medios,
                  (SELECT string_agg(DISTINCT COALESCE(NULLIF(l.nro_lote, ''), '#' || l.id::text), ', ')
                     FROM liquidacion_factura lf JOIN liquidaciones l ON l.id = lf.liquidacion_id
                     WHERE lf.factura_id = f.id) AS liqs
                  FROM facturas f WHERE f.fecha BETWEEN ? AND ? ORDER BY f.fecha""", (d, h))
        r2 = sin_nc(r2)
        if r2.empty:
            st.info("No hay facturas en el rango.")
            return
        r2["pendiente"] = [saldo_factura(int(i), t) for i, t in zip(r2.id, r2.total)]
        r2["pagada"] = r2.pendiente <= TOL
        r2["nc"] = [aplicado_factura(int(i)) for i in r2.id]
        r2["medio"] = [" + ".join(((["Liquidación de tarjeta"] if v > 0 else []) + ([m] if isinstance(m, str) else [])
                                   + (["Nota de crédito"] if n > TOL else [])))
                       for v, m, n in zip(r2.vinc, r2.medios, r2.nc)]
        r2["estado"] = [("Pagada" if p else "Parcial" if m else "Pendiente") + (f" ({m})" if m else "")
                        for p, m in zip(r2.pagada, r2.medio)]
        st.metric("Total impago", fmt(r2[~r2.pagada].pendiente.sum()))
        st.caption("Las facturas tachadas están pagadas en su totalidad (liquidación de tarjeta, efectivo, transferencia u otro medio).")
        filas = []
        for r in r2.itertuples():
            est = "text-decoration: line-through; color: #888;" if r.pagada else ""
            celdas = [str(r.fecha), f"{r.tipo} {r.punto_venta}-{r.numero}", str(r.razon_social),
                      fmt(r.total), fmt(r.pendiente), r.estado, r.liqs if isinstance(r.liqs, str) else "-"]
            tds = "".join(f"<td style='padding:4px 8px;border-bottom:1px solid #ddd;{est}'>{html.escape(c)}</td>" for c in celdas)
            filas.append(f"<tr>{tds}</tr>")
        enc = "".join(f"<th style='padding:4px 8px;text-align:left;border-bottom:2px solid #999'>{c}</th>"
                      for c in ["Fecha", "Comprobante", "Razón social", "Total", "Saldo pendiente", "Estado", "Liquidación Nº"])
        st.markdown(f"<table style='width:100%;border-collapse:collapse'><thead><tr>{enc}</tr></thead>"
                    f"<tbody>{''.join(filas)}</tbody></table>", unsafe_allow_html=True)
        boton_pdf("Facturas impagas", [(None, pd.DataFrame({
            "Fecha": r2.fecha, "Comprobante": r2.tipo + " " + r2.punto_venta.astype(str) + "-" + r2.numero.astype(str),
            "Razón social": r2.razon_social, "Total": r2.total, "Saldo pendiente": r2.pendiente,
            "Estado": r2.estado, "Liquidación Nº": r2.liqs.fillna("-")}))])

    elif op == "Impuestos para declaración jurada":
        d, h = rango("dj")
        plats = q("""SELECT DISTINCT l.plataforma FROM liquidacion_impuestos li JOIN liquidaciones l ON l.id = li.liquidacion_id
                     WHERE l.fecha_liquidacion BETWEEN ? AND ? AND li.impuesto <> 'Bienes Durables' ORDER BY l.plataforma""", (d, h))
        opc = ["— Elegí una plataforma —"] + list(plats.plataforma)
        plat = st.selectbox("Plataforma", opc)
        if plat == opc[0]:
            st.info("Elegí una plataforma para ver el detalle de impuestos.")
            return
        det = q("""SELECT l.id AS liquidacion, l.fecha_liquidacion, li.impuesto, li.monto
                   FROM liquidacion_impuestos li JOIN liquidaciones l ON l.id = li.liquidacion_id
                   WHERE l.fecha_liquidacion BETWEEN ? AND ? AND l.plataforma = ?
                     AND li.impuesto <> 'Bienes Durables' ORDER BY l.fecha_liquidacion""", (d, h, plat))
        if det.empty:
            st.info("No hay impuestos para esta plataforma en el rango.")
            return
        st.metric(f"Total impuestos - {plat}", fmt(det.monto.sum()))
        tipo_p = det.groupby("impuesto", as_index=False).monto.sum()
        st.markdown("#### Resumen por tipo de impuesto")
        tabla(tipo_p)
        det_p = det.pivot_table(index=["liquidacion", "fecha_liquidacion"], columns="impuesto",
                                values="monto", aggfunc="sum", fill_value=0).reset_index()
        det_p = det_p.rename(columns={"liquidacion": "Liquidación #", "fecha_liquidacion": "Fecha"})
        st.markdown("#### Detalle por liquidación")
        tabla(det_p)
        boton_pdf(f"Impuestos para declaración jurada - {plat}", [(None, tipo_p), ("Detalle por liquidación", det_p)])

    else:
        d, h = rango("res")
        plats_r = q("SELECT DISTINCT plataforma FROM liquidaciones WHERE fecha_liquidacion BETWEEN ? AND ? ORDER BY plataforma", (d, h))
        opc = ["Todas las plataformas"] + list(plats_r.plataforma)
        plat = st.selectbox("Plataforma", opc, key="res_plat")
        liq = q("SELECT * FROM liquidaciones WHERE fecha_liquidacion BETWEEN ? AND ?", (d, h))
        if plat != opc[0]:
            liq = liq[liq.plataforma == plat]
        if liq.empty:
            st.info("No hay liquidaciones para ese rango y plataforma.")
            return
        liq = liq.copy()
        liq["deducciones"] = liq.comision + liq.costo_financiero + liq.iva_comision + liq.retenciones
        # Cobrado = bruto menos deducciones
        liq["cobrado"] = (liq.bruto - liq.deducciones).round(2)
        k1, k2, k3 = st.columns(3)
        k1.metric("Facturado (bruto)", fmt(liq.bruto.sum()))
        k2.metric("Deducciones", fmt(liq.deducciones.sum()))
        k3.metric("Cobrado (después de deducciones)", fmt(liq.cobrado.sum()))
        res_pl = liq.groupby("plataforma")[["bruto", "deducciones", "cobrado"]].sum().reset_index()
        res_pl.columns = ["Plataforma", "Facturado", "Deducciones", "Cobrado"]
        st.markdown("#### Totales por plataforma")
        tabla(res_pl)
        det = liq[["fecha_liquidacion", "plataforma", "nro_lote", "bruto", "deducciones", "cobrado"]]
        det.columns = ["Fecha", "Plataforma", "Nº liquidación", "Facturado", "Deducciones", "Cobrado"]
        st.markdown("#### Detalle por liquidación")
        tabla(det)
        st.markdown("#### Detalle por comprobante (cómo se acredita cada venta)")
        st.caption("Desde el monto de cada factura hasta lo que efectivamente se acredita por esa venta.")
        plan = armar_planilla(d, h, None if plat == opc[0] else plat)
        if plan.empty:
            st.info("No hay comprobantes vinculados en este rango.")
            secciones = [(None, res_pl), ("Detalle por liquidación", det)]
        else:
            tabla(plan)
            st.download_button("📥 Descargar detalle en Excel", data=excel_bytes(plan),
                               file_name="detalle_por_comprobante.xlsx",
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            pdf_comp = pd.DataFrame({
                "Fecha": plan["FECHA FAC."], "Comprobante": plan["COMPROBANTE"], "Cliente": plan["CLIENTE"],
                "Monto factura": plan["MONTO FACTURA"], "Deducciones": plan["TOTAL DEDUCCIONES"],
                "Neto cobrado": plan["NETO PARA ESTA VENTA"]})
            secciones = [(None, res_pl), ("Detalle por liquidación", det), ("Detalle por comprobante", pdf_comp)]
        boton_pdf("Resumen de liquidaciones por plataforma", secciones)


def reportes_ventas():
    st.markdown("#### Reportes")
    cols = st.columns(len(REPORTES_VENTAS))
    for i, (col, r) in enumerate(zip(cols, REPORTES_VENTAS)):
        with col:
            with st.container(key=f"rep_{i}"):
                activo = st.session_state.get("rep") == r
                if st.button(r, key=f"repbtn_{i}", type="primary" if activo else "secondary", use_container_width=True):
                    st.session_state["rep"] = r
                    st.rerun()
    if st.session_state.get("rep") == "Facturas impagas":
        vista_contabilidad("Facturas impagas")


# ======================= LOGIN Y MENÚ =======================
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

# ======================= TABLERO DE CONTROL =======================
MODS = [("ventas", "📊", "Ventas"), ("tesoreria", "💰", "Tesorería"),
        ("contabilidad", "📈", "Contabilidad"), ("config", "⚙️", "Configuración")]
SUBS = {
    "ventas": ["Ventas", "Notas de crédito", "Notas de débito", "Reportes"],
    "tesoreria": ["Carga de liquidación", "Vinculación de comprobantes", "Planilla Excel"],
    "contabilidad": ["Impuestos para declaración jurada", "Resumen por plataforma"],
    "config": ["Reiniciar datos de prueba"],
}
REPORTES_VENTAS = ["Facturas impagas"]
ESTILO = """
<style>
[class*="st-key-mod_"] button {
    height: 150px !important; font-size: 1.45rem !important; font-weight: 700 !important;
    border: none !important; border-radius: 22px !important; color: #3b3350 !important;
    box-shadow: 0 6px 16px rgba(60,40,90,.10); transition: transform .2s, box-shadow .2s;
}
[class*="st-key-mod_"] button:hover { transform: translateY(-4px); box-shadow: 0 10px 22px rgba(60,40,90,.18); }
.st-key-mod_ventas button { background: #FBD5E3 !important; }
.st-key-mod_tesoreria button { background: #CDEFE2 !important; }
.st-key-mod_contabilidad button { background: #D9D2F5 !important; }
.st-key-mod_config button { background: #FCE6BF !important; }
@keyframes cascada { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
.st-key-mod_ventas, .st-key-mod_tesoreria, .st-key-mod_contabilidad, .st-key-mod_config,
[class*="st-key-sub_"] { animation: cascada .45s ease both; }
.st-key-mod_tesoreria { animation-delay: .10s; }
.st-key-mod_contabilidad { animation-delay: .20s; }
.st-key-mod_config { animation-delay: .30s; }
[class*="st-key-sub_"] button { font-size: 1.2rem !important; font-weight: 600 !important; min-height: 60px !important; }
.st-key-sub_0 { animation-delay: .00s; }
.st-key-sub_1 { animation-delay: .12s; }
.st-key-sub_2 { animation-delay: .24s; }
.st-key-sub_3 { animation-delay: .36s; }
.st-key-salir { position: fixed; bottom: 18px; left: 18px; width: 240px; z-index: 999; }
.st-key-salir button { background: #F3EEF8 !important; border-radius: 14px !important; }
</style>
"""
st.markdown(ESTILO, unsafe_allow_html=True)

if "mod" not in st.session_state:
    st.session_state["mod"] = None
if "sub" not in st.session_state:
    st.session_state["sub"] = None


def ir_a(mod):
    st.session_state["mod"] = mod
    st.session_state["sub"] = None
    st.session_state["rep"] = None
    st.rerun()


def tablero():
    st.markdown("### ¿Qué querés gestionar hoy?")
    mods = [m for m in MODS if m[0] != "config" or usuario == ADMIN]
    cols = st.columns(len(mods))
    for col, (k, icono, nombre) in zip(cols, mods):
        with col:
            if st.button(f"{icono}  {nombre}", key=f"mod_{k}", use_container_width=True):
                ir_a(k)


def menu_subs(mod):
    opciones = SUBS[mod]
    cols = st.columns(len(opciones))
    for i, (col, s) in enumerate(zip(cols, opciones)):
        with col:
            with st.container(key=f"sub_{i}"):
                activo = st.session_state["sub"] == s
                if st.button(s, key=f"subbtn_{mod}_{i}", type="primary" if activo else "secondary",
                             use_container_width=True):
                    st.session_state["sub"] = s
                    st.session_state["rep"] = None
                    st.rerun()


def configuracion():
    st.subheader("Reiniciar datos de prueba")
    st.caption("Borra liquidaciones, facturas, extractos y actividad. No se puede deshacer.")
    conf_todo = st.checkbox("Entiendo que se borrará todo", key="chk_reset_todo")
    if st.button("Borrar todos los datos", disabled=not conf_todo, key="btn_reset_todo"):
        borrar_todos_los_datos()
        st.session_state.pop("resultado_conc", None)
        st.rerun()


# ======================= BARRA LATERAL =======================
with st.sidebar:
    st.markdown(f"### 👤 {usuario}")
    with st.container(key="salir"):
        if st.button("🚪 Salir", key="btn_salir", use_container_width=True):
            registrar("Salida", "Sesión cerrada")
            st.session_state["usuario"] = None
            st.session_state["mod"] = None
            st.rerun()

# ======================= CONTENIDO =======================
st.title("Castillo de Oro")
mod, sub = st.session_state["mod"], st.session_state["sub"]
if mod is None:
    tablero()
else:
    top = st.columns([1, 5])
    if top[0].button("← Tablero", key="volver"):
        st.session_state["mod"] = None
        st.rerun()
    nombre_mod = next(n for k, _, n in MODS if k == mod)
    icono_mod = next(i for k, i, _ in MODS if k == mod)
    st.markdown(f"## {icono_mod} {nombre_mod}")
    menu_subs(mod)
    st.divider()
    if sub is None:
        pass  # Solo se muestran los subtítulos hasta que se elige uno
    elif mod == "ventas":
        if sub == "Notas de crédito":
            vista_notas_credito(usuario)
        elif sub == "Notas de débito":
            st.info("Notas de débito: en desarrollo.")
        elif sub == "Reportes":
            reportes_ventas()
        else:
            vista_ventas(usuario)
    elif mod == "tesoreria":
        if sub == "Carga de liquidación":
            liq_carga(usuario)
        elif sub == "Vinculación de comprobantes":
            liq_vinculacion(usuario)
        else:
            liq_planilla()
    elif mod == "contabilidad":
        vista_contabilidad(sub)
    elif mod == "config":
        if usuario == ADMIN:
            configuracion()
        else:
            st.info("Solo el administrador puede acceder a la configuración.")

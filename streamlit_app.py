import base64
from datetime import date
from io import BytesIO

import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Sistema de Gestión y Conciliación - Mueblería Castillo de Oro",
    page_icon="💼",
    layout="wide",
)

# --- USUARIOS (cambiar las contraseñas antes de usar) ---
USUARIOS = {"socio1": "clave1", "socio2": "clave2"}

SUBMENUS = {
    "📊 Ventas": ["Carga de Registro de Ventas"],
    "💰 Tesorería": ["Liquidaciones", "Extractos Bancarios"],
    "📈 Contabilidad": [
        "Reporte Consolidado Ventas vs Acreditaciones",
        "Reporte de Deducciones y Aranceles",
        "Reporte de Pendientes de Acreditación",
        "Reporte de Conciliación Bancaria",
    ],
}


# ======================= UTILIDADES =======================
def leer_archivo(archivo):
    """Lee CSV o XLSX y devuelve un DataFrame. Reinicia el puntero del archivo."""
    archivo.seek(0)
    if archivo.name.lower().endswith(".xlsx"):
        return pd.read_excel(archivo, dtype=str)
    return pd.read_csv(archivo, sep=None, engine="python", dtype=str)


def selector_rango(key):
    hoy = date.today()
    c1, c2 = st.columns(2)
    desde = c1.date_input("📅 Desde la fecha:", value=date(hoy.year, 1, 1), key=f"{key}_desde")
    hasta = c2.date_input("📅 Hasta la fecha:", value=hoy, key=f"{key}_hasta")
    return desde, hasta


# ======================= SESIÓN =======================
if "autenticado" not in st.session_state:
    st.session_state["autenticado"] = False
if "archivos" not in st.session_state:
    st.session_state["archivos"] = {}
if "bloque_abierto" not in st.session_state:
    st.session_state["bloque_abierto"] = "📊 Ventas"


def alternar_bloque(nombre):
    actual = st.session_state["bloque_abierto"]
    st.session_state["bloque_abierto"] = None if actual == nombre else nombre


# ======================= LOGIN =======================
st.sidebar.image("https://img.icons8.com/color/96/wooden-furniture.png", width=70)
st.sidebar.title("Mueblería Castillo de Oro")
st.sidebar.markdown("---")

if not st.session_state["autenticado"]:
    st.sidebar.subheader("🔒 Acceso al Sistema")
    with st.sidebar.form("login"):
        usuario = st.text_input("Usuario")
        password = st.text_input("Contraseña", type="password")
        ingresar = st.form_submit_button("Ingresar al Sistema", type="primary")

    if ingresar:
        if usuario in USUARIOS and USUARIOS[usuario] == password:
            st.session_state["autenticado"] = True
            st.session_state["usuario_actual"] = usuario
            st.rerun()
        else:
            st.sidebar.error("Usuario o contraseña incorrectos.")
    st.stop()

# ======================= CABECERA DE USUARIO =======================
col_u1, col_u2 = st.sidebar.columns([2, 1])
with col_u1:
    st.write(f"👤 **{st.session_state['usuario_actual']}**")
with col_u2:
    if st.button("🚪 Salir", key="btn_logout"):
        st.session_state["autenticado"] = False
        st.rerun()

# ======================= MENÚ ACORDEÓN =======================
st.sidebar.markdown("---")
st.sidebar.subheader("📌 Módulos Principales")

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


# ======================= 1. VENTAS =======================
def vista_ventas():
    st.markdown("## 📊 Ventas")
    st.markdown("### Carga de Registro de Ventas")
    st.write(
        "Gestión de facturación, comprobantes comerciales y registro de bruto "
        "declarado para **Mueblería Castillo de Oro**."
    )
    st.markdown("---")

    ventas_file = st.file_uploader(
        "Subir archivo de registro de ventas / facturación (CSV, XLSX):",
        type=["csv", "xlsx"],
        key="ventas_uploader",
    )

    if st.button("Registrar Ventas", type="primary"):
        if ventas_file:
            try:
                st.session_state["archivos"]["ventas"] = leer_archivo(ventas_file)
                st.success(
                    f"¡Registro de ventas actualizado! "
                    f"{len(st.session_state['archivos']['ventas'])} filas leídas."
                )
            except Exception as e:
                st.error(f"No se pudo leer el archivo: {e}")
        else:
            st.warning("Selecciona un archivo de ventas para registrar.")

    if "ventas" in st.session_state["archivos"]:
        st.markdown("#### Vista previa de ventas registradas")
        st.dataframe(st.session_state["archivos"]["ventas"], hide_index=True)


# ======================= 2. TESORERÍA =======================
def vista_liquidaciones():
    st.markdown("## 💰 Tesorería - Liquidaciones")
    st.write(
        "Bandeja unificada de carga de liquidaciones y comprobantes para procesar "
        "comisiones, tasas e impuestos."
    )
    st.markdown("---")

    archivos_liq = st.file_uploader(
        "Arrastra o selecciona todos los archivos de liquidación aquí:",
        type=["csv", "xlsx", "pdf"],
        accept_multiple_files=True,
        key="uploader_liquidaciones",
    )

    if st.button("Procesar Liquidaciones", type="primary"):
        if archivos_liq:
            st.session_state["archivos"]["liquidaciones"] = archivos_liq
            st.success(f"¡Liquidaciones procesadas! {len(archivos_liq)} archivo(s).")
        else:
            st.warning("Sube al menos un archivo para procesar.")

    for archivo in st.session_state["archivos"].get("liquidaciones", []):
        st.text(f"📄 {archivo.name}")
        if not archivo.name.lower().endswith(".pdf"):
            try:
                st.dataframe(leer_archivo(archivo), hide_index=True)
            except Exception as e:
                st.error(f"No se pudo leer {archivo.name}: {e}")


def vista_extractos():
    st.markdown("## 💰 Tesorería - Extractos Bancarios")
    st.write(
        "Carga de extractos bancarios para automatizar el cálculo y la conciliación "
        "de acreditaciones frente a lo pendiente de cobro."
    )
    st.markdown("---")

    extractos_files = st.file_uploader(
        "Subir extractos bancarios (PDF, CSV, XLSX):",
        type=["csv", "xlsx", "pdf"],
        accept_multiple_files=True,
        key="uploader_extractos",
    )

    if st.button("Conciliar con Extractos Bancarios", type="primary"):
        if extractos_files:
            st.session_state["archivos"]["extractos"] = extractos_files
            st.success(f"¡Extractos cargados! {len(extractos_files)} archivo(s).")
        else:
            st.warning("Sube un extracto bancario para realizar la conciliación.")

    for ext in st.session_state["archivos"].get("extractos", []):
        st.text(f"🏦 Extracto detectado: {ext.name}")
        if not ext.name.lower().endswith(".pdf"):
            try:
                st.dataframe(leer_archivo(ext), hide_index=True)
            except Exception as e:
                st.error(f"No se pudo leer {ext.name}: {e}")


# ======================= 3. CONTABILIDAD =======================
def vista_contabilidad(nombre_informe):
    st.markdown(f"## 📈 Contabilidad - {nombre_informe}")
    st.write(
        "Seleccione el rango de fechas para auditar y generar el informe oficial "
        "histórico de **Mueblería Castillo de Oro**."
    )
    st.markdown("---")

    fecha_desde, fecha_hasta = selector_rango("filtro")
    st.markdown(
        f"🔍 *Filtrando registros desde el **{fecha_desde.strftime('%d/%m/%Y')}** "
        f"al **{fecha_hasta.strftime('%d/%m/%Y')}***"
    )
    st.markdown("---")

    st.info(
        "Los importes se calcularán cuando estén conectados los datos de ventas, "
        "liquidaciones y extractos. Por ahora se muestran como referencia."
    )

    if nombre_informe == "Reporte Consolidado Ventas vs Acreditaciones":
        st.subheader("📊 Resumen Consolidado por Período")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Bruto Ventas", "$ 0,00")
        c2.metric("Total Liquidado", "$ 0,00")
        c3.metric("Acreditado en Banco", "$ 0,00")
        c4.metric("Pendiente de Cobro", "$ 0,00")

    elif nombre_informe == "Reporte de Deducciones y Aranceles":
        st.subheader("🧾 Detalle de Aranceles e Impuestos del Período")
        df_ded = pd.DataFrame({
            "Concepto": [
                "Aranceles",
                "Costo Financiación / Cuotas",
                "IVA 21%",
                "Percepciones IVA / SIRTAC",
                "Total Deducciones",
            ],
            "Importe ($)": [0.0, 0.0, 0.0, 0.0, 0.0],
        })
        st.table(df_ded)

    elif nombre_informe == "Reporte de Pendientes de Acreditación":
        st.subheader("⏳ Listado de Fondos en Tránsito a la Fecha")
        st.write("Sin datos cargados todavía para este período.")

    elif nombre_informe == "Reporte de Conciliación Bancaria":
        st.subheader("🏦 Verificación de Extracto Bancario del Período")
        st.write("Sin datos cargados todavía para este período.")

    html_reporte = f"""<html>
<head><meta charset="utf-8">
<style>
  body {{ font-family: Helvetica, Arial, sans-serif; color: #333; margin: 30px; }}
  h1 {{ color: #1e3a8a; text-align: center; border-bottom: 2px solid #1e3a8a; padding-bottom: 10px; font-size: 18px; }}
  .header-info {{ margin-bottom: 20px; font-size: 12px; }}
  p {{ font-size: 13px; }}
</style></head>
<body>
  <h1>MUEBLERÍA CASTILLO DE ORO - {nombre_informe.upper()}</h1>
  <div class="header-info">
    <p><strong>Período Auditado:</strong> {fecha_desde.strftime('%d/%m/%Y')} al {fecha_hasta.strftime('%d/%m/%Y')}</p>
    <p><strong>Fecha de Emisión:</strong> {pd.Timestamp.now().strftime('%d/%m/%Y %H:%M')}</p>
    <p><strong>Estado:</strong> Documento Oficial (Estudio Contable)</p>
  </div>
  <p>Informe histórico generado y filtrado para auditoría contable.</p>
</body></html>"""

    nombre_archivo = (
        f"{nombre_informe.replace(' ', '_')}_"
        f"{fecha_desde.strftime('%Y%m%d')}_al_{fecha_hasta.strftime('%Y%m%d')}.html"
    )
    st.markdown("<br>", unsafe_allow_html=True)
    st.download_button(
        "📥 Descargar Informe Oficial del Período",
        data=html_reporte.encode("utf-8"),
        file_name=nombre_archivo,
        mime="text/html",
        type="primary",
    )


# ======================= RENDERIZADO SEGÚN MENÚ =======================
bloque_abierto = st.session_state["bloque_abierto"]

if bloque_abierto is None:
    st.info("Haga clic en un módulo del menú lateral para desplegar sus opciones.")
elif bloque_abierto == "📊 Ventas":
    vista_ventas()
elif bloque_abierto == "💰 Tesorería":
    sub = st.session_state.get("sub_💰 Tesorería", SUBMENUS["💰 Tesorería"][0])
    if sub == "Extractos Bancarios":
        vista_extractos()
    else:
        vista_liquidaciones()
elif bloque_abierto == "📈 Contabilidad":
    sub = st.session_state.get("sub_📈 Contabilidad", SUBMENUS["📈 Contabilidad"][0])
    vista_contabilidad(sub)

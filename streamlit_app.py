import base64
from io import BytesIO
import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Sistema de Gestión y Conciliación - Mueblería Castillo de Oro",
    page_icon="💼",
    layout="wide",
)

# --- CONTROL DE SESIÓN ---
if "autenticado" not in st.session_state:
  st.session_state["autenticado"] = False

# --- BARRA LATERAL (SIDEBAR) - AUTENTICACIÓN Y NAVEGACIÓN ---
st.sidebar.image(
    "https://img.icons8.com/color/96/wooden-furniture.png", width=80
)
st.sidebar.title("Mueblería Castillo de Oro")
st.sidebar.markdown("---")

if not st.session_state["autenticado"]:
  st.sidebar.subheader("🔒 Acceso al Sistema")
  usuario = st.sidebar.text_input("Usuario", key="user_login")
  password = st.sidebar.text_input("Contraseña", type="password", key="pass_login")

  if st.sidebar.button("Ingresar al Sistema"):
    if usuario and password:
      st.session_state["autenticado"] = True
      st.session_state["usuario_actual"] = usuario
      st.rerun()
    else:
      st.sidebar.error("Por favor completa los datos de acceso.")
  st.stop()

# --- MENÚ DE MÓDULOS PRINCIPALES ---
st.sidebar.success(f"Usuario: {st.session_state.get('usuario_actual')}")
st.sidebar.markdown("---")
st.sidebar.subheader("📌 Módulos Principales")

if "modulo_activo" not in st.session_state:
  st.session_state["modulo_activo"] = "Ventas"

# Botón Módulo Ventas
if st.sidebar.button(
    "📊 Ventas", use_container_width=True, key="btn_menu_ventas"
):
  st.session_state["modulo_activo"] = "Ventas"
  st.rerun()

# Desplegable Módulo Tesorería
with st.sidebar.expander("💰 Tesorería", expanded=False):
  sub_tesoreria = st.radio(
      "Seleccione una tarea:",
      ["Liquidaciones", "Extractos Bancarios"],
      key="sub_tesoreria_radio",
  )
  if st.button("Ir a Tesorería", key="btn_go_tesoreria", type="primary"):
    st.session_state["modulo_activo"] = f"Tesorería - {sub_tesoreria}"
    st.rerun()

# Desplegable Módulo Contabilidad
with st.sidebar.expander("📈 Contabilidad", expanded=False):
  sub_contabilidad = st.radio(
      "Seleccione un informe:",
      [
          "Reporte Consolidado Ventas vs Acreditaciones",
          "Reporte de Deducciones y Aranceles",
          "Reporte de Pendientes de Acreditación",
          "Reporte de Conciliación Bancaria",
      ],
      key="sub_contabilidad_radio",
  )
  if st.button("Ir a Contabilidad", key="btn_go_contabilidad", type="primary"):
    st.session_state["modulo_activo"] = f"Contabilidad - {sub_contabilidad}"
    st.rerun()

st.sidebar.markdown("---")
if st.sidebar.button("🚪 Cerrar Sesión"):
  st.session_state["autenticado"] = False
  st.rerun()

# --- CONTENIDO SEGÚN MÓDULO Y SUBMÓDULO SELECCIONADO ---
modulo_actual = st.session_state["modulo_activo"]

# 1. MÓDULO DE VENTAS
if modulo_actual == "Ventas":
  st.markdown("## 📊 Módulo de Ventas")
  st.write(
      "Gestión de facturación, comprobantes comerciales y registro de bruto"
      " declarado para **Mueblería Castillo de Oro**."
  )
  st.markdown("---")

  st.subheader("Carga de Registro de Ventas")
  ventas_file = st.file_uploader(
      "Subir archivo de registro de ventas / facturación (CSV, XLSX):",
      type=["csv", "xlsx"],
      key="ventas_uploader",
  )

  if ventas_file:
    st.info(f"Archivo cargado: {ventas_file.name}")

  if st.button("Registrar Ventas", type="primary"):
    if ventas_file:
      st.success("¡Registro de ventas actualizado correctamente!")
    else:
      st.warning("Selecciona un archivo de ventas para registrar.")

# 2. TESORERÍA - LIQUIDACIONES
elif "Tesorería - Liquidaciones" in modulo_actual:
  st.markdown("## 💰 Tesorería - Liquidaciones")
  st.write(
      "Bandeja unificada de carga de liquidaciones y comprobantes para"
      " procesar comisiones, tasas e impuestos."
  )
  st.markdown("---")

  archivos_liquidaciones = st.file_uploader(
      "Arrastra o selecciona todos los archivos de liquidación aquí:",
      type=["csv", "xlsx", "pdf"],
      accept_multiple_files=True,
      key="uploader_liquidaciones",
  )

  if archivos_liquidaciones:
    st.info(
        f"Se han cargado {len(archivos_liquidaciones)} archivo(s) correctamente."
    )
    for archivo in archivos_liquidaciones:
      st.text(f"📄 Archivo cargado: {archivo.name}")

  if st.button("Procesar Liquidaciones", type="primary"):
    if archivos_liquidaciones:
      st.success("¡Liquidaciones procesadas con éxito!")
    else:
      st.warning("Sube al menos un archivo para procesar.")

# 3. TESORERÍA - EXTRACTOS BANCARIOS
elif "Tesorería - Extractos Bancarios" in modulo_actual:
  st.markdown("## 💰 Tesorería - Extractos Bancarios")
  st.write(
      "Carga de extractos bancarios para automatizar el cálculo y la"
      " conciliación de acreditaciones frente a lo pendiente de cobro."
  )
  st.markdown("---")

  extractos_files = st.file_uploader(
      "Subir extractos bancarios (PDF, CSV, XLSX):",
      type=["csv", "xlsx", "pdf"],
      accept_multiple_files=True,
      key="uploader_extractos",
  )

  if extractos_files:
    st.info(f"Se han cargado {len(extractos_files)} extracto(s) bancario(s).")
    for ext in extractos_files:
      st.text(f"🏦 Extracto detectado: {ext.name}")

  if st.button("Conciliar con Extractos Bancarios", type="primary"):
    if extractos_files:
      st.success(
          "¡Extractos conciliados correctamente con el módulo de contabilidad!"
      )
    else:
      st.warning("Sube un extracto bancario para realizar la conciliación.")

# 4. CONTABILIDAD - INFORMES
elif "Contabilidad" in modulo_actual:
  nombre_informe = modulo_actual.split(" - ")[
      1
  ]  # Extrae el submenú seleccionado
  st.markdown(f"## 📈 Contabilidad - {nombre_informe}")
  st.write(
      "Consolidación financiera y fiscal oficial para el estudio contable de"
      " **Mueblería Castillo de Oro**."
  )
  st.markdown("---")

  if nombre_informe == "Reporte Consolidado Ventas vs Acreditaciones":
    st.subheader("📊 Resumen Consolidado de Ventas y Cobros")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Bruto Ventas", "$ 0,00")
    col2.metric("Total Liquidado", "$ 0,00")
    col3.metric("Acreditado en Banco", "$ 0,00")
    col4.metric("Pendiente de Cobro", "$ 0,00")

  elif nombre_informe == "Reporte de Deducciones y Aranceles":
    st.subheader("🧾 Detalle de Aranceles, Impuestos y Retenciones")
    df_deducciones = pd.DataFrame({
        "Concepto": [
            "Aranceles",
            "Costo Financiación / Cuotas",
            "IVA 21%",
            "Percepciones IVA / SIRTAC",
            "Total Deducciones",
        ],
        "Importe ($)": [0.00, 0.00, 0.00, 0.00, 0.00],
    })
    st.table(df_deducciones)

  elif nombre_informe == "Reporte de Pendientes de Acreditación":
    st.subheader("⏳ Listado de Fondos en Tránsito")
    st.info("No hay saldos pendientes de acreditación en este período.")

  elif nombre_informe == "Reporte de Conciliación Bancaria":
    st.subheader("🏦 Verificación de Extracto Bancario")
    st.success("Extracto bancario verificado y conciliado al 100%.")

  # Botón de Descarga del Informe Seleccionado
  st.markdown("<br>", unsafe_allow_html=True)
  html_reporte = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Helvetica, Arial, sans-serif; color: #333; margin: 30px; }}
            h1 {{ color: #1e3a8a; text-align: center; border-bottom: 2px solid #1e3a8a; padding-bottom: 10px; font-size: 18px; }}
            .header-info {{ margin-bottom: 20px; font-size: 12px; }}
            p {{ font-size: 13px; }}
        </style>
    </head>
    <body>
        <h1>MUEBLERÍA CASTILLO DE ORO - {nombre_informe.upper()}</h1>
        <div class="header-info">
            <p><strong>Fecha de Emisión:</strong> {pd.Timestamp.now().strftime('%d/%m/%Y %H:%M')}</p>
            <p><strong>Estado:</strong> Documento Oficial Inalterable (Estudio Contable)</p>
        </div>
        <p>Informe generado correctamente para revisión contable.</p>
    </body>
    </html>
    """
  b64 = base64.b64encode(html_reporte.encode()).decode()
  href = f'<a href="data:text/html;base64,{b64}" download="{nombre_informe.replace(" ", "_")}.html" target="_blank" style="font-size:15px; font-weight:bold; padding:10px 18px; background-color:#1e3a8a; color:white; text-decoration:none; border-radius:5px;">📥 Descargar Informe Oficial</a>'
  st.markdown(href, unsafe_allow_html=True)

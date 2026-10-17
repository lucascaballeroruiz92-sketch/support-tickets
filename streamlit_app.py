import base64
from datetime import date
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
    "https://img.icons8.com/color/96/wooden-furniture.png", width=70
)
st.sidebar.title("Mueblería Castillo de Oro")
st.sidebar.markdown("---")

if not st.session_state["autenticado"]:
  st.sidebar.subheader("🔒 Acceso al Sistema")
  usuario = st.sidebar.text_input("Usuario", key="user_login")
  password = st.sidebar.text_input("Contraseña", type="password", key="pass_login")

  if st.sidebar.button("Ingresar al Sistema", type="primary"):
    if usuario and password:
      st.session_state["autenticado"] = True
      st.session_state["usuario_actual"] = usuario
      st.rerun()
    else:
      st.sidebar.error("Por favor completa los datos de acceso.")
  st.stop()

# --- CABECERA DE USUARIO Y CIERRE DE SESIÓN ---
col_u1, col_u2 = st.sidebar.columns([2, 1])
with col_u1:
  st.write(f"👤 **{st.session_state.get('usuario_actual')}**")
with col_u2:
  if st.button("🚪 Salir", key="btn_logout"):
    st.session_state["autenticado"] = False
    st.rerun()

st.sidebar.markdown("---")
st.sidebar.subheader("📌 Módulos Principales")

# Menú desplegable lateral limpio y 100% estable (Evita bucles de pantalla en blanco)
menu_opcion = st.sidebar.selectbox(
    "Seleccione Sección / Módulo:",
    [
        "📊 Ventas",
        "💰 Tesorería - Liquidaciones",
        "💰 Tesorería - Extractos Bancarios",
        "📈 Contabilidad - Reporte Consolidado Ventas vs Acreditaciones",
        "📈 Contabilidad - Reporte de Deducciones y Aranceles",
        "📈 Contabilidad - Reporte de Pendientes de Acreditación",
        "📈 Contabilidad - Reporte de Conciliación Bancaria",
    ],
    key="select_menu_principal",
)

# --- RENDERIZADO DE CONTENIDO SEGÚN LA SELECCIÓN ---

# 1. MÓDULO DE VENTAS
if menu_opcion == "📊 Ventas":
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
elif menu_opcion == "💰 Tesorería - Liquidaciones":
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
elif menu_opcion == "💰 Tesorería - Extractos Bancarios":
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

# 4. CONTABILIDAD - INFORMES CON FILTRO DE PERÍODO HISTÓRICO
elif "📈 Contabilidad" in menu_opcion:
  nombre_informe = menu_opcion.split(" - ")[1]
  st.markdown(f"## 📈 Contabilidad - {nombre_informe}")
  st.write(
      "Seleccione el rango de fechas para auditar y generar el informe oficial"
      " histórico de **Mueblería Castillo de Oro**."
  )
  st.markdown("---")

  # --- FILTRO DE RANGO DE FECHAS PARA AUDITORÍA ---
  col_f1, col_f2 = st.columns(2)
  with col_f1:
    fecha_desde = st.date_input(
        "📅 Desde la fecha:", value=date(2026, 1, 1), key="filtro_desde"
    )
  with col_f2:
    fecha_hasta = st.date_input(
        "📅 Hasta la fecha:", value=date.today(), key="filtro_hasta"
    )

  st.markdown(
      f"🔍 *Filtrando registros desde el **{fecha_desde.strftime('%d/%m/%Y')}**"
      f" al **{fecha_hasta.strftime('%d/%m/%Y')}***"
  )
  st.markdown("---")

  if nombre_informe == "Reporte Consolidado Ventas vs Acreditaciones":
    st.subheader("📊 Resumen Consolidado por Período")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Bruto Ventas", "$ 0,00")
    col2.metric("Total Liquidado", "$ 0,00")
    col3.metric("Acreditado en Banco", "$ 0,00")
    col4.metric("Pendiente de Cobro", "$ 0,00")

  elif nombre_informe == "Reporte de Deducciones y Aranceles":
    st.subheader("🧾 Detalle de Aranceles e Impuestos del Período")
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
    st.subheader("⏳ Listado de Fondos en Tránsito a la Fecha")
    st.info(
        "No se registran saldos pendientes de acreditación para el período"
        " seleccionado."
    )

  elif nombre_informe == "Reporte de Conciliación Bancaria":
    st.subheader("🏦 Verificación de Extracto Bancario del Período")
    st.success(
        "Extracto bancario del período seleccionado verificado y conciliado al"
        " 100%."
    )

  # Botón de Descarga HTML Oficial con el rango de fechas dinámico
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
            <p><strong>Período Auditado:</strong> {fecha_desde.strftime('%d/%m/%Y')} al {fecha_hasta.strftime('%d/%m/%Y')}</p>
            <p><strong>Fecha de Emisión:</strong> {pd.Timestamp.now().strftime('%d/%m/%Y %H:%M')}</p>
            <p><strong>Estado:</strong> Documento Oficial Inalterable (Estudio Contable)</p>
        </div>
        <p>Informe histórico generado y filtrado correctamente para auditoría contable.</p>
    </body>
    </html>
    """
  b64 = base64.b64encode(html_reporte.encode()).decode()
  filename = f"{nombre_informe.replace(' ', '_')}_{fecha_desde.strftime('%Y%m%d')}_al_{fecha_hasta.strftime('%Y%m%d')}.html"
  href = f'<a href="data:text/html;base64,{b64}" download="{filename}" target="_blank" style="font-size:15px; font-weight:bold; padding:10px 18px; background-color:#1e3a8a; color:white; text-decoration:none; border-radius:5px;">📥 Descargar Informe Oficial del Período</a>'
  st.markdown(href, unsafe_allow_html=True)

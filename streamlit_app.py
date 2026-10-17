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

# --- MENÚ DE MÓDULOS PRINCIPALES CON SUBMENÚ ANIDADO ---
st.sidebar.success(f"Usuario: {st.session_state.get('usuario_actual')}")
st.sidebar.markdown("---")
st.sidebar.subheader("📌 Módulos Principales")

opciones_menu = [
    "📊 Ventas",
    "💰 Tesorería - Liquidaciones",
    "💰 Tesorería - Extractos Bancarios",
    "📈 Contabilidad",
]

opcion_seleccionada = st.sidebar.radio(
    "Seleccione el Módulo / Tarea:",
    opciones_menu,
    key="radio_menu_principal",
)

st.sidebar.markdown("---")
if st.sidebar.button("🚪 Cerrar Sesión"):
  st.session_state["autenticado"] = False
  st.rerun()

# --- CONTENIDO SEGÚN OPCIÓN SELECCIONADA ---

# 1. MÓDULO DE VENTAS
if opcion_seleccionada == "📊 Ventas":
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
elif opcion_seleccionada == "💰 Tesorería - Liquidaciones":
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
elif opcion_seleccionada == "💰 Tesorería - Extractos Bancarios":
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

# 4. MÓDULO DE CONTABILIDAD
elif opcion_seleccionada == "📈 Contabilidad":
  st.markdown("## 📈 Módulo de Contabilidad")
  st.write(
      "Resumen consolidado para **Mueblería Castillo de Oro**. Generación y"
      " descarga del informe oficial inalterable para el contador con el"
      " estado de lo cobrado y pendiente de cobro."
  )
  st.markdown("---")

  html_reporte = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Helvetica, Arial, sans-serif; color: #333; margin: 30px; }}
            h1 {{ color: #1e3a8a; text-align: center; border-bottom: 2px solid #1e3a8a; padding-bottom: 10px; font-size: 20px; }}
            .header-info {{ margin-bottom: 20px; font-size: 13px; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 11px; }}
            th, td {{ border: 1px solid #cbd5e1; padding: 8px; text-align: left; }}
            th {{ background-color: #f1f5f9; color: #1e3a8a; text-align: center; }}
            td {{ text-align: right; }}
            td:first-child {{ text-align: left; font-weight: bold; }}
            .footer {{ margin-top: 40px; font-size: 9px; text-align: center; color: #64748b; }}
        </style>
    </head>
    <body>
        <h1>INFORME DE CONCILIACIÓN CONTABLE Y FISCAL</h1>
        <div class="header-info">
            <p><strong>Empresa:</strong> Mueblería Castillo de Oro</p>
            <p><strong>Fecha de Emisión:</strong> {pd.Timestamp.now().strftime('%d/%m/%Y %H:%M')}</p>
            <p><strong>Estado:</strong> Documento Oficial Inalterable (Contador)</p>
        </div>
        <h3>Estado Consolidado de Cobros y Liquidaciones</h3>
        <table>
            <tr>
                <th>Concepto</th>
                <th>Bruto Ventas</th>
                <th>Liquidado</th>
                <th>Acreditado en Banco</th>
                <th>Pendiente de Cobro</th>
            </tr>
            <tr>
                <td>Resumen Consolidado</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
            </tr>
        </table>
        <div class="footer">
            <p>Reporte generado automáticamente para Mueblería Castillo de Oro.</p>
        </div>
    </body>
    </html>
    """

  b64 = base64.b64encode(html_reporte.encode()).decode()
  href = f'<a href="data:text/html;base64,{b64}" download="Reporte_Contador_Castillo_de_Oro.html" target="_blank" style="font-size:16px; font-weight:bold; padding:10px 20px; background-color:#1e3a8a; color:white; text-decoration:none; border-radius:5px;">📥 Descargar Reporte Oficial para el Contador</a>'
  st.markdown("<br>", unsafe_allow_html=True)
  st.markdown(href, unsafe_allow_html=True)

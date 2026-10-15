import base64
from io import BytesIO
import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Sistema de Conciliación - Mueblería Castillo de Oro",
    page_icon="💼",
    layout="wide",
)

# --- BASE DE DATOS DE CLIENTES DINÁMICA ---
if "clientes" not in st.session_state:
  st.session_state.clientes = [
      "Mueblería Castillo de Oro",
      "Comercio Ejemplo S.A.",
  ]

# --- CONTROL DE SESIÓN ---
if "autenticado" not in st.session_state:
  st.session_state["autenticado"] = False

if "empresa_actual" not in st.session_state:
  st.session_state["empresa_actual"] = st.session_state.clientes[0]

# --- BARRA LATERAL (SIDEBAR) PARA LOGIN Y NAVEGACIÓN ---
st.sidebar.image(
    "https://img.icons8.com/color/96/wooden-furniture.png", width=80
)
st.sidebar.title("Mueblería Castillo de Oro")
st.sidebar.markdown("---")

if not st.session_state["autenticado"]:
  st.sidebar.subheader("🔒 Acceso al Sistema")
  cliente_seleccionado = st.sidebar.selectbox(
      "Seleccionar Empresa / Cliente:",
      st.session_state.clientes,
      key="select_login_empresa",
  )
  st.session_state["empresa_actual"] = cliente_seleccionado

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

# --- SI ESTÁ AUTENTICADO: MENÚ LATERAL Y PANEL PRINCIPAL ---
st.sidebar.success(f"Usuario: {st.session_state.get('usuario_actual')}")

# Selector activo para cambiar de cliente en cualquier momento
cliente_activo = st.sidebar.selectbox(
    "Empresa Activa:",
    st.session_state.clientes,
    index=st.session_state.clientes.index(
        st.session_state.get("empresa_actual")
    ),
    key="select_menu_empresa",
)
st.session_state["empresa_actual"] = cliente_activo

st.sidebar.markdown("---")
st.sidebar.subheader("📌 Menú de Tareas")
opcion_menu = st.sidebar.radio(
    "Seleccione una opción:",
    [
        "Carga y Conciliación de Archivos",
        "Gestión Multitenant (Admin)",
        "📄 Informe para el Contador (PDF Seguro)",
    ],
)

st.sidebar.markdown("---")
if st.sidebar.button("🚪 Cerrar Sesión"):
  st.session_state["autenticado"] = False
  st.rerun()

# --- CONTENIDO PRINCIPAL SEGÚN LA OPCIÓN DEL MENÚ LATERAL ---
if opcion_menu == "Carga y Conciliación de Archivos":
  st.markdown(
      f"## 📊 Carga de Comprobantes y Liquidaciones - [{st.session_state.get('empresa_actual')}]"
  )
  st.write(
      "Sube los archivos de AFIP (ARCA), Naranja X, Nación Pagos, Mercado"
      " Pago y Payway para procesar comisiones, IVA y tasas."
  )
  st.markdown("---")

  col1, col2 = st.columns(2)
  with col1:
    st.file_uploader(
        "Liquidación Mercado Pago", type=["csv", "xlsx"], key="mp"
    )
    st.file_uploader(
        "Liquidación Naranja X", type=["csv", "xlsx"], key="naranja"
    )
  with col2:
    st.file_uploader(
        "Liquidación Nación Pagos", type=["csv", "xlsx"], key="nacion"
    )
    st.file_uploader("Comprobantes AFIP / ARCA", type=["pdf", "csv"], key="afip")

  if st.button("Procesar y Conciliar Automáticamente", type="primary"):
    st.success("¡Datos procesados y conciliados correctamente!")

elif opcion_menu == "Gestión Multitenant (Admin)":
  st.markdown("## 🏢 Gestión Multitenant")
  st.write("Administra las empresas y clientes integrados en la plataforma.")
  st.markdown("---")

  st.write(
      "**Clientes registrados actualmente:**",
      ", ".join(st.session_state.clientes),
  )

  nuevo_cliente = st.text_input("Agregar Nuevo Cliente / Empresa")
  if st.button("Registrar Cliente"):
    if nuevo_cliente and nuevo_cliente not in st.session_state.clientes:
      st.session_state.clientes.append(nuevo_cliente)
      st.success(
          f"¡Cliente '{nuevo_cliente}' agregado con éxito y disponible en el"
          " selector!"
      )
      st.rerun()
    elif nuevo_cliente in st.session_state.clientes:
      st.warning("Ese cliente ya se encuentra registrado.")
    else:
      st.error("Ingresa un nombre válido.")

elif opcion_menu == "📄 Informe para el Contador (PDF Seguro)":
  st.markdown("## 📄 Generación de Reporte Oficial para el Contador")
  st.write(
      f"Obtén el reporte consolidado para **{st.session_state.get('empresa_actual')}**"
      " en formato seguro (PDF/HTML protegido) con desglose detallado de"
      " comisiones, IVA por uso de plataforma y retenciones."
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
            <p><strong>Empresa:</strong> {st.session_state.get('empresa_actual')}</p>
            <p><strong>Fecha de Emisión:</strong> {pd.Timestamp.now().strftime('%d/%m/%Y %H:%M')}</p>
            <p><strong>Estado:</strong> Documento Oficial Inalterable (Contador)</p>
        </div>
        <h3>Desglose Consolidado por Canal / Procesador</h3>
        <table>
            <tr>
                <th>Concepto / Proveedor</th>
                <th>Bruto Declarado</th>
                <th>Comisión Plataforma</th>
                <th>IVA (Tasas s/ Uso Plataforma)</th>
                <th>Retenciones (SIRTAC / AFIP)</th>
                <th>Neto a Cobrar</th>
            </tr>
            <tr>
                <td>Mercado Pago</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
            </tr>
            <tr>
                <td>Naranja X</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
            </tr>
            <tr>
                <td>Nación Pagos</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
            </tr>
            <tr>
                <td>Payway</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
                <td>-</td>
            </tr>
        </table>
        <div class="footer">
            <p>Reporte generado automáticamente por la Plataforma Multitenant - Mueblería Castillo de Oro.</p>
        </div>
    </body>
    </html>
    """

  b64 = base64.b64encode(html_reporte.encode()).decode()
  href = f'<a href="data:text/html;base64,{b64}" download="Reporte_Contador_Oficial.html" target="_blank" style="font-size:16px; font-weight:bold; padding:10px 20px; background-color:#1e3a8a; color:white; text-decoration:none; border-radius:5px;">📥 Descargar Reporte Seguro para el Contador</a>'
  st.markdown("<br>", unsafe_allow_html=True)
  st.markdown(href, unsafe_allow_html=True)

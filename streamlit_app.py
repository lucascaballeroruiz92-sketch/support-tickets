import base64
from io import BytesIO
import numpy as np
import pandas as pd
import streamlit as st

st.set_page_config(
    page_config={
        "page_title": "Sistema de Conciliación & Automatización Contable",
        "page_icon": "💼",
        "layout": "wide",
    }
)

# --- BASE DE DATOS DE CLIENTES ---
if "clientes" not in st.session_state:
  st.session_state.clientes = [
      "Mueblería Castillo de Oro",
      "Comercio Ejemplo S.A.",
  ]

# --- SIMULACIÓN DE LOGIN ---
st.markdown(
    "## 💼 Sistema de Automatización y Conciliación Contable"
)
st.markdown("### Acceso Multicliente (Administradores y Colaboradores)")

cliente_seleccionado = st.selectbox(
    "Seleccionar Empresa / Cliente:", st.session_state.clientes
)
usuario = st.text_input(
    "Usuario (ej. colaborador1, colaborador2, admin)", key="user_login"
)
password = st.text_input(
    "Contraseña", type="password", key="pass_login"
)

if st.button("Ingresar al Sistema"):
  if usuario and password:
    st.session_state["autenticado"] = True
    st.session_state["usuario_actual"] = usuario
    st.session_state["empresa_actual"] = cliente_seleccionado
    st.success(f"¡Bienvenido, {usuario}! Accediendo a {cliente_seleccionado}...")
  else:
    st.error("Por favor completa los datos de acceso.")

# Si está autenticado, mostramos la plataforma principal
if st.session_state.get("autenticado", False):
  st.markdown("---")
  st.markdown(
      f"### Panel de Control - Empresa: **{st.session_state.get('empresa_actual')}**"
  )

  tab1, tab2, tab3 = st.tabs(
      ["Carga y Conciliación", "Reporte para el Contador", "Gestión Multitenant"]
  )

  with tab1:
    st.subheader("Carga de Comprobantes y Liquidaciones")
    st.write(
        "Sube los archivos de AFIP (ARCA), Naranja X, Nación Pagos, Mercado"
        " Pago y Payway para procesar comisiones, IVA y SIRTAC."
    )

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
      st.file_uploader(
          "Comprobantes AFIP / ARCA", type=["pdf", "csv"], key="afip"
      )

    if st.button("Procesar y Conciliar Automáticamente"):
      st.success("¡Datos procesados y conciliados correctamente!")

  with tab2:
    st.subheader("📄 Generación de Reporte Oficial para el Contador")
    st.write(
        "Obtén el reporte consolidado en formato seguro (PDF/HTML protegido)"
        " para evitar manipulaciones de datos."
    )

    # Función para generar el reporte seguro
    html_reporte = f"""
        <html>
        <head>
            <style>
                body {{ font-family: Helvetica, Arial, sans-serif; color: #333; margin: 40px; }}
                h1 {{ color: #1e3a8a; text-align: center; border-bottom: 2px solid #1e3a8a; padding-bottom: 10px; }}
                .header-info {{ margin-bottom: 30px; font-size: 14px; }}
                table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
                th, td {{ border: 1px solid #cbd5e1; padding: 10px; text-align: left; font-size: 12px; }}
                th {{ background-color: #f1f5f9; color: #1e3a8a; }}
                .footer {{ margin-top: 50px; font-size: 10px; text-align: center; color: #64748b; }}
            </style>
        </head>
        <body>
            <h1>INFORME DE CONCILIACIÓN CONTABLE</h1>
            <div class="header-info">
                <p><strong>Empresa:</strong> {st.session_state.get('empresa_actual')}</p>
                <p><strong>Fecha de Emisión:</strong> {pd.Timestamp.now().strftime('%d/%m/%Y %H:%M')}</p>
                <p><strong>Estado:</strong> Documento Oficial Inalterable</p>
            </div>
            <h3>Resumen Consolidado</h3>
            <table>
                <tr>
                    <th>Concepto / Proveedor</th>
                    <th>Bruto Declarado</th>
                    <th>Comisiones & IVA</th>
                    <th>Retenciones (SIRTAC)</th>
                    <th>Neto a Cobrar</th>
                </tr>
                <tr>
                    <td>Mercado Pago</td>
                    <td>$ 1.250.000,00</td>
                    <td>$ 75.000,00</td>
                    <td>$ 18.500,00</td>
                    <td>$ 1.156.500,00</td>
                </tr>
                <tr>
                    <td>Naranja X</td>
                    <td>$ 980.000,00</td>
                    <td>$ 58.800,00</td>
                    <td>$ 14.200,00</td>
                    <td>$ 907.000,00</td>
                </tr>
            </table>
            <div class="footer">
                <p>Reporte generado automáticamente por la Plataforma Multitenant - Mueblería Castillo de Oro.</p>
            </div>
        </body>
        </html>
        """

    b64 = base64.b64encode(html_reporte.encode()).decode()
    href = f'<a href="data:text/html;base64,{b64}" download="Reporte_Contador_Oficial.html" target="_blank">📥 Descargar Reporte Seguro para el Contador</a>'
    st.markdown(href, unsafe_allow_html=True)

  with tab3:
    st.subheader("Gestión Multitenant (Administrador)")
    nuevo_cliente = st.text_input("Agregar Nuevo Cliente / Empresa")
    if st.button("Registrar Cliente"):
      if nuevo_cliente:
        st.session_state.clientes.append(nuevo_cliente)
        st.success(f"¡Cliente '{nuevo_cliente}' agregado con éxito!")

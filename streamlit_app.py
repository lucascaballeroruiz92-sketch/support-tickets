import streamlit as st
import pandas as pd
import numpy as np

st.set_page_config(
    page_title="Sistema de Conciliación & Automatización Contable",
    page_icon="💼",
    layout="wide"
)

# --- BASE DE DATOS DE CLIENTES ---
if "clientes" not in st.session_state:
    st.session_state.clientes = [
        "Mueblería Castillo de Oro",
        "Comercio Ejemplo S.A.",
        "➕ Agregar Nuevo Cliente..."
    ]

# --- CONTROL DE AUTENTICACIÓN ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.user = None
    st.session_state.cliente_activo = "Mueblería Castillo de Oro"

USERS = {
    "colaborador1": "oro2026",
    "colaborador2": "oro2026",
    "admin": "admin2026"
}

def login_screen():
    st.title("💼 Sistema de Automatización y Conciliación Contable")
    st.markdown("### Acceso Multicliente (Administradores y Colaboradores)")
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form"):
            cliente_sel = st.selectbox("Seleccionar Empresa / Cliente:", st.session_state.clientes)
            username = st.text_input("Usuario (ej. colaborador1, colaborador2, admin)")
            password = st.text_input("Contraseña", type="password")
            submit = st.form_submit_button("Ingresar al Sistema", use_container_width=True)
            
            if submit:
                if username in USERS and USERS[username] == password:
                    st.session_state.logged_in = True
                    st.session_state.user = username
                    st.session_state.cliente_activo = cliente_sel
                    st.rerun()
                else:
                    st.error("Usuario o contraseña incorrectos.")

if not st.session_state.logged_in:
    login_screen()
else:
    user = st.session_state.user
    
    # Barra lateral de navegación y selección de cliente
    st.sidebar.title(f"👤 Usuario: {user}")
    st.sidebar.markdown("---")
    
    cliente_actual = st.sidebar.selectbox(
        "🏢 Cliente Activo:", 
        st.session_state.clientes, 
        index=st.session_state.clientes.index(st.session_state.cliente_activo) if st.session_state.cliente_activo in st.session_state.clientes else 0
    )
    st.session_state.cliente_activo = cliente_actual
    
    # Encabezado principal personalizado
    st.markdown(f"# 🏢 {st.session_state.cliente_activo}")
    st.caption("Panel de Control de Conciliación de Pagos, Liquidaciones y ARCA")
    st.markdown("---")
    
    menu = st.sidebar.radio("Navegación", [
        "📥 Carga de Archivos y Liquidaciones", 
        "📊 Planilla Maestra & Conciliación", 
        "📑 Reportes para el Contador"
    ])
    
    st.sidebar.markdown("---")
    if st.sidebar.button("Cerrar Sesión", use_container_width=True):
        st.session_state.logged_in = False
        st.session_state.user = None
        st.rerun()

    # --- MÓDULO 1: CARGA DE ARCHIVOS ---
    if menu == "📥 Carga de Archivos y Liquidaciones":
        st.header(f"📥 Módulo de Carga - {st.session_state.cliente_activo}")
        st.info("Tanto colaborador1, colaborador2 como el administrador pueden subir comprobantes en cualquier momento.")
        
        tab1, tab2, tab3, tab4 = st.tabs(["ARCA (Facturación)", "Naranja X", "Nación Pagos", "Mercado Pago"])
        
        with tab1:
            st.subheader("Comprobantes Emitidos - ARCA (AFIP)")
            st.file_uploader("Subir reporte ARCA (Excel / CSV)", type=["xlsx", "csv"], key="arca_up")
            
        with tab2:
            st.subheader("Liquidaciones Naranja X")
            st.file_uploader("Subir liquidación Naranja X", type=["pdf", "xlsx", "csv"], key="naranja_up")
            
        with tab3:
            st.subheader("Liquidaciones Nación Pagos")
            st.file_uploader("Subir liquidación Nación Pagos", type=["pdf", "xlsx", "csv"], key="nacion_up")
            
        with tab4:
            st.subheader("Movimientos Mercado Pago")
            st.file_uploader("Subir reporte Mercado Pago", type=["xlsx", "csv", "pdf"], key="mp_up")
            
        st.markdown("---")
        if st.button("🚀 Procesar Archivos y Actualizar Planilla Maestra", use_container_width=True):
            st.success(f"¡Archivos procesados con éxito para {st.session_state.cliente_activo}!")

    # --- MÓDULO 2: PLANILLA MAESTRA & CONCILIACIÓN ---
    elif menu == "📊 Planilla Maestra & Conciliación":
        st.header(f"📊 Planilla Maestra Consolidada - {st.session_state.cliente_activo}")
        st.write("Vista unificada de facturas ARCA vs. cupones de cobro y deducciones.")
        
        df_demo = pd.DataFrame({
            "Fecha": ["01/09/2026", "01/09/2026", "02/09/2026"],
            "Factura ARCA": ["B-0006-2936", "B-0005-2533", "B-0006-2938"],
            "Monto Total Factura": [1390000.0, 329000.0, 700000.0],
            "Plataforma": ["NARANJA X", "MERCADO PAGO", "NACIÓN PAGOS"],
            "Monto Cupón": [1390000.0, 329000.0, 700000.0],
            "Aranceles / Cargos": [25020.0, 24373.0, 14140.0],
            "IVA Comisiones": [5254.20, 5118.33, 2969.40],
            "Retención SIRTAC": [27800.0, 6580.0, 14000.0],
            "Neto Acreditar": [1357025.80, 292928.67, 668890.60],
            "Estado": ["Conciliado", "Conciliado", "Conciliado"]
        })
        st.dataframe(df_demo, use_container_width=True)

    # --- MÓDULO 3: REPORTES CONTADOR ---
    elif menu == "📑 Reportes para el Contador":
        st.header(f"📑 Reporte Impositivo - {st.session_state.cliente_activo}")
        st.write("Resumen mensual de retenciones (SIRTAC, IVA, Comisiones) listo para el estudio contable.")
        
        df_cont = pd.DataFrame({
            "Plataforma": ["NARANJA X", "NACIÓN PAGOS", "MERCADO PAGO"],
            "Total Presentado": [183713900.42, 168261229.99, 28264899.48],
            "Total Aranceles": [16534251.04, 15143510.69, 1413244.97],
            "IVA Comisiones": [3472192.72, 3180137.24, 296781.44],
            "Retención SIRTAC": [1837139.00, 1682612.30, 282648.99],
            "Neto Acreditado": [135908930.58, 124685735.63, 2795032.00]
        })
        st.dataframe(df_cont, use_container_width=True)
        st.download_button("📥 Descargar Reporte en Excel para el Contador", data="mock_bytes", file_name=f"Reporte_Contador_{st.session_state.cliente_activo.replace(' ', '_')}.xlsx", use_container_width=True)

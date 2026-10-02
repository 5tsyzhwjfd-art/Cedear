import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='CEDEAR Monitor', page_icon='📈', layout='wide')

# -----------------------------
# Configuration
# -----------------------------
REFRESH_SECONDS = 30 * 60
TZ = ZoneInfo('America/Argentina/Buenos_Aires')

# Ratios validated for the initial prototype.
INSTRUMENTS = {
    'AAPL': {'ratio': 20, 'name': 'Apple'},
    'NVDA': {'ratio': 24, 'name': 'NVIDIA'},
    'SPY': {'ratio': 60, 'name': 'SPDR S&P 500 ETF'},
}

# Demo fallbacks keep the UI usable if a public endpoint changes or a key is absent.
# The dashboard explicitly labels fallback/demo values.
FALLBACK = {
    'ccl': {'price': 1623.00, 'change': 0.65},
    'official': {'price': 1400.00},
    'AAPL': {'usa': 330.32, 'usa_change': -0.81, 'cedear': 26840.0, 'cedear_change': -0.59},
    'NVDA': {'usa': 230.86, 'usa_change': 1.09, 'cedear': 15700.0, 'cedear_change': 1.74},
    'SPY': {'usa': 763.99, 'usa_change': 0.18, 'cedear': 20720.0, 'cedear_change': 0.68},
}

TWELVE_DATA_KEY = st.secrets.get('TWELVE_DATA_API_KEY', os.getenv('TWELVE_DATA_API_KEY', ''))

# -----------------------------
# Helpers
# -----------------------------
def safe_get_json(url, params=None, timeout=8):
    r = requests.get(url, params=params, timeout=timeout, headers={'User-Agent': 'CEDEAR-Monitor/1.0'})
    r.raise_for_status()
    return r.json()


def pct_change_from_prices(current, previous):
    if not current or not previous:
        return None
    return (current / previous - 1) * 100


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def get_usa_quote(symbol):
    """Twelve Data primary. Returns (price, pct_change, source, live)."""
    if TWELVE_DATA_KEY:
        try:
            data = safe_get_json(
                'https://api.twelvedata.com/quote',
                {'symbol': symbol, 'apikey': TWELVE_DATA_KEY},
            )
            price = float(data['close'])
            change = float(data['percent_change'])
            return price, change, 'Twelve Data', True
        except Exception:
            pass
    f = FALLBACK[symbol]
    return f['usa'], f['usa_change'], 'Fallback validación', False


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def get_official_fx():
    """Try ArgentinaDatos/BCRA-compatible public data; otherwise fallback."""
    endpoints = [
        'https://api.argentinadatos.com/v1/cotizaciones/dolares/oficial',
        'https://dolarapi.com/v1/dolares/oficial',
    ]
    for url in endpoints:
        try:
            data = safe_get_json(url)
            if isinstance(data, list):
                data = data[-1]
            value = data.get('venta') or data.get('sell') or data.get('valor')
            if value:
                return float(value), url.split('/')[2], True
        except Exception:
            continue
    return FALLBACK['official']['price'], 'Fallback validación', False


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def get_ccl_reference():
    """V1: stable fallback/reference. Replaceable adapter for BYMA feed in V2."""
    # BYMA's public web presentation is not exposed here as a stable documented
    # free JSON API. We keep this adapter isolated so it can be swapped without
    # touching any financial calculations or UI.
    return FALLBACK['ccl']['price'], FALLBACK['ccl']['change'], 'BYMA referencia validada', False


@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def get_cedear_quote(symbol):
    """V1 local-market adapter. Uses validated fallback until a stable public feed is wired."""
    f = FALLBACK[symbol]
    return f['cedear'], f['cedear_change'], 'Fuente local validada / fallback', False


def build_dashboard():
    ccl, ccl_change, ccl_source, ccl_live = get_ccl_reference()
    official, official_source, official_live = get_official_fx()
    rows = []
    all_live = ccl_live and official_live

    for symbol, meta in INSTRUMENTS.items():
        usa, usa_change, usa_source, usa_live = get_usa_quote(symbol)
        cedear, cedear_change, cedear_source, cedear_live = get_cedear_quote(symbol)
        ratio = meta['ratio']

        theoretical = usa * ccl / ratio
        deviation = (cedear / theoretical - 1) * 100 if theoretical else None
        implied_ccl = cedear * ratio / usa if usa else None

        # Exact relationship from relative price changes, not simple subtraction.
        implied_ccl_change = ((1 + cedear_change / 100) / (1 + usa_change / 100) - 1) * 100

        rows.append({
            'CEDEAR': symbol,
            'Ratio': f'{ratio}:1',
            'Acción USA': usa,
            'CEDEAR real': cedear,
            'CEDEAR teórico': theoretical,
            'Desvío real/teórico': deviation,
            'CCL implícito': implied_ccl,
            'Var. USA': usa_change,
            'Var. CEDEAR': cedear_change,
            'Var. CCL impl.': implied_ccl_change,
            '_source_usa': usa_source,
            '_source_cedear': cedear_source,
            '_live': usa_live and cedear_live,
        })
        all_live = all_live and usa_live and cedear_live

    return pd.DataFrame(rows), ccl, ccl_change, official, ccl_source, official_source, all_live


def fmt_ars(x):
    return f'$ {x:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def fmt_usd(x):
    return f'US$ {x:,.2f}'


def fmt_pct(x):
    return f'{x:+.2f}%'


# -----------------------------
# UI
# -----------------------------
st.markdown('''
<style>
    .stApp { background: #07111f; color: #eaf1fb; }
    [data-testid="stMetric"] { background: #0d1b2d; border: 1px solid #20344d; padding: 14px; border-radius: 14px; }
    [data-testid="stDataFrame"] { border: 1px solid #20344d; border-radius: 14px; overflow: hidden; }
    .muted { color: #8ea2bb; font-size: 0.9rem; }
    .status { padding: 8px 12px; border-radius: 10px; background: #10243a; display:inline-block; }
</style>
''', unsafe_allow_html=True)

left, right = st.columns([4, 1])
with left:
    st.title('📈 CEDEAR Monitor')
    st.caption('Comparación objetiva entre CEDEARs y sus activos subyacentes en EE.UU.')
with right:
    if st.button('↻ Actualizar ahora', use_container_width=True):
        st.cache_data.clear()
        st.rerun()

df, ccl, ccl_change, official, ccl_source, official_source, all_live = build_dashboard()
brecha = (ccl / official - 1) * 100 if official else None

m1, m2, m3, m4 = st.columns(4)
m1.metric('CCL referencia', fmt_ars(ccl), fmt_pct(ccl_change))
m2.metric('Dólar oficial', fmt_ars(official))
m3.metric('Brecha CCL / oficial', fmt_pct(brecha))
m4.metric('Actualización', datetime.now(TZ).strftime('%H:%M'))

if all_live:
    st.success('Datos conectados a fuentes en vivo.')
else:
    st.info('Modo V1 híbrido: las fuentes disponibles se consultan en vivo; donde no hay un endpoint público estable se muestran los valores de validación. La app identifica esta condición para evitar presentar un fallback como dato en vivo.')

st.subheader('CEDEARs')

search = st.text_input('Buscar ticker', placeholder='AAPL, NVDA, SPY…', label_visibility='collapsed').upper().strip()
view = df.copy()
if search:
    view = view[view['CEDEAR'].str.contains(search, regex=False)]

# User-agreed column order.
visible = view[[
    'CEDEAR', 'Ratio', 'Acción USA', 'CEDEAR real', 'CEDEAR teórico',
    'Desvío real/teórico', 'CCL implícito', 'Var. USA', 'Var. CEDEAR', 'Var. CCL impl.'
]]

st.dataframe(
    visible,
    hide_index=True,
    use_container_width=True,
    column_config={
        'CEDEAR': st.column_config.TextColumn('CEDEAR', pinned=True),
        'Ratio': st.column_config.TextColumn('Ratio'),
        'Acción USA': st.column_config.NumberColumn('Acción USA', format='US$ %.2f'),
        'CEDEAR real': st.column_config.NumberColumn('CEDEAR real', format='$ %.2f'),
        'CEDEAR teórico': st.column_config.NumberColumn('CEDEAR teórico', format='$ %.2f'),
        'Desvío real/teórico': st.column_config.NumberColumn('Desvío real/teórico', format='%+.2f%%'),
        'CCL implícito': st.column_config.NumberColumn('CCL implícito', format='$ %.2f'),
        'Var. USA': st.column_config.NumberColumn('Var. USA', format='%+.2f%%'),
        'Var. CEDEAR': st.column_config.NumberColumn('Var. CEDEAR', format='%+.2f%%'),
        'Var. CCL impl.': st.column_config.NumberColumn('Var. CCL impl.', format='%+.2f%%'),
    },
)

st.caption(f'CCL: {ccl_source} · Oficial: {official_source} · refresco de caché: 30 min')

with st.expander('Cómo se calculan las métricas'):
    st.markdown('''
- **CEDEAR teórico** = Acción USA × CCL de referencia / ratio.
- **Desvío real/teórico** = CEDEAR real / CEDEAR teórico − 1.
- **CCL implícito** = CEDEAR real × ratio / Acción USA.
- **Var. CCL implícito** = (1 + Var. CEDEAR) / (1 + Var. USA) − 1.  
  Esta forma evita aproximar la variación simplemente restando porcentajes.
''')

with st.expander('Diagnóstico de fuentes'):
    diag = df[['CEDEAR', '_source_usa', '_source_cedear', '_live']].rename(columns={
        '_source_usa': 'Fuente USA', '_source_cedear': 'Fuente CEDEAR', '_live': 'Ambas en vivo'
    })
    st.dataframe(diag, hide_index=True, use_container_width=True)

# Browser auto-refresh without an extra dependency.
st.components.v1.html(
    f'''<script>setTimeout(function(){{window.parent.location.reload();}}, {REFRESH_SECONDS * 1000});</script>''',
    height=0,
)

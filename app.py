import os
import time
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from html import escape

st.set_page_config(page_title='CEDEAR Monitor', page_icon='📈', layout='wide')

# -----------------------------
# Configuration
# -----------------------------
REFRESH_SECONDS = 30 * 60
# Market requests are intentionally not cached: each app rerun performs a fresh query.
TZ = ZoneInfo('America/Argentina/Buenos_Aires')

# Ratios validated for the initial prototype.
INSTRUMENTS = {
    'AAPL': {'ratio': 20, 'name': 'Apple'}, 'NVDA': {'ratio': 24, 'name': 'NVIDIA'},
    'SPY': {'ratio': 60, 'name': 'SPDR S&P 500 ETF'}, 'AMZN': {'ratio': 143, 'name': 'Amazon'},
    'TSLA': {'ratio': 15, 'name': 'Tesla'}, 'MSFT': {'ratio': 30, 'name': 'Microsoft'},
    'GOOGL': {'ratio': 58, 'name': 'Alphabet'}, 'META': {'ratio': 24, 'name': 'Meta'},
    'AMD': {'ratio': 10, 'name': 'AMD'}, 'MELI': {'ratio': 120, 'name': 'MercadoLibre'},
    'KO': {'ratio': 5, 'name': 'Coca-Cola'}, 'QQQ': {'ratio': 20, 'name': 'Invesco QQQ'},
    'BRKB': {'ratio': 22, 'name': 'Berkshire Hathaway'}, 'JPM': {'ratio': 15, 'name': 'JPMorgan'},
    'V': {'ratio': 18, 'name': 'Visa'}, 'WMT': {'ratio': 18, 'name': 'Walmart'},
    'XOM': {'ratio': 10, 'name': 'Exxon Mobil'}, 'DIS': {'ratio': 12, 'name': 'Disney'},
    'NFLX': {'ratio': 48, 'name': 'Netflix'}, 'BABA': {'ratio': 9, 'name': 'Alibaba'},
    'INTC': {'ratio': 5, 'name': 'Intel'}, 'PFE': {'ratio': 4, 'name': 'Pfizer'},
    'BA': {'ratio': 24, 'name': 'Boeing'}, 'NKE': {'ratio': 12, 'name': 'Nike'},
    'PYPL': {'ratio': 8, 'name': 'PayPal'}, 'AVGO': {'ratio': 39, 'name': 'Broadcom'},
}

# Seed values only exist for the original validation trio. Other symbols are
# omitted gracefully until a live/local source returns them.

# No fixed market-price fallbacks: stale demo prices can be mistaken for live data.
# Last valid observations are retained instead.
FALLBACK = {
    # Solo se usa si nunca hubo una lectura CCL válida. No participa de la validación normal.
    'ccl': {'price': 1623.00, 'change': None},
    'official': {'price': 1400.00},
}

LAST_GOOD_FILE = Path('/tmp/cedear_monitor_last_good.json')

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


def get_usa_market_snapshot():
    """Secondary USA feed from Data912. One request covers the supported universe."""
    try:
        data = safe_get_json('https://data912.com/live/usa_stocks')
        items = data if isinstance(data, list) else data.get('data', data.get('results', []))
        return {str(x.get('symbol', '')).upper(): x for x in items}
    except Exception:
        return {}


def get_usa_quote(symbol, usa_snapshot=None):
    """USA quote with automatic redundancy: Twelve Data -> Yahoo -> Data912."""
    if TWELVE_DATA_KEY:
        try:
            data = safe_get_json('https://api.twelvedata.com/quote', {'symbol': symbol, 'apikey': TWELVE_DATA_KEY})
            price = float(data['close'])
            change = float(data['percent_change'])
            return price, change, 'Twelve Data', True
        except Exception:
            pass

    yahoo_symbol = {'BRKB': 'BRK-B'}.get(symbol, symbol)
    try:
        data = safe_get_json(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}',
            {'range': '5d', 'interval': '1d', 'includePrePost': 'false'},
        )
        result = data['chart']['result'][0]
        meta = result.get('meta', {})
        price = meta.get('regularMarketPrice')
        previous = meta.get('chartPreviousClose') or meta.get('previousClose')
        closes = result.get('indicators', {}).get('quote', [{}])[0].get('close', [])
        valid_closes = [float(x) for x in closes if x is not None]
        if previous is None and len(valid_closes) >= 2:
            previous = valid_closes[-2]
        if price is None and valid_closes:
            price = valid_closes[-1]
        if price is not None:
            price = float(price)
            change = pct_change_from_prices(price, float(previous)) if previous else None
            return price, change, 'Yahoo Finance', True
    except Exception:
        pass

    item = (usa_snapshot or {}).get(symbol, {})
    try:
        price = float(item.get('c')) if item.get('c') is not None else None
        if price is not None:
            change = float(item.get('pct_change', 0) or 0)
            return price, change, 'Data912 USA', True
    except (TypeError, ValueError):
        pass
    return None, None, 'Sin dato USA', False


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


def _mid_from_quote(data):
    """Valor representativo de una cotización compra/venta."""
    buy = data.get('compra')
    sell = data.get('venta')
    try:
        buy = float(buy) if buy is not None else None
        sell = float(sell) if sell is not None else None
    except (TypeError, ValueError):
        return None
    if buy and sell:
        return (buy + sell) / 2
    return sell or buy


def get_ccl_reference(store, now_iso):
    """
    CCL validado con 3 fuentes: DólarHoy (DolarAPI), Ámbito y Data912.
    Se toma la mediana de fuentes comparables y se descartan outliers >1,5%.
    Si no hay al menos 2 fuentes válidas, conserva el último CCL validado.
    """
    observations = []

    # 1) DolarAPI: su endpoint CCL declara como fuente a DólarHoy.
    try:
        data = safe_get_json('https://dolarapi.com/v1/dolares/contadoconliqui')
        value = _mid_from_quote(data)
        if value:
            observations.append({'source': 'DólarHoy / DolarAPI', 'value': value, 'change': None})
    except Exception:
        pass

    # 2) DolarAPI Ámbito: endpoint separado cuya fuente declarada es Ámbito Financiero.
    try:
        data = safe_get_json('https://dolarapi.com/v1/ambito/dolares/contadoconliqui')
        value = _mid_from_quote(data)
        change = data.get('variacion')
        change = float(change) if change is not None else None
        if value:
            observations.append({'source': 'Ámbito', 'value': value, 'change': change})
    except Exception:
        pass

    # 3) Data912: mediana del CCL_mark de los instrumentos de mayor volumen ARS.
    # Usar varios pares evita que un CEDEAR/ADR puntual distorsione la referencia.
    try:
        data = safe_get_json('https://data912.com/live/ccl')
        items = data if isinstance(data, list) else data.get('data', data.get('results', []))
        valid = []
        for x in items:
            try:
                mark = float(x.get('CCL_mark'))
                volume = float(x.get('ars_volume', 0) or 0)
                if mark > 0:
                    valid.append((volume, mark))
            except (TypeError, ValueError):
                continue
        if valid:
            top_marks = [m for _, m in sorted(valid, reverse=True)[:10]]
            value = float(pd.Series(top_marks).median())
            observations.append({'source': 'Data912 CCL', 'value': value, 'change': None})
    except Exception:
        pass

    if observations:
        raw_median = float(pd.Series([x['value'] for x in observations]).median())
        accepted = [x for x in observations if abs(x['value'] / raw_median - 1) <= 0.015]
    else:
        accepted = []

    # Exigimos consenso de al menos dos fuentes para reemplazar el último valor válido.
    if len(accepted) >= 2:
        ccl = float(pd.Series([x['value'] for x in accepted]).median())
        changes = [x['change'] for x in accepted if x['change'] is not None]
        old = store.get('fx', {}).get('ccl', {})
        if changes:
            ccl_change = float(pd.Series(changes).median())
        elif old.get('price'):
            ccl_change = pct_change_from_prices(ccl, float(old['price']))
        else:
            ccl_change = None
        store.setdefault('fx', {})['ccl'] = {
            'price': ccl, 'change': ccl_change, 'timestamp': now_iso,
            'sources': observations, 'accepted': [x['source'] for x in accepted],
        }
        source = 'Mediana validada: ' + ' + '.join(x['source'] for x in accepted)
        return ccl, ccl_change, source, True, observations, now_iso

    # Fallo/discordancia: no adelantar fecha ni sustituir por el viejo 1623 fijo.
    old = store.get('fx', {}).get('ccl', {})
    if old.get('price') is not None:
        return (float(old['price']), old.get('change'), 'Último CCL validado', False,
                observations, old.get('timestamp'))

    return (FALLBACK['ccl']['price'], FALLBACK['ccl']['change'],
            'Fallback inicial (sin consenso CCL)', False, observations, None)


def get_local_market_snapshot():
    try:
        data = safe_get_json('https://data912.com/live/arg_cedears')
        items = data if isinstance(data, list) else data.get('data', data.get('results', []))
        return {str(x.get('symbol','')).upper(): x for x in items}
    except Exception:
        return {}


def get_cedear_yahoo_quote(symbol):
    """Independent local backup via Yahoo's Buenos Aires (.BA) listing."""
    try:
        data = safe_get_json(
            f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}.BA',
            {'range': '5d', 'interval': '1d', 'includePrePost': 'false'},
        )
        result = data['chart']['result'][0]
        meta = result.get('meta', {})
        price = meta.get('regularMarketPrice')
        previous = meta.get('chartPreviousClose') or meta.get('previousClose')
        closes = result.get('indicators', {}).get('quote', [{}])[0].get('close', [])
        valid = [float(x) for x in closes if x is not None]
        if price is None and valid:
            price = valid[-1]
        if previous is None and len(valid) >= 2:
            previous = valid[-2]
        if price is not None:
            price = float(price)
            change = pct_change_from_prices(price, float(previous)) if previous else None
            return price, change
    except Exception:
        pass
    return None, None


def get_cedear_historical_last(symbol):
    """Secondary local source: last available historical CEDEAR close from Data912."""
    try:
        data = safe_get_json(f'https://data912.com/historical/cedears/{symbol}')
        items = data if isinstance(data, list) else data.get('data', data.get('results', []))
        if not items:
            return None, None
        item = items[-1]
        price = item.get('c') or item.get('close')
        if price is None:
            return None, None
        change = item.get('pct_change')
        return float(price), float(change) if change is not None else None
    except Exception:
        return None, None


def load_last_good():
    if 'last_good_market' in st.session_state:
        return st.session_state.last_good_market
    try:
        data = json.loads(LAST_GOOD_FILE.read_text())
    except Exception:
        data = {'usa': {}, 'cedear': {}}
    st.session_state.last_good_market = data
    return data


def save_last_good(data):
    st.session_state.last_good_market = data
    try:
        LAST_GOOD_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def remember_quote(store, market, symbol, price, change, source, now_iso):
    if price is not None:
        store.setdefault(market, {})[symbol] = {
            'price': price, 'change': change, 'source': source, 'timestamp': now_iso
        }


def recover_quote(store, market, symbol):
    item = store.get(market, {}).get(symbol)
    if not item:
        return None, None, None, None
    return item.get('price'), item.get('change'), item.get('source'), item.get('timestamp')


def build_dashboard():
    store = load_last_good()
    now = datetime.now(TZ)
    now_iso = now.isoformat()
    ccl, ccl_change, ccl_source, ccl_live, ccl_observations, ccl_ts = get_ccl_reference(store, now_iso)
    official, official_source, official_live = get_official_fx()
    rows = []
    market = get_local_market_snapshot()
    usa_snapshot = get_usa_market_snapshot()
    displayed_timestamps = []
    using_stale = not ccl_live
    if ccl_ts:
        try:
            displayed_timestamps.append(datetime.fromisoformat(ccl_ts))
        except Exception:
            pass

    ranked = sorted(
        INSTRUMENTS.items(),
        key=lambda kv: float(market.get(kv[0], {}).get('c', 0) or 0) * float(market.get(kv[0], {}).get('v', 0) or 0),
        reverse=True,
    )
    selected = ranked[:20] if market else list(INSTRUMENTS.items())[:20]

    for symbol, meta in selected:
        usa, usa_change, usa_source, usa_live = get_usa_quote(symbol, usa_snapshot)
        usa_ts = None
        if usa is not None:
            remember_quote(store, 'usa', symbol, usa, usa_change, usa_source, now_iso)
            usa_ts = now_iso
        else:
            usa, usa_change, old_source, usa_ts = recover_quote(store, 'usa', symbol)
            if usa is not None:
                usa_source = f'Último válido · {old_source}'
                using_stale = True

        md = market.get(symbol, {})
        try:
            cedear = float(md.get('c')) if md.get('c') is not None else None
            cedear_change = float(md.get('pct_change', 0) or 0) if cedear is not None else None
        except (TypeError, ValueError):
            cedear, cedear_change = None, None
        cedear_source = 'Data912 live' if cedear is not None else 'Sin dato local'
        cedear_ts = None
        current_cedear_observation = cedear is not None
        if cedear is None:
            yahoo_local, yahoo_local_change = get_cedear_yahoo_quote(symbol)
            if yahoo_local is not None:
                cedear, cedear_change = yahoo_local, yahoo_local_change
                cedear_source = 'Yahoo Finance .BA'
                current_cedear_observation = True
        if cedear is None:
            hist_price, hist_change = get_cedear_historical_last(symbol)
            if hist_price is not None:
                cedear, cedear_change = hist_price, hist_change
                cedear_source = 'Data912 histórico'
                current_cedear_observation = False
        if cedear is not None and current_cedear_observation:
            remember_quote(store, 'cedear', symbol, cedear, cedear_change, cedear_source, now_iso)
            cedear_ts = now_iso
        elif cedear is not None:
            _, _, _, cedear_ts = recover_quote(store, 'cedear', symbol)
            using_stale = True
        else:
            cedear, cedear_change, old_source, cedear_ts = recover_quote(store, 'cedear', symbol)
            if cedear is not None:
                cedear_source = f'Último válido · {old_source}'
                using_stale = True

        for ts in (usa_ts, cedear_ts):
            if ts:
                try:
                    displayed_timestamps.append(datetime.fromisoformat(ts))
                except Exception:
                    pass

        ratio = meta['ratio']
        theoretical = usa * ccl / ratio if usa is not None else None
        deviation = (cedear / theoretical - 1) * 100 if theoretical and cedear is not None else None
        implied_ccl = cedear * ratio / usa if usa and cedear is not None else None
        implied_ccl_change = (((1 + cedear_change / 100) / (1 + usa_change / 100) - 1) * 100
                              if cedear_change is not None and usa_change is not None else None)
        volume = float(md.get('v', 0) or 0)
        traded_amount = float(md.get('c', 0) or 0) * volume

        rows.append({
            'CEDEAR': symbol, 'Ratio': f'{ratio}:1', 'Acción USA': usa, 'CEDEAR real': cedear,
            'CEDEAR teórico': theoretical, 'Desvío real/teórico': deviation, 'CCL implícito': implied_ccl,
            'Var. USA': usa_change, 'Var. CEDEAR': cedear_change, 'Var. CCL impl.': implied_ccl_change,
            'Volumen': volume, 'Monto operado': traded_amount, '_source_usa': usa_source,
            '_source_cedear': cedear_source, '_live': usa_live and cedear_source == 'Data912 live',
        })

    save_last_good(store)
    # Conservative global timestamp: oldest observation currently used in the table.
    # It never advances merely because Streamlit reran; only a successful market observation changes it.
    data_timestamp = min(displayed_timestamps) if displayed_timestamps else None
    return pd.DataFrame(rows), ccl, ccl_change, official, ccl_source, official_source, using_stale, data_timestamp, ccl_observations


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
    .block-container {max-width: 1180px; padding-top: 1.35rem; padding-bottom: 2rem;}
    .hero-title {font-size: 2rem; font-weight: 800; letter-spacing: -0.04em; color:#f4f7fb; margin-bottom:.05rem;}
    h3 {font-size:1.05rem !important; margin-top:.75rem !important;}
    div[data-testid="stTextInput"] input {background:#0b1727; border:1px solid #20344d; border-radius:10px;}
    .fx-strip {
        display: grid; grid-template-columns: repeat(3, 1fr); gap: 0;
        margin: 0.7rem 0 0.15rem 0; padding: 0.55rem 0;
        border-top: 1px solid #16263a; border-bottom: 1px solid #16263a;
    }
    .fx-item { padding: 0 0.8rem; border-right: 1px solid #20344d; min-width: 0; }
    .fx-item:first-child { padding-left: 0; }
    .fx-item:last-child { border-right: 0; }
    .fx-label { color: #9aabc0; font-size: 0.78rem; margin-bottom: 0.15rem; white-space: nowrap; }
    .fx-line { display: flex; align-items: baseline; gap: 0.35rem; white-space: nowrap; }
    .fx-value { color: #f3f7fd; font-size: 1.12rem; font-weight: 700; }
    .fx-up { color: #22c96b; font-size: 0.78rem; font-weight: 600; }
    .fx-down { color: #ff5a55; font-size: 0.78rem; font-weight: 600; }
    .updated { color: #8ea2bb; font-size: 0.72rem; margin: 0.15rem 0 0.65rem 0; }
    @media (max-width: 640px) {
        .fx-item { padding: 0 0.42rem; }
        .fx-label { font-size: 0.66rem; }
        .fx-value { font-size: 0.91rem; }
        .fx-up, .fx-down { font-size: 0.63rem; }
        .updated { font-size: 0.65rem; }
    }
    .table-shell {border:1px solid #20344d; border-radius:14px; overflow:auto; background:#081522; box-shadow:0 10px 28px rgba(0,0,0,.18);}
    .cedear-table {width:100%; min-width:1040px; border-collapse:separate; border-spacing:0; font-size:.82rem;}
    .cedear-table th {position:sticky; top:0; z-index:2; background:#0d2237; color:#c9d6e6; font-size:.72rem; font-weight:700; text-transform:none; padding:11px 10px; border-bottom:1px solid #29425f; text-align:right; white-space:nowrap;}
    .cedear-table th:first-child {left:0; z-index:3; text-align:left;}
    .cedear-table td {padding:10px; border-bottom:1px solid #15283b; text-align:right; color:#e8eef7; white-space:nowrap; background:#081522;}
    .cedear-table tr:last-child td {border-bottom:0;}
    .cedear-table tbody tr:hover td {background:#0b1c2c;}
    .cedear-table td:first-child {position:sticky; left:0; z-index:1; text-align:left; font-weight:800; color:#f5f8fc; background:#081522;}
    .ticker-cell {display:flex; align-items:center; gap:8px;}
    .ticker-dot {width:7px; height:7px; border-radius:50%; background:#2d8cff; box-shadow:0 0 10px rgba(45,140,255,.7); flex:0 0 auto;}
    .num-muted {color:#8da0b7;}
    .pill {display:inline-block; min-width:68px; padding:5px 8px; border-radius:7px; font-weight:800; text-align:center;}
    .pill-pos {color:#ffb0ad; background:linear-gradient(180deg,#632326,#45191d); border:1px solid #743036;}
    .pill-neg {color:#71e6a5; background:linear-gradient(180deg,#0b5738,#073b28); border:1px solid #126746;}
    .pct-pos {color:#45d986; font-weight:700;} .pct-neg {color:#ff6b65; font-weight:700;}
    @media (max-width:640px){.cedear-table{font-size:.76rem;min-width:980px}.cedear-table th{font-size:.66rem;padding:9px 8px}.cedear-table td{padding:9px 8px}.table-shell{border-radius:12px}}
    @media (max-width:640px){.block-container{padding-left:.7rem;padding-right:.7rem;padding-top:.8rem}.hero-title{font-size:1.55rem}}
    .muted { color: #8ea2bb; font-size: 0.9rem; }
    .status { padding: 8px 12px; border-radius: 10px; background: #10243a; display:inline-block; }
</style>
''', unsafe_allow_html=True)

left, right = st.columns([4, 1])
with left:
    st.markdown('<div class="hero-title">CEDEAR Monitor</div>', unsafe_allow_html=True)
    st.caption('Comparación objetiva entre CEDEARs y sus activos subyacentes en EE.UU.')
with right:
    if st.button('↻ Actualizar ahora', use_container_width=True):
        st.rerun()

df, ccl, ccl_change, official, ccl_source, official_source, using_stale, data_timestamp, ccl_observations = build_dashboard()
brecha = (ccl / official - 1) * 100 if official else None

# Compact FX header: three indicators on one row, timestamp below.
def delta_html(value):
    if value is None:
        return ''
    css = 'fx-up' if value >= 0 else 'fx-down'
    arrow = '↑' if value >= 0 else '↓'
    return f'<span class="{css}">{arrow} {fmt_pct(value)}</span>'

updated_at = data_timestamp
st.markdown(
    f'''
    <div class="fx-strip">
      <div class="fx-item">
        <div class="fx-label">CCL referencia</div>
        <div class="fx-line"><span class="fx-value">{fmt_ars(ccl)}</span>{delta_html(ccl_change)}</div>
      </div>
      <div class="fx-item">
        <div class="fx-label">Dólar oficial</div>
        <div class="fx-line"><span class="fx-value">{fmt_ars(official)}</span></div>
      </div>
      <div class="fx-item">
        <div class="fx-label">Brecha</div>
        <div class="fx-line"><span class="fx-value">{fmt_pct(brecha)}</span></div>
      </div>
    </div>
    <div class="updated">{('Último dato válido' if using_stale else 'Actualizado')}: {(updated_at.strftime('%d/%m/%Y · %H:%M') if updated_at else 'sin datos válidos')}</div>
    ''',
    unsafe_allow_html=True,
)

if using_stale:
    st.warning('Una o más consultas actuales fallaron. Se conserva el último dato válido y la hora no se adelanta artificialmente.')
else:
    st.success('Datos de mercado obtenidos correctamente. No se utilizan precios fijos de demostración.')

st.subheader('Top 20 CEDEARs por liquidez')

search = st.text_input('Buscar ticker', placeholder='AAPL, NVDA, SPY…', label_visibility='collapsed').upper().strip()
view = df.copy()
if search:
    view = view[view['CEDEAR'].str.contains(search, regex=False)]

# User-agreed column order, rendered as a compact financial table matching the approved mockup.
def cell_money(x, usd=False):
    if pd.isna(x):
        return '<span class="num-muted">—</span>'
    if usd:
        return f'US$ {x:,.2f}'
    return f'$ {x:,.0f}'.replace(',', '.')

def cell_pct(x, pill=False):
    if pd.isna(x):
        return '<span class="num-muted">—</span>'
    cls = ('pill pill-pos' if x >= 0 else 'pill pill-neg') if pill else ('pct-pos' if x >= 0 else 'pct-neg')
    txt = f'{x:+.2f}%'.replace('.', ',')
    return f'<span class="{cls}">{txt}</span>'

def cell_number(x):
    if pd.isna(x):
        return '<span class="num-muted">—</span>'
    return f'{x:,.0f}'.replace(',', '.')

headers = ['CEDEAR','Ratio','Acción USA','CEDEAR real','Teórico','Desvío','CCL implícito','Var. USA','Var. CEDEAR','Volumen']
rows_html = []
for _, r in view.iterrows():
    rows_html.append(
        '<tr>'
        f'<td><div class="ticker-cell"><span class="ticker-dot"></span>{escape(str(r["CEDEAR"]))}</div></td>'
        f'<td>{escape(str(r["Ratio"]))}</td>'
        f'<td>{cell_money(r["Acción USA"], usd=True)}</td>'
        f'<td>{cell_money(r["CEDEAR real"])}</td>'
        f'<td>{cell_money(r["CEDEAR teórico"])}</td>'
        f'<td>{cell_pct(r["Desvío real/teórico"], pill=True)}</td>'
        f'<td>{cell_money(r["CCL implícito"])}</td>'
        f'<td>{cell_pct(r["Var. USA"])}</td>'
        f'<td>{cell_pct(r["Var. CEDEAR"])}</td>'
        f'<td>{cell_number(r["Volumen"])}</td>'
        '</tr>'
    )
head_html = ''.join(f'<th>{h}</th>' for h in headers)
st.markdown(
    '<div class="table-shell"><table class="cedear-table"><thead><tr>' + head_html +
    '</tr></thead><tbody>' + ''.join(rows_html) + '</tbody></table></div>',
    unsafe_allow_html=True,
)

st.caption(f'CCL: {ccl_source} · Oficial: {official_source} · actualización automática: 30 min · ranking gratuito: liquidez intradiaria')

with st.expander('Cómo se calculan las métricas'):
    st.markdown('''
- **CEDEAR teórico** = Acción USA × CCL de referencia / ratio.
- **Desvío real/teórico** = CEDEAR real / CEDEAR teórico − 1.
- **CCL implícito** = CEDEAR real × ratio / Acción USA.
- **Var. CCL implícito** = (1 + Var. CEDEAR) / (1 + Var. USA) − 1.  
  Esta forma evita aproximar la variación simplemente restando porcentajes.
''')

with st.expander('Diagnóstico de fuentes'):
    st.markdown('**Validación CCL**')
    if ccl_observations:
        ccl_diag = pd.DataFrame([
            {'Fuente': x['source'], 'CCL observado': x['value'],
             'Diferencia vs usado': (x['value'] / ccl - 1) * 100 if ccl else None}
            for x in ccl_observations
        ])
        st.dataframe(ccl_diag, hide_index=True, use_container_width=True)
    else:
        st.caption('No respondió ninguna fuente CCL en esta actualización.')
    st.caption(f'CCL utilizado: {fmt_ars(ccl)} · {ccl_source}')

    st.markdown('**Fuentes de mercado por CEDEAR**')
    diag = df[['CEDEAR', '_source_usa', '_source_cedear', '_live']].rename(columns={
        '_source_usa': 'Fuente USA', '_source_cedear': 'Fuente CEDEAR', '_live': 'Ambas en vivo'
    })
    st.dataframe(diag, hide_index=True, use_container_width=True)

# Browser auto-refresh without an extra dependency.
st.components.v1.html(
    f'''<script>setTimeout(function(){{window.parent.location.reload();}}, {REFRESH_SECONDS * 1000});</script>''',
    height=0,
)

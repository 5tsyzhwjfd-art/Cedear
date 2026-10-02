# CEDEAR Monitor — Streamlit V1

Primera versión funcional del dashboard acordado para AAPL, NVDA y SPY.

## Ejecutar localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Twelve Data

La app funciona sin clave en modo híbrido/fallback. Para consultar acciones USA desde Twelve Data, cree `.streamlit/secrets.toml`:

```toml
TWELVE_DATA_API_KEY = "..."
```

En Streamlit Community Cloud agregue la misma clave en **App settings > Secrets**.

## Estado de fuentes en V1

- Acción USA: Twelve Data cuando hay API key; fallback explícitamente identificado si no.
- Dólar oficial: intenta APIs públicas de ArgentinaDatos/DolarAPI.
- CEDEAR local: adaptador preparado, con fallback de los valores usados en la validación hasta conectar un feed público estable.
- CCL referencia: adaptador preparado, usando en V1 el valor BYMA validado como fallback.
- Ratios: tabla local para AAPL/NVDA/SPY.

Los cálculos financieros están desacoplados de las fuentes para reemplazar cada adaptador sin modificar la interfaz.

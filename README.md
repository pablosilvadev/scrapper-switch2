# Scraper WePlay Preventas (Switch 2 Zelda)

Monitorea [weplay.cl/preventas.html](https://www.weplay.cl/preventas.html) cada 30 minutos (GitHub Actions) y avisa por **ntfy** solo cuando aparece un producto **nuevo** cuyo nombre coincida con las keywords de `config.json`.

## Qué busca

Por defecto busca variantes de la consola Zelda (`consola zelda`, `zelda edition`, `the legend of zelda`, etc.). El matching es case-insensitive, ignora tildes y **no exige que las palabras estén juntas**.

`exclude_keywords` evita juegos conocidos (Ocarina, Tears of the Kingdom, etc.). Edita esas listas en `config.json` cuando quieras.

Hoy la consola no está en el listado; sí hay *Preventa The Legend of Zelda Ocarina of Time Switch 2* (solo en tienda). Ese ítem **no** dispara alerta con la config por defecto porque “ocarina” está excluido.

## Prueba local

```bash
python -m venv .venv
# Windows Git Bash:
source .venv/Scripts/activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt

# 1) Ver que parsea y matchea, sin notificar ni guardar estado:
python scraper.py --dry-run
```

Para una notificación real, exporta el topic y corre sin `--dry-run`:

```bash
export NTFY_TOPIC="pon-aqui-un-tema-largo-y-secreto"
# opcional, por defecto https://ntfy.sh
# export NTFY_SERVER="https://ntfy.sh"
# export NTFY_TOKEN="..."   # solo si tu servidor pide auth

python scraper.py
```

Si hay coincidencia nueva, se actualiza `seen_products.json`. Si no hay nada nuevo, el script no envía mensajes.

## Configurar ntfy

1. Instala la app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy), [iOS](https://apps.apple.com/app/ntfy/id1625396347)) o abre [ntfy.sh](https://ntfy.sh) en el navegador.
2. Inventa un **topic largo y aleatorio** (letras, números, `_` o `-`). No uses `zelda` ni `weplay`: en el servidor público quien adivine el nombre puede leer o publicar.
   - Ejemplo: `wp-sw2-a7k9mQ2xL4pR8nT`
3. En la app: **Subscribe** a ese mismo topic.
4. Prueba que llega:
   ```bash
   curl -d "hola desde el scraper" ntfy.sh/TU_TOPIC
   ```
5. Ese topic es el secret `NTFY_TOPIC`.

Servidor propio: define también `NTFY_SERVER` (sin barra final) y, si aplica, `NTFY_TOKEN`.

## Secrets en GitHub

1. Sube este repo a GitHub.
2. **Settings → Secrets and variables → Actions → New repository secret**.
3. Crea:
   - `NTFY_TOPIC` (obligatorio)
   - `NTFY_SERVER` (opcional; si no existe, se usa `https://ntfy.sh`)
   - `NTFY_TOKEN` (opcional; auth Bearer)
4. **Actions → WePlay preventas scraper → Run workflow** para probar a mano.
5. El cron `*/30 * * * *` corre cada ~30 min (UTC; GitHub puede atrasarlo). Tras una alerta, el workflow hace commit de `seen_products.json` para no repetirla.

Habilita Actions en el repo (pestaña Actions la primera vez). El job necesita permiso de escritura en el repo (ya está en el workflow).

## Archivos

| Archivo | Rol |
| --- | --- |
| `scraper.py` | Descarga, parsea, filtra, notifica, persiste estado |
| `config.json` | URL, keywords, exclusiones, reintentos |
| `seen_products.json` | URLs ya notificadas |
| `.github/workflows/scrape.yml` | Cron + commit del estado |

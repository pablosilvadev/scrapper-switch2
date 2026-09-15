#!/usr/bin/env python3
"""Scraper de preventas WePlay (Magento) para alertar coincidencias nuevas.

En GitHub Actions el fetch va por ZenRows (plan Free, sin tarjeta) porque
Cloudflare challengea las IPs de datacenter. En local, sin API key, se
descarga directo.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cf_requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen_products.json"
ENV_PATH = ROOT / ".env"
ZENROWS_ENDPOINT = "https://api.zenrows.com/v1/"

# WePlay está detrás de Cloudflare. requests “puro” desde GitHub Actions
# (IPs de datacenter) suele recibir 403. curl_cffi imita TLS/HTTP2 de Chrome.
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

_cf_session: cf_requests.Session | None = None


def browser_session() -> cf_requests.Session:
    """Sesión reutilizable que imita Chrome (TLS fingerprint, no solo User-Agent)."""
    global _cf_session
    if _cf_session is None:
        _cf_session = cf_requests.Session(impersonate="chrome")
        try:
            _cf_session.get("https://www.weplay.cl/", headers=BROWSER_HEADERS, timeout=20)
        except cf_requests.RequestsError:
            logging.warning("No se pudo hacer warmup a la home de WePlay; se sigue igual.")
    return _cf_session

# ---------------------------------------------------------------------------
# Selectores Magento 2 + tema Porto (inspeccionados en weplay.cl/preventas.html)
#
# Estructura real (abr-2026 / sep-2026):
#   <div class="products wrapper grid ... products-grid">
#     <ol class="filterproducts products list items product-items">
#       <li class="item product product-item">
#         <a class="product-item-link" href="...">Nombre</a>
#         <span class="price-wrapper" data-price-type="finalPrice" data-price-amount="69990">
#           <span class="price">$69.990</span>
#         </span>
#         <!-- Solo si no hay compra online: -->
#         <div class="product-label stock unavailable">
#           <span>Disponible solo en tienda</span>
#         </div>
#         <!-- Si hay stock online: -->
#         <button class="action tocart primary">Agregar al Carro</button>
#       </li>
#     </ol>
#   </div>
#
# Si Magento/Porto cambia el HTML, ajusta SOLO estas constantes.
# ---------------------------------------------------------------------------
SELECTOR_PRODUCT_ITEMS = "ol.product-items > li.product-item"
SELECTOR_PRODUCT_LINK = "a.product-item-link"
SELECTOR_FINAL_PRICE = 'span.price-wrapper[data-price-type="finalPrice"]'
SELECTOR_PRICE_FALLBACK = "span.price"
SELECTOR_STORE_ONLY = "div.product-label.stock.unavailable"
SELECTOR_ADD_TO_CART = "button.action.tocart, form[data-role='tocart-form']"
SELECTOR_TOOLBAR_COUNT = "p.toolbar-amount span.toolbar-number"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_env_file() -> None:
    """Carga .env local. No pisa variables ya definidas (p. ej. GitHub Secrets)."""
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def zenrows_api_key() -> str:
    return os.getenv("ZENROWS_API_KEY", "").strip()


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    with SEEN_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return set(data.get("seen", []))


def save_seen(urls: set[str]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "seen": sorted(urls),
    }
    with SEEN_PATH.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def normalize_text(value: str) -> str:
    """Minúsculas, sin tildes, sin puntuación. Sirve para matching tolerante."""
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"[^a-z0-9]+", " ", without_accents.lower())
    return " ".join(cleaned.split())


def phrase_in_name(phrase: str, normalized_name: str, name_tokens: set[str]) -> bool:
    """True si la frase aparece seguida, o si TODAS sus palabras están en el nombre.

    Así "consola zelda" matchea "Preventa Consola Switch 2 The Legend of Zelda"
    aunque las palabras no estén juntas.
    """
    normalized_phrase = normalize_text(phrase)
    if not normalized_phrase:
        return False
    if normalized_phrase in normalized_name:
        return True
    phrase_tokens = normalized_phrase.split()
    return bool(phrase_tokens) and set(phrase_tokens).issubset(name_tokens)


def is_match(name: str, keywords: list[str], exclude_keywords: list[str]) -> bool:
    normalized_name = normalize_text(name)
    name_tokens = set(normalized_name.split())
    if any(phrase_in_name(ex, normalized_name, name_tokens) for ex in exclude_keywords):
        return False
    return any(phrase_in_name(kw, normalized_name, name_tokens) for kw in keywords)


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def with_query(url: str, extra: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(extra)
    return urlunparse(parsed._replace(query=urlencode(query)))


def fetch_via_zenrows(url: str, timeout: int) -> str:
    """Baja el HTML por ZenRows (residencial + stealth). Plan Free: 5000 créditos/mes.

    mode=auto usa JS/proxy solo si hace falta. 1 corrida/día cabe de sobra
    (peor caso ~25 créditos vs 5000).
    """
    params = {
        "apikey": zenrows_api_key(),
        "url": url,
        "mode": "auto",
        "proxy_country": "cl",
        "wait_for": "ol.product-items",
        "original_status": "true",
    }
    response = requests.get(ZENROWS_ENDPOINT, params=params, timeout=timeout)
    cost = response.headers.get("X-Request-Cost", "?")
    logging.info("ZenRows HTTP %s cost=%s id=%s", response.status_code, cost, response.headers.get("X-Request-Id", "-"))
    if response.status_code != 200:
        raise RuntimeError(f"ZenRows HTTP {response.status_code}: {response.text[:400]}")
    text = response.text
    head = text[:2500].lower()
    if "just a moment" in head or "cf-mitigated" in head:
        raise RuntimeError("ZenRows devolvió el challenge de Cloudflare, no el HTML de WePlay.")
    return text


def fetch_html(url: str, timeout: int, max_retries: int, backoff: int) -> str:
    if zenrows_api_key():
        last_error: Exception | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return fetch_via_zenrows(url, timeout)
            except Exception as exc:  # noqa: BLE001
                logging.warning("ZenRows intento %s/%s: %s", attempt, max_retries, exc)
                last_error = exc
                if attempt < max_retries:
                    time.sleep(backoff * attempt)
        raise RuntimeError(f"No se pudo descargar {url} via ZenRows: {last_error}")

    last_error = None
    session = browser_session()
    for attempt in range(1, max_retries + 1):
        try:
            response = session.get(url, headers=BROWSER_HEADERS, timeout=timeout)
            cache = response.headers.get("cf-cache-status", "?")
            mitigated = response.headers.get("cf-mitigated", "")
            if response.status_code != 200:
                logging.warning(
                    "Intento %s/%s: HTTP %s en %s (cf-cache=%s cf-mitigated=%s)",
                    attempt,
                    max_retries,
                    response.status_code,
                    url,
                    cache,
                    mitigated or "-",
                )
                hint = f"HTTP {response.status_code}"
                if response.status_code == 403:
                    hint += (
                        " — Cloudflare bloqueó la IP. En Actions usa ZENROWS_API_KEY "
                        "(plan Free en zenrows.com, sin tarjeta)."
                    )
                last_error = RuntimeError(hint)
            else:
                logging.info("HTTP 200 (cf-cache=%s) %s", cache, url)
                return response.text
        except cf_requests.RequestsError as exc:
            logging.warning("Intento %s/%s falló (%s): %s", attempt, max_retries, url, exc)
            last_error = exc
        if attempt < max_retries:
            time.sleep(backoff * attempt)
    raise RuntimeError(f"No se pudo descargar {url}: {last_error}")


def parse_availability(item) -> str:
    store_only = item.select_one(SELECTOR_STORE_ONLY)
    if store_only:
        label = store_only.get_text(" ", strip=True)
        return label or "Disponible solo en tienda"
    if item.select_one(SELECTOR_ADD_TO_CART):
        return "Disponible online"
    return "Estado desconocido"


def parse_price(item) -> str:
    wrapper = item.select_one(SELECTOR_FINAL_PRICE)
    if wrapper and wrapper.get("data-price-amount"):
        amount = wrapper.get("data-price-amount")
        visible = wrapper.select_one(SELECTOR_PRICE_FALLBACK)
        if visible:
            return visible.get_text(strip=True)
        try:
            return f"${int(float(amount)):,}".replace(",", ".")
        except (TypeError, ValueError):
            return str(amount)
    fallback = item.select_one(SELECTOR_PRICE_FALLBACK)
    return fallback.get_text(strip=True) if fallback else "Sin precio"


def parse_products(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    products: list[dict[str, str]] = []
    toolbar = soup.select_one(SELECTOR_TOOLBAR_COUNT)
    if toolbar:
        logging.info("Toolbar Magento reporta %s artículos (página actual).", toolbar.get_text(strip=True))

    for item in soup.select(SELECTOR_PRODUCT_ITEMS):
        link = item.select_one(SELECTOR_PRODUCT_LINK)
        if not link or not link.get("href"):
            continue
        name = link.get_text(" ", strip=True)
        href = canonicalize_url(urljoin(base_url, link["href"]))
        products.append(
            {
                "name": name,
                "url": href,
                "price": parse_price(item),
                "availability": parse_availability(item),
            }
        )
    return products


def scrape_all_pages(config: dict[str, Any]) -> list[dict[str, str]]:
    base_url = config["target_url"]
    limit = str(config.get("product_list_limit", 36))
    max_pages = int(config.get("max_pages", 10))
    timeout = int(config.get("request_timeout_seconds", 25))
    retries = int(config.get("max_retries", 3))
    backoff = int(config.get("retry_backoff_seconds", 5))

    collected: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for page in range(1, max_pages + 1):
        # Página 1 sin query: Cloudflare suele servirla desde caché (HIT).
        # product_list_limit/p fuerzan BYPASS al origen y disparan más 403.
        if page == 1:
            page_url = base_url
        else:
            page_url = with_query(base_url, {"product_list_limit": limit, "p": str(page)})
        logging.info("Descargando página %s: %s", page, page_url)
        html = fetch_html(page_url, timeout, retries, backoff)
        products = parse_products(html, base_url)
        new_on_page = 0
        for product in products:
            if product["url"] in seen_urls:
                continue
            seen_urls.add(product["url"])
            collected.append(product)
            new_on_page += 1
        logging.info("Página %s: %s productos parseados (%s nuevos).", page, len(products), new_on_page)
        if not products or new_on_page == 0 or len(products) < int(limit):
            break

    return collected


def format_message(product: dict[str, str]) -> str:
    return (
        "Coincidencia nueva en WePlay Preventas\n"
        f"Nombre: {product['name']}\n"
        f"Precio: {product['price']}\n"
        f"Estado: {product['availability']}\n"
        f"{product['url']}"
    )


def ntfy_endpoint() -> tuple[str, str]:
    """Devuelve (url de publicación, topic) o ("", "") si falta config."""
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if not topic:
        return "", ""
    server = os.getenv("NTFY_SERVER", "").strip().rstrip("/") or "https://ntfy.sh"
    return f"{server}/{topic}", topic


def notify_ntfy(product: dict[str, str]) -> None:
    """Publica en ntfy. El tema (topic) es el secreto: no uses nombres adivinables."""
    url, _topic = ntfy_endpoint()
    if not url:
        raise RuntimeError("Falta NTFY_TOPIC")

    headers = {
        # Title tiene que ser ASCII: requests no envía UTF-8 en headers.
        "Title": "WePlay Preventas",
        "Priority": "high",
        "Tags": "video_game,shopping",
        "Click": product["url"],
    }
    token = os.getenv("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.post(
        url,
        data=format_message(product).encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    if response.status_code != 200:
        raise RuntimeError(f"ntfy HTTP {response.status_code}: {response.text[:300]}")


def notify(product: dict[str, str]) -> bool:
    """Envía la alerta por ntfy. True si salió bien."""
    if not ntfy_endpoint()[0]:
        logging.error("No hay notificación configurada. Define NTFY_TOPIC.")
        return False
    try:
        notify_ntfy(product)
        logging.info("Notificado por ntfy: %s", product["url"])
        return True
    except Exception as exc:  # noqa: BLE001
        logging.error("Fallo ntfy: %s", exc)
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitorea preventas WePlay.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parsea y muestra coincidencias, sin notificar ni guardar estado.",
    )
    return parser.parse_args()


def main() -> int:
    setup_logging()
    load_env_file()
    args = parse_args()
    logging.info("Inicio de ejecución (dry_run=%s).", args.dry_run)

    if os.getenv("GITHUB_ACTIONS") and not zenrows_api_key():
        logging.error(
            "En GitHub Actions hace falta el secret ZENROWS_API_KEY "
            "(cuenta Free en https://www.zenrows.com/ — sin tarjeta)."
        )
        return 1

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        logging.error("No se pudo leer config.json: %s", exc)
        return 1

    try:
        products = scrape_all_pages(config)
    except Exception as exc:  # noqa: BLE001
        logging.error("Error de red/parseo (no se actualiza estado): %s", exc)
        return 1

    logging.info("Total productos encontrados: %s", len(products))
    if not products:
        logging.warning("La grilla quedó vacía. Revisa selectores o un posible bloqueo.")
        return 1

    keywords = config.get("keywords", [])
    excludes = config.get("exclude_keywords", [])
    matches = [p for p in products if is_match(p["name"], keywords, excludes)]
    logging.info("Coincidencias con keywords: %s", len(matches))
    for product in matches:
        logging.info(
            "  MATCH | %s | %s | %s | %s",
            product["name"],
            product["price"],
            product["availability"],
            product["url"],
        )

    seen = load_seen()
    new_matches = [p for p in matches if p["url"] not in seen]
    logging.info("Coincidencias nuevas (no notificadas antes): %s", len(new_matches))

    if args.dry_run:
        logging.info("Dry-run: no se envían notificaciones ni se escribe seen_products.json.")
        return 0

    if not new_matches:
        logging.info("Nada nuevo. No se notifica.")
        return 0

    notified_urls: list[str] = []
    for product in new_matches:
        if notify(product):
            notified_urls.append(product["url"])

    if not notified_urls:
        logging.error("Hubo coincidencias nuevas pero ninguna notificación salió. No se marca como visto.")
        return 1

    seen.update(notified_urls)
    save_seen(seen)
    logging.info("Estado actualizado con %s URL(s) nueva(s).", len(notified_urls))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
scraper.py — Scraping de plataformas de desaparecidos (terremoto Venezuela 24 jun 2026).

ARQUITECTURA:
  - venezuelatebusca.com       → API REST JSON  /api/persons?query=NOMBRE
  - desaparecidosterremotovenezuela.com → Next.js SSR, datos en JSON inline del HTML
  - sosvzla.lat                → fallback HTML con BeautifulSoup

Función principal: buscar_en_todas_las_fuentes(nombre_query) -> list[dict]
"""

import concurrent.futures
import hashlib
import json
import logging
import re

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BotAyudaVenezuela/1.0)",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "es-VE,es;q=0.9,en;q=0.8",
}
TIMEOUT_CONNECT = 8   # tiempo para abrir la conexión
TIMEOUT_READ = 20     # tiempo para leer la respuesta (sitios rápidos)
TIMEOUT = (TIMEOUT_CONNECT, TIMEOUT_READ)  # tupla (connect, read)
# Bug 2 fix: venezuelatebusca puede tardar hasta 75s bajo carga del terremoto
TIMEOUT_VTB = (8, 75)
_TIMEOUT_SOSVZLA = (5, 8)  # sosvzla.lat suele estar caído, timeouts cortos

# ---------------------------------------------------------------------------
# Sesión global con retry automático — Fix #3
# Una sola sesión HTTP reutilizable con pool de conexiones y reintentos.
# Esto evita abrir/cerrar sockets en cada llamada (mucho más eficiente).
# ---------------------------------------------------------------------------

def _crear_sesion() -> requests.Session:
    """Crea una sesión HTTP con retry automático para errores 429 y 5xx."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,                          # espera 1s, 2s, 4s entre reintentos
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=5,   # conexiones paralelas máximas por host
        pool_maxsize=10,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(HEADERS)
    return session

SESSION = _crear_sesion()  # sesión global reutilizada por todos los scrapers

# Textos que indican que la página está caída/vacía — se ignoran
_NOISE_PHRASES = {
    "estamos sobrecargados",
    "reintentar",
    "0 personas registradas",
    "0 por localizar",
    "0 localizadas",
    "todos los estados",
    "por localizar",
    "localizadas",
    "buscar nombre",
    "registrar persona",
    "venezuela te busca",
    "registro de personas",
}


def _es_ruido(texto: str) -> bool:
    t = texto.strip().lower()
    return len(t) < 8 or any(f in t for f in _NOISE_PHRASES)


# ---------------------------------------------------------------------------
# 1. venezuelatebusca.com  — API JSON directa
# ---------------------------------------------------------------------------

def _scrape_venezuelatebusca(nombre_query: str) -> list[dict]:
    """
    Consulta la API REST de venezuelatebusca.com.
    Endpoint: GET /api/persons?query=NOMBRE&status=missing
    Devuelve personas con status 'missing' o 'found' que coincidan con el nombre.
    """
    fuente = "venezuelatebusca.com"
    base_url = "https://venezuelatebusca.com"
    resultados = []

    try:
        # Buscar tanto desaparecidos como localizados (para cubrir el caso de "ya aparecieron")
        params_lista = [
            {"query": nombre_query, "status": "missing"},
            {"query": nombre_query, "status": "found"},
        ]

        todos_json = []
        for params in params_lista:
            try:
                resp = SESSION.get(
                    f"{base_url}/api/persons",
                    params=params,
                    timeout=TIMEOUT_VTB,  # Bug 2 fix: hasta 75s de lectura
                )
                if resp.status_code == 200:
                    data = resp.json()
                    todos_json.extend(data.get("persons", []))
                else:
                    logger.warning(f"[{fuente}] HTTP {resp.status_code} para status={params['status']}")
            except (requests.RequestException, json.JSONDecodeError) as e:
                logger.warning(f"[{fuente}] Error en petición ({params}): {e}")

        vistos = set()
        for p in todos_json:
            pid = p.get("id", "")
            if pid in vistos:
                continue
            vistos.add(pid)

            nombre_completo = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            estado_str = p.get("status", "missing")
            estado_es = "🔴 Sin contacto" if estado_str == "missing" else "🟢 Localizado"
            ubicacion = p.get("last_seen_location") or "Desconocido"
            descripcion = p.get("description") or ""
            edad = p.get("age")
            cedula = p.get("national_id") or ""

            raw_parts = [nombre_completo, ubicacion, descripcion, cedula]
            if edad:
                raw_parts.append(f"Edad: {edad}")
            if estado_str == "found" and p.get("found_notes"):
                raw_parts.append(f"Nota: {p['found_notes']}")
            raw_text = " | ".join(filter(None, raw_parts))

            resultados.append({
                "nombre": nombre_completo,
                "estado": f"{estado_es} | {ubicacion}",
                "fuente": fuente,
                "url": f"{base_url}",
                "raw_text": raw_text[:500],
            })

        logger.info(f"[{fuente}] {len(resultados)} resultado(s) para '{nombre_query}'")

    except Exception as e:
        logger.error(f"[{fuente}] Error inesperado: {e}")

    return resultados


# ---------------------------------------------------------------------------
# 2. desaparecidosterremotovenezuela.com — JSON inline en el HTML (Next.js)
# ---------------------------------------------------------------------------

def _scrape_desaparecidos_terremoto(nombre_query: str) -> list[dict]:
    """
    PROBLEMA 2: desaparecidosterremotovenezuela.com carga datos por JS.
    Se reemplaza scraping HTML por llamadas GET a sus endpoints JSON.
    """
    fuente = "desaparecidosterremotovenezuela.com"
    base_url = "https://desaparecidosterremotovenezuela.com"
    resultados = []

    # Endpoint 1
    url1 = f"{base_url}/api/persons"
    try:
        resp = SESSION.get(url1, params={"q": nombre_query}, timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    resultados.append({
                        "nombre": item.get("nombre", ""),
                        "estado": item.get("estado", "Desconocido"),
                        "fuente": fuente,
                        "url": base_url,
                        "raw_text": str(item)[:500]
                    })
                return resultados
    except Exception as e:
        logger.warning(f"[{fuente}] Error en {url1}: {e}")

    # Endpoint 2 (Fallback)
    url2 = f"{base_url}/api/search"
    try:
        resp = SESSION.get(url2, params={"nombre": nombre_query}, timeout=TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    resultados.append({
                        "nombre": item.get("nombre", ""),
                        "estado": item.get("estado", "Desconocido"),
                        "fuente": fuente,
                        "url": base_url,
                        "raw_text": str(item)[:500]
                    })
                return resultados
    except Exception as e:
        logger.warning(f"[{fuente}] Error en {url2}: {e}")

    logger.info(f"[{fuente}] No se obtuvieron resultados de los endpoints JSON.")
    return resultados


# ---------------------------------------------------------------------------
# 3. sosvzla.lat — BeautifulSoup (fallback)
# ---------------------------------------------------------------------------

def _scrape_sosvzla(nombre_query: str) -> list[dict]:
    """
    Scrape de https://sosvzla.lat con BeautifulSoup.
    El sitio devuelve 404 en /buscar, intentamos múltiples rutas.
    """
    fuente = "sosvzla.lat"
    base_url = "https://sosvzla.lat"
    resultados = []

    urls_a_probar = [
        f"{base_url}/?s={requests.utils.quote(nombre_query)}",
        f"{base_url}/buscar?nombre={requests.utils.quote(nombre_query)}",
        base_url,
    ]

    try:
        resp = None
        for url in urls_a_probar:
            try:
                r = SESSION.get(url, timeout=_TIMEOUT_SOSVZLA)
                if r.status_code == 200:
                    resp = r
                    break
            except requests.RequestException:
                continue

        if resp is None:
            logger.warning(f"[{fuente}] No accesible")
            return []

        soup = BeautifulSoup(resp.text, "html.parser")
        primer_token = nombre_query.split()[0].lower()

        candidatos = (
            soup.select(".card, .persona, .desaparecido, article, .result-item, .post")
            or [
                el for el in soup.find_all(["div", "li", "article"])
                if primer_token in el.get_text().lower()
                and 20 < len(el.get_text(separator=" ").strip()) < 1500
                and not _es_ruido(el.get_text(separator=" ").strip())
            ][:15]
        )

        for elem in candidatos:
            texto = elem.get_text(separator=" ").strip()
            if _es_ruido(texto):
                continue

            nombre_tag = elem.find(["h2", "h3", "h4", "strong", "b"])
            nombre_ext = nombre_tag.get_text(strip=True) if nombre_tag else texto[:80]

            link = elem.find("a", href=True)
            url_ext = (
                base_url + link["href"] if link and link["href"].startswith("/")
                else link["href"] if link
                else resp.url
            )

            resultados.append({
                "nombre": nombre_ext,
                "estado": "Desconocido",
                "fuente": fuente,
                "url": url_ext,
                "raw_text": texto[:500],
            })

        logger.info(f"[{fuente}] {len(resultados)} resultado(s)")

    except requests.RequestException as e:
        logger.warning(f"[{fuente}] Error de red: {e}")
    except Exception as e:
        logger.error(f"[{fuente}] Error inesperado: {e}")

    from matcher import calcular_similitud
    resultados_filtrados = [
        r for r in resultados
        if calcular_similitud(nombre_query, r["nombre"]) >= 0.55
    ]
    return resultados_filtrados


# ---------------------------------------------------------------------------
# Función principal
# ---------------------------------------------------------------------------

def buscar_en_todas_las_fuentes(nombre_query: str) -> list[dict]:
    """
    Busca en PARALELO en todas las fuentes. Fix #4.

    Antes era secuencial: si sosvzla tardaba 15s y venezuelatebusca 10s → 25s total.
    Ahora con ThreadPoolExecutor: todas corren al mismo tiempo → máximo 15s total.

    Si una fuente falla o tarda demasiado, las demás siguen respondiendo.
    """
    scrapers = [
        _scrape_venezuelatebusca,        # API JSON → más fiable
        _scrape_desaparecidos_terremoto,  # Next.js inline JSON
        _scrape_sosvzla,                  # HTML fallback
    ]

    todos = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(fn, nombre_query): fn.__name__
            for fn in scrapers
        }
        for future in concurrent.futures.as_completed(futures, timeout=90):  # Bug 1 fix: 90s
            nombre_fn = futures[future]
            try:
                r = future.result(timeout=85)  # Bug 1 fix: venezeulatabusca puede tardar 75s
                todos.extend(r)
            except concurrent.futures.TimeoutError:
                logger.warning(f"[{nombre_fn}] Timeout en búsqueda paralela")
            except Exception as e:
                logger.error(f"[{nombre_fn}] Fallo inesperado: {e}")

    logger.info(f"Total resultados para '{nombre_query}': {len(todos)}")
    return todos


# ---------------------------------------------------------------------------
# Prueba rápida: python scraper.py "Nombre Apellido"
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    # Forzar UTF-8 en la salida para Windows
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    nombre_test = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Jose Martinez"
    print(f"\n[BUSCAR] {nombre_test}\n{'='*60}")

    conteo = {
        "venezuelatebusca.com": 0,
        "desaparecidosterremotovenezuela.com": 0,
        "sosvzla.lat": 0,
    }

    resultados = buscar_en_todas_las_fuentes(nombre_test)

    for r in resultados:
        fuente = r["fuente"]
        conteo[fuente] = conteo.get(fuente, 0) + 1
        print(f"\n  [{fuente}]")
        print(f"   Nombre : {r['nombre']}")
        print(f"   Estado : {r['estado']}")
        print(f"   URL    : {r['url']}")
        print(f"   Texto  : {r['raw_text'][:120]}...")

    print(f"\n{'='*60}")
    print("Resultados por fuente:")
    for fuente, count in conteo.items():
        print(f"  {fuente}: {count} resultado(s)")
    print(f"\nTotal: {len(resultados)} resultado(s)")
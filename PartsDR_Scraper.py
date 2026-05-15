#!/usr/bin/env python3
"""
PartsDR "Replaces" scraper
==========================

Dado un número de parte (ej. WPW10476828), busca el producto en partsdr.com
y devuelve los números a los que reemplaza:

  - Replaces                          (reemplazo directo / legacy)
  - <Brand> part numbers              (números cruzados del fabricante)
  - SKUs and competitor part numbers  (SKUs y competidores)

Uso:
    python3 partsdr_replaces.py WPW10476828
    python3 partsdr_replaces.py WPW10476828 --json
    python3 partsdr_replaces.py WPW10476828 --json > out.json
    python3 partsdr_replaces.py WPW10476828 --save-html debug.html
    python3 partsdr_replaces.py --file ruta/al/archivo.html         (parsea sin red)

Dependencias (recomendado, pasa Cloudflare):
    pip install curl_cffi beautifulsoup4

Dependencias mínimas (fallback, puede fallar con 403):
    pip install requests beautifulsoup4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Tuple
from urllib.parse import urljoin

# ---------------------------------------------------------------------------
# Cliente HTTP: preferimos curl_cffi (imita TLS de Chrome → pasa Cloudflare)
# y caemos a requests si no está instalado.
# ---------------------------------------------------------------------------

_HTTP_BACKEND: str
try:
    from curl_cffi import requests as _http  # type: ignore
    _HTTP_BACKEND = "curl_cffi"
except ImportError:
    try:
        import requests as _http  # type: ignore
        _HTTP_BACKEND = "requests"
    except ImportError:
        print(
            "Falta un cliente HTTP. Instala (recomendado para esquivar Cloudflare):\n"
            "    pip install curl_cffi beautifulsoup4\n"
            "o, como mínimo:\n"
            "    pip install requests beautifulsoup4",
            file=sys.stderr,
        )
        sys.exit(127)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print(
        "Falta beautifulsoup4. Instala con:\n"
        "    pip install beautifulsoup4",
        file=sys.stderr,
    )
    sys.exit(127)


BASE_URL = "https://partsdr.com"
SEARCH_URL = f"{BASE_URL}/search"

# Headers de Chrome real (los sec-ch-* y sec-fetch-* son clave si toca caer
# al backend "requests").
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}


# ---------------------------------------------------------------------------
# Red
# ---------------------------------------------------------------------------

def _new_session():
    """Crea una sesión HTTP del backend disponible, con headers e impersonate."""
    if _HTTP_BACKEND == "curl_cffi":
        # impersonate="chrome" replica TLS + HTTP/2 + headers de Chrome.
        sess = _http.Session(impersonate="chrome")
        # curl_cffi ya pone los headers de Chrome al impersonar; añadimos los
        # nuestros sin pisar los suyos.
        extra = {k: v for k, v in DEFAULT_HEADERS.items()
                 if k.lower() not in {"user-agent", "accept",
                                       "accept-language",
                                       "accept-encoding",
                                       "sec-ch-ua",
                                       "sec-ch-ua-mobile",
                                       "sec-ch-ua-platform"}}
        sess.headers.update(extra)
        return sess
    sess = _http.Session()
    sess.headers.update(DEFAULT_HEADERS)
    return sess


def fetch_part_page(part_number: str, session, timeout: int = 30) -> Tuple[str, str]:
    """
    Resuelve un número de parte a su página y devuelve (url_final, html).

    Estrategia:
      1) /search?query=<part> normalmente redirige a /part/<slug> cuando
         hay coincidencia exacta. Seguimos los redirects.
      2) Si en cambio aterriza en una página de resultados, tomamos el
         primer enlace que apunta a /part/.
    """
    resp = session.get(
        SEARCH_URL,
        params={"query": part_number},
        timeout=timeout,
        allow_redirects=True,
    )
    resp.raise_for_status()

    final_url = str(resp.url)
    html = resp.text

    if "/part/" in final_url:
        return final_url, html

    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one('a[href*="/part/"]')
    if not link:
        raise RuntimeError(
            f"No se encontró ninguna página de producto para '{part_number}'."
        )

    part_url = urljoin(BASE_URL, link["href"])
    resp = session.get(part_url, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return str(resp.url), resp.text


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _collect_cross_refs(h3) -> list:
    """Toma un <h3> y devuelve los part numbers del grid hermano siguiente."""
    if not h3:
        return []
    grid = h3.find_next_sibling("div")
    if not grid:
        return []
    items = []
    for d in grid.find_all("div", attrs={"wire:key": re.compile(r"cross-reference")}):
        text = d.get_text(strip=True)
        if text:
            items.append(text)
    return items


def parse_part_page(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    result: dict = {
        "title": None,
        "current_part_number": None,
        "name": None,
        "manufacturer": None,
        "price": None,
        "replaces": [],
        "also_replaces": {
            "manufacturer_label": None,
            "manufacturer_part_numbers": [],
            "skus_and_competitor_part_numbers": [],
        },
    }

    # <title>
    if soup.title:
        result["title"] = soup.title.get_text(strip=True)

    # Datos estructurados JSON-LD (mpn / nombre / precio)
    ld = soup.find("script", {"type": "application/ld+json"})
    if ld and ld.string:
        try:
            data = json.loads(ld.string)
            result["current_part_number"] = data.get("mpn")
            result["name"] = data.get("name")
            brand = data.get("brand") or {}
            result["manufacturer"] = brand.get("name")
            offers = data.get("offers") or {}
            if offers.get("price") is not None:
                avail = offers.get("availability", "")
                result["price"] = {
                    "amount": offers.get("price"),
                    "currency": offers.get("priceCurrency"),
                    "availability": avail.rsplit("/", 1)[-1] if avail else None,
                }
        except (json.JSONDecodeError, AttributeError):
            pass

    # Fallback al <h1>
    if not result["current_part_number"]:
        h1 = soup.find("h1")
        if h1:
            span = h1.find("span")
            if span:
                result["current_part_number"] = span.get_text(strip=True)
            if not result["name"]:
                result["name"] = h1.get_text(" ", strip=True)

    # ---- "Replaces" (reemplazo directo) ----
    replaces_h4 = soup.find(
        "h4", string=re.compile(r"^\s*Replaces\s*$", re.IGNORECASE)
    )
    if replaces_h4:
        nxt = replaces_h4.find_next_sibling("div")
        if nxt:
            for strong in nxt.find_all("strong"):
                num = strong.get_text(strip=True)
                if num:
                    result["replaces"].append(num)

    # ---- "Also Replaces": fabricante + SKUs/competidores ----
    # Solo h3s que tengan un grid hermano con cross-references (descarta
    # encabezados decorativos como "Alternate Part Numbers").
    h3_mfr = None
    h3_skus = None
    for h3 in soup.find_all("h3"):
        text = h3.get_text(strip=True)
        sib = h3.find_next_sibling("div")
        if not sib or not sib.find(
            "div", attrs={"wire:key": re.compile(r"cross-reference")}
        ):
            continue
        if re.search(r"SKUs.*competitor.*part numbers", text, re.IGNORECASE):
            h3_skus = h3
        elif re.search(r"\bpart numbers\b", text, re.IGNORECASE) and h3_mfr is None:
            h3_mfr = h3

    if h3_mfr is not None:
        result["also_replaces"]["manufacturer_part_numbers"] = _collect_cross_refs(h3_mfr)
        m = re.match(
            r"^(.+?)\s+part numbers", h3_mfr.get_text(strip=True), re.IGNORECASE
        )
        if m:
            result["also_replaces"]["manufacturer_label"] = m.group(1)

    result["also_replaces"]["skus_and_competitor_part_numbers"] = _collect_cross_refs(h3_skus)

    return result


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------

def print_pretty(data: dict) -> None:
    print()
    title = data.get("name") or data.get("title") or "(parte desconocida)"
    bar = "=" * max(60, min(len(title) + 4, 80))
    print(bar)
    print(f"  {title}")
    print(bar)

    if data.get("current_part_number"):
        print(f"Número de parte : {data['current_part_number']}")
    if data.get("manufacturer"):
        print(f"Fabricante      : {data['manufacturer']}")
    if data.get("price"):
        p = data["price"]
        avail = p.get("availability") or ""
        print(f"Precio          : {p.get('amount')} {p.get('currency')} ({avail})")
    if data.get("source_url"):
        print(f"Fuente          : {data['source_url']}")
    print()

    if data["replaces"]:
        print(f"Replaces ({len(data['replaces'])}):")
        for r in data["replaces"]:
            print(f"  • {r}")
    else:
        print("Replaces: (ninguno listado)")
    print()

    ar = data["also_replaces"]
    label = ar.get("manufacturer_label") or "Manufacturer"
    if ar["manufacturer_part_numbers"]:
        print(f"{label} part numbers ({len(ar['manufacturer_part_numbers'])}):")
        for n in ar["manufacturer_part_numbers"]:
            print(f"  • {n}")
        print()

    if ar["skus_and_competitor_part_numbers"]:
        print(
            f"SKUs and competitor part numbers "
            f"({len(ar['skus_and_competitor_part_numbers'])}):"
        )
        for n in ar["skus_and_competitor_part_numbers"]:
            print(f"  • {n}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Obtiene los 'Replaces' de una parte desde partsdr.com",
    )
    parser.add_argument(
        "part_number",
        nargs="?",
        help="Número de parte a consultar, p. ej. WPW10476828",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Salida en JSON (útil para scripting)",
    )
    parser.add_argument(
        "--save-html",
        metavar="FILE",
        help="Guarda el HTML descargado en este archivo (debug)",
    )
    parser.add_argument(
        "--file",
        metavar="HTML",
        help="No hace request, parsea un archivo HTML local (modo offline / debug)",
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="Timeout HTTP en segundos (default 30)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Imprime info de diagnóstico (backend HTTP, etc.)",
    )
    args = parser.parse_args()

    if not args.part_number and not args.file:
        parser.error("Debes proveer un número de parte o usar --file ARCHIVO.html")

    if args.debug and not args.file:
        print(f"[debug] HTTP backend: {_HTTP_BACKEND}", file=sys.stderr)
        if _HTTP_BACKEND == "requests":
            print(
                "[debug] Aviso: 'requests' suele ser bloqueado por Cloudflare con 403.\n"
                "[debug] Instala curl_cffi para imitar TLS de Chrome:\n"
                "[debug]     pip install curl_cffi",
                file=sys.stderr,
            )

    # Modo offline
    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
        data = parse_part_page(html)
        data["source_url"] = f"file://{args.file}"
        data["queried_part_number"] = args.part_number
    else:
        session = _new_session()
        try:
            url, html = fetch_part_page(
                args.part_number, session, timeout=args.timeout
            )
        except Exception as e:  # cubre HTTPError de ambos backends
            msg = str(e)
            if "403" in msg:
                print(
                    f"Error HTTP 403 (bloqueado por Cloudflare): {e}\n"
                    f"  Backend actual: {_HTTP_BACKEND}\n"
                    f"  Sugerencia: instala curl_cffi para imitar TLS de Chrome:\n"
                    f"      pip install curl_cffi",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(2)

        if args.save_html:
            with open(args.save_html, "w", encoding="utf-8") as f:
                f.write(html)

        data = parse_part_page(html)
        data["source_url"] = url
        data["queried_part_number"] = args.part_number

    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_pretty(data)


if __name__ == "__main__":
    main()
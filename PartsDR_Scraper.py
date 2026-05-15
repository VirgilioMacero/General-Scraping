#!/usr/bin/env python3
"""
PartsDR Replaces Scraper
========================
Extrae la sección "Replaces" de una página de parte en partsdr.com:
  - Brand part numbers (GE, Samsung, Whirlpool, etc.)
  - SKUs and competitor part numbers
  - Part production numbers

Uso:
    python partsdr_scraper.py WB27K10090
    python partsdr_scraper.py https://partsdr.com/part/wb27k10090-some-slug
    python partsdr_scraper.py WB27K10090 --format json
    python partsdr_scraper.py WB27K10090 --format json --output out.json
    python partsdr_scraper.py WB27K10090 --use-cloudscraper   # si recibes 403

Requisitos:
    pip install requests beautifulsoup4 lxml
    # Opcional (para evadir Cloudflare):
    pip install cloudscraper
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urlparse

try:
    import requests
    from bs4 import BeautifulSoup, Tag
except ImportError:
    sys.exit("Faltan dependencias. Ejecuta: pip install requests beautifulsoup4 lxml")


BASE_URL = "https://partsdr.com"

# Headers de un navegador real para evitar respuestas 403.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# Heurística para detectar algo que se ve como número de parte.
# Acepta tokens alfanuméricos con al menos 4 caracteres y al menos un dígito.
PART_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\/\.]{3,}$")


# ----------------------------- HTTP --------------------------------------- #

def build_session(use_cloudscraper: bool = False):
    """Construye una sesión HTTP. Usa cloudscraper si está disponible y se pide."""
    if use_cloudscraper:
        try:
            import cloudscraper  # type: ignore
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            scraper.headers.update(DEFAULT_HEADERS)
            return scraper
        except ImportError:
            print(
                "[!] cloudscraper no está instalado. Continuando con requests. "
                "Instala con: pip install cloudscraper",
                file=sys.stderr,
            )
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch(session, url: str, timeout: int = 30) -> str:
    """Descarga una URL y devuelve HTML, lanzando una excepción legible si falla."""
    response = session.get(url, timeout=timeout)
    if response.status_code == 403:
        raise RuntimeError(
            f"HTTP 403 en {url}. El sitio detectó al bot. "
            "Vuelve a intentar con --use-cloudscraper o usa Playwright."
        )
    response.raise_for_status()
    return response.text


# --------------------------- URL Resolution ------------------------------- #

def resolve_part_url(session, query: str) -> str:
    """
    Si `query` es una URL, la devuelve tal cual.
    Si es un número de parte, busca en el sitio y resuelve la URL canónica /part/.
    """
    if query.startswith("http://") or query.startswith("https://"):
        return query

    # Intento directo: el slug puede ser predecible (parte-en-minúsculas-...).
    # Pero como no siempre coincide, hacemos una búsqueda y seguimos el primer hit.
    search_url = f"{BASE_URL}/search?q={quote_plus(query)}"
    html = fetch(session, search_url)
    soup = BeautifulSoup(html, "lxml")

    # Caso 1: redirección directa a la página del part — el HTML mismo es la
    # página del producto. Si encontramos los marcadores de producto, devolvemos.
    if soup.find(string=re.compile(r"part number", re.I)) and "/part/" in str(
        soup.find_all("link", rel="canonical"))[:300]:
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href"):
            return canonical["href"]

    # Caso 2: lista de resultados — agarra el primer enlace que contenga /part/
    for a in soup.select("a[href*='/part/']"):
        href = a.get("href", "")
        if href.startswith("/"):
            href = BASE_URL + href
        if "/part/" in href:
            return href.split("?")[0]

    # Último recurso: probar URL adivinada
    raise RuntimeError(
        f"No se encontró URL de producto para '{query}'. "
        f"Intenta pasando la URL completa de partsdr.com directamente."
    )


# ------------------------ Parsing del HTML -------------------------------- #

def _looks_like_part_number(text: str) -> bool:
    """Filtra ruido. Acepta strings tipo WB27K10090, AP4980366, 1121940066."""
    t = text.strip().upper()
    if not t or " " in t:
        return False
    if len(t) < 4 or len(t) > 30:
        return False
    if not any(c.isdigit() for c in t):
        return False
    return bool(PART_TOKEN_RE.match(t))


def _collect_tokens_after(node: Tag, stop_headings=("h1", "h2", "h3", "h4")) -> List[str]:
    """
    Desde un nodo de encabezado, recolecta tokens tipo número de parte en los
    siguientes hermanos hasta encontrar otro encabezado del mismo nivel.
    """
    tokens: List[str] = []
    for sib in node.find_next_siblings():
        if sib.name in stop_headings:
            break
        # Cada <li>, <span>, <a>, <td>, <p> puede contener un número
        for el in sib.find_all(["li", "span", "a", "td", "p", "div"]):
            txt = el.get_text(" ", strip=True)
            # Algunos elementos contienen muchos números separados por coma o /
            for piece in re.split(r"[,\s/|·•]+", txt):
                if _looks_like_part_number(piece):
                    tokens.append(piece.upper())
        # También revisamos el texto plano del propio sibling
        for piece in re.split(r"[,\s/|·•]+", sib.get_text(" ", strip=True)):
            if _looks_like_part_number(piece):
                tokens.append(piece.upper())
    # Dedup conservando orden
    seen, out = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def parse_replaces(html: str) -> Dict[str, object]:
    """
    Recorre el HTML buscando encabezados que coincidan con:
      - "<Brand> part numbers"
      - "SKUs and competitor part numbers"
      - "Part production numbers"
    y captura los tokens posteriores.
    """
    soup = BeautifulSoup(html, "lxml")

    # Tomamos el título de la página para tener contexto
    title = soup.title.get_text(strip=True) if soup.title else ""

    result: Dict[str, object] = {
        "page_title": title,
        "brand_part_numbers": {},        # {"GE": [...], "Samsung": [...]}
        "skus_and_competitor_numbers": [],
        "part_production_numbers": [],
    }

    headings = soup.find_all(re.compile(r"^h[1-6]$"))
    for h in headings:
        label = h.get_text(" ", strip=True)
        lower = label.lower()

        # Brand-specific: "GE part numbers", "Samsung part numbers", etc.
        m = re.match(r"^([\w\.\-& ]+?)\s+part numbers?$", lower)
        if m and "sku" not in lower and "production" not in lower:
            brand = m.group(1).strip().title()
            tokens = _collect_tokens_after(h)
            if tokens:
                result["brand_part_numbers"].setdefault(brand, []).extend(tokens)
            continue

        if "sku" in lower and "competitor" in lower:
            result["skus_and_competitor_numbers"].extend(_collect_tokens_after(h))
            continue

        if "production number" in lower:
            result["part_production_numbers"].extend(_collect_tokens_after(h))
            continue

    # Si no detectamos nada por encabezados, fallback: buscar por texto suelto
    if not (
        result["brand_part_numbers"]
        or result["skus_and_competitor_numbers"]
        or result["part_production_numbers"]
    ):
        text = soup.get_text("\n", strip=True)
        result["_warning"] = (
            "No se detectaron secciones por encabezado. "
            "Inspecciona el HTML manualmente; la estructura puede haber cambiado."
        )
        # Como último recurso, extrae cualquier token que parezca part number
        candidates = [t for t in re.split(r"\s+", text) if _looks_like_part_number(t)]
        result["raw_candidates"] = sorted(set(candidates))

    return result


# ------------------------------ CLI --------------------------------------- #

def format_text(data: Dict[str, object]) -> str:
    out = []
    if data.get("page_title"):
        out.append(f"# {data['page_title']}")
        out.append("")

    for brand, nums in (data.get("brand_part_numbers") or {}).items():
        out.append(f"{brand} part numbers")
        out.extend(nums)
        out.append("")

    skus = data.get("skus_and_competitor_numbers") or []
    if skus:
        out.append("SKUs and competitor part numbers")
        out.extend(skus)
        out.append("")

    prod = data.get("part_production_numbers") or []
    if prod:
        out.append("Part production numbers")
        out.extend(prod)
        out.append("")

    if data.get("_warning"):
        out.append(f"[!] {data['_warning']}")
        if data.get("raw_candidates"):
            out.append("Raw candidates encontrados:")
            out.extend(data["raw_candidates"])  # type: ignore[arg-type]

    return "\n".join(out).rstrip() + "\n"


def format_csv(data: Dict[str, object]) -> str:
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["category", "label", "value"])
    for brand, nums in (data.get("brand_part_numbers") or {}).items():
        for n in nums:
            writer.writerow(["brand_part_number", brand, n])
    for n in data.get("skus_and_competitor_numbers") or []:
        writer.writerow(["sku_or_competitor", "", n])
    for n in data.get("part_production_numbers") or []:
        writer.writerow(["production_number", "", n])
    return buf.getvalue()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrapea la sección 'Replaces' de PartsDR."
    )
    parser.add_argument(
        "query",
        help="Número de parte (p. ej. WB27K10090) o URL completa de partsdr.com",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "csv"],
        default="text",
        help="Formato de salida (por defecto: text)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Archivo de salida. Si se omite, escribe a stdout.",
    )
    parser.add_argument(
        "--use-cloudscraper",
        action="store_true",
        help="Usa cloudscraper para evadir Cloudflare si recibes 403.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Segundos de espera antes de la petición (sé amable con el sitio).",
    )

    args = parser.parse_args(argv)

    session = build_session(use_cloudscraper=args.use_cloudscraper)

    if args.delay:
        time.sleep(args.delay)

    try:
        url = resolve_part_url(session, args.query)
        print(f"[i] Fetching: {url}", file=sys.stderr)
        html = fetch(session, url)
    except Exception as exc:
        print(f"[!] Error: {exc}", file=sys.stderr)
        return 1

    data = parse_replaces(html)
    data["source_url"] = url

    if args.format == "json":
        rendered = json.dumps(data, indent=2, ensure_ascii=False)
    elif args.format == "csv":
        rendered = format_csv(data)
    else:
        rendered = format_text(data)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"[i] Guardado en {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
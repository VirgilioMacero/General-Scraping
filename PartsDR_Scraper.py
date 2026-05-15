#!/usr/bin/env python3
"""
PartsDR Replaces Scraper (Playwright)
=====================================
Extrae la sección "Replaces" de una página de parte en partsdr.com:
  - Brand part numbers (GE, Samsung, Whirlpool, etc.)
  - SKUs and competitor part numbers
  - Part production numbers

Usa Playwright + Chromium para renderizar JS y evadir bloqueos anti-bot (403/Cloudflare).

Instalación:
    pip install playwright beautifulsoup4 lxml
    playwright install chromium

Uso:
    python partsdr_scraper.py WB27K10090
    python partsdr_scraper.py https://partsdr.com/part/wd35x35958-hardware-kit
    python partsdr_scraper.py WB27K10090 --format json
    python partsdr_scraper.py WB27K10090 --format json --output out.json
    python partsdr_scraper.py WB27K10090 --headed        # ver el navegador
    python partsdr_scraper.py WB27K10090 --screenshot debug.png

    # Procesar varios part numbers desde un archivo (uno por línea):
    python partsdr_scraper.py --batch parts.txt --format csv --output all.csv
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote_plus

try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    sys.exit("Falta beautifulsoup4. Ejecuta: pip install beautifulsoup4 lxml")

try:
    from playwright.sync_api import (
        Browser,
        Page,
        Playwright,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:
    sys.exit(
        "Falta playwright. Ejecuta:\n"
        "    pip install playwright\n"
        "    playwright install chromium"
    )


BASE_URL = "https://partsdr.com"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Heurística para validar tokens que parecen número de parte.
PART_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\/\.]{3,}$")


# --------------------------- Playwright helpers --------------------------- #

class PartsDrBrowser:
    """Context manager para reutilizar el browser entre múltiples fetches."""

    def __init__(self, headed: bool = False, timeout_ms: int = 30_000) -> None:
        self.headed = headed
        self.timeout_ms = timeout_ms
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self.page: Optional[Page] = None

    def __enter__(self) -> "PartsDrBrowser":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=not self.headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        # Pequeño parche: muchas detecciones miran navigator.webdriver
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self.page = context.new_page()
        self.page.set_default_timeout(self.timeout_ms)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def goto(self, url: str) -> str:
        """Navega y devuelve el HTML renderizado."""
        assert self.page is not None
        self.page.goto(url, wait_until="domcontentloaded")
        # Damos un respiro a contenidos hidratados por JS
        try:
            self.page.wait_for_load_state("networkidle", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        return self.page.content()

    def screenshot(self, path: str) -> None:
        assert self.page is not None
        self.page.screenshot(path=path, full_page=True)


# --------------------------- URL Resolution ------------------------------- #

def resolve_part_url(browser: PartsDrBrowser, query: str) -> str:
    """
    Si `query` es URL, la devuelve.
    Si es un part number, navega a la búsqueda y resuelve la URL canónica /part/.
    """
    if query.startswith("http://") or query.startswith("https://"):
        return query

    search_url = f"{BASE_URL}/search?q={quote_plus(query)}"
    html = browser.goto(search_url)
    assert browser.page is not None

    # Si la búsqueda redirige directamente al producto, la URL final ya es /part/
    final_url = browser.page.url
    if "/part/" in final_url:
        return final_url.split("?")[0]

    # Si no, buscamos en el DOM el primer enlace a /part/
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a[href*='/part/']"):
        href = a.get("href", "")
        if href.startswith("/"):
            href = BASE_URL + href
        if "/part/" in href:
            return href.split("?")[0]

    raise RuntimeError(
        f"No se encontró URL de producto para '{query}'. "
        f"Intenta pasando la URL completa de partsdr.com directamente."
    )


# ------------------------ Parsing del HTML -------------------------------- #

def _looks_like_part_number(text: str) -> bool:
    t = text.strip().upper()
    if not t or " " in t:
        return False
    if len(t) < 4 or len(t) > 30:
        return False
    if not any(c.isdigit() for c in t):
        return False
    return bool(PART_TOKEN_RE.match(t))


def _collect_tokens_after(node: Tag, stop_headings=("h1", "h2", "h3", "h4")) -> List[str]:
    """Desde un encabezado, junta tokens tipo número de parte hasta el siguiente encabezado."""
    tokens: List[str] = []
    for sib in node.find_next_siblings():
        if sib.name in stop_headings:
            break
        for el in sib.find_all(["li", "span", "a", "td", "p", "div"]):
            for piece in re.split(r"[,\s/|·•]+", el.get_text(" ", strip=True)):
                if _looks_like_part_number(piece):
                    tokens.append(piece.upper())
        for piece in re.split(r"[,\s/|·•]+", sib.get_text(" ", strip=True)):
            if _looks_like_part_number(piece):
                tokens.append(piece.upper())

    seen, out = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def parse_replaces(html: str) -> Dict[str, object]:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else ""

    result: Dict[str, object] = {
        "page_title": title,
        "brand_part_numbers": {},
        "skus_and_competitor_numbers": [],
        "part_production_numbers": [],
    }

    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        label = h.get_text(" ", strip=True)
        lower = label.lower()

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
        result["raw_candidates"] = sorted(
            {t for t in re.split(r"\s+", text) if _looks_like_part_number(t)}
        )

    return result


# ------------------------------ Output ------------------------------------ #

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


def format_csv_rows(data: Dict[str, object], query: str, writer) -> None:
    for brand, nums in (data.get("brand_part_numbers") or {}).items():
        for n in nums:
            writer.writerow([query, "brand_part_number", brand, n])
    for n in data.get("skus_and_competitor_numbers") or []:
        writer.writerow([query, "sku_or_competitor", "", n])
    for n in data.get("part_production_numbers") or []:
        writer.writerow([query, "production_number", "", n])


# ------------------------------ CLI --------------------------------------- #

def scrape_one(
    browser: PartsDrBrowser,
    query: str,
    screenshot: Optional[str] = None,
) -> Dict[str, object]:
    url = resolve_part_url(browser, query)
    print(f"[i] Fetching: {url}", file=sys.stderr)
    html = browser.goto(url)
    if screenshot:
        browser.screenshot(screenshot)
        print(f"[i] Screenshot: {screenshot}", file=sys.stderr)
    data = parse_replaces(html)
    data["source_url"] = url
    data["query"] = query
    return data


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrapea la sección 'Replaces' de PartsDR con Playwright."
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Número de parte (WB27K10090) o URL completa de partsdr.com",
    )
    parser.add_argument(
        "--batch",
        metavar="FILE",
        help="Archivo con un part number/URL por línea para procesar en lote.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json", "csv"],
        default="text",
        help="Formato de salida (por defecto: text).",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Archivo de salida. Si se omite, escribe a stdout.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Muestra el navegador (útil para depurar).",
    )
    parser.add_argument(
        "--screenshot",
        metavar="PATH",
        help="Guarda un screenshot de la página final (solo en modo single query).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Timeout en segundos por petición (por defecto: 30).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Espera entre peticiones en modo batch (por defecto: 1.0 s).",
    )

    args = parser.parse_args(argv)

    if not args.query and not args.batch:
        parser.error("Debes pasar un part number/URL o usar --batch FILE.")

    queries: List[str]
    if args.batch:
        path = Path(args.batch)
        if not path.exists():
            print(f"[!] No existe {path}", file=sys.stderr)
            return 1
        queries = [
            ln.strip()
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        if not queries:
            print("[!] El archivo batch está vacío.", file=sys.stderr)
            return 1
    else:
        queries = [args.query]  # type: ignore[list-item]

    results: List[Dict[str, object]] = []

    with PartsDrBrowser(headed=args.headed, timeout_ms=args.timeout * 1000) as browser:
        for i, q in enumerate(queries):
            try:
                data = scrape_one(
                    browser,
                    q,
                    screenshot=args.screenshot if len(queries) == 1 else None,
                )
                results.append(data)
            except Exception as exc:
                print(f"[!] Error con '{q}': {exc}", file=sys.stderr)
                results.append({"query": q, "error": str(exc)})
            if i < len(queries) - 1 and args.delay:
                time.sleep(args.delay)

    # Render
    if args.format == "json":
        payload = results if args.batch else results[0]
        rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    elif args.format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["query", "category", "label", "value"])
        for data in results:
            if "error" in data:
                writer.writerow([data.get("query", ""), "error", "", data["error"]])  # type: ignore[index]
                continue
            format_csv_rows(data, str(data.get("query", "")), writer)
        rendered = buf.getvalue()
    else:
        chunks = []
        for data in results:
            if "error" in data:
                chunks.append(f"# {data.get('query', '')}\n[!] {data['error']}\n")
            else:
                chunks.append(format_text(data))
        rendered = "\n".join(chunks)

    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"[i] Guardado en {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
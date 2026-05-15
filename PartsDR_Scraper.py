#!/usr/bin/env python3
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

# 1. IMPORTACIÓN CORREGIDA
from playwright_stealth import stealth as apply_stealth
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
    sys.exit("Falta playwright. Ejecuta: pip install playwright && playwright install chromium")

BASE_URL = "https://partsdr.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
PART_TOKEN_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\/\.]{3,}$")

class PartsDrBrowser:
    def __init__(self, headed: bool = False, timeout_ms: int = 30_000) -> None:
        self.headed = headed
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self.page = None

    def __enter__(self) -> "PartsDrBrowser":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=not self.headed,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = self._browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
        )
        self.page = context.new_page()
        
        # 2. LLAMADA CORREGIDA (Módulo.Función)
        apply_stealth(self.page)
        
        self.page.set_default_timeout(self.timeout_ms)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._browser: self._browser.close()
        finally:
            if self._pw: self._pw.stop()

    def goto(self, url: str) -> str:
        assert self.page is not None
        time.sleep(2) # Pausa humana
        self.page.goto(url, wait_until="domcontentloaded")
        
        # Espera extra si aparece el bloqueo de Cloudflare
        if "Just a moment" in self.page.content():
            print("[i] Esperando a Cloudflare...", file=sys.stderr)
            time.sleep(6)
            
        try:
            self.page.wait_for_load_state("networkidle", timeout=10_000)
        except:
            pass
        return self.page.content()

    def screenshot(self, path: str) -> None:
        if self.page: self.page.screenshot(path=path, full_page=True)

# --- Funciones de resolución y parseo ---

def resolve_part_url(browser: PartsDrBrowser, query: str) -> str:
    if query.startswith("http"): return query
    search_url = f"{BASE_URL}/search?q={quote_plus(query)}"
    html = browser.goto(search_url)
    if "/part/" in browser.page.url:
        return browser.page.url.split("?")[0]
    soup = BeautifulSoup(html, "lxml")
    for a in soup.select("a[href*='/part/']"):
        href = a.get("href", "")
        if href.startswith("/"): href = BASE_URL + href
        return href.split("?")[0]
    raise RuntimeError(f"No se encontró producto para '{query}'")

def _looks_like_part_number(text: str) -> bool:
    t = text.strip().upper()
    return bool(t and " " not in t and 4 <= len(t) <= 30 and any(c.isdigit() for c in t) and PART_TOKEN_RE.match(t))

def _collect_tokens_after(node: Tag) -> List[str]:
    tokens = []
    for sib in node.find_next_siblings():
        if sib.name in ("h1", "h2", "h3", "h4"): break
        for el in sib.find_all(["li", "span", "a", "td", "p", "div"]):
            for piece in re.split(r"[,\s/|·•]+", el.get_text(" ", strip=True)):
                if _looks_like_part_number(piece): tokens.append(piece.upper())
    return list(dict.fromkeys(tokens))

def parse_replaces(html: str) -> Dict[str, object]:
    soup = BeautifulSoup(html, "lxml")
    result = {"page_title": soup.title.text if soup.title else "", "brand_part_numbers": {}, "skus_and_competitor_numbers": [], "part_production_numbers": []}
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        label = h.get_text(" ", strip=True).lower()
        if "part numbers" in label and "sku" not in label and "production" not in label:
            brand = label.replace("part numbers", "").replace("part number", "").strip().title()
            result["brand_part_numbers"][brand] = _collect_tokens_after(h)
        elif "sku" in label and "competitor" in label:
            result["skus_and_competitor_numbers"].extend(_collect_tokens_after(h))
        elif "production number" in label:
            result["part_production_numbers"].extend(_collect_tokens_after(h))
    
    if not any([result["brand_part_numbers"], result["skus_and_competitor_numbers"], result["part_production_numbers"]]):
        result["_warning"] = "No se detectaron secciones. Posible cambio de estructura."
        result["raw_candidates"] = sorted({t for t in re.split(r"\s+", soup.get_text()) if _looks_like_part_number(t)})
    return result

def format_text(data: Dict[str, object]) -> str:
    out = [f"# {data.get('page_title')}\n"]
    for brand, nums in data.get("brand_part_numbers", {}).items():
        out.append(f"{brand} part numbers:\n" + "\n".join(nums) + "\n")
    if data.get("skus_and_competitor_numbers"):
        out.append("SKUs and competitor numbers:\n" + "\n".join(data["skus_and_competitor_numbers"]) + "\n")
    if data.get("_warning"): out.append(f"WARN: {data['_warning']}")
    return "\n".join(out)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Part number o URL")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    with PartsDrBrowser(headed=args.headed) as browser:
        try:
            url = resolve_part_url(browser, args.query)
            print(f"[i] Analizando: {url}")
            html = browser.goto(url)
            data = parse_replaces(html)
            print(format_text(data))
        except Exception as e:
            print(f"[!] Error: {e}")

if __name__ == "__main__":
    main()
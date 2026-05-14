#!/usr/bin/env python3
"""
Scraper para supco.com -> department 6010.

Recorre todas las paginas (gotopage=1, 2, 3, ...) hasta que ya no
encuentre productos, extrae para cada producto:

    - product_code        (ej. "SSDW7")
    - product_url         (ej. "https://supco.com/web/supco_live/products/SSDW7.html")
    - product_description (ej. "SSDW7--DRAIN HOSE")
    - can_replace         (lista separada por " | " con los codigos "Can replace")
    - can_replace_urls    (URLs correspondientes, mismo orden)

Y guarda todo en `supco_products.csv`.

Uso:
    pip install requests beautifulsoup4 lxml certifi
    python scrape_supco.py
"""

import csv
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------- Configuracion ----------------------------------------------------

LIST_URL_TEMPLATE = (
    "https://supco.com/web/supco_live/product.php"
    "?gotopage={page}"
    "&department=6010"
    "&token=c4e5ba67537ea3e42da15bfa907618743bfbe120036397f0b1e6cbc49b08e17864974a483d5711838ea853564a"
)

OUTPUT_CSV = "supco_products.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}

SLEEP_BETWEEN_REQUESTS = 1.0
MAX_PAGES = 500
MAX_RETRIES = 3

PRODUCT_LINK_RE = re.compile(r"/web/supco_live/products/([^/\"'?#]+)\.html?", re.I)


# ---------- SSL / verify ----------------------------------------------------


def resolve_verify():
    """
    Decide que pasar como `verify=` a requests:
      1) Si `certifi` esta instalado, usa su bundle (lo mejor).
      2) Si no, desactiva la verificacion SSL con un aviso.
    """
    try:
        import certifi
        path = certifi.where()
        print(f"[ssl] usando bundle de certifi: {path}")
        return path
    except ImportError:
        print("[ssl] certifi no instalado. Recomendado:  pip install certifi")

    print("[ssl] AVISO: desactivando verificacion SSL (verify=False).")
    print("      Solo aceptable para scraping de paginas publicas.")
    try:
        from urllib3.exceptions import InsecureRequestWarning
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    except Exception:
        pass
    return False


# ---------- Helpers ---------------------------------------------------------


def fetch(url, session, verify):
    """GET con reintentos. Si hay SSLError, degrada a verify=False."""
    last_exc = None
    current_verify = verify
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=30, verify=current_verify)
            if r.status_code == 200:
                return r.text, current_verify
            print(f"  [warn] HTTP {r.status_code} en {url} (intento {attempt})")
        except requests.exceptions.SSLError as e:
            last_exc = e
            print(f"  [warn] SSLError: {e} (intento {attempt})")
            if current_verify is not False:
                print("  [ssl] degradando a verify=False para el resto de la sesion.")
                current_verify = False
                try:
                    from urllib3.exceptions import InsecureRequestWarning
                    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
                except Exception:
                    pass
        except requests.RequestException as e:
            last_exc = e
            print(f"  [warn] error de red en {url}: {e} (intento {attempt})")
        time.sleep(2 * attempt)
    if last_exc:
        raise last_exc
    return None, current_verify


def normalize_text(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def find_product_rows(soup):
    rows = []
    for tr in soup.find_all("tr"):
        if tr.find("a", href=PRODUCT_LINK_RE):
            rows.append(tr)
    if rows:
        return rows

    seen = set()
    for a in soup.find_all("a", href=PRODUCT_LINK_RE):
        parent = a.find_parent(["li", "div", "tr"])
        if parent is not None and id(parent) not in seen:
            seen.add(id(parent))
            rows.append(parent)
    return rows


def extract_product(row, page_url):
    links = row.find_all("a", href=PRODUCT_LINK_RE)
    if not links:
        return None

    main_a = links[0]
    main_href = urljoin(page_url, main_a.get("href", ""))
    main_code = main_a.get_text(strip=True)
    if not main_code:
        m = PRODUCT_LINK_RE.search(main_a.get("href", ""))
        main_code = m.group(1) if m else ""

    description = ""
    for a in links[1:]:
        href_full = urljoin(page_url, a.get("href", ""))
        if href_full == main_href:
            description = normalize_text(a.get_text(" ", strip=True))
            break
    if not description:
        description = normalize_text(row.get_text(" ", strip=True))

    can_replace = []
    can_replace_urls = []
    for a in links[1:]:
        href_full = urljoin(page_url, a.get("href", ""))
        if href_full == main_href:
            continue
        text = normalize_text(a.get_text(" ", strip=True))
        if not text:
            m = PRODUCT_LINK_RE.search(a.get("href", ""))
            text = m.group(1) if m else ""
        if text and text not in can_replace:
            can_replace.append(text)
            can_replace_urls.append(href_full)

    return {
        "product_code": main_code,
        "product_url": main_href,
        "product_description": description,
        "can_replace": " | ".join(can_replace),
        "can_replace_urls": " | ".join(can_replace_urls),
    }


# ---------- Main ------------------------------------------------------------


def main():
    verify = resolve_verify()
    session = requests.Session()
    all_rows = []
    seen_codes = set()
    empty_streak = 0

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "page",
                "product_code",
                "product_url",
                "product_description",
                "can_replace",
                "can_replace_urls",
            ],
        )
        writer.writeheader()

        for page in range(1, MAX_PAGES + 1):
            url = LIST_URL_TEMPLATE.format(page=page)
            print(f"[page {page}] {url}")

            try:
                html, verify = fetch(url, session, verify)
            except Exception as e:
                print(f"  [error] no se pudo obtener la pagina {page}: {e}")
                empty_streak += 1
                if empty_streak >= 2:
                    print("  [stop] dos paginas fallidas seguidas, terminando.")
                    break
                continue

            if html is None:
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue

            soup = BeautifulSoup(html, "lxml")
            rows = find_product_rows(soup)
            print(f"  -> {len(rows)} filas con link de producto")

            new_in_page = 0
            for row in rows:
                item = extract_product(row, url)
                if not item or not item["product_code"]:
                    continue
                key = (item["product_code"], item["product_url"])
                if key in seen_codes:
                    continue
                seen_codes.add(key)
                writer.writerow({"page": page, **item})
                all_rows.append(item)
                new_in_page += 1

            print(f"  -> {new_in_page} productos nuevos guardados")

            if new_in_page == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    print("  [stop] dos paginas sin productos nuevos, terminando.")
                    break
            else:
                empty_streak = 0

            time.sleep(SLEEP_BETWEEN_REQUESTS)

    print(f"\nListo. {len(all_rows)} productos escritos en {OUTPUT_CSV}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrumpido por el usuario.", file=sys.stderr)
        sys.exit(1)

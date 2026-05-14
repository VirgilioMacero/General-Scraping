"""
Scraper con curl_cffi - imita Chrome a nivel TLS sin abrir navegador.
Uso: python scrape_cffi.py
"""

import re
import sys
from curl_cffi import requests


def scrape(url: str) -> dict:
    print(f"[*] GET {url}")
    
    # impersonate="chrome131" imita huella TLS de Chrome 131
    r = requests.get(url, impersonate="chrome131", timeout=30)
    print(f"[*] Status: {r.status_code}")
    
    with open("debug.html", "w", encoding="utf-8") as f:
        f.write(r.text)
    
    if r.status_code != 200:
        raise Exception(f"HTTP {r.status_code}")
    
    html = r.text
    resultado = {"peso": None, "dimensiones": None}
    
    m1 = re.search(r"Weight\s*:?\s*([\d.]+\s*(?:lbs?|kg|oz))", html, re.I)
    if m1:
        resultado["peso"] = m1.group(1).strip()
    
    m2 = re.search(
        r"Package\s*Dimension\s*:?\s*([\d.]+\s*[xX]\s*[\d.]+\s*[xX]\s*[\d.]+)",
        html, re.I,
    )
    if m2:
        resultado["dimensiones"] = m2.group(1).strip()
    
    return resultado


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.reliableparts.com/gen-wh12x1082.html"
    
    datos = scrape(url)
    print("\n" + "=" * 50)
    print(f"Peso:        {datos['peso'] or 'NO ENCONTRADO'}")
    print(f"Dimensiones: {datos['dimensiones'] or 'NO ENCONTRADO'}")
    print("=" * 50)
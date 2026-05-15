import argparse
import time
import pandas as pd
from playwright.sync_api import sync_playwright, TimeoutError

def scrape_product_details(page, url, timeout):
    """Extrae la información detallada de una página de producto."""
    try:
        page.goto(url, timeout=timeout)
        page.wait_for_load_state("networkidle", timeout=timeout)
    except TimeoutError:
        print(f"  [!] Timeout al cargar: {url}")
        return None

    # Inicializar diccionario de datos
    item_data = {
        "N° de Parte": "N/A",
        "Nombre de la parte": "N/A",
        "Marca": "N/A",
        "Precio": "N/A",
        "Stock": "N/A",
        "Precio Comparativo": "N/A",
        "Cross Reference Information": "N/A",
        "Marcas Compatibles": "N/A",
        "Cantidad de Modelos": 0,
        "Modelos_List": []
    }

    try:
        # === ACTUALIZAR ESTOS SELECTORES SEGÚN EL DOM REAL DE LA PÁGINA ===
        
        # Número de Parte y Nombre
        if page.locator("h1[itemprop='name']").is_visible():
            item_data["Nombre de la parte"] = page.locator("h1[itemprop='name']").inner_text().strip()
        
        if page.locator(".part-number, [itemprop='sku']").is_visible():
            item_data["N° de Parte"] = page.locator(".part-number, [itemprop='sku']").first.inner_text().strip()

        # Marca
        if page.locator("[itemprop='brand']").is_visible():
            item_data["Marca"] = page.locator("[itemprop='brand']").inner_text().strip()

        # Precio y Precio Comparativo
        if page.locator(".price, [itemprop='price']").is_visible():
            item_data["Precio"] = page.locator(".price, [itemprop='price']").first.inner_text().strip()
        
        if page.locator(".retail-price, .msrp").is_visible():
            item_data["Precio Comparativo"] = page.locator(".retail-price, .msrp").first.inner_text().strip()

        # Stock
        if page.locator(".stock-status, .availability").is_visible():
            item_data["Stock"] = page.locator(".stock-status, .availability").first.inner_text().strip()

        # Cross Reference (Reemplazos)
        if page.locator(".cross-reference, #replaces-parts").is_visible():
            item_data["Cross Reference Information"] = page.locator(".cross-reference, #replaces-parts").inner_text().strip()

        # Marcas Compatibles
        if page.locator(".compatible-brands").is_visible():
            item_data["Marcas Compatibles"] = page.locator(".compatible-brands").inner_text().strip()

        # Modelos Compatibles (Puede requerir hacer clic en un botón "View All")
        btn_view_models = page.locator("text='View all models', .view-models-btn")
        if btn_view_models.is_visible():
            btn_view_models.click()
            time.sleep(2) # Esperar a que el modal/lista se despliegue
            
        model_elements = page.locator(".model-list li, .model-table tr td.model-name").all_inner_texts()
        if model_elements:
            # Limpiar textos y quitar vacíos
            clean_models = [m.strip() for m in model_elements if m.strip()]
            item_data["Modelos_List"] = clean_models
            item_data["Cantidad de Modelos"] = len(clean_models)

    except Exception as e:
        print(f"  [!] Error extrayendo datos en {url}: {e}")

    return item_data

def main():
    parser = argparse.ArgumentParser(description="Scraper para AppliancePartsPros")
    parser.add_argument("query", help="Nombre por Categoría (Ej: 'Dishwasher Board')")
    parser.add_argument("--headed", action="store_true", help="Abre el navegador visualmente")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout en segundos")
    args = parser.parse_args()

    timeout_ms = args.timeout * 1000
    search_query = args.query.replace(' ', '+')
    search_url = f"https://www.appliancepartspros.com/search.aspx?q={search_query}"
    
    scraped_data = []

    print(f"[*] Iniciando búsqueda para: '{args.query}'")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(viewport={'width': 1280, 'height': 720})
        page = context.new_page()

        try:
            print(f"[*] Navegando a resultados de búsqueda...")
            page.goto(search_url, timeout=timeout_ms)
            
            # Recopilar enlaces de productos de la página de resultados
            # Ajustar el selector '.product-title a' según el HTML real
            page.wait_for_selector(".product-list, .search-results", timeout=timeout_ms)
            product_links = page.locator("a.product-link, .product-title a").all_get_attribute("href")
            
            # Completar URLs relativas
            base_url = "https://www.appliancepartspros.com"
            full_links = [link if link.startswith("http") else base_url + link for link in product_links]
            
            # Eliminar duplicados
            full_links = list(set(full_links))
            print(f"[*] Se encontraron {len(full_links)} productos. Iniciando extracción...")

            for i, link in enumerate(full_links, 1):
                print(f"  [{i}/{len(full_links)}] Extrayendo: {link}")
                data = scrape_product_details(page, link, timeout_ms)
                if data:
                    scraped_data.append(data)

        except Exception as e:
            print(f"[!] Ocurrió un error general: {e}")
        finally:
            browser.close()

    # ==========================================
    # PROCESAMIENTO Y EXPORTACIÓN A EXCEL
    # ==========================================
    if not scraped_data:
        print("[!] No se obtuvieron datos para exportar.")
        return

    print("[*] Generando archivo Excel...")
    df_main = pd.DataFrame(scraped_data)

    # 1. Hoja 1: Solo números de parte enumerados
    df_partes = df_main[['N° de Parte']].copy()
    df_partes.index = df_partes.index + 1  # Empezar enumeración en 1
    df_partes.index.name = 'Índice'

    # 2. Hoja 2: Toda la información con modelos separados por columnas
    # Encontrar el producto con la mayor cantidad de modelos
    max_models = df_main['Cantidad de Modelos'].max() if not df_main.empty else 0

    # Crear columnas dinámicas para los modelos
    for i in range(max_models):
        col_name = f'Modelo_{i+1}'
        df_main[col_name] = df_main['Modelos_List'].apply(
            lambda x: x[i] if isinstance(x, list) and i < len(x) else None
        )

    # Eliminar la lista cruda para que el Excel quede limpio
    df_main.drop(columns=['Modelos_List'], inplace=True)

    # Ordenar columnas como se solicitó
    column_order = [
        "N° de Parte", "Nombre de la parte", "Marca", "Precio", "Stock", 
        "Precio Comparativo", "Cross Reference Information", "Marcas Compatibles", 
        "Cantidad de Modelos"
    ]
    # Agregar las columnas de modelos generadas
    column_order.extend([f'Modelo_{i+1}' for i in range(max_models)])
    
    # Reordenar el dataframe principal
    df_main = df_main.reindex(columns=column_order)

    # Nombre seguro para la hoja de Excel (máx 31 caracteres, sin caracteres especiales)
    safe_sheet_name = args.query[:31].replace('/', '-').replace('\\', '-')
    excel_filename = f"{args.query.replace(' ', '_')}_Resultados.xlsx"

    # Guardar usando Pandas ExcelWriter
    with pd.ExcelWriter(excel_filename, engine='openpyxl') as writer:
        df_partes.to_excel(writer, sheet_name='N° de Parte')
        df_main.to_excel(writer, sheet_name=safe_sheet_name, index=False)

    print(f"[*] ¡Proceso finalizado con éxito! Archivo guardado como: {excel_filename}")

if __name__ == "__main__":
    raise SystemExit(main())
import requests
from bs4 import BeautifulSoup
from wec_analytics.parser import download_pdf, parse_pdf_bytes
from wec_analytics.db import list_imports, save_import

def sync_bpillar_reports(target_url):
    """Scrapes a URL for PDF links and imports missing ones (Strictly WEC only)."""
    
    existing_imports = list_imports()
    known_urls = {rep["source_url"] for rep in existing_imports if rep.get("source_url")}
    
    headers = {"User-Agent": "WEC-Analytics/0.1"}
    r = requests.get(target_url, headers=headers, timeout=15)
    r.raise_for_status()
    
    soup = BeautifulSoup(r.text, "html.parser")
    
    pdf_links = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        href_lower = href.lower()
        
        if href_lower.endswith(".pdf"):
            
            # --- STRICT WEC FILTER ---
            is_wec = False
            link_text = link.get_text(separator=" ").lower()
            
            # 1. Check if WEC is in the URL or the link's own text
            if "wec" in href_lower or "world endurance championship" in href_lower:
                is_wec = True
            elif "wec" in link_text or "world endurance championship" in link_text:
                is_wec = True
            else:
                # 2. Check ONLY the exact table row (tr) this link belongs to
                row = link.find_parent("tr")
                if row:
                    row_text = row.get_text(separator=" ").lower()
                    if "wec" in row_text or "world endurance championship" in row_text:
                        is_wec = True
            
            if not is_wec:
                continue  # Skip this link, it's not WEC
            # -------------------------

            if href.startswith("/"):
                from urllib.parse import urljoin
                href = urljoin(target_url, href)
            pdf_links.append(href)
            
    results = {"success": [], "failed": [], "skipped": 0}
    
    for url in set(pdf_links):
        if url in known_urls:
            results["skipped"] += 1
            continue
            
        try:
            name, data = download_pdf(url)
            parsed = parse_pdf_bytes(data, name)
            parsed["source_url"] = url
            save_import(parsed)
            results["success"].append(name)
        except Exception as e:
            results["failed"].append(f"{url}: {str(e)}")
            
    return results
import io, re, ipaddress, socket
from urllib.parse import urlparse
import requests
import fitz

REPORT_PATTERNS = [
    ("Driver Performance", ["DRIVER PERFORMANCE REPORT", "DRIVER PERFORMANCE"]),
    ("Lap Analysis", ["LAP ANALYSIS"]),
    ("Lap Chart", ["LAP CHART"]),
    ("Pit Stop Analysis", ["PIT STOP ANALYSIS", "PIT STOP"]),
    ("Classification", ["CLASSIFICATION", "FINAL CLASSIFICATION"]),
    ("Sector Times", ["SECTOR ANALYSIS", "SECTOR TIMES"]),
]

def _safe_https_url(url):
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise ValueError("Only HTTPS URLs are accepted.")
    try:
        for item in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(item[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("The URL resolves to a non-public address and was blocked.")
    except socket.gaierror:
        raise ValueError("Could not resolve the URL host.")

def download_pdf(url):
    _safe_https_url(url)
    r = requests.get(url, timeout=25, headers={"User-Agent": "WEC-Analytics/0.1"})
    r.raise_for_status()
    data = r.content
    if not data.startswith(b"%PDF"):
        raise ValueError("The URL did not return a PDF file.")
    name = urlparse(url).path.rsplit("/", 1)[-1] or "website_import.pdf"
    return name, data

def parse_pdf_bytes(data, filename):
    doc = fitz.open(stream=data, filetype="pdf")
    pages = [p.get_text("text") for p in doc]
    text = "\n".join(pages)
    upper = text.upper()
    report_type = "Other / unclassified"
    for label, terms in REPORT_PATTERNS:
        if any(t in upper for t in terms):
            report_type = label
            break
    event = "Unknown event"
    m = re.search(r"(?:202\d\s+)?(?:FIA\s+)?WORLD ENDURANCE CHAMPIONSHIP\s*[-–:]?\s*([^\n;]+)", text, re.I)
    if m:
        event = re.sub(r"\s+", " ", m.group(1)).strip(" -;")
    if event == "Unknown event":
        m = re.search(r"(?:6|8|10|24)\s+HOURS?\s+OF\s+[A-ZÀ-ÿ ]+", text, re.I)
        if m:
            event = re.sub(r"\s+", " ", m.group(0)).title()
    season_match = re.search(r"\b(20\d{2})\b", text)
    season = season_match.group(1) if season_match else ""
    series = "WEC" if ("WORLD ENDURANCE" in upper or "FIA WEC" in upper) else "Unknown"
    summary = f"{len(doc)} page(s); {len(text):,} extracted characters. Report type: {report_type}."
    return {
        "filename": filename, "series": series, "season": season, "event": event,
        "report_type": report_type, "summary": summary, "text": text
    }

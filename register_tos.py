"""
Register all data source ToS URLs with the legal compliance system.
Run once to seed legal_compliance.db, then schedule weekly.

Usage: cd C:\\Users\\johnk\\graphfinder-clean && python -m register_tos
"""
import requests, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'webapp'))
from pathlib import Path
from webapp.legal_compliance import register_obligation, check_for_changes

DATA_DIR = Path(__file__).parent / "webapp" / "data"
DB_PATH = str(DATA_DIR / "legal_compliance.db")

SOURCES = {
    "SEC.gov (EDGAR/IAPD)": "https://www.sec.gov/privacy.htm",
    "Wikipedia (Wikimedia Foundation)": "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
    "GDELT Project": "https://www.gdeltproject.org/about.html",
    "IRS.gov (TEOS)": "https://www.irs.gov/help/irs-website-terms-and-conditions",
    "FEC.gov": "https://www.fec.gov/website-privacy-and-security-policy/",
    "GitHub API": "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service",
    "OpenSecrets.org": "https://www.opensecrets.org/terms-of-service",
    "LittleSis": "https://littlesis.org/terms-of-service",
    "Wikidata (Wikimedia)": "https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use",
}

def main():
    for name, url in SOURCES.items():
        print(f"\n=== {name} ===")
        print(f"  URL: {url}")
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "SixDegrees.net/1.0"})
            r.raise_for_status()
            text = r.text

            change = check_for_changes(DB_PATH, url, text)
            if change:
                register_obligation(DB_PATH, url, text, {"source_name": name})
                print(f"  ✅ Registered (status: {change})")
            else:
                print(f"  ➖ No change since last check")
        except Exception as e:
            print(f"  ❌ Failed: {e}")

    print("\nDone. Run this script weekly to detect ToS changes.")

if __name__ == "__main__":
    main()

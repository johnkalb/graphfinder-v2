import time
import sqlite3
import json
import requests
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from src.config import SEC_USER_AGENT, SEC_RATE_LIMIT_PER_SEC
from src.utils.logger import logger


class Form4Client:
    """Parses SEC Form 4 (insider transaction reports) XML and extracts
    issuer → insider relationships.

    Raw Form 4 XML is fetched WITHOUT the xsl stylesheet prefix:
      https://www.sec.gov/Archives/edgar/data/{CIK}/{ACC}/form4.xml
    """

    def __init__(self, db_manager, cache_db_path=None):
        self.db = db_manager
        self.headers = {"User-Agent": SEC_USER_AGENT}
        self.last_req_time = 0.0
        self.cache_db_path = cache_db_path or self.db.db_path

        # Ensure form4_cache table exists
        conn = sqlite3.connect(self.cache_db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS form4_cache (
                url TEXT PRIMARY KEY,
                xml_content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _wait_for_rate_limit(self):
        min_spacing = 1.0 / SEC_RATE_LIMIT_PER_SEC
        elapsed = time.time() - self.last_req_time
        if elapsed < min_spacing:
            time.sleep(min_spacing - elapsed)
        self.last_req_time = time.time()

    # ------------------------------------------------------------------
    # XML cache helpers
    # ------------------------------------------------------------------

    def _get_cached_xml(self, url):
        conn = sqlite3.connect(self.cache_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT xml_content FROM form4_cache WHERE url = ?", (url,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def _save_xml_cache(self, url, xml_content):
        conn = sqlite3.connect(self.cache_db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO form4_cache (url, xml_content) VALUES (?, ?)",
            (url, xml_content),
        )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Namespace stripping
    # ------------------------------------------------------------------

    def strip_namespace(self, root):
        for elem in root.iter():
            if "}" in elem.tag:
                elem.tag = elem.tag.split("}", 1)[1]
        return root

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def parse_raw_xml(self, xml_content):
        """Parse raw form4.xml content.

        Returns a list of dicts, each describing one insider relationship:
            {
                "issuer_cik": str,
                "issuer_name": str,
                "owner_cik": str,
                "owner_name": str,
                "is_director": bool,
                "is_officer": bool,
                "is_ten_percent": bool,
                "officer_title": str or None,
            }

        Raises XML parsing errors.
        """
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.error(f"XML parsing error: {e}")
            raise

        root = self.strip_namespace(root)

        doc = root.find(".//ownershipDocument")
        if doc is None:
            doc = root  # fallback: root is ownershipDocument

        # --- Issuer ---
        issuer = doc.find("issuer")
        if issuer is None:
            logger.warning("No <issuer> element found in Form 4 XML")
            return []

        issuer_cik_el = issuer.find("issuerCik")
        issuer_name_el = issuer.find("issuerName")
        issuer_cik = (
            issuer_cik_el.text.strip() if issuer_cik_el is not None and issuer_cik_el.text else ""
        )
        issuer_name = (
            issuer_name_el.text.strip()
            if issuer_name_el is not None and issuer_name_el.text
            else ""
        )

        if not issuer_cik or not issuer_name:
            logger.warning("Incomplete issuer information in Form 4 XML")
            return []

        # --- Reporting owner ---
        owner = doc.find("reportingOwner")
        if owner is None:
            logger.warning("No <reportingOwner> element found in Form 4 XML")
            return []

        owner_id = owner.find("reportingOwnerId")
        owner_rel = owner.find("reportingOwnerRelationship")

        owner_cik = ""
        owner_name = ""
        if owner_id is not None:
            cik_el = owner_id.find("rptOwnerCik")
            name_el = owner_id.find("rptOwnerName")
            if cik_el is not None and cik_el.text:
                owner_cik = cik_el.text.strip()
            if name_el is not None and name_el.text:
                owner_name = name_el.text.strip()

        if not owner_name:
            logger.warning("No reporting owner name found in Form 4 XML")
            return []

        # --- Roles ---
        is_director = False
        is_officer = False
        is_ten_percent = False
        officer_title = None

        if owner_rel is not None:
            for child in owner_rel:
                tag = child.tag
                text = (child.text or "").strip()
                if tag == "isDirector" and text in ("1", "true"):
                    is_director = True
                elif tag == "isOfficer" and text in ("1", "true"):
                    is_officer = True
                elif tag == "isTenPercentOwner" and text in ("1", "true"):
                    is_ten_percent = True
                elif tag == "officerTitle" and text:
                    officer_title = text

        # Always return at least one result; even if no flags are set the
        # person has filed a Form 4 so they are an insider of some kind.
        return [
            {
                "issuer_cik": issuer_cik,
                "issuer_name": issuer_name,
                "owner_cik": owner_cik,
                "owner_name": owner_name,
                "is_director": is_director,
                "is_officer": is_officer,
                "is_ten_percent": is_ten_percent,
                "officer_title": officer_title,
            }
        ]

    # ------------------------------------------------------------------
    # Single-filing processor
    # ------------------------------------------------------------------

    def process_form4(self, cik, accession_number, source_data="SEC_FORM_4"):
        """Download a single Form 4 XML, parse it, and insert relationships.

        Parameters
        ----------
        cik : str
            Issuer CIK (may be padded or unpadded).
        accession_number : str
            SEC accession number (may include dashes).
        source_data : str
            Value for the relationships.source_data column.

        Returns
        -------
        int
            Number of relationships inserted.
        """
        cik_clean = str(cik).strip()
        if not cik_clean.isdigit():
            raise ValueError(f"Invalid CIK: {cik_clean}. Must be numeric.")

        # URL construction: unpadded CIK, raw accession (no dashes), plain form4.xml
        cik_unpadded = str(int(cik_clean))
        acc_clean = str(accession_number).strip().replace("-", "")

        # Use the SEC's .txt filing which always contains the raw XML
        acc_dashed = str(accession_number).strip()
        url = f"https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{acc_clean}/{acc_dashed}.txt"

        xml_content = self._get_cached_xml(url)
        if not xml_content:
            self._wait_for_rate_limit()
            logger.info(f"Fetching Form 4 data from {url}")
            try:
                res = requests.get(url, headers=self.headers, timeout=15)
                if res.status_code == 404:
                    logger.warning(f"Form 4 filing not found at {url}")
                    return 0
                res.raise_for_status()
                xml_content = res.text
                self._save_xml_cache(url, xml_content)
            except Exception as e:
                logger.error(f"Failed to fetch Form 4 data: {e}")
                raise
        
        # Extract the XML section from the .txt filing
        xml_match = re.search(r'<XML>(.*?)</XML>', xml_content, re.DOTALL)
        if not xml_match:
            logger.warning(f"No <XML> section found in filing {acc_dashed}")
            return 0
        
        raw_xml = xml_match.group(1).strip()
        # Remove XML declaration if present
        raw_xml = re.sub(r'<\?xml[^>]*\?>', '', raw_xml).strip()
        xml_content = '<ownershipDocument>' + raw_xml + '</ownershipDocument>'

        # Parse
        parsed = self.parse_raw_xml(xml_content)
        if not parsed:
            return 0

        relationships_added = 0
        for entry in parsed:
            issuer_cik_padded = (
                entry["issuer_cik"].strip().zfill(10) if entry["issuer_cik"] else None
            )

            # Derive relationship type(s)
            if entry["is_director"]:
                rel = "DIRECTOR"
            elif entry["is_officer"]:
                rel = "OFFICER"
            elif entry["is_ten_percent"]:
                rel = "TEN_PERCENT_OWNER"
            else:
                # Fallback: if officerTitle is present, use "OFFICER"
                if entry.get("officer_title"):
                    rel = "OFFICER"
                else:
                    rel = "INSIDER"

            # If officerTitle is available, append it as a detail
            relation_value = rel
            if entry.get("officer_title"):
                relation_value = f"{rel} ({entry['officer_title']})"

            self.db.add_relationship(
                src_id=None,
                src_name=entry["owner_name"],
                src_type="PERSON",
                tgt_id=issuer_cik_padded,
                tgt_name=entry["issuer_name"],
                tgt_type="COMPANY",
                relation=relation_value,
                source_data=source_data,
            )
            relationships_added += 1

        return relationships_added

    # ------------------------------------------------------------------
    # Bulk scanner
    # ------------------------------------------------------------------

    def scan_and_process_form4(
        self, limit=None, source_data="SEC_FORM_4"
    ):
        """Scan the sec_cache table for Form 4 filings, deduplicate by
        company (take the 3 most recent per company), download, parse,
        and insert relationships.

        Parameters
        ----------
        limit : int or None
            Maximum total filings to process across all companies.
        source_data : str
            Value for the relationships.source_data column.

        Returns
        -------
        dict
            {"processed_filings": int, "relationships_added": int}
        """
        logger.info("Scanning sec_cache table for Form 4 filings...")

        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sec_cache'"
        )
        if not cursor.fetchone():
            logger.warning("sec_cache table does not exist in the database.")
            conn.close()
            return {"processed_filings": 0, "relationships_added": 0}

        cursor.execute("SELECT cik, data FROM sec_cache")
        rows = cursor.fetchall()
        conn.close()

        logger.info(f"Loaded {len(rows)} records from sec_cache. Identifying Form 4 filings...")

        # Collect all Form 4 filings grouped by CIK
        # Each entry: (cik, accession_number, filing_date_index)
        company_filings = defaultdict(list)

        for cik, data_json in rows:
            try:
                data = json.loads(data_json)
            except Exception as e:
                logger.error(f"Failed to parse JSON for CIK {cik}: {e}")
                continue

            recent = data.get("filings", {}).get("recent", {})
            if not recent or "form" not in recent:
                continue

            forms = recent["form"]
            acc_nums = recent.get("accessionNumber", [])
            # filingDate is optional but helps with ordering
            filing_dates = recent.get("filingDate", [])

            for i, form in enumerate(forms):
                if form == "4":
                    acc = acc_nums[i] if i < len(acc_nums) else None
                    if not acc:
                        continue
                    date = filing_dates[i] if i < len(filing_dates) else ""
                    company_filings[cik].append(
                        {
                            "accession_number": acc,
                            "date": date,
                            "index": i,
                        }
                    )

        logger.info(
            f"Found Form 4 filings for {len(company_filings)} companies."
        )

        # Deduplicate: take all filings per company (no limit)
        filings_to_process = []
        for cik, filings in company_filings.items():
            for f in filings:
                filings_to_process.append(
                    {
                        "cik": cik,
                        "accession_number": f["accession_number"],
                    }
                )

        # Apply global limit
        if limit is not None and len(filings_to_process) > limit:
            logger.info(
                f"Limiting processing to {limit} filings (from {len(filings_to_process)} total)"
            )
            filings_to_process = filings_to_process[:limit]

        logger.info(f"Will process {len(filings_to_process)} Form 4 filings.")

        total_processed = 0
        total_relationships = 0

        for f in filings_to_process:
            try:
                added = self.process_form4(
                    cik=f["cik"],
                    accession_number=f["accession_number"],
                    source_data=source_data,
                )
                if added > 0:
                    total_relationships += added
                total_processed += 1
            except Exception as e:
                logger.error(
                    f"Error processing Form 4 for CIK {f['cik']}, "
                    f"Accession {f['accession_number']}: {e}"
                )

        logger.info(
            f"Completed Form 4 scan. Processed {total_processed} filings, "
            f"added {total_relationships} relationships."
        )
        return {
            "processed_filings": total_processed,
            "relationships_added": total_relationships,
        }

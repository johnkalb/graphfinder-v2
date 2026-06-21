import time
import sqlite3
import json
import requests
import xml.etree.ElementTree as ET
from src.config import SEC_USER_AGENT, SEC_RATE_LIMIT_PER_SEC
from src.utils.logger import logger


class ADVClient:
    """
    SEC Form ADV (IAPD) client that extracts VC/PE General Partners,
    Managing Directors, and Executive Officers from Schedule A/B data.
    """

    IAPD_SEARCH_URL = "https://api.brokercheck.finra.org/search/firm"
    IAPD_FIRM_URL = "https://api.brokercheck.finra.org/firm"

    def __init__(self, db_manager=None):
        self.db = db_manager
        self.headers = {
            "User-Agent": SEC_USER_AGENT,
            "Accept": "application/json",
        }
        self.last_req_time = 0.0

        # Ensure adv_cache table exists for storing raw API/XML responses
        if self.db:
            conn = sqlite3.connect(self.db.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS adv_cache (
                    key TEXT PRIMARY KEY,
                    data TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            conn.close()

    def _wait_for_rate_limit(self):
        """Enforce SEC rate limit (10 requests/sec = 100ms minimum spacing)."""
        min_spacing = 1.0 / SEC_RATE_LIMIT_PER_SEC
        elapsed = time.time() - self.last_req_time
        if elapsed < min_spacing:
            time.sleep(min_spacing - elapsed)
        self.last_req_time = time.time()

    def _get_cached(self, key):
        """Retrieve cached data by key from adv_cache table."""
        if not self.db:
            return None
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM adv_cache WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return json.loads(row[0]) if row else None

    def _save_cache(self, key, data):
        """Save data to adv_cache table."""
        if not self.db:
            return
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO adv_cache (key, data) VALUES (?, ?)",
            (key, json.dumps(data)),
        )
        conn.commit()
        conn.close()

    # ----------------------------------------------------------------
    # Schedule A XML Parsing (for SEC Form ADV XML feed / compilation)
    # ----------------------------------------------------------------

    def parse_schedule_a(self, xml_content):
        """
        Parse Schedule A from Form ADV XML content.

        Returns a list of owner/officer dicts with keys:
            name, title, crd, firm_name
        """
        root = ET.fromstring(xml_content)

        # Extract firm name from various possible locations
        firm_name = "Unknown VC Firm"
        for tag in ("OrganizationName", "FirmName", "LegalName"):
            elem = root.find(f".//{tag}")
            if elem is not None and elem.text:
                firm_name = elem.text.strip()
                break

        owners = []
        for owner in root.findall(".//ScheduleA/Owner"):
            name = ""
            title = ""
            crd = ""

            name_elem = owner.find("Name")
            if name_elem is not None and name_elem.text:
                name = name_elem.text.strip()

            title_elem = owner.find("Title")
            if title_elem is not None and title_elem.text:
                title = title_elem.text.strip()

            crd_elem = owner.find("CRD")
            if crd_elem is not None and crd_elem.text:
                crd = crd_elem.text.strip()

            if not name:
                # Try alternate naming: IndividualName or FullName
                for alt_tag in ("IndividualName", "FullName"):
                    alt = owner.find(alt_tag)
                    if alt is not None and alt.text:
                        name = alt.text.strip()
                        break

            if name:
                owners.append({
                    "name": name,
                    "title": title,
                    "crd": crd,
                    "firm_name": firm_name,
                })

        return owners

    # ----------------------------------------------------------------
    # IAPD JSON API methods
    # ----------------------------------------------------------------

    def search_firm(self, firm_name, rows=20):
        """
        Search the IAPD API for a firm by name.

        Returns list of firm dicts with keys:
            crd_number, legal_name, status, sec_registered
        """
        # Check cache first
        cache_key = f"search:{firm_name}:{rows}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.debug(f"Cache hit for IAPD search: {firm_name}")
            return cached

        self._wait_for_rate_limit()
        params = {"q": firm_name, "rows": rows}
        logger.info(f"Searching IAPD API for firm: {firm_name}")

        try:
            res = requests.get(
                self.IAPD_SEARCH_URL,
                headers=self.headers,
                params=params,
                timeout=15,
            )
            if res.status_code == 404:
                logger.warning(f"No IAPD results for: {firm_name}")
                return []
            res.raise_for_status()
            data = res.json()
        except requests.RequestException as e:
            logger.error(f"IAPD search failed for '{firm_name}': {e}")
            raise

        results = self._parse_search_results(data)
        self._save_cache(cache_key, results)
        return results

    def _parse_search_results(self, json_data):
        """
        Convert IAPD search API JSON into a list of firm summary dicts.
        """
        firms = []
        hits = json_data.get("hits", {}).get("hits", []) if isinstance(json_data, dict) and "hits" in json_data else []

        if not hits and isinstance(json_data, dict):
            # Try alternate response shape: results array
            hits = json_data.get("results", json_data.get("firms", []))

        for hit in hits:
            source = hit.get("_source", hit) if isinstance(hit, dict) else {}
            crd = source.get("crd_number", source.get("crd", source.get("firmId", "")))
            name = source.get("legal_name", source.get("firm_name", source.get("name", "")))
            status = source.get("status", source.get("registration_status", ""))
            sec_reg = source.get("sec_registered", source.get("secRegistered", False))

            if crd or name:
                firms.append({
                    "crd_number": str(crd),
                    "legal_name": name,
                    "status": status,
                    "sec_registered": sec_reg,
                })

        return firms

    def get_firm_details(self, crd_number):
        """
        Retrieve detailed firm info (including Schedule A / ownership data)
        from the IAPD API by CRD number.

        Returns a dict with firm_name, crd_number, and owners list.
        """
        cache_key = f"firm:{crd_number}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.debug(f"Cache hit for IAPD firm details: CRD {crd_number}")
            return cached

        self._wait_for_rate_limit()
        url = f"{self.IAPD_FIRM_URL}/{crd_number}"
        logger.info(f"Fetching IAPD firm details for CRD: {crd_number}")

        try:
            res = requests.get(url, headers=self.headers, timeout=15)
            if res.status_code == 404:
                logger.warning(f"Firm CRD {crd_number} not found in IAPD.")
                return {"firm_name": "", "crd_number": crd_number, "owners": []}
            res.raise_for_status()
            data = res.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch IAPD firm details for CRD {crd_number}: {e}")
            raise

        result = self._parse_firm_details(data, crd_number)
        self._save_cache(cache_key, result)
        return result

    def _parse_firm_details(self, json_data, crd_number):
        """
        Convert IAPD firm detail JSON into a structured dict with owners/control persons.

        The IAPD API returns ownership/control person data that maps to
        Form ADV Schedule A (Direct Owners and Executive Officers).
        """
        if not isinstance(json_data, dict):
            return {"firm_name": "", "crd_number": crd_number, "owners": []}

        firm_name = json_data.get(
            "legal_name",
            json_data.get("firm_name", json_data.get("name", "")),
        )

        owners = []

        # Extract from various possible owner/control person arrays
        # IAPD API may return these under different keys
        for owners_key in (
            "direct_owners", "control_persons", "executive_officers",
            "schedule_a", "owners", "management",
        ):
            owner_list = json_data.get(owners_key, [])
            if isinstance(owner_list, list):
                for item in owner_list:
                    if isinstance(item, dict):
                        name = item.get(
                            "name",
                            item.get("full_name", item.get("individual_name", "")),
                        )
                        title = item.get(
                            "title",
                            item.get("position_title", item.get("position", "")),
                        )
                        crd = item.get(
                            "crd",
                            item.get("crd_number", item.get("individual_crd", "")),
                        )
                        if name:
                            owners.append({
                                "name": str(name).strip(),
                                "title": str(title).strip() if title else "",
                                "crd": str(crd).strip() if crd else "",
                                "firm_name": firm_name,
                            })

        return {
            "firm_name": firm_name,
            "crd_number": str(crd_number),
            "owners": owners,
        }

    # ----------------------------------------------------------------
    # High-level pipeline: search, fetch, parse, and persist
    # ----------------------------------------------------------------

    def process_firm(self, firm_name):
        """
        Full pipeline: search IAPD for a VC firm by name, fetch its details,
        extract owners/officers from Schedule A data, and persist to SQLite.

        Returns dict with:
            processed_firms (int): number of firms processed
            total_owners (int): total owners/officers extracted
        """
        if not self.db:
            logger.warning("No DB manager provided — relationships will not be persisted.")
            results = self.search_firm(firm_name)
            total_owners = 0
            processed_firms = 0
            for firm in results:
                crd = firm.get("crd_number", "")
                if crd:
                    details = self.get_firm_details(crd)
                    total_owners += len(details.get("owners", []))
                    processed_firms += 1
            return {"processed_firms": processed_firms, "total_owners": total_owners}

        logger.info(f"Processing firm: {firm_name}")

        # Step 1: Search for the firm
        firms = self.search_firm(firm_name)
        if not firms:
            logger.warning(f"No firm found for: {firm_name}")
            return {"processed_firms": 0, "total_owners": 0}

        total_owners_added = 0
        firms_processed = 0

        for firm in firms:
            crd = firm.get("crd_number", "")
            legal_name = firm.get("legal_name", firm_name)

            if not crd:
                logger.debug(f"Skipping firm without CRD: {legal_name}")
                continue

            # Step 2: Get firm details including owners
            details = self.get_firm_details(crd)
            owners = details.get("owners", [])

            # Step 3: Insert relationships into database
            for owner in owners:
                person_name = owner["name"]
                title = owner["title"]
                owner_crd = owner.get("crd", "")

                # Determine relation type based on title keywords
                relation_type = self._title_to_relation_type(title)

                # Add relation: Person -> Firm (as GENERAL_PARTNER / EXECUTIVE / OTHER_CONTROL)
                self.db.add_relationship(
                    src_id=owner_crd or None,
                    src_name=person_name,
                    src_type="PERSON",
                    tgt_id=crd,
                    tgt_name=legal_name,
                    tgt_type="VC_FIRM",
                    relation=relation_type,
                    source_data="SEC_ADV",
                )
                total_owners_added += 1

            firms_processed += 1
            logger.info(
                f"Added {len(owners)} owners for {legal_name} (CRD: {crd})"
            )

        logger.info(
            f"Completed processing '{firm_name}': "
            f"{firms_processed} firms, {total_owners_added} owners"
        )
        return {"processed_firms": firms_processed, "total_owners": total_owners_added}

    @staticmethod
    def _title_to_relation_type(title):
        """
        Map a title string to a relationship type constant.

        Order matters: more specific patterns checked before general ones.
        e.g., "Vice President" must be checked for "vice president" / "vp"
        before checking for "president" (which would incorrectly map to CEO).
        """
        title_lower = title.lower().strip()

        if any(kw in title_lower for kw in ("general partner", "gp", "managing partner")):
            return "GENERAL_PARTNER"
        elif any(kw in title_lower for kw in ("managing director", "md")):
            return "MANAGING_DIRECTOR"
        # Check vp / vice president before president
        elif any(kw in title_lower for kw in ("vice president", "vp")):
            return "EXECUTIVE_OFFICER"
        elif any(kw in title_lower for kw in ("partner",)):
            return "MANAGING_DIRECTOR"
        elif any(kw in title_lower for kw in ("ceo", "chief executive", "president")):
            return "CEO"
        elif any(kw in title_lower for kw in ("cfo", "chief financial")):
            return "CFO"
        elif any(kw in title_lower for kw in ("coo", "chief operating")):
            return "COO"
        elif any(kw in title_lower for kw in ("executive", "officer")):
            return "EXECUTIVE_OFFICER"
        elif any(kw in title_lower for kw in ("director", "board")):
            return "DIRECTOR"
        elif any(kw in title_lower for kw in ("owner", "member", "principal")):
            return "CONTROL_PERSON"
        else:
            return "OTHER_CONTROL"

    def parse_and_ingest_xml(self, xml_content, crd_number="", firm_name="", source_data="SEC_ADV"):
        """
        Parse Schedule A from raw XML content and directly insert
        relationships into the database.

        This is useful when working with the SEC Form ADV bulk XML feed
        rather than the IAPD JSON API.
        """
        owners = self.parse_schedule_a(xml_content)
        if not owners:
            logger.warning("No owners found in Schedule A XML.")
            return 0

        effective_firm_name = firm_name or (owners[0].get("firm_name", "") if owners else "Unknown VC Firm")

        inserted = 0
        for owner in owners:
            relation_type = self._title_to_relation_type(owner.get("title", ""))
            if self.db:
                self.db.add_relationship(
                    src_id=owner.get("crd", ""),
                    src_name=owner["name"],
                    src_type="PERSON",
                    tgt_id=crd_number,
                    tgt_name=effective_firm_name,
                    tgt_type="VC_FIRM",
                    relation=relation_type,
                    source_data=source_data,
                )
                inserted += 1

        return inserted

import time
import sqlite3
import json
import requests
import xml.etree.ElementTree as ET
from src.config import SEC_USER_AGENT, SEC_RATE_LIMIT_PER_SEC
from src.utils.logger import logger

class FormDClient:
    def __init__(self, db_manager):
        self.db = db_manager
        self.headers = {"User-Agent": SEC_USER_AGENT}
        self.last_req_time = 0.0

        # Ensure form_d_cache table exists
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS form_d_cache (
                url TEXT PRIMARY KEY,
                xml_content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()

    def _wait_for_rate_limit(self):
        # 10 requests per second = 100ms minimum spacing
        min_spacing = 1.0 / SEC_RATE_LIMIT_PER_SEC
        elapsed = time.time() - self.last_req_time
        if elapsed < min_spacing:
            time.sleep(min_spacing - elapsed)
        self.last_req_time = time.time()

    def _get_cached_xml(self, url):
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT xml_content FROM form_d_cache WHERE url = ?", (url,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def _save_xml_cache(self, url, xml_content):
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO form_d_cache (url, xml_content) VALUES (?, ?)", (url, xml_content))
        conn.commit()
        conn.close()

    def strip_namespace(self, root):
        for elem in root.iter():
            if '}' in elem.tag:
                elem.tag = elem.tag.split('}', 1)[1]
        return root

    def parse_raw_xml(self, xml_content):
        """
        Parses raw Form D XML content, strips namespaces, and extracts details
        about the issuer and related persons.
        """
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            logger.error(f"XML parsing error: {e}")
            raise

        # Strip namespaces for easier querying
        root = self.strip_namespace(root)

        issuer_elem = root.find(".//primaryIssuer")
        if issuer_elem is None:
            logger.warning("No <primaryIssuer> element found in XML")
            return []

        # Extract issuer details
        entity_name_elem = issuer_elem.find(".//entityName")
        cik_elem = issuer_elem.find(".//cik")

        issuer_name = entity_name_elem.text.strip() if entity_name_elem is not None and entity_name_elem.text else ""
        issuer_cik = cik_elem.text.strip() if cik_elem is not None and cik_elem.text else ""

        parsed_persons = []
        # Find all related person info elements
        related_persons = root.findall(".//relatedPersonInfo")
        for p in related_persons:
            # Support both actual SEC XML schema and test mock format
            name_elem = p.find(".//relatedPersonName")
            if name_elem is not None:
                first_name_elem = name_elem.find(".//firstName")
                last_name_elem = name_elem.find(".//lastName")
            else:
                first_name_elem = p.find(".//firstName")
                last_name_elem = p.find(".//lastName")

            first_name = first_name_elem.text.strip() if first_name_elem is not None and first_name_elem.text else ""
            last_name = last_name_elem.text.strip() if last_name_elem is not None and last_name_elem.text else ""
            full_name = f"{first_name} {last_name}".strip()

            if not full_name:
                continue

            roles = []
            
            # Support actual SEC Form D XML list format
            rel_list = p.find(".//relatedPersonRelationshipList")
            if rel_list is not None:
                for rel in rel_list.findall("relationship"):
                    if rel.text:
                        roles.append(rel.text.strip())
            
            # Support test mock relationshipDetails format
            rel_details = p.find(".//relationshipDetails")
            if rel_details is not None:
                for child in rel_details:
                    if child.text and child.text.strip().lower() in ["true", "1"]:
                        roles.append(child.tag)

            parsed_persons.append({
                "name": full_name,
                "issuer_name": issuer_name,
                "issuer_cik": issuer_cik,
                "roles": roles
            })

        return parsed_persons

    def process_form_d(self, cik, accession_number, primary_document=None, source_data="SEC_D"):
        """
        Downloads Form D XML, parses it, and inserts relationships into SQLite.
        """
        cik_clean = str(cik).strip()
        if not cik_clean.isdigit():
            raise ValueError(f"Invalid CIK: {cik_clean}. Must be numeric.")
        
        # Remove leading zeros to construct the unpadded CIK for SEC Archives path
        cik_unpadded = str(int(cik_clean))
        acc_clean = str(accession_number).strip().replace("-", "")
        doc = primary_document if primary_document else "primary_doc.xml"

        # Strip any stylesheet or directory prefixes (e.g. "xslFormDX01/primary_doc.xml" -> "primary_doc.xml")
        if doc and "/" in doc:
            doc = doc.split("/")[-1]

        url = f"https://www.sec.gov/Archives/edgar/data/{cik_unpadded}/{acc_clean}/{doc}"

        # Check database cache first
        xml_content = self._get_cached_xml(url)
        if xml_content:
            logger.debug(f"Cache hit for Form D XML: {url}")
        else:
            self._wait_for_rate_limit()
            logger.info(f"Fetching Form D XML from {url}")
            try:
                res = requests.get(url, headers=self.headers, timeout=10)
                if res.status_code == 404:
                    logger.warning(f"Form D XML not found (404) at {url}")
                    return 0
                res.raise_for_status()
                xml_content = res.text
                self._save_xml_cache(url, xml_content)
            except Exception as e:
                logger.error(f"Failed to fetch Form D XML from {url}: {e}")
                raise

        # Parse the downloaded XML
        parsed_persons = self.parse_raw_xml(xml_content)

        relationships_added = 0
        if source_data == "SEC_FORM_D":
            ROLE_MAP = {
                "Director": "DIRECTOR",
                "Executive Officer": "OFFICER",
                "Promoter": "PROMOTER",
                "isDirector": "DIRECTOR",
                "isExecutiveOfficer": "OFFICER",
                "isPromoter": "PROMOTER"
            }
            default_suffix = ""
        else:
            ROLE_MAP = {
                "Director": "DIRECTOR_D",
                "Executive Officer": "OFFICER_D",
                "Promoter": "PROMOTER_D",
                "isDirector": "DIRECTOR_D",
                "isExecutiveOfficer": "OFFICER_D",
                "isPromoter": "PROMOTER_D"
            }
            default_suffix = "_D"

        for person in parsed_persons:
            issuer_cik_padded = person["issuer_cik"].strip().zfill(10) if person["issuer_cik"] else None
            for role in person["roles"]:
                relation_type = ROLE_MAP.get(role, f"{role.upper()}{default_suffix}")
                self.db.add_relationship(
                    src_id=None,
                    src_name=person["name"],
                    src_type="PERSON",
                    tgt_id=issuer_cik_padded,
                    tgt_name=person["issuer_name"],
                    tgt_type="COMPANY",
                    relation=relation_type,
                    source_data=source_data
                )
                relationships_added += 1

        return relationships_added

    def scan_and_process_form_d(self, source_data="SEC_FORM_D"):
        """
        Scans the local SQLite database cache (sec_cache table), finds all "D" and "D/A" filings,
        downloads, parses, and ingests relationships.
        """
        logger.info("Scanning sec_cache table for Form D and D/A filings...")
        
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        
        # Verify if sec_cache table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sec_cache'")
        if not cursor.fetchone():
            logger.warning("sec_cache table does not exist in the database.")
            conn.close()
            return {"processed_filings": 0, "relationships_added": 0}
            
        cursor.execute("SELECT cik, data FROM sec_cache")
        rows = cursor.fetchall()
        conn.close()
        
        logger.info(f"Loaded {len(rows)} records from sec_cache. Identifying Form D/A filings...")
        
        filings_to_process = []
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
            prim_docs = recent.get("primaryDocument", [])
            
            for i, form in enumerate(forms):
                if form in ["D", "D/A"]:
                    if i < len(acc_nums):
                        acc = acc_nums[i]
                        doc = prim_docs[i] if i < len(prim_docs) else None
                        filings_to_process.append({
                            "cik": cik,
                            "accession_number": acc,
                            "primary_document": doc
                        })
                        
        logger.info(f"Found {len(filings_to_process)} Form D or D/A filings to process.")
        
        total_processed = 0
        total_relationships = 0
        
        for f in filings_to_process:
            try:
                added = self.process_form_d(
                    cik=f["cik"],
                    accession_number=f["accession_number"],
                    primary_document=f["primary_document"],
                    source_data=source_data
                )
                if added > 0:
                    total_relationships += added
                total_processed += 1
            except Exception as e:
                logger.error(f"Error processing Form D for CIK {f['cik']}, Accession {f['accession_number']}: {e}")
                
        logger.info(f"Completed Form D scan. Processed {total_processed} filings, added {total_relationships} relationships.")
        return {
            "processed_filings": total_processed,
            "relationships_added": total_relationships
        }

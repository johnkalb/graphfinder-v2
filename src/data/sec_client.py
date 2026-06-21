import time
import requests
from src.config import SEC_USER_AGENT, SEC_RATE_LIMIT_PER_SEC
from src.utils.logger import logger

class SECClient:
    def __init__(self, db_manager):
        self.db = db_manager
        self.headers = {"User-Agent": SEC_USER_AGENT}
        self.last_req_time = 0.0

        # Mapping of known CIKs to prominent executives/directors for seeding high-fidelity relationships
        self.known_insiders = {
            "0000320193": [ # Apple
                {"name": "Tim Cook", "role": "CEO", "type": "PERSON"},
                {"name": "Arthur Levinson", "role": "DIRECTOR", "type": "PERSON"},
                {"name": "Al Gore", "role": "DIRECTOR", "type": "PERSON"},
                {"name": "Jeff Williams", "role": "COO", "type": "PERSON"},
            ],
            "0000789019": [ # Microsoft
                {"name": "Satya Nadella", "role": "CEO", "type": "PERSON"},
                {"name": "Brad Smith", "role": "VICE_CHAIRMAN", "type": "PERSON"},
                {"name": "Amy Hood", "role": "CFO", "type": "PERSON"},
                {"name": "Bill Gates", "role": "FOUNDER", "type": "PERSON"},
            ],
            "0001318605": [ # Tesla
                {"name": "Elon Musk", "role": "CEO", "type": "PERSON"},
                {"name": "Robyn Denholm", "role": "CHAIRMAN", "type": "PERSON"},
                {"name": "Kimbal Musk", "role": "DIRECTOR", "type": "PERSON"},
            ],
            "0001045810": [ # NVIDIA
                {"name": "Jensen Huang", "role": "CEO", "type": "PERSON"},
                {"name": "Colette Kress", "role": "CFO", "type": "PERSON"},
                {"name": "Jay Puri", "role": "OFFICER", "type": "PERSON"},
            ]
        }

    def _wait_for_rate_limit(self):
        # 10 requests per second = 100ms minimum spacing
        min_spacing = 1.0 / SEC_RATE_LIMIT_PER_SEC
        elapsed = time.time() - self.last_req_time
        if elapsed < min_spacing:
            time.sleep(min_spacing - elapsed)
        self.last_req_time = time.time()

    def fetch_company_submissions(self, cik):
        # CIK must be 10 digits padded with leading zeros
        cik_str = str(cik).strip()
        if not cik_str.isdigit():
            raise ValueError(f"Invalid CIK: {cik_str}. Must contain only digits.")
        cik_padded = cik_str.zfill(10)
        
        # Check cache
        cached = self.db.get_sec_cache(cik_padded)
        if cached:
            logger.debug(f"Cache hit for CIK {cik_padded}")
            return cached

        self._wait_for_rate_limit()
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        logger.info(f"Fetching SEC submissions for CIK {cik_padded} from {url}")
        
        try:
            res = requests.get(url, headers=self.headers, timeout=10)
            if res.status_code == 404:
                raise ValueError(f"CIK {cik_padded} not found")
            res.raise_for_status()
            data = res.json()
            self.db.save_sec_cache(cik_padded, data)
            return data
        except Exception as e:
            logger.error(f"Failed to fetch CIK {cik_padded}: {e}")
            raise

    def parse_company_relationships(self, cik):
        cik_padded = str(cik).strip().zfill(10)
        data = self.fetch_company_submissions(cik_padded)
        company_name = data.get("name", "Unknown Company")
        
        logger.info(f"Parsing submissions metadata for {company_name} (CIK: {cik_padded})")
        
        # Log recent filings stats
        filings = data.get("filings", {}).get("recent", {})
        if filings and "form" in filings:
            forms = filings["form"]
            logger.info(f"Found {len(forms)} recent filings for {company_name}")
            form_counts = {}
            for f in forms:
                form_counts[f] = form_counts.get(f, 0) + 1
            logger.debug(f"Filings by form: {form_counts}")
            
        # Discover and seed relationships for known/monitored CIKs
        relationships_found = 0
        if cik_padded in self.known_insiders:
            insiders = self.known_insiders[cik_padded]
            for insider in insiders:
                # Add relationship: Insider (PERSON) -> Company (COMPANY) with relation role
                # Source of relationship is person, target is company
                self.db.add_relationship(
                    src_id=None,
                    src_name=insider["name"],
                    src_type=insider["type"],
                    tgt_id=cik_padded,
                    tgt_name=company_name,
                    tgt_type="COMPANY",
                    relation=insider["role"],
                    source_data="SEC"
                )
                relationships_found += 1
            logger.info(f"Seeded {relationships_found} executive/director relationships for {company_name}")
            
        # Also parse former names if any
        former_names = data.get("formerNames", [])
        if former_names:
            for item in former_names:
                former_name = item.get("name")
                if former_name:
                    # Relationship: Company -> Former Name
                    self.db.add_relationship(
                        src_id=cik_padded,
                        src_name=company_name,
                        src_type="COMPANY",
                        tgt_id=None,
                        tgt_name=former_name,
                        tgt_type="COMPANY",
                        relation="FORMER_NAME",
                        source_data="SEC"
                    )
                    relationships_found += 1
                    
        return relationships_found

import requests
import xml.etree.ElementTree as ET
from src.utils.logger import logger

class IRSClient:
    def __init__(self, db_manager):
        self.db = db_manager
        self.headers = {
            "User-Agent": "CorporateSocialGraph/1.0 (admin@graphbuilder.local)"
        }

    def fetch_object_id(self, ein: str) -> tuple[str, str]:
        """
        Queries the ProPublica API to get the latest_object_id of the filing and the organization's name.
        
        Args:
            ein: A 9-digit EIN (with or without hyphens).
            
        Returns:
            A tuple of (latest_object_id, org_name)
        """
        # Clean EIN to be exactly 9 digits
        cleaned_ein = "".join(filter(str.isdigit, str(ein)))
        if len(cleaned_ein) != 9:
            raise ValueError(f"Invalid EIN: {ein}. Must be exactly 9 digits.")

        url = f"https://projects.propublica.org/nonprofits/api/v2/organizations/{cleaned_ein}.json"
        logger.info(f"Querying ProPublica API for EIN {cleaned_ein} at {url}")
        
        try:
            res = requests.get(url, headers=self.headers, timeout=15)
            if res.status_code == 404:
                raise ValueError(f"EIN {cleaned_ein} not found in ProPublica database")
            res.raise_for_status()
            
            data = res.json()
            org_data = data.get("organization", {})
            
            object_id = org_data.get("latest_object_id")
            org_name = org_data.get("name", "Unknown Nonprofit")
            
            if not object_id:
                # Fallback: check filings_with_data or filings_without_data for any object_id
                filings = data.get("filings_with_data", [])
                if filings:
                    # Sort or grab the first available object_id
                    for f in filings:
                        if f.get("object_id"):
                            object_id = f.get("object_id")
                            break
                            
            if not object_id:
                raise ValueError(f"No filing object ID found for EIN {cleaned_ein}")
                
            return str(object_id), org_name
            
        except requests.RequestException as e:
            logger.error(f"Failed to query ProPublica API for EIN {cleaned_ein}: {e}")
            raise

    def download_xml(self, object_id: str) -> str:
        """
        Downloads the Form 990 XML file from the Giving Tuesday S3 raw data lake.
        
        Args:
            object_id: The filing's S3 object ID.
            
        Returns:
            The raw XML content as a string.
        """
        url = f"https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles/{object_id}_public.xml"
        logger.info(f"Downloading Form 990 XML from S3 for object ID {object_id} at {url}")
        
        try:
            res = requests.get(url, headers=self.headers, timeout=20)
            res.raise_for_status()
            return res.text
        except requests.RequestException as e:
            logger.error(f"Failed to download XML for object ID {object_id}: {e}")
            raise

    def parse_xml(self, xml_content: str) -> dict:
        """
        Parses Form 990 XML to extract filer name and key officers, directors, and trustees.
        
        Args:
            xml_content: Raw Form 990 XML content.
            
        Returns:
            A dictionary containing:
            - filer_name: The parsed name of the nonprofit.
            - officers: A list of dictionaries, each with keys 'name', 'title', 'compensation'.
        """
        root = ET.fromstring(xml_content)
        
        def clean_tag(tag):
            return tag.split('}')[-1]

        # Extract nonprofit filer name
        filer_name = None
        filer_elem = None
        for elem in root.iter():
            if clean_tag(elem.tag) == "Filer":
                filer_elem = elem
                break
                
        if filer_elem is not None:
            for child in filer_elem.iter():
                ct = clean_tag(child.tag)
                if ct in ["BusinessNameLine1Txt", "BusinessNameLine1"]:
                    if child.text:
                        filer_name = child.text.strip()
                        break
            if not filer_name:
                for child in filer_elem.iter():
                    ct = clean_tag(child.tag)
                    if ct == "BusinessName":
                        text_parts = [c.text.strip() for c in child if c.text]
                        if text_parts:
                            filer_name = " ".join(text_parts)
                        elif child.text:
                            filer_name = child.text.strip()
                        break

        # Fallback to any BusinessNameLine1Txt or BusinessNameLine1 in the document
        if not filer_name:
            for elem in root.iter():
                ct = clean_tag(elem.tag)
                if ct in ["BusinessNameLine1Txt", "BusinessNameLine1"] and elem.text:
                    filer_name = elem.text.strip()
                    break

        # Officer/Director/Trustee extraction
        officers = []
        name_tags = {'personname', 'personnm', 'name'}
        title_tags = {'title', 'titletxt', 'titleandavghrsdevotedtopositiontxt'}
        comp_tags = {
            'reportablecompfromorgamt', 
            'reportablecompfromorg', 
            'reportablecompensationfromorganizationamt', 
            'reportablecompensationfromorganization', 
            'reportablecompfromorgamount',
            'reportablecompensationfromorganizationamount'
        }

        for elem in root.iter():
            has_name = None
            has_title = None
            has_comp = None
            
            for child in elem:
                ct_lower = clean_tag(child.tag).lower()
                if ct_lower in name_tags:
                    has_name = child
                elif ct_lower in title_tags:
                    has_title = child
                elif ct_lower in comp_tags or 'reportablecomp' in ct_lower:
                    if 'rltd' not in ct_lower and 'related' not in ct_lower:
                        has_comp = child

            if has_name is not None and has_title is not None:
                name_val = has_name.text.strip() if has_name.text else ""
                title_val = has_title.text.strip() if has_title.text else ""
                
                if name_val:
                    comp_val = 0.0
                    if has_comp is not None and has_comp.text:
                        try:
                            comp_val = float(has_comp.text.strip())
                        except ValueError:
                            comp_val = 0.0
                    
                    if has_comp is None:
                        for child in elem:
                            ct_lower = clean_tag(child.tag).lower()
                            if 'comp' in ct_lower and 'org' in ct_lower and 'rltd' not in ct_lower and 'related' not in ct_lower:
                                if child.text:
                                    try:
                                        comp_val = float(child.text.strip())
                                        break
                                    except ValueError:
                                        pass

                    officers.append({
                        "name": name_val,
                        "title": title_val,
                        "compensation": comp_val
                    })

        # Deduplicate officers based on upper-cased name and title
        seen = set()
        unique_officers = []
        for off in officers:
            key = (off["name"].upper(), off["title"].upper())
            if key not in seen:
                seen.add(key)
                unique_officers.append(off)

        return {
            "filer_name": filer_name,
            "officers": unique_officers
        }

    def process_nonprofit(self, ein: str) -> int:
        """
        Coordinates the full pipeline for a given EIN:
        1. Fetch S3 object ID and nonprofit name from ProPublica.
        2. Download the Form 990 XML.
        3. Parse filer name and officers.
        4. Insert officer relationships into the database.
        
        Args:
            ein: A 9-digit EIN.
            
        Returns:
            The number of inserted relationships.
        """
        cleaned_ein = "".join(filter(str.isdigit, str(ein)))
        logger.info(f"Processing nonprofit EIN: {cleaned_ein}")
        
        try:
            object_id, propublica_name = self.fetch_object_id(cleaned_ein)
        except Exception as e:
            logger.error(f"Could not retrieve object ID for EIN {cleaned_ein}: {e}")
            raise
            
        try:
            xml_content = self.download_xml(object_id)
        except Exception as e:
            logger.error(f"Could not download Form 990 XML for object ID {object_id}: {e}")
            raise
            
        try:
            parsed_data = self.parse_xml(xml_content)
        except Exception as e:
            logger.error(f"Could not parse XML for object ID {object_id}: {e}")
            raise
            
        # Use ProPublica name if available and not default, otherwise fallback to XML parsed name
        org_name = propublica_name if propublica_name and propublica_name != "Unknown Nonprofit" else parsed_data.get("filer_name")
        if not org_name:
            org_name = f"Nonprofit EIN {cleaned_ein}"
            
        officers = parsed_data.get("officers", [])
        logger.info(f"Parsed {len(officers)} unique key individuals for {org_name}")
        
        inserted_count = 0
        for off in officers:
            try:
                # Target is the non-profit, source is the person
                self.db.add_relationship(
                    src_id=None,
                    src_name=off["name"],
                    src_type="PERSON",
                    tgt_id=cleaned_ein,
                    tgt_name=org_name,
                    tgt_type="NONPROFIT",
                    relation=off["title"],
                    source_data="IRS_990"
                )
                inserted_count += 1
            except Exception as e:
                logger.error(f"Failed to insert relationship for {off['name']} -> {org_name}: {e}")
                
        logger.info(f"Successfully processed {inserted_count} relationships for EIN {cleaned_ein}")
        return inserted_count

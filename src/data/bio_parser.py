import re
from src.utils.logger import logger

class BioParser:
    def __init__(self):
        # Relationship keyword match dictionaries
        self.patterns = {
            "EMPLOYER": [
                # e.g., "joining Apple", "at Compaq", "with Ford Motor Company"
                r"\b(?:joined|joining|at|with)\s+([A-Z][A-Za-z0-9&'\s,\.-]+?)(?:\s+in\s+\d{4}|\s+since|\s+where|\.|\s+and|\s+as|\s+to|,)",
                # e.g., "served as Chief Operating Officer of Compaq"
                r"\b(?:served as|has been|was|is)\s+(?:[\w\s\-]{1,50})\s+(?:of|at|for)\s+([A-Z][A-Za-z0-9&'\s,\.-]+?)(?:\s+in\s+\d{4}|\s+since|\s+where|\.|\s+and|,)"
            ],
            "ALMA_MATER": [
                # e.g., "graduated from Harvard", "MBA from Duke"
                r"\b(?:graduated from|degree from|attended|MBA from|holds a (?:[\w\s\-]{1,50}) from|received a (?:[\w\s\-]{1,50}) from|earned a (?:[\w\s\-]{1,50}) from)\s+([A-Z][A-Za-z0-9\s'&,\.-]+?)(?:\s+in\s+\d{4}|\s+and|\s+where|\.|\s+with|,)"
            ],
            "ASSOCIATE": [
                # e.g., "reported directly to Michael Capellas", "reported to Chairman Bill Gates"
                r"\b(?:reported directly to|reported to)\s+(?:CEO|Chairman|President|executive VP)?\s*([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)",
                # e.g., "founded Microsoft alongside Bill Gates"
                r"\b(?:founded|co-founded)\s+(?:[\w\s\-']{1,50})\s+(?:with|alongside)\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)",
                # e.g., "served under Michael Capellas"
                r"\b(?:served under|working under)\s+([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)"
            ]
        }

    def clean_target_name(self, name, relation_type):
        if not name:
            return ""
        name = name.strip()
        
        # Split by common transition/verb words that might be captured accidentally due to wildcards
        # This keeps the core entity name clean
        split_pattern = r'\b(?:since|joined|joining|served|prior|where|reported|graduated|holds|received|earned|attended|degree|mba|bs|ba|phd|in|at|from|to|with|and)\b'
        name = re.split(split_pattern, name, flags=re.IGNORECASE)[0].strip()
        
        if relation_type in ("EMPLOYER", "ALMA_MATER"):
            # Clean corporate suffixes to prevent duplicate nodes and keep graph high fidelity
            # (e.g. "Apple Inc." -> "Apple", "Ford Motor Co." -> "Ford Motor")
            suffix_pattern = r'\s*,?\s*\b(?:Inc|Corp|Corporation|Ltd|Limited|LLC|L\.L\.C\.|Co|Company|Companies|SA|S\.A\.|AG|A\.G\.|PLC|plc)\b\.?$'
            name = re.sub(suffix_pattern, '', name, flags=re.IGNORECASE)
            
        # Strip trailing and leading punctuation, especially commas and periods
        name = name.strip(",. ")
        return name

    def extract_relations(self, person_name, bio_text):
        if not bio_text or not person_name:
            return []
            
        logger.info(f"Extracting biography relationships for {person_name}")
        extracted = []
        seen = set()  # Prevent duplicate relations of the same type to the same target

        for rel_type, pattern_list in self.patterns.items():
            for pat in pattern_list:
                for match in re.finditer(pat, bio_text):
                    target = match.group(1).strip()
                    cleaned_target = self.clean_target_name(target, rel_type)
                    
                    # Ensure name is not empty, is not the person themselves, and is at least 3 characters
                    if (
                        len(cleaned_target) > 2 
                        and cleaned_target.lower() != person_name.lower()
                    ):
                        # Unique identifier for the relation to deduplicate
                        rel_key = (rel_type, cleaned_target.lower())
                        if rel_key not in seen:
                            seen.add(rel_key)
                            extracted.append({
                                "source_name": person_name,
                                "relation_type": rel_type,
                                "target_name": cleaned_target
                            })
                            logger.debug(f"Discovered relation: {person_name} -> {rel_type} -> {cleaned_target}")
                            
        return extracted

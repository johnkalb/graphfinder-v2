import os

# SEC Guidelines: User-Agent must identify company/individual and email
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "GraphBuilderAdmin admin@graphbuilder.local")
SEC_RATE_LIMIT_PER_SEC = 10  # Limit to 10 requests/sec to prevent ban

# Wikidata Rate Limit
WIKIDATA_USER_AGENT = "CorporateSocialGraph/1.0 (admin@graphbuilder.local)"

# File Paths
DB_PATH = "data/pipeline_cache.db"
OUTPUT_DIR = "data/output"

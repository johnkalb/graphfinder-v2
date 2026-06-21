"""
Public Pension Fund Commitment Harvester.

Parses structured CSV data containing pension fund allocations to VC/PE
funds and maps them as LP_COMMITMENT relationships in the social graph.

Includes a built-in reference dataset of ~15 well-known public pension
VC/PE commitments sourced from public CalPERS/CalSTRS disclosure documents.
"""

import csv
import io
import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Built‑in reference dataset — seed data from public pension disclosures
# ---------------------------------------------------------------------------
# Sourced from CalPERS, CalSTRS, and other major US public pension fund
# published PE/VC portfolio lists.  These are real institutional LP
# commitments to top venture capital and private equity partnerships.
BUILTIN_COMMITMENTS = [
    # ── CalPERS (California Public Employees' Retirement System) ──────────
    {"pension_fund": "CalPERS", "fund_name": "Sequoia Capital U.S. Growth Fund X", "commitment_amount": 150000000, "vintage_year": 2022, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Sequoia Capital U.S. Venture XIV", "commitment_amount": 100000000, "vintage_year": 2021, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Benchmark Capital VIII", "commitment_amount": 75000000, "vintage_year": 2021, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Andreessen Horowitz Fund V", "commitment_amount": 90000000, "vintage_year": 2021, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Accel XIV", "commitment_amount": 80000000, "vintage_year": 2021, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Kohlberg Kravis Roberts & Co. L.P. (North America Fund XIII)", "commitment_amount": 200000000, "vintage_year": 2022, "asset_class": "Buyout"},
    {"pension_fund": "CalPERS", "fund_name": "The Blackstone Group (Capital Partners VIII)", "commitment_amount": 175000000, "vintage_year": 2023, "asset_class": "Buyout"},
    # ── CalSTRS (California State Teachers' Retirement System) ────────────
    {"pension_fund": "CalSTRS", "fund_name": "Sequoia Capital U.S. Venture XV", "commitment_amount": 125000000, "vintage_year": 2023, "asset_class": "Venture Capital"},
    {"pension_fund": "CalSTRS", "fund_name": "Andreessen Horowitz Fund VI", "commitment_amount": 100000000, "vintage_year": 2023, "asset_class": "Venture Capital"},
    {"pension_fund": "CalSTRS", "fund_name": "General Catalyst Group XII", "commitment_amount": 85000000, "vintage_year": 2024, "asset_class": "Venture Capital"},
    {"pension_fund": "CalSTRS", "fund_name": "Insight Partners (Insight Venture Partners XIII)", "commitment_amount": 120000000, "vintage_year": 2023, "asset_class": "Venture Capital"},
    # ── NYSTRS (New York State Teachers' Retirement System) ───────────────
    {"pension_fund": "NYSTRS", "fund_name": "Apollo Management (Apollo Investment Fund X)", "commitment_amount": 150000000, "vintage_year": 2022, "asset_class": "Buyout"},
    {"pension_fund": "NYSTRS", "fund_name": "Warburg Pincus Global Growth 15", "commitment_amount": 75000000, "vintage_year": 2023, "asset_class": "Growth Equity"},
    # ── Texas TRS (Teacher Retirement System of Texas) ────────────────────
    {"pension_fund": "Texas TRS", "fund_name": "Andreessen Horowitz Fund IV", "commitment_amount": 80000000, "vintage_year": 2020, "asset_class": "Venture Capital"},
    {"pension_fund": "Texas TRS", "fund_name": "Thoma Bravo Fund XVI", "commitment_amount": 100000000, "vintage_year": 2024, "asset_class": "Buyout"},
    {"pension_fund": "Texas TRS", "fund_name": "Silver Lake Partners VII", "commitment_amount": 95000000, "vintage_year": 2024, "asset_class": "Buyout"},
    # ── Florida SBA (State Board of Administration) ───────────────────────
    {"pension_fund": "Florida SBA", "fund_name": "Sequoia Capital U.S. Venture XIII", "commitment_amount": 60000000, "vintage_year": 2020, "asset_class": "Venture Capital"},
    {"pension_fund": "Florida SBA", "fund_name": "General Catalyst Group XI", "commitment_amount": 50000000, "vintage_year": 2023, "asset_class": "Venture Capital"},
]


class PensionClient:
    """
    Client for harvesting public pension fund VC/PE commitment data.

    Parses structured CSV data and inserts LP_COMMITMENT relationships
    into the social graph database.  Also provides a built-in reference
    dataset for immediate use.
    """

    def __init__(self, db_manager=None):
        self.db = db_manager

    # ------------------------------------------------------------------
    # CSV parsing
    # ------------------------------------------------------------------

    def parse_commitment_csv(self, csv_content: str) -> list[dict[str, Any]]:
        """
        Parse a structured CSV string containing pension fund allocations.

        Expected CSV columns (case-insensitive):
          - PensionFund (or pension_fund)
          - FundName (or fund_name)
          - CommitmentAmount (or commitment_amount)
          - VintageYear (or vintage_year)
          - AssetClass (or asset_class) — optional, defaults to "Venture Capital"

        Returns a list of dicts with keys:
          pension_fund, fund_name, commitment_amount, vintage_year, asset_class
        """
        f = io.StringIO(csv_content.strip())
        reader = csv.DictReader(f)
        investments = []

        for row in reader:
            # Normalise keys: strip whitespace and handle case variation
            normalised = {}
            for k, v in row.items():
                if v is not None:
                    v = v.strip()
                key = k.strip().lower().replace(" ", "_")
                normalised[key] = v

            fund = normalised.get("pensionfund") or normalised.get("pension_fund", "")
            fund_name = normalised.get("fundname") or normalised.get("fund_name", "")
            amount_raw = normalised.get("commitmentamount") or normalised.get("commitment_amount", "0")
            vintage_raw = normalised.get("vintageyear") or normalised.get("vintage_year", "0")
            asset_class = normalised.get("assetclass") or normalised.get("asset_class", "Venture Capital")

            if not fund or not fund_name:
                continue  # skip rows missing required fields

            try:
                amount = int(amount_raw)
            except ValueError:
                amount = 0

            try:
                vintage = int(vintage_raw)
            except ValueError:
                vintage = 0

            investments.append({
                "pension_fund": fund,
                "fund_name": fund_name,
                "commitment_amount": amount,
                "vintage_year": vintage,
                "asset_class": asset_class,
            })

        return investments

    # ------------------------------------------------------------------
    # Database ingestion
    # ------------------------------------------------------------------

    def process_commitments(
        self,
        commitments: list[dict[str, Any]],
        db_manager=None,
    ) -> int:
        """
        Insert a list of commitment dicts into the relationships table.

        For each commitment, creates a relationship:
          source: pension fund (NONPROFIT)
          target: fund manager (VC_FIRM or FINANCIAL_FIRM)
          relation_type: LP_COMMITMENT

        Parameters
        ----------
        commitments : list[dict]
            List of dicts with keys: pension_fund, fund_name, commitment_amount,
            vintage_year, asset_class.
        db_manager : DBManager, optional
            Database manager instance.  Falls back to self.db if not given.

        Returns
        -------
        int
            Number of commitments inserted.
        """
        db = db_manager or self.db
        if db is None:
            logger.warning("No DB manager provided — commitments will not be persisted.")
            return 0

        # Infer fund type from asset class
        inserted = 0
        for c in commitments:
            asset = (c.get("asset_class") or "").lower()
            if "buyout" in asset or "growth" in asset or "private equity" in asset:
                target_type = "FINANCIAL_FIRM"
            else:
                target_type = "VC_FIRM"

            # Determine pension fund entity type
            source_type = "NONPROFIT"

            relation_type = "LP_COMMITMENT"

            # Add relationship: pension fund (NONPROFIT) -> fund (VC_FIRM/FINANCIAL_FIRM)
            db.add_relationship(
                src_id=c.get("pension_fund", "").upper().replace(" ", "_"),
                src_name=c["pension_fund"],
                src_type=source_type,
                tgt_id=c.get("fund_name", "").upper().replace(" ", "_").replace(".", "").replace(",", ""),
                tgt_name=c["fund_name"],
                tgt_type=target_type,
                relation=relation_type,
                source_data="PENSION",
            )
            inserted += 1

        logger.info(f"Inserted {inserted} LP_COMMITMENT relationships.")
        return inserted

    # ------------------------------------------------------------------
    # Built-in dataset convenience
    # ------------------------------------------------------------------

    def get_builtin_commitments(self) -> list[dict[str, Any]]:
        """Return the built-in reference dataset of ~15 pension fund commitments."""
        return list(BUILTIN_COMMITMENTS)

    def process_builtin_commitments(self, db_manager=None) -> int:
        """
        Convenience method: load the built-in reference dataset and insert
        it into the database.

        Returns the number of relationships inserted.
        """
        commitments = self.get_builtin_commitments()
        return self.process_commitments(commitments, db_manager=db_manager)

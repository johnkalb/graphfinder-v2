"""Categorize raw relation strings into ~25 semantic categories. Shared by scoring + analysis."""
import re

def categorize(rt):
    r = (rt or "").upper().strip()
    # Same-entity markers — NOT relationships (should merge nodes)
    if r in ("ALIAS", "FORMER_NAME"):
        return "SAME_ENTITY"
    # Wikidata time-overlap edges (pass through directly)
    if r == "SAME_ORG_OVERLAP":
        return "SAME_ORG_OVERLAP"
    if r == "SAME_SCHOOL_OVERLAP":
        return "SAME_SCHOOL_OVERLAP"
    if r == "CO_INVENTOR":
        return "CO_INVENTOR"
    if r == "PATENT_ASSIGNED_TO":
        return "PATENT_ASSIGNED_TO"
    # SEC co-directorship (all DIRECTOR(...) title variants + board roles)
    if r.startswith("DIRECTOR") or r in (
        "CO_DIRECTOR", "INDEPENDENT_DIRECTOR", "NON-EXECUTIVE_DIRECTOR",
        "LEAD_DIRECTOR", "BOARD_OF_DIRECTORS", "BOARD_MEMBER", "BOARD",
        "BOARD_CHAIR", "BOARD_MEMBER_OF", "LEAD_INDEPENDENT_DIRECTOR",
        "CHAIRMAN_OF_THE_BOARD", "CHAIRMAN_(BOARD_OF_DIRECTORS)",
    ):
        return "CO_DIRECTOR"
    # SEC co-officership (all OFFICER(...) title variants)
    if r.startswith("OFFICER") or r == "CO_OFFICER":
        return "CO_OFFICER"
    # Other executive titles -> co-executive (same-company colleague)
    if r in ("POSITION", "CHIEF_FINANCIAL_OFFICER", "CHIEF_EXECUTIVE_OFFICER",
             "CHIEF_OPERATING_OFFICER", "CHIEF_ACCOUNTING_OFFICER",
             "CHIEF_INVESTMENT_OFFICER", "CHIEF_TECHNOLOGY_OFFICER",
             "CHIEF_INFORMATION_OFFICER", "CHIEF_ADMINISTRATIVE_OFFICER",
             "EXECUTIVE_OFFICER", "EXECUTIVE_DIRECTOR", "EXECUTIVE_CHAIRMAN",
             "MANAGING_PARTNER", "GENERAL_PARTNER", "FOUNDING_PARTNER",
             "CO_FOUNDER", "CO-FOUNDER", "FOUNDING_MEMBER"):
        return "CO_EXECUTIVE"
    if any(k in r for k in (
        "CEO", "CFO", "COO", "CIO", "CTO", "PRESIDENT", "CHAIRMAN", "CHAIR",
        "VICE_PRESIDENT", "EXECUTIVE", "SVP", "EVP", " VP", "TREASURER",
        "SECRETARY", "GENERAL_COUNSEL", "COMPTROLLER", "CONTROLLER",
        "MANAGING_DIRECTOR", "PRINCIPAL", "PARTNER", "FOUNDER", "OWNER",
        "PROVOST", "CHANCELLOR", "DEAN", "REGENT", "OVERSEER",
    )):
        return "CO_EXECUTIVE"
    # Political donations
    if r in ("DONATION", "FUNDRAISING", "BUNDLER", "MAJOR_CONTRIBUTOR", "CONTRIBUTOR"):
        return "DONATION"
    # Epstein document co-occurrence (the low-quality "transaction" guesses)
    if r == "TRANSACTED_WITH":
        return "CO_OCCURS_DOC"
    # GDELT news co-mention
    if r == "MENTIONED_WITH":
        return "NEWS_COMENTION"
    # Direct communication records (calls/emails)
    if r == "COMMUNICATED_WITH":
        return "COMMUNICATED"
    # Family ties
    if r in ("FAMILY", "SPOUSE"):
        return "FAMILY"
    # Friendship (explicitly labeled)
    if r in (
        "FRIEND", "CLOSE_FRIEND", "LONGTIME_FRIEND", "GOOD_FRIEND",
        "PERSONAL_FRIEND", "OLD_FRIEND", "CHILDHOOD_FRIEND", "A_FRIEND",
        "BOYFRIEND", "GIRLFRIEND", "ROMANTIC_PARTNER", "CLOSEST_FRIEND",
        "INTIMATE_FRIEND", "DEAR_FRIEND", "WEALTHY_FRIEND",
    ):
        return "FRIEND"
    # Shared education
    if r in ("EDUCATION", "ALMA_MATER", "ALUMNI", "ALUMNI_OF", "EDUCATED_AT"):
        return "EDUCATION"
    # Shared org membership / affiliation
    if r in ("MEMBERSHIP", "MEMBER", "MEMBER_OF", "AFFILIATED_WITH",
             "ASSOCIATED_WITH", "FOUNDING_MEMBER"):
        return "MEMBERSHIP"
    # Advisory roles
    if r in ("TRUSTEE", "ADVISORY_BOARD_MEMBER", "ADVISORY_BOARD", "ADVISOR",
             "ADVISER", "SENIOR_ADVISOR", "SENIOR_ADVISER", "ADVISORY_COUNCIL_MEMBER"):
        return "ADVISORY"
    # Direct employment
    if r in ("EMPLOYER", "EMPLOYEE", "EMPLOYED_BY", "PROFESSIONAL"):
        return "EMPLOYMENT"
    # Financial flows (non-donation)
    if r in ("PAID_BY", "GRANTED_TO", "LP_COMMITMENT", "INVESTOR"):
        return "FINANCIAL"
    # Travel / in-person meeting
    if r in ("TRAVELED_TO", "MET_WITH"):
        return "TRAVEL_MET"
    # Served in same body (governors/judges/justices — current officeholders)
    if r in ("FELLOW_GOVERNOR", "FELLOW_JUDGE", "FELLOW_JUSTICE"):
        return "FELLOW_OFFICEHOLDER"
    # Held a public office (connects person to office/role)
    if r in ("MEMBER_OF_CONGRESS", "SENATOR", "GOVERNOR", "REPRESENTATIVE",
             "STATE_SENATOR", "STATE_REPRESENTATIVE", "MAYOR", "ATTORNEY_GENERAL",
             "LIEUTENANT_GOVERNOR", "JUSTICE", "JUDGE", "APPELLATE_JUDGE",
             "ASSOCIATE_JUSTICE", "PRESIDENT_OF_THE_UNITED_STATES", "COMMISSIONER",
             "SECRETARY_OF_STATE", "AMBASSADOR"):
        return "PUBLIC_OFFICE"
    # Lobbying relationship
    if r in ("LOBBYING", "LOBBYIST"):
        return "LOBBYING"
    # SEC insider/ownership signals
    if r in ("INSIDER", "TEN_PERCENT_OWNER", "FILER", "HELD", "CONTROLLED_BY"):
        return "SEC_INSIDER"
    # Political/personal staff relationships (works-for)
    if any(k in r for k in ("STAFF", "AIDE", "CHIEF_OF_STAFF", "CAMPAIGN",
                            "LEGISLATIVE", "PRESS_SECRETARY", "SCHEDULER",
                            "SPEECHWRITER", "COUNSELOR", "INTERN")):
        return "STAFF_AIDE"
    # Weak social ties
    if r in ("MENTOR", "MENTEE", "NEIGHBOR", "ACQUAINTANCE", "COLLEAGUE",
             "ROOMMATE", "CLASSMATE", "COLLEGE_ROOMMATE", "PARTY_HOST"):
        return "WEAK_SOCIAL"
    # Academic / faculty roles -> employment at the institution
    if any(k in r for k in ("PROFESSOR", "FACULTY", "LECTURER", "INSTRUCTOR",
                            "SCHOLAR_IN_RESIDENCE", "FELLOW_IN_RESIDENCE",
                            "RESEARCHER", "POSTDOC")):
        return "EMPLOYMENT"
    # Law clerk / clerk / counsel / attorney -> employment (works at/for)
    if any(k in r for k in ("LAW_CLERK", "CLERK", "COUNSEL", "ATTORNEY",
                            "ASSOCIATE", "CONSULTANT")):
        return "EMPLOYMENT"
    # Board / trustee / advisory committee variants -> advisory
    if any(k in r for k in ("TRUSTEE", "BOARD_OF_TRUSTEES", "BOARD_OF_ADVISORS",
                            "BOARD_OF_GOVERNORS", "BOARD_OF_VISITORS", "ADVISORY_BOARD",
                            "ADVISOR", "ADVISER", "BOARD_OF_OVERSEERS")):
        return "ADVISORY"
    # Fellow / senior fellow at an institution -> membership/affiliation
    if "FELLOW" in r or r in ("SENIOR_FELLOW", "RESIDENT_FELLOW"):
        return "MEMBERSHIP"
    # Generic committee / council / membership roles -> membership
    if any(k in r for k in ("COMMITTEE_MEMBER", "COUNCIL_MEMBER", "MEMBER", "DELEGATE",
                            "RANKING_MEMBER", "COMMITTEE", "COUNCIL", "DELEGATION")):
        return "MEMBERSHIP"
    # Legislative co-sponsorship -> a working relationship
    if r in ("CO-SPONSOR", "CO_SPONSOR", "COSPONSOR", "BILL_COSPONSOR"):
        return "MEMBERSHIP"
    # Candidates / political roles connecting a person to office
    if any(k in r for k in ("CANDIDATE", "NOMINEE", "PARLIAMENTARY")):
        return "PUBLIC_OFFICE"
    return "OTHER"

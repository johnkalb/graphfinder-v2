#!/usr/bin/env python3
"""Add US Cabinet secretaries and appellate judges from last 10 years via Wikipedia."""
import sys, os, requests, re, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from src.config import DB_PATH
from src.data.db_manager import DBManager

HEADERS = {"User-Agent": "GraphBuilderAdmin/1.0"}
WIKI = "https://en.wikipedia.org/w/api.php"
db = DBManager(DB_PATH)
total = 0

# ── Cabinet Secretaries (last 10 years: ~2016-2026) ──
cabinets = [
    # Trump Administration (2017-2021)
    "Rex Tillerson", "Mike Pompeo", "James Mattis", "Mark Esper", "Mark T. Esper",
    "Steve Mnuchin", "Jeff Sessions", "William Barr", "John F. Kelly", "Kirstjen Nielsen",
    "Ryan Zinke", "David Bernhardt", "Sonny Perdue", "Elaine Chao", "Alex Azar",
    "Tom Price", "Seema Verma", "Betsy DeVos", "Ben Carson", "Rick Perry",
    "Dan Brouillette", "Scott Pruitt", "Andrew Wheeler", "Wilbur Ross",
    "Robert Lighthizer", "Mike Pence", "Reince Priebus", "John Kelly",
    "Mick Mulvaney", "Mark Meadows", "Robert O'Brien", "John R. Bolton",
    "H. R. McMaster", "Michael T. Flynn", "Linda McMahon", "Jovita Carranza",
    "Eugene Scalia", "Patricia Haslach",
    # Biden Administration (2021-2025)
    "Antony Blinken", "Lloyd Austin", "Janet Yellen", "Merrick Garland",
    "Deb Haaland", "Tom Vilsack", "Gina Raimondo", "Marty Walsh", "Julie Su",
    "Xavier Becerra", "Miguel Cardona", "Jennifer Granholm", "Pete Buttigieg",
    "Marcia Fudge", "Adrianne Todman", "Denis McDonough", "David Shulkin",
    "Robert Wilkie", "Denis McDonough", "Michael Regan", "Katherine Tai",
    "Alejandro Mayorkas", "Avril Haines", "Jake Sullivan", "Ron Klain",
    "Jeff Zients", "Karoline Leavitt", "Karine Jean-Pierre",
    # Obama Administration second term (2014-2017)
    "John Kerry", "Ash Carter", "Jack Lew", "Loretta Lynch",
    "Sally Jewell", "Tom Vilsack", "Penny Pritzker", "Thomas Perez",
    "Arne Duncan", "John King Jr.", "Julian Castro", "Jeh Johnson",
    "Ernest Moniz", "Anthony Foxx", "Sylvia Mathews Burwell",
    "Robert McDonald", "John Koskinen", "Shaun Donovan",
]

# ── Circuit Court of Appeals Judges (appointed last 10 years) ──
circt_judges = [
    # Trump appointees
    "Neil Gorsuch", "Brett Kavanaugh", "Amy Coney Barrett",
    "Don Willett", "James Ho", "Kyle Duncan", "Andrew Oldham",
    "Stuart Kyle Duncan", "Kurt Engelhardt", "Cory Wilson",
    "Elizabeth Branch", "Michael B. Brennan", "Michael Y. Scudder",
    "Amy St. Eve", "Michael B. Brennan", "David Stras",
    "L. Steven Grasz", "Ralph R. Erickson", "Steven Colloton",
    "Jonathan Kobes", "Britt Grant", "Barbara Lagoa", "Robert Luck",
    "Kevin Newsom", "Elizabeth Branch", "William H. Pryor Jr.",
    "Gregory Katsas", "Neomi Rao", "Mark J. Bennett",
    "Eric D. Miller", "Lawrence VanDyke", "Daniel Bress",
    "Kenneth K. Lee", "Ryan Nelson", "Bridget S. Bade",
    "Daniel P. Collins", "John B. Owens", "Lawrence VanDyke",
    "Justin R. Walker", "Amul Thapar", "John K. Bush",
    "Joan Larsen", "Stephanos Bibas", "David J. Porter",
    "D. Brooks Smith", "Paul Matey", "Peter J. Phipps",
    "Marvin J. Garbis", "Stephanie D. Davis",
    # Biden appointees
    "Ketanji Brown Jackson", "J. Michelle Childs", "Eunice C. Lee",
    "Myrna Pérez", "Alison J. Nathan", "Dale Ho",
    "Sarah A. L. Merriam", "Maria Araújo Kahn", "Omar A. Ali",
    "L. Andre Davis", "Toby J. Heytens", "DeAndrea G. Benjamin",
    "Gabriel P. Sanchez", "Ana C. Reyes", "Nancy G. Abudu",
    "Dana M. Douglas", "James E. Graves Jr.", "Cory T. Wilson",
    "Benjamin J. Beaton", "André B. Mathis", "Charles E. Fleming",
    "John Z. Lee", "J. Michelle Childs", "Tiffany P. Cunningham",
    "Leonard P. Stark", "William J. Nardini", "Sarah A. L. Merriam",
    "Margaret L. Carter", "Stephanie A. Finley", "Richard C. Wesley",
    "Denny Chin", "Susan L. Carney", "Denny Chin",
    # Obama second-term appointees
    "David J. Barron", "William Kayatta Jr.", "Patricia Ann Millett",
    "Nina Pillard", "Robert L. Wilkins", "Michelle Friedland",
    "Paul J. Watford", "Andrew D. Hurwitz", "Jacqueline Nguyen",
    "Kermit Lipez", "Roger L. Gregory", "Henry F. Floyd",
    "Albert Diaz", "James A. Wynn Jr.", "Stephanie Thacker",
    "Pamela Harris", "Cheryl Ann Krause", "Luis Felipe Restrepo",
    "Patty Shwartz", "Jane Branstetter Stranch",
    "Bernice B. Donald", "John M. Rogers", "Raymond Kethledge",
    "Helene N. White", "David McKeague", "Richard Allen Griffin",
]

all_people = cabinets + circt_judges
seen = set()
for i, name in enumerate(all_people):
    time.sleep(0.6)
    if name in seen:
        continue
    seen.add(name)
    
    # Get Wikipedia page content
    r = requests.get(WIKI, params={
        "action": "query", "prop": "revisions", "rvprop": "content",
        "titles": name, "format": "json"
    }, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"  [{i+1}] {name[:40]:40s} → Wiki error")
        continue
    pages = list(r.json().get("query", {}).get("pages", {}).values())
    if not pages:
        print(f"  [{i+1}] {name[:40]:40s} → not found")
        continue
    content = pages[0].get("revisions", [{}])[0].get("*", "")
    if not content:
        print(f"  [{i+1}] {name[:40]:40s} → no content")
        continue
    
    # Extract infobox
    ib = re.search(r'\{\{Infobox\s+(?:officeholder|judge|office\s+holder)\s*([\s\S]*?)\n\}', content, re.IGNORECASE)
    if not ib:
        ib = re.search(r'\{\{Infobox\s+(?:person|scientist|professor|military|politician)\s*([\s\S]*?)\n\}', content, re.IGNORECASE)
    
    # Extract education, employer, board memberships from text
    education = []
    employer = []
    titles = []
    
    if ib:
        text = ib.group(1)
        # Extract key fields
        for field in ["president", "office", "title", "minister", "secretary", "judgeship"]:
            m = re.search(r'\|\s*' + field + r'\s*=\s*(.+?)(?=\n\||\n\})', text, re.DOTALL | re.IGNORECASE)
            if m:
                val = re.sub(r'\[\[[^\]]*?\|([^\]]*)\]\]', r'\1', m.group(1))
                val = re.sub(r'\[\[([^\]]*)\]\]', r'\1', val)
                val = re.sub(r'\s+', ' ', val).strip()
                val = re.sub(r"'{2,}", '', val)
                if val and len(val) < 200:
                    titles.append(val)
        
        # Education
        for field in ["education", "alma_mater"]:
            m = re.search(r'\|\s*' + field + r'\s*=\s*(.+?)(?=\n\||\n\})', text, re.DOTALL | re.IGNORECASE)
            if m:
                vals = re.findall(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', m.group(1))
                education.extend(vals)
        
    # Create the person node
    # Add position
    for title in titles[:2]:
        try:
            db.add_relationship(None, name, "PERSON", None, title, "POSITION", "HELD", "WIKIPEDIA")
            total += 1
        except:
            pass
    
    # Add education connections (alma mater → person)
    for edu in education:
        edu = edu.strip()
        if edu and len(edu) > 5 and not any(x in edu.lower() for x in ['[', ']', '{', '}']):
            try:
                db.add_relationship(None, edu, "UNIVERSITY", None, name, "PERSON", "ALUMNI", "WIKIPEDIA")
                total += 1
            except:
                pass
    
    if (i+1) % 25 == 0:
        print(f"  Progress: {i+1}/{len(all_people)}, {total} relationships")
    
    if total >= 500:
        print(f"  Hit 500 relationship cap. Some remaining skipped.")
        break

print(f"\n\nTotal added: {total}")

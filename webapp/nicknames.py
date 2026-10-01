"""English given-name nicknames -> formal forms, for merging graph nodes like
"Larry Summers" / "Lawrence Summers" (used by build_scored_edges.py).

Only the first token is mapped, and the caller must still corroborate a merge
structurally (shared neighbours): "Bob Smith" and "Robert Smith" are as often
two people as one. Ambiguous nicknames list every formal form (Ted -> Edward,
Theodore); the caller merges only into a form that exists and corroborates.
"""
NICKNAMES = {
    "al": ["albert", "alan", "allen", "alfred"], "alex": ["alexander"], "andy": ["andrew"],
    "barb": ["barbara"], "becky": ["rebecca"], "ben": ["benjamin"], "bernie": ["bernard"],
    "beth": ["elizabeth"], "betsy": ["elizabeth"], "betty": ["elizabeth"], "bill": ["william"],
    "billy": ["william"], "bob": ["robert"], "bobby": ["robert"], "cathy": ["catherine"],
    "charlie": ["charles"], "chris": ["christopher", "christine"], "chuck": ["charles"],
    "cindy": ["cynthia"], "dan": ["daniel"], "danny": ["daniel"], "dave": ["david"],
    "debbie": ["deborah"], "deb": ["deborah"], "dick": ["richard"], "don": ["donald"],
    "doug": ["douglas"], "ed": ["edward"], "eddie": ["edward"], "fred": ["frederick"],
    "gene": ["eugene"], "greg": ["gregory"], "hank": ["henry"], "jack": ["john"],
    "jake": ["jacob"], "jeff": ["jeffrey"], "jen": ["jennifer"], "jenny": ["jennifer"],
    "jerry": ["gerald", "jerome"], "jim": ["james"], "jimmy": ["james"], "joe": ["joseph"],
    "johnny": ["john"], "jon": ["jonathan"], "kate": ["katherine", "catherine"],
    "kathy": ["katherine", "kathleen"], "ken": ["kenneth"], "kim": ["kimberly"], "larry": ["lawrence"],
    "len": ["leonard"], "liz": ["elizabeth"], "maggie": ["margaret"], "matt": ["matthew"],
    "mike": ["michael"], "mickey": ["michael"], "nate": ["nathan", "nathaniel"], "nick": ["nicholas"],
    "pam": ["pamela"], "pat": ["patrick", "patricia"], "peggy": ["margaret"], "pete": ["peter"],
    "phil": ["philip", "phillip"], "ray": ["raymond"], "rich": ["richard"], "rick": ["richard"],
    "rob": ["robert"], "ron": ["ronald"], "russ": ["russell"], "sam": ["samuel"], "sandy": ["sandra"],
    "steve": ["steven", "stephen"], "sue": ["susan"], "ted": ["edward", "theodore"],
    "terry": ["terence", "terrence"], "tim": ["timothy"], "tom": ["thomas"], "tommy": ["thomas"],
    "tony": ["anthony"], "vince": ["vincent"], "walt": ["walter"], "will": ["william"],
}


def formal_forms(key):
    """Formal-name variants of a canon_key()'d name whose first token is a
    nickname: "larry summers" -> ["lawrence summers"]. Needs a surname after."""
    first, _, rest = key.partition(" ")
    if not rest:
        return []
    return [f"{f} {rest}" for f in NICKNAMES.get(first, ())]

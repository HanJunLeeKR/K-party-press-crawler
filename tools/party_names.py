# -*- coding: utf-8 -*-
"""Name of the party on the publication date (column `party_name_at_time`).

`party` is the successor name that identifies the file (e.g. 더불어민주당, 국민의힘). Those parties
absorbed earlier names, so each row also records the party's official name on its `date`.
Rows without a usable date get an empty value. Dates are the official name-change dates (inclusive start).
Cross-checked against the party names printed in the closing lines of the documents themselves.
"""
# (first day the name was in use, name)
LINEAGES = {
    "더불어민주당": [
        ("0000-00-00", "열린우리당(창당 이전)"),   # before the 2003-11-11 founding (new-party preparation period)
        ("2003-11-11", "열린우리당"),
        ("2007-08-20", "대통합민주신당"),
        ("2008-02-18", "통합민주당"),
        ("2008-07-06", "민주당"),
        ("2011-12-18", "민주통합당"),
        ("2013-05-04", "민주당"),
        ("2014-03-26", "새정치민주연합"),
        ("2015-12-28", "더불어민주당"),
    ],
    "국민의힘": [
        ("0000-00-00", "한나라당"),
        ("2012-02-13", "새누리당"),
        ("2017-02-13", "자유한국당"),
        ("2020-02-17", "미래통합당"),
        ("2020-09-02", "국민의힘"),   # rename announced for 2026 was postponed until after the 3 June local elections
    ],
}


def name_at(party, date):
    """Return the party's name on `date` (YYYY-MM-DD). Parties without renames return `party`."""
    if not date or len(date) < 10:
        return ""
    steps = LINEAGES.get(party)
    if not steps:
        return party
    name = steps[0][1]
    for start, n in steps:
        if date >= start:
            name = n
    return name

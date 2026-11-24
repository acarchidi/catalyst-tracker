"""Two small filings for a made-up company, in the layout EDGAR's inline XBRL filings use (ADR 0066).

Each has a hidden `<div style="display:none"><ix:header>` with data tags, a cover page, a table
of contents in a table or in lines, and `PART` and `Item` headings with the title on the same
line or the next. No real 10-Q or 10-K main document is recorded yet (the recorded Vertex 10-Q
in `tests/fixtures/sec-vertex-10q-index` is its filing index only), so these are built here, not
kept under `tests/fixtures`, whose pages are recorded copies that other tests scan. They test
where sections start and end, how parts are cut, and that the hidden XBRL header does not hold
every claim. Join them with a recorded filing (`scripts/record_fixtures.py`) once the first live
filing reads show a layout these miss.
"""

import re

_FILLER = " ".join(
    [
        "Research and development expenses increased compared with the same period of the prior year, "
        "mainly "
        "because of higher clinical trial costs for our late-stage programs and higher personnel costs."
    ]
    * 3
)
_RISK = " ".join(
    [
        "If our clinical trials fail to demonstrate safety and efficacy, we may not obtain approval, "
        "and we may "
        "decide to stop a program at any time."
    ]
    * 3
)


def _page(title: str, body: str) -> bytes:
    body = re.sub(r"(?<!>)\n(?!<)", " ", body)  # lines wrapped here are one paragraph on the page
    return f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">
<head><title>{title}</title></head>
<body>
<div style="display:none"><ix:header><ix:hidden><ix:nonNumeric name="dei:AmendmentFlag"
contextRef="c-1">false\
</ix:nonNumeric><ix:nonNumeric name="dei:DocumentFiscalPeriodFocus"
contextRef="c-1">Q3</ix:nonNumeric></ix:hidden>\
<ix:resources><xbrli:context id="c-1"><xbrli:entity><xbrli:identifier
scheme="http://www.sec.gov/CIK">0000000001\
</xbrli:identifier></xbrli:entity></xbrli:context></ix:resources></ix:header></div>
{body}
</body></html>
""".encode()


QUARTERLY = _page(
    "acme-20260930",
    f"""
<div><p style="text-align:center"><b>FORM 10-Q</b></p>
<p>QUARTERLY REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934 For the
quarterly
period ended September 30, 2026</p>
<p style="text-align:center"><b>Acme Bio, Inc.</b></p></div>
<div><p><b>TABLE OF CONTENTS</b></p>
<table>
<tr><td>PART I.</td><td>FINANCIAL INFORMATION</td><td></td></tr>
<tr><td>Item 1.</td><td>Financial Statements</td><td>3</td></tr>
<tr><td>Item 2.</td><td>Management's Discussion and Analysis of Financial Condition and Results of
Operations</td>
<td>18</td></tr>
<tr><td>Item 3.</td><td>Quantitative and Qualitative Disclosures About Market Risk</td><td>27</td></tr>
<tr><td>Item 4.</td><td>Controls and Procedures</td><td>27</td></tr>
<tr><td>PART II.</td><td>OTHER INFORMATION</td><td></td></tr>
<tr><td>Item 1.</td><td>Legal Proceedings</td><td>28</td></tr>
<tr><td>Item 1A.</td><td>Risk Factors</td><td>28</td></tr>
<tr><td>Item 2.</td><td>Unregistered Sales of Equity Securities and Use of Proceeds</td><td>29</td></tr>
<tr><td>Item 5.</td><td>Other Information</td><td>29</td></tr>
<tr><td>Item 6.</td><td>Exhibits</td><td>30</td></tr>
</table></div>
<div><p><b>PART I. FINANCIAL INFORMATION</b></p>
<p><b>Item 1. Financial Statements</b></p>
<p>Condensed Consolidated Balance Sheets (unaudited). Cash and cash equivalents were $412.5 million
at September
30, 2026. Total liabilities were $88.1 million.</p>
<p>The financial statements include the accounts of the Company. The topline data from the Phase 3
RAINIER trial
are expected in the second half of 2027, which does not change any estimate in these statements.</p>
</div>
<div><p><b>Item 2.</b></p>
<p><b>Management&#8217;s Discussion and Analysis of Financial Condition and Results of Operations</b></p>
<p>Overview. Acme Bio is a clinical-stage biotechnology company developing medicines for people
with serious liver
diseases.</p>
<p>Recent Developments. We completed enrollment in the Phase 3 RAINIER trial of pegozafermin in
MASH, and we expect
to report topline data in the first half of 2027.</p>
<p>We plan to submit a Biologics License Application for pegozafermin to the FDA by the end of 2027.</p>
<p>{_FILLER}</p>
<p>Liquidity and Capital Resources. We believe our cash will fund operations into 2029.</p>
</div>
<div><p><b>Item 3. Quantitative and Qualitative Disclosures About Market Risk</b></p>
<p>Our exposure to market risk is confined to our cash equivalents and short-term investments.</p>
<p><b>Item 4. Controls and Procedures</b></p>
<p>Our disclosure controls and procedures were effective as of September 30, 2026.</p>
</div>
<div><p><b>PART II. OTHER INFORMATION</b></p>
<p><b>Item 1. Legal Proceedings</b></p>
<p>We are not a party to any material legal proceedings.</p>
<p><b>Item 1A. Risk Factors</b></p>
<p>{_RISK}</p>
<p><b>Item 2. Unregistered Sales of Equity Securities and Use of Proceeds</b></p>
<p>None.</p>
<p><b>Item 5. Other Information</b></p>
<p>On November 3, 2026, the FDA granted Breakthrough Therapy designation to pegozafermin. An
end-of-phase 2 meeting
for ACM-201 is expected in the first quarter of 2027.</p>
<p><b>Item 6. Exhibits</b></p>
<p>31.1 Certification of the Principal Executive Officer.</p>
</div>
""",
)

ANNUAL = _page(
    "acme-20260630",
    f"""
<div><p style="text-align:center"><b>FORM 10-K</b></p>
<p>ANNUAL REPORT PURSUANT TO SECTION 13 OR 15(d) OF THE SECURITIES EXCHANGE ACT OF 1934 For the
fiscal year ended
June 30, 2026</p>
<p style="text-align:center"><b>Acme Bio, Inc.</b></p></div>
<div><p><b>TABLE OF CONTENTS</b></p>
<p>Item 1. Business 4</p>
<p>Item 1A. Risk Factors 30</p>
<p>Item 1B. Unresolved Staff Comments 61</p>
<p>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations 70</p>
<p>Item 8. Financial Statements and Supplementary Data 82</p>
</div>
<div><p><b>PART I</b></p>
<p><b>ITEM 1. BUSINESS</b></p>
<p>Our pipeline. Pegozafermin is in the Phase 3 RAINIER trial in MASH. We expect topline data from
RAINIER in the
first half of 2027.</p>
<p>ACM-201 is in a Phase 2 trial in primary biliary cholangitis. We expect to present 48-week data
at the AASLD
meeting in November 2027.</p>
<p>{_FILLER}</p>
<p><b>ITEM 1A. RISK FACTORS</b></p>
<p>{_RISK}</p>
<p><b>ITEM 1B. UNRESOLVED STAFF COMMENTS</b></p>
<p>None.</p>
<p><b>PART II</b></p>
<p><b>ITEM 7. MANAGEMENT&#8217;S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF
OPERATIONS</b></p>
<p>We plan to submit a Biologics License Application for pegozafermin to the FDA by the end of 2027.</p>
<p>{_FILLER}</p>
<p><b>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</b></p>
<p>The financial statements begin on page F-1.</p>
</div>
""",
)

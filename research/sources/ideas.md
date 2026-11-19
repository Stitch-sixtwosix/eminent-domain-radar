# Source ideas with no reader yet

Each entry: what it is, the address, how it could be read, and what was checked. Add new ideas at the bottom.

## California CEQAnet (State Clearinghouse environmental documents) - checked 2026-11-18
- What: every CEQA notice filed with the State Clearinghouse. A Notice of Preparation (NOP) is the earliest public
  sign of a project large enough to need an EIR: roads, transit, transmission, water, schools, redevelopment.
- Address: `https://ceqanet.lci.ca.gov/Search?StartRange=YYYY-MM-DD&EndRange=YYYY-MM-DD&DocumentType=NOP+-+Notice+of+Preparation+of+a+Draft+EIR&OutputFormat=CSV`
  (the older host ceqanet.opr.ca.gov redirects here). No login or key.
- Verified: answered 200 with text/csv for 1 to 18 November 2026. Without a date range it refuses
  ("Results too large to download. Target: 10000"), so read it in date windows.
- Columns include: SCH Number, Lead Agency Name, Document Title, Document Type, Received, Document Description,
  Document Portal URL, Project Title, Location Coordinates, Cities, Counties, Location Cross Streets,
  Location Total Acres, Location Parcel Number, Location State Highways, NOC Development Type.
- Reader needed: a dated CSV reader (new kind). Location Coordinates gives a point; Location Parcel Number gives APNs
  directly. The Contact columns name agency staff and should not be stored.
- Terms: not read yet. Read the site's conditions of use before turning anything on.

## Federal court condemnation filings (CourtListener / RECAP) - checked 2026-11-18
- What: federal civil cases with nature-of-suit 210 (Land Condemnation): pipeline (Natural Gas Act), border, federal
  projects. Shows takings already filed, with the defendant parcels named in the caption.
- Address: `https://www.courtlistener.com/api/rest/v4/search/?type=r&nature_of_suit=210&order_by=dateFiled desc`
- Checked: answers 401 "Authentication credentials were not provided". It needs a free account token, so it is
  outside what the daily run may use. Owner decision: register for a token, or leave it.

## Legistar councils that answer but matched nothing - checked 2026-11-18
- 23 Legistar clients are on and answer (visalia, cabq, miamifl, cityofno, lausd tested: all 200) but no matter
  title in the last year contains the search phrases. For school boards and committees this is expected. A reader
  that also searched matter text or attachments, or used each client's own wording ("resolution of necessity" in
  California, "quick-take" in Illinois, "declaration of taking"), would find more.

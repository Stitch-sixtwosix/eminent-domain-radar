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

## State catalogue index of county parcel services (ArcGIS account GDITAdmin) - checked 2026-11-19
- What: one ArcGIS Online account registers each county's own parcel service under the title
  "Parcels - <ST> - <County> County": 85 for Georgia, 20 for Kentucky, 1 for Indiana. It is an index of where each
  county serves parcels, which saves searching county by county.
- Address: `https://www.arcgis.com/sharing/rest/search?f=json&num=100&q=title:"Parcels - GA" owner:GDITAdmin`
  (swap GA for KY, IN or another state code). Each result's `url` is the county's layer.
- Checked: of the 105 Georgia and Kentucky entries, 61 answer but have no owner-name field (many Georgia counties
  publish only an owner key, OWNKEY, which joins to a table that is not public), 30 did not answer on this date,
  and the rest are on file. New from it today: Fayette GA and Athens-Clarke GA (both stale, held).
- Reader idea: a weekly check that walks this index, tests each `url` for an owner-like field and reports new ones.
  The Kentucky state server also hosts PVA parcel services by county
  (`https://kygisserver.ky.gov/arcgis/rest/services/WGS84WM_Services/Ky_PVA_<County>_Parcels_WGS84WM/MapServer`);
  Webster's has no owner field, the others were not walked.
- Terms: the index carries none of its own; each county's terms still have to be read one by one.

## Washington SEPA Register (state environmental-review clearinghouse) - checked 2026-11-20
- What: every SEPA and NEPA notice filed with the Department of Ecology since 2000 by any state or local lead
  agency (cities, counties, WSDOT, ports, school and utility districts): 192,110 records, updated daily
  (latest published date 2026-11-19). 5,223 records in the last 12 months, 3,829 of them type "Project", 2,723
  with a latitude and longitude. It shows road, utility, school and port projects at the environmental-checklist
  step, usually before design is final.
- Address (Socrata, no login): `https://data.wa.gov/resource/mmcb-z6jf.json`
  e.g. `?$where=publisheddate > '2026-08-01' AND proposaltypename='Project'&$order=publisheddate DESC`
- Fields: `sepanumber` (id), `proposalname`, `proposaldescription`, `proposaltypename` (Project / Nonproject),
  `leadagencyname`, `documenttypecode` (DNS, ODNS, MDNS, DS/Scoping, DEIS, FEIS ...), `countyname`,
  `siteline1address`, `sitecityname`, `siteparcelnumber`, `sitelatitudedecimal`, `sitelongitudedecimal`,
  `publisheddate`, `commentsduedate`, `separegisterlink.url`.
- Checked: count, three newest records, and a text search. In the last 12 months 17 records say "right-of-way
  acquisition", "property acquisition" or similar in the description (for example Ferndale Main Street
  Improvements, Chelan County South Lakeshore Road Phase I, Lake Stevens 117th Avenue NE Side Path).
- Reader idea: a Socrata reader that takes the last 90 days of type "Project", keeps public lead agencies and
  descriptions matching road, utility, pipeline, transmission, acquisition and easement words, and places each by
  its latitude and longitude (or its parcel number when no point is given).
- Cautions: the dataset's licence field is empty and no terms were read. The rows carry applicant and agency
  contact names, emails and phone numbers; applicants can be private people, so a reader should not take
  `applicantname`, `applicantcontactinfo` or the `leadagencycontact*` fields. RCW 42.56.070(8) bars using lists
  of individuals from Washington public records for commercial purposes.

## Utility project map accounts and an old turnpike plan - checked 2026-11-20
- Northland Reliability Project (Great River Energy and Minnesota Power, 345 kV, Minnesota): the project's own
  ArcGIS account publishes the anticipated right-of-way as polygons (14) and the centerline (3 lines):
  `https://services1.arcgis.com/xNtEFNEb4YGVCG9X/arcgis/rest/services/NRP_Anticipated_Right_of_Way_view/FeatureServer/5`.
  Both answered. Not recorded as a source: the same account now publishes construction-status layers (foundations,
  structure setting, wire pull), so easements are already bought, and the layer credit says "all requests for
  these data should be directed to the identified contact person". The pattern is the useful part: large
  transmission projects often have their own ArcGIS account with a right-of-way layer while the route permit is
  pending. Search: `https://www.arcgis.com/sharing/rest/search?f=json&q="Anticipated Right-of-Way" OR "Proposed Right-of-Way" type:"Feature Service"`.
- Oklahoma Turnpike Authority capital plan (`.../RBtoEUQ2lmN0K3GY/arcgis/rest/services/OTA_CIP/FeatureServer`,
  six layers) answered but was last edited 2022-02-07 and covers 2022 to 2026 only; too old to load. No current
  ACCESS Oklahoma alignment layer was found in the catalogue.
- Not readable: Massachusetts MEPA Environmental Monitor (eeaonline.eea.state.ma.us answered 403).

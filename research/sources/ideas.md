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

## Per-project right-of-way and route maps in the ArcGIS catalogue (a "catalogue sweep") - checked 2026-11-21
- What: DOT staff, toll authorities, utilities and their consultants publish one public ArcGIS service per project
  for open houses and permit applications, with a sublayer that is the proposed right-of-way or the proposed route
  itself. These appear while the route is still being decided, well before any program list names parcels.
- How to find them (no login): search the catalogue by title and sort by date modified, e.g.
  `https://www.arcgis.com/sharing/rest/search?f=json&num=50&sortField=modified&sortOrder=desc&q=title:("proposed right of way" OR "proposed ROW" OR "right of way acquisition") AND type:"Feature Service"`
  (285 services today) and
  `...&q=title:("proposed transmission" OR "transmission line route" OR "route alternatives" OR "proposed route") AND type:"Feature Service"`
  (444 services today). Note the `title:(...)` form: the plain quoted-phrase form returned unrelated results.
- Checked live today (layer answered, count and two records read):
  - Gopher to Badger Link 345 kV (MN/WI) route permit application map:
    `https://services1.arcgis.com/yfalcTIlueMdl9fz/arcgis/rest/services/Gopher_to_Badger_Link_RPA_Map_WFL1/FeatureServer`
    layer 4 "Proposed Right-Of-Way" (3 polygons), 5 "Proposed Route", 8 "Route_Segment_Options", 29 "Notice Area";
    edited 2026-11-13.
  - Xcel Energy Ashland to Ironwood: `.../eM84fwjsSggLQk61/arcgis/rest/services/Ashland_Ironwood_Proposed_Route/FeatureServer/0`
    (6 lines, `Section` gives the segment and its dates, e.g. "Hurley 08/2025-06/2027").
  - Creek to Croix (WI): `.../3fjYPqJf7qalQMlb/arcgis/rest/services/Creek_to_Croix_Proposed_Route/FeatureServer/0`
    (11 lines; `Status2` is Built / Unbuilt, so unbuilt segments can be selected).
  - Maple Leaf: `https://services.arcgis.com/DN2fPfpggEPlLhP6/arcgis/rest/services/Maple_Leaf_Proposed_Route/FeatureServer/0` (1 line).
  - CTRMA US 290 extension design map (Austin): `https://services.arcgis.com/04HiymDgLlsbhaV4/arcgis/rest/services/CTRMA_DesignUpdates_20260927/FeatureServer`
    layer 0 "Proposed ROW" (4 lines), 66 "Proposed Easement", 65 "Property Lines".
  - Ventura County Transportation Commission US 101 HOV: `.../T5Uio9fkgzjt3fIZ/arcgis/rest/services/US101_HOV_Lane_-_Alternative_3_(Sept._2026)/FeatureServer/7`
    "Proposed_Right_of_Way_Aquisition" (3,624 CAD line pieces).
  - I-45 (Houston): `https://services.arcgis.com/rD2ylXRs80UroD90/arcgis/rest/services/I45_v11/FeatureServer/1` "Proposed Right Of Way" (326 lines).
  - Did not answer: Jefferson County WV "ProposedTransmissionLine_Oct2026" (layer id is 10, not 0; not re-tried).
- Why no reader exists: these layers are drawings exported from CAD or KMZ. They carry geometry but no project
  name, cost or date in the rows (fields are things like `Name: "Style150"`, `Layer: "rd-row_shape"`); the project
  is named only by the service title. The existing layer reader needs name and description fields.
- Reader idea: a weekly sweep that runs the two searches, keeps services modified in the last 12 months whose
  owner or title points to a US agency, utility or known consultant, picks sublayers whose NAME matches
  "proposed (right of way|ROW|easement|route)" or "acquisition", takes the service title as the project name and
  the union of the sublayer's shapes as the project footprint, then matches parcels against that footprint. A
  person should confirm each new service once (state, authority, still live) before it is switched on.
- Cautions: every item read today has an empty licence field, and several are published by consultants (HDR,
  HNTB, Stantec, SRF) rather than the authority, so terms could not be read; treat each as held until someone
  looks. Many titles are not US projects or are student work. Contractor accounts (for example Emery Sapp & Sons,
  `EHhJx7V4ByXIPvxe`) publish "Proposed ROW" layers for jobs already under construction, where the land is bought.
- Also found by the same search and now recorded as a source: IDOT's Annual Highway Improvement Program
  "Land Acquisition" layer (research/states/IL.json).

## PJM transmission upgrade list (grid operator's regional plan, 13 states and DC) - checked 2026-11-22
- Address: https://www.pjm.com/pjmfiles/media/planning/projectConstruction-data/projectCostUpgrades.xml
  (linked from https://www.pjm.com/planning/m/project-construction; one XML file, about 25 MB, no login or key).
- Verified live 2026-11-22: 15,759 `<Upgrade>` records; newest `LastUpdated` 11/17/2026. Each record has
  `UpgradeId`, `Description`, `ProjectType` (Baseline 5,639 / Supplemental 6,763 / Network 3,357), `Voltage`,
  `CostEstimate` (millions of dollars), `TransmissionOwner`, `State`, `Location` (substation names, comma
  separated), `Equipment`, `Status`, `ProjectedInServiceDate`, `PJMBoardApprovalDate`, `PercentComplete`.
- Status values: IS 9,330 (in service), EP 3,850 (engineering and procurement), Active 759, UC 604 (under
  construction), PL 132 (planning), On Hold 219, Cancelled 847, UC-ISP 12, W 6. Planned work = Status in
  (PL, EP, Active). States with most records: OH 3,339, PA 3,074, VA 2,315, NJ 1,776, WV 980, IN 920, IL 804,
  MD 762, KY 501, DE 245, MI 183, NC 158.
- Why it matters: 1,123 records that are not yet in service are `Equipment` = "Transmission Line" and describe new
  or rebuilt line ("Construct 0.2 miles of greenfield single circuit 138kV line from Progress Park to ...",
  AEP, VA, in service 2/8/2027). These appear here when the utility presents the need, usually before any state
  siting docket or route map exists, and data-centre growth in OH and VA is driving many of them.
- Why no reader exists: the file is XML (the readers take ArcGIS, Socrata, CKAN, JSON, CSV, GeoJSON and XLSX) and
  has no geometry, only substation names and a state.
- Reader idea: parse the XML, keep Status in (PL, EP, Active) with Equipment "Transmission Line" or
  "Substation" and the words new, greenfield, construct, build or extend in the description; place each one by
  looking up the `Location` substation names in a public substations layer (HIFLD publishes one; not checked today) and
  drawing a line between the two named substations as an approximate corridor. Mark the stage as "plan" and
  the location as approximate until a route map appears.
- Terms: NOT read. The site's "Legal and Privacy" link (https://www.pjm.com/about-pjm/legal.aspx) returned a
  "Page Not Found" shell to a scripted client, so any source built on this file should be held until the owner
  reads PJM's terms in a browser. The records are about utilities' projects and name no individuals.
- Same pattern likely at other grid operators (not verified today): MISO's MTEP project list, CAISO and ISO-NE
  transmission plan pages answered 200 but no machine-readable project list was located in the time available;
  ERCOT answered 403 and NYISO did not answer. SPP's list is already on file.

## Wisconsin Public Service Commission case maps (utility-commission siting dockets) - checked 2026-11-26

The PSC of Wisconsin publishes one ArcGIS service per construction docket on its own account, with no login:
`https://services3.arcgis.com/MD5qUVyAWtAyL43E/arcgis/rest/services?f=json` (66 services; the case maps are the ones
named `<Project>_<docket>`, for example `Emerson_137CE225`). Each is the "Environmental Review Map" for a utility's
application for a certificate to build a power line, substation or gas main, which is the approval that gives the
utility condemnation power in Wisconsin. Verified live today:

- `Emerson_137CE225/FeatureServer` (American Transmission Co., Emerson Road substation and 345 kV line, item
  modified 2026-04-22): layer 21 `S_1103_Segment_Routes` (3 lines; fields `Route`, `Seg_ID`), layer 26
  `A_Proposed_Easements` (2 polygons), layer 30 `S_Project_Area`, layer 1 `Mailer_Parcels` (87 parcels notified).
- `ElmRoadtoRacine_137CE215_WFL1/FeatureServer` (ATC Elm Road to Racine line, item modified 2026-04-15): layer 37
  `Proposed Routes` (7 lines; `Route` = Preferred, `Seg_ID`, `PSC_Len_Mi`), layer 28 `Alternative Routes` (7),
  layer 26 `Under Consideration`, layer 27 `Removed from Consideration`, layer 36 `Easement` (3 polygons with
  `Acreage`, `Route`), layer 33 `Right of Way (ROW)`, layer 5 `Parcels - Received Mailer` (435 parcels with
  `PARCEL_N_1`, `OWNER_NAME`, `MAIL_ADDR`, `MAIL_CITY`, `MAIL_ZIP`, `COUNTY`, `Route`): the owners of record the
  utility notified, already matched to the route.
- Older cases in the same shape: `DarienParis_0173CE193`, `Highland_173CE191`, `WECLNG_0005CG106` (proposed gas
  pipeline), `PlymouthChilton_6690CG174_20200629` (Proposed Routes, Proposed Easements), `CardinalHickoryCreek_0005CE146_APPROVED`.
  `Arcadian_0137CE192` did not answer today. The `98xxCE100` services are private solar farms (no condemnation).

Terms: the items carry no licence text; description is only the case title ("project data submitted by ..."), so
terms were not found and anything read from here should be held for the owner. The notified-parcel layers are
owner-of-record lists compiled by the utility for the PSC mailing; Wisconsin has no statute on file here limiting
mailing-list use, but that should be confirmed before those names are used.

How it could be read: a "docket sweep" reader that lists the account's services each day, keeps names ending in a
docket number with CE (electric construction) or CG (gas construction), and in each one takes line layers whose name
contains Route (skipping "Removed from Consideration" and "Approved" services older than two years) as the project
geometry, with the service description as the project title. New services appearing on the account are new cases.
Layer numbers and names differ per case, so they must be matched by name, not by a fixed address. Other commissions
worth the same check: Minnesota (Commerce EERA), Iowa Utilities Commission, Illinois Commerce Commission, North Dakota PSC.

## TVA transmission-line open-house maps (federal utility with condemnation power; AL, TN, NC so far) - checked 2026-11-27

The Tennessee Valley Authority publishes one ArcGIS service per new transmission-line project for its "virtual open house",
on its own ArcGIS Online account (owner `TVA-GIS`, org `w8auYAijfGK1Mydj`, 565 public items). Each has the candidate routes,
the right-of-way strips and, most usefully, the tax parcels TVA itself marks as affected, with owner and mailing address.
TVA acquires easements by federal condemnation, so these are the earliest public list of who is in the path.

Verified today (no login, no token; `https://services.arcgis.com/w8auYAijfGK1Mydj/arcgis/rest/services/<service>/FeatureServer/<n>`):

| Service | Routes layer | ROW layer | Affected Tax Parcels layer |
|---|---|---|---|
| `Roane_County_TN_Interactive_Map` (item 0980965309624b258b4f248ae789b3ac, 2026-09) | 0, 38 segments (`Segment`, `Length_ft`) | 1 | 5, 222 parcels (`PARNO`, `OWNNAME`, `MAILADD`, `MCITY`, `MSTATE`, `MZIP`, `SITEADD`, `GISACRE`, `CNTYNAME`, `SOURCEAGEN`, `SOURCEDATE`) |
| `Crossville_AL_Interactive_Webmap` (item ebc6f0598df6464a815eaaee26923117, 2026-09) | 2, 46 segments (`Segment_ID`) | 3 | 7, 202 parcels (`PARCELNUMB`, `Owner`, `MailAdd1`-`MailAdd3`, `MailCity`, `MailState`, `MailZip1`, `SitusAddNu`, `SitusAddNa`, `TTV`, `CalcAcres`) |
| `Shooting_Creek_NC_Interactive_Map` (item 7902c5ff989046a7b0fe213d8b2faa4a, 2025-11) | 1, 22 segments (`Segment_No`) | 2 "Proposed ROW" | 6, 218 parcels (same fields as Roane) |

Each also has "Tap Points" and "Proposed Substation" point layers and the existing TVA lines. A newer web map,
`TVA.com_HickoryGrove` (2026-11), suggests a fourth project is coming.

- Planned vs built: every route is an alternative under study at the time of the open house; there is no status field.
  The route layers carry only a segment number, so the project name has to come from the service title.
- Terms: the items' licence and credit fields are empty; the parcel layers' description says the tax parcel data "has been
  compiled from various sources" (county and state files, named per record in `SOURCEAGEN`) and credits "TVA GIS & Mapping".
  No terms of use were found, so anything read from here would be held until the owner decides. The owner names come from
  county files whose own terms may differ (Tennessee's state parcel file is already held on file).
- Reader needed: list the account's Feature Services (`https://www.arcgis.com/sharing/rest/search?f=json&q=owner:TVA-GIS type:"Feature Service"`),
  keep those with an "Alternative Routes" layer, make one project per service (title, county and state from the parcel
  layer), and take the "Affected Tax Parcels" layer as that project's parcels directly instead of matching by distance.
  This is the same shape as the per-docket case maps noted for Wisconsin on 2026-11-26 and the catalogue sweep of 2026-11-21.
- Skipped on purpose: the account's "Comments or Feedback" survey layers (people who commented on a plan).

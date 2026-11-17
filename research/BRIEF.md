# State data research brief

Goal: for each assigned U.S. state, find and VERIFY free public data sources that EminentTracker can read
automatically, and write one JSON file per state to `research/states/<XX>.json` (XX = two-letter code).
Work only inside this repository folder. Do not edit any file outside `research/states/`.

EminentTracker finds land that may be taken for public projects. It needs two things per state:

1. **Planned transportation projects with geometry**: a queryable GIS service (ArcGIS REST FeatureServer/MapServer
   layer preferred) from the state DOT (or statewide MPO data) of projects that are PLANNED or IN DEVELOPMENT:
   STIP / TIP / long-range plan / "projects in development" / "future projects". Not completed work. It must
   return line or point geometry and attributes such as project name or description, type of work, cost,
   phase or status, and a letting/construction date.
2. **Parcels with OWNER NAME (and ideally mailing address)**: a statewide parcel layer that includes the owner
   name, if the state publishes one for free. If none exists statewide, say so and give up to five of the
   most populous counties' parcel layers that do include owner names.

## Rules

- VERIFY every URL you report by actually requesting it (curl `<layer>?f=json`, then a real query such as
  `<layer>/query?where=1%3D1&outFields=*&resultRecordCount=1&f=json`). Record only layers that answered with
  real data. Never report a URL from memory or from a search snippet without fetching it yourself.
- Copy field names exactly as the service returns them. Do not guess which field means what: look at a
  sample record.
- If you cannot find or verify a source in about 10 minutes for a state, record `null` with a one-line
  reason and move on. An honest "not found" is far more useful than a plausible guess.
- Note any stated restriction on commercial use or on using the data for solicitation.
- Use generic client identification in requests. Do not send any personal or company details anywhere.
  Do not sign up for anything, and do not use any source that needs a login or API key.

## Output: research/states/<XX>.json

```json
{
  "state": "Ohio", "code": "OH", "checked": "YYYY-MM-DD",
  "projects": [
    {"name": "ODOT ... layer title", "url": "https://.../FeatureServer/0", "verified": true,
     "geometry": "polyline|point|polygon", "record_count": 1234,
     "fields": {"id": "FIELD", "name": "FIELD", "description": "FIELD", "work_type": "FIELD",
                "cost": "FIELD", "status": "FIELD", "let_date": "FIELD", "county": "FIELD"},
     "work_type_values": ["up to 25 distinct values of the work-type field"],
     "status_values": ["distinct values of the status/phase field"],
     "notes": "what the layer covers; how planned vs under-construction can be told apart"}
  ],
  "parcels_statewide": {"name": "...", "url": "...", "verified": true, "record_count": 0,
     "fields": {"apn": "FIELD", "owner": "FIELD", "mailing_address": "FIELD or list of fields",
                "address": "FIELD", "market_value": "FIELD", "acres": "FIELD", "county": "FIELD"},
     "restrictions": "text or null", "notes": "..."},
  "parcels_counties": [ {"county": "FRANKLIN", "url": "...", "verified": true, "fields": {...}} ],
  "summary": "one or two sentences: what works, what is missing"
}
```

Use `null` for anything not found (e.g. `"parcels_statewide": null`) and leave unmapped fields out.
When finished, reply with ONE line per state: code, projects found (yes/no), statewide owners (yes/no).

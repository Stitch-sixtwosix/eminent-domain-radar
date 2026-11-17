# Project-source research brief (nationwide)

EminentTracker finds private land that may be taken for public projects, as early as possible. We already read
most state DOT project maps. Your job is to find OTHER machine-readable, free, public sources of PLANNED projects
that can lead to eminent domain, in the category assigned to you, and to VERIFY each one.

Write your findings to `research/sources/<your-category>.json` in this repository
(absolute: the `research/sources/` folder next to this file). Do not edit anything else. Save the file as soon as
you have the first few verified sources and keep rewriting it as you add more; do not hold results to the end.

## What counts as a good source

- Lists projects that are PLANNED or in development (not finished), from an authority that can condemn land or
  whose approval leads to condemnation.
- Queryable by a program without a login, API key or payment: ArcGIS REST layers, Socrata/CKAN/open-data APIs,
  documented JSON/CSV/GeoJSON downloads, or a documented public API.
- Ideally has geometry (line/point/polygon) or at least a place name, plus a description of the work.

## Rules

- VERIFY every URL by actually requesting it and reading real records. Never report a URL from memory or from a
  search result without fetching it. If you cannot verify something, list it under "unverified" with the reason.
- Copy field names exactly as returned. Note how to tell planned work from completed work.
- Record any stated limit on commercial use.
- Use a generic client identification. Do not sign up for anything, and do not send personal or company details.
- Breadth matters: aim for as many verified sources as you can find in your category across all 50 states,
  largest first. An honest "not found" is better than a guess.

## Output: research/sources/<category>.json

```json
{"category": "...", "checked": "YYYY-MM-DD",
 "sources": [
   {"name": "...", "authority": "who runs the projects", "states": ["TX"] , "coverage": "statewide|metro area|national|city",
    "kind": "arcgis|socrata|ckan|json|csv|geojson|api", "url": "https://...", "verified": true, "record_count": 0,
    "geometry": "polyline|point|polygon|none",
    "fields": {"id": "F", "name": "F", "description": "F", "work_type": "F", "cost": "F", "status": "F", "date": "F", "county": "F"},
    "planned_filter": "how to select only planned work, as a where clause or in words",
    "sample": "one real record, abbreviated", "restrictions": null, "notes": "..."}
 ],
 "unverified": [{"name": "...", "url": "...", "reason": "..."}],
 "summary": "what you covered, what you could not"}
```

When finished, reply with: how many sources verified, which states they cover, and what is still missing.

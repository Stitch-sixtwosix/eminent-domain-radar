# edradar

Early-warning lead finder for Pennsylvania eminent domain attorneys. It watches public planning
data for projects that will need private land, finds the parcels along each project, and ranks
them as leads, years before a declaration of taking is filed.

```
sources ──► signals ──► scored projects ──► parcels along the alignment ──► leads dashboard / CSV
```

## What it watches

| Source | What it gives | Why it is early |
|---|---|---|
| **PennDOT project map (MPMS)** | Every planned state highway/bridge project with its mapped alignment, cost, status and project manager | Projects appear in the Twelve Year Program and "Under Development" lists long before right-of-way plans exist |
| **Federal Register** | NEPA notices (FHWA, FERC pipelines, Army Corps, FRA, FTA) mentioning Pennsylvania | Scoping and environmental review come before any acquisition |
| **Council legislation (Legistar)** | Ordinances authorizing acquisition or condemnation; Pittsburgh is configured | Often names the exact parcels before filing |

Owner of record, mailing address and county value come from **35 counties' public parcel layers**
(Philadelphia OPA plus the catalog in `edradar/data/pa_parcel_layers.yaml`). The other 32 counties
fall back to the **PASDA statewide parcel layer**, which gives a parcel ID only.

Owner data: Beaver, Bedford, Berks, Bradford, Bucks, Butler, Cambria, Cameron, Carbon, Chester, Clinton,
Columbia, Cumberland, Dauphin, Erie, Fayette, Forest, Huntingdon, Indiana, Juniata, Lancaster, Lebanon,
Mercer, Montgomery, Northampton, Philadelphia, Potter, Snyder, Sullivan, Susquehanna, Tioga, Union,
Wayne, Wyoming, York.

Parcel ID only (no public owner layer found, or the county server did not answer): Adams, Allegheny,
Armstrong, Blair, Centre, Clarion, Clearfield, Crawford, Delaware, Elk, Franklin, Fulton, Greene,
Jefferson, Lackawanna, Lawrence, Lehigh, Luzerne, Lycoming, McKean, Mifflin, Monroe, Montour,
Northumberland, Perry, Pike, Schuylkill, Somerset, Venango, Warren, Washington, Westmoreland.
Add a layer for any of these under `parcel_layers` in `config.yaml` when you find one.

Each catalog entry records its `source`: some are dated snapshots (PASDA) or third-party copies, so
confirm current ownership at the county before outreach. "County value" is whatever the county
publishes, usually an assessed value rather than market value.

## How leads are scored

* **Project score (0-100)**: likelihood the project needs private land (new roadway 95, widening 85,
  interchange 70, bridge replacement 40 ...) plus a bonus for early stages and large budgets.
  PennDOT records are scored by transparent rules in `edradar/classify.py`; free-text notices are
  read by Claude when an Anthropic API key is set, otherwise by keyword rules.
* **Lead score**: the project score for parcels that abut the mapped alignment (within 20 m) or are
  named in the document; 75% of it for parcels in the wider 75 m ring.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml      # optional: restrict counties, add parcel layers
edradar run                              # ingest + score + geocode + match (a few minutes)
edradar serve                            # dashboard at http://127.0.0.1:8000
```

`edradar run` matches the 150 highest-scoring unmatched projects per run (`--limit N` to change);
run it again to work down the list. Individual steps: `edradar ingest | classify | geocode | match | stats`.

Set `ANTHROPIC_API_KEY` to have Claude read council and federal notices (better stage detection,
address and parcel-ID extraction). Without it everything still runs on keyword rules.

To run without a local machine, enable the GitHub Actions workflows: move the two files in
`ci-workflows/` to `.github/workflows/` (they are parked because the token that created this repo
could not write workflow files). **Refresh leads** then runs the pipeline from the Actions tab and
saves the SQLite database as a downloadable artifact; **CI** runs the tests on every push.

## Known limits

* In the 32 counties without a public owner layer (listed above), leads carry a parcel ID only.
* "Abuts alignment" is a proximity screen against the existing centerline, not a right-of-way plan.
  Treat leads as a research queue, not a list of confirmed takings.
* Philadelphia City Council's Legistar API is not publicly readable, so only Pittsburgh is configured.
  PA Turnpike Commission, PUC siting dockets, SEPTA and the Pennsylvania Bulletin are not yet connected.
* Geocoding uses OpenStreetMap Nominatim at one request per second; set a real contact in `user_agent`.

## Before contacting owners

Outreach to prospective clients is governed by Pa. R.P.C. 7.1-7.3 (advertising and solicitation).
Have the firm's ethics counsel approve the outreach method and wording before using lead lists.

## Layout

```
edradar/sources/     penndot.py, federal_register.py, legistar.py
edradar/classify.py  scoring rules, keyword engine, Claude engine
edradar/parcels.py   ArcGIS and Philadelphia parcel lookups
edradar/pipeline.py  ingest -> classify -> geocode -> match
edradar/web/         FastAPI app and single-page dashboard
tests/               offline tests (pytest)
ci-workflows/        GitHub Actions workflows, to move into .github/workflows/
```

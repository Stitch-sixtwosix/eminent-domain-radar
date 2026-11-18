# EminentTracker

Early-warning lead finder for Pennsylvania eminent domain attorneys. (The Python package and command are still named `edradar`.) It watches public planning
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

Every lead gets a score out of 100, in whole points from four parts that add up to the score:

| Part | Points | How it is set |
|---|---|---|
| Need for private land | 0-60 | Each kind of project is rated 0-100 for how likely it is to take land; 60% of that rating counts. New roadway or interchange 95 (57 points), realignment 90 (54), widening or added lane 85 (51), new bridge 75 (45), interchange improvement 70 (42), intersection or turning lane 60 (36), reconstruction 45 (27), bridge replacement 40 (24). Wording such as "right-of-way" or "bypass" adds 8 to the rating. Free-text notices are rated by Claude or keyword rules. |
| How early it is | 0-15 | Planning or environmental review 15, design 12, right-of-way acquisition 8, stage not published 6, condemnation already authorized 4, construction 0. |
| Size of the project | 0-10 | Budget of $50M or more 10, $10M-$50M 7, $1M-$10M 4, under $1M or not published 0. |
| Where the parcel sits | 5 or 15 | Abuts the mapped alignment (within 20 m) or is named in the document 15; nearby but not abutting (20-75 m) 5. |

A project's own score assumes a parcel on the alignment, so it equals the score of its best leads. The rules live
in `edradar/classify.py`; after changing them run `edradar rescore`.

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

## Daily updates

`scripts/daily_update.sh` runs the whole pipeline, republishes the public snapshot and prints how many new
signals, parcels and leads it found. Schedule it however you like (cron, launchd, a scheduled task). Set
`EDRADAR_PUBLISH=0` to refresh the data without republishing.

## Parcel details

Click any lead to open its parcel panel:

* **Boundary**: the county's mapped parcel outline is drawn on the map (with an aerial view) and measured: area,
  perimeter and the length of each main side. It is the county's GIS line, not a survey.
* **Land background**: land use, zoning, deeded acres, sale history, assessment and the full county record,
  as far as that county publishes them.
* **Other parcels held by this owner** anywhere in the lead list.
* **Similar cases**: Pennsylvania appellate opinions matching this kind of taking, from
  [CourtListener](https://www.courtlistener.com). Set `COURTLISTENER_TOKEN` (free account) to list them in the panel;
  without it the panel links to the same search on CourtListener's site. These are keyword matches, not legal research.

## Owner outreach

Each lead row has **Postcard** and **Email** actions. Nothing is sent automatically: every send is one
confirmed click, each is logged so an owner is not contacted twice, and both services stay off until
their key is set in the environment.

| | Service | Turn on | Notes |
|---|---|---|---|
| Postcard | [Lob](https://www.lob.com) | `LOB_API_KEY` + firm return address under `outreach.firm` | A `test_` key renders a proof and mails nothing; a `live_` key mails a 4x6 card and bills the Lob account. |
| Email | [Apollo](https://www.apollo.io) | `APOLLO_API_KEY` + `outreach.apollo.sequence_id`, `email_account_id` | Finds decision-makers at company owners and adds the one you pick to your Apollo sequence. Revealing an email uses one Apollo credit. |

**Outreach template.** "Outreach template" in the top bar is the one place to set the firm's name, attorney,
phone, email and return address and to edit the wording. The same text fills the email, the printed letter and
the postcard for every owner, using merge fields such as `{{owner_name}}`, `{{property}}`, `{{project_name}}`
and `{{firm_name}}`. Each parcel panel shows the finished letter with Open as email, Copy, Print letter and
Postcard actions. The template is saved in the database and also applies to command-line sends.

**Contact information.** The parcel panel lists the mailing address, any care-of name, the agency's project
manager, and for company owners the people Apollo finds there. County files carry no phone or email for
individual owners, and Pa. R.P.C. 7.3 restricts live telephone solicitation, so none is collected for them.

**Skip tracing.** For individual owners, "Skip trace this owner" in the parcel panel (or
`edradar skip-trace --min-score 90 --limit 25`, a dry run unless `--run`) looks up phone numbers and email through
[BatchData](https://batchdata.io) once `BATCHDATA_API_KEY` is set. Each owner and address is looked up once and the
answer is kept. A result whose surname does not match the owner of record is flagged, as are Do Not Call, TCPA and
known-litigator markers. Skip-traced details never go into the public snapshot.

**Billing clients.** Every matched skip trace and every postcard mailed with a live key is written to a usage ledger
with your cost and the client price from `billing:` in `config.yaml`. "Usage" in the top bar (or `edradar usage`)
shows counts, cost, amount billed and margin for the month and all time, with a CSV download. The app records
usage; it does not charge a card or send invoices.

**Finding email leads.** The lead table's outreach filter has "Email leads (company owners)" and
"Postcard-ready". `edradar find-emails` asks Apollo which company owners actually have contacts (search only,
no credits) so the filter and the Email button show real availability.

**Postcard campaigns.** "Postcard campaign" in the dashboard, or `edradar postcards --min-score 90 --limit 25`,
mails one card per owner and address, highest score first. Both preview the batch first; the command mails only
with `--send`. To mail automatically at the end of every `edradar run`, set `outreach.postcard.auto.enabled: true`
(with `min_score`, `max_per_run` and optional `counties`). An owner already mailed is never mailed again.

Public bodies (Commonwealth, municipalities, authorities) are never offered outreach. A postcard is
refused when the mailing address cannot be read reliably (about 1.5% of addresses), rather than guessed.
Apollo holds business contacts, so email works for company-owned parcels; individual owners get postcards.
The postcard wording lives in `edradar/outreach.py` and prints "ADVERTISEMENT." by default.

**Keys.** Put API keys in a file named `.env` in the project folder, one per line (`LOB_API_KEY=...`,
`APOLLO_API_KEY=...`, `BATCHDATA_API_KEY=...`, `COURTLISTENER_TOKEN=...`). The file is ignored by git and read
automatically by every command and by the dashboard.

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
edradar/outreach.py  Lob postcards and Apollo email sequences
edradar/pipeline.py  ingest -> classify -> geocode -> match
edradar/web/         FastAPI app and single-page dashboard
tests/               offline tests (pytest)
ci-workflows/        GitHub Actions workflows, to move into .github/workflows/
```

## Payments, postcards and owner lookups for subscribers

The published site is static, so anything that needs a secret runs in three Supabase Edge Functions under `supabase/functions`:

- `billing`: opens Stripe Checkout for the plan a firm chose at sign-up (charged at once, renews monthly or yearly) and Stripe's page for changing the card or cancelling.
- `stripe-webhook`: the only thing that opens or closes an account for payment reasons. Point a Stripe webhook at `https://<project>.supabase.co/functions/v1/stripe-webhook` with the events `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted` and `invoice.paid`.
- `outreach`: postcards through Lob and owner lookups through BatchData, with a usage ledger. Charges go on the firm's next invoice, or are charged at once when they reach $50.

Set up once:

1. Run `supabase/migrations/20261118_billing_outreach.sql` in the Supabase SQL editor.
2. `supabase login`, then `supabase functions deploy --project-ref <project> --use-api`.
3. In Supabase, Edge Functions, Secrets: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `LOB_API_KEY`, `BATCHDATA_API_KEY`. A `test_` Lob key makes proofs only; a `live_` key mails.

Test the shared rules with `/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc -m supabase/functions/_shared/core_test.js`.

import json

from fastapi.testclient import TestClient

from edradar import classify as C
from edradar import geo, parcels, pipeline
from edradar.db import DB
from edradar.web.app import create_app

LINE = {"paths": [[[-75.16, 39.95], [-75.15, 39.96]]]}


def penndot_signal(improvement, program="Under Development", cost=None):
    return {"source": "penndot", "title": "Test (MPMS 1)", "body": "somewhere", "geometry": json.dumps(LINE),
            "raw": json.dumps({"IMPROVEMENT_CODE_DESC": improvement, "program": program,
                               "PHS_EST_COST_AM": cost, "PRJCT_MNGR_CONTACT_NM": "A. Manager"})}


def test_penndot_rules_rank_new_roads_above_bridge_swaps():
    road = C.classify_penndot(penndot_signal("New Roadway", cost=60e6))
    bridge = C.classify_penndot(penndot_signal("Bridge Replacement"))
    assert road["score"] > 85 > 60 > bridge["score"]
    assert road["stage"] == "planning" and road["contact"] == "A. Manager"
    assert road["lat"] and road["geometry"] == LINE


def test_keywords_catch_condemnation_and_parcel_ids():
    out = C.classify_keywords({"source": "legistar:pittsburgh", "county": "ALLEGHENY",
                               "title": "Resolution authorizing the taking by eminent domain of parcel 39-F-280 "
                                        "at 120 Main Street", "body": ""})
    assert out["score"] >= 70 and out["stage"] == "condemnation_filed"
    assert out["parcel_ids_mentioned"] == ["39-F-280"]
    assert "120 Main Street" in out["locations"]


def test_keywords_ignore_unrelated_and_out_of_state():
    assert C.classify_keywords({"source": "x", "title": "Resurfacing contract award", "body": ""})["score"] == 0
    assert C.classify_keywords({"source": "x", "title": "Eminent domain in Ohio", "body": ""})["score"] == 0


def test_geo_helpers():
    assert geo.center(LINE) == (39.955, -75.155)
    long_line = {"paths": [[[i / 1000.0, 40.0] for i in range(5000)]]}
    thinned = geo.thin(long_line, 500)
    assert len(thinned["paths"][0]) <= 501 and thinned["paths"][0][-1] == long_line["paths"][0][-1]
    assert geo.to_geojson({"points": [[1, 2]]})["type"] == "MultiPoint"


def test_layer_selection_and_lead_scores():
    layers = [{"name": "phl", "counties": ["PHILADELPHIA"]}, {"name": "state"}]
    assert parcels.pick_layer(layers, "philadelphia")["name"] == "phl"
    assert parcels.pick_layer(layers, "BERKS")["name"] == "state"
    assert parcels.normalize_apn("ALLEGHENY", "39-F-280") == "0039F00280000000"
    assert parcels.normalize_apn("Allegheny", "76-D-1-0-2") == "0076D00001000002"
    assert parcels.normalize_apn("BERKS", " 12-34 ") == "12-34"
    assert pipeline.lead_score(80, "abuts_alignment") == 80
    assert pipeline.lead_score(80, "within_buffer") == 60
    assert pipeline.lead_score(95, "named_in_document") == 100


def seeded_db(path):
    db = DB(path)
    assert db.upsert_signal({"source": "penndot", "external_id": "1", "title": "Widen X", "county": "BERKS",
                             "geometry": LINE, "raw": {}})
    assert not db.upsert_signal({"source": "penndot", "external_id": "1", "title": "Widen X", "county": "BERKS"})
    sid = db.unclassified_signals()[0]["id"]
    pid = db.save_project(sid, {"name": "Widen X", "project_type": "highway", "stage": "planning",
                                "acquisition_likelihood": 85, "score": 82, "geometry": LINE,
                                "lat": 39.9, "lon": -75.1}, "rules")
    parcel = db.upsert_parcel({"layer": "t", "apn": "A-1", "owner": "Jane Roe", "address": "1 Main St"})
    db.upsert_lead(pid, parcel, 5.0, "abuts_alignment", 82)
    db.upsert_lead(pid, parcel, 50.0, "within_buffer", 60)  # must not downgrade
    db.commit()
    return db


def test_db_and_api(tmp_path):
    path = str(tmp_path / "t.db")
    db = seeded_db(path)
    lead = db.rows("SELECT * FROM leads")[0]
    assert (lead["score"], lead["distance_m"], lead["match_reason"]) == (82, 5.0, "abuts_alignment")
    assert db.unclassified_signals() == []

    client = TestClient(create_app({"database": path}))
    assert client.get("/").status_code == 200
    assert [x["projects"] for x in client.get("/api/stats").json()["stages"]] == [1, 0, 0, 0, 0, 0]
    assert len(client.get("/api/projects", params={"stage": "planning"}).json()) == 1
    assert client.get("/api/projects", params={"stage": "design"}).json() == []
    assert len(client.get("/api/leads", params={"stage": "planning"}).json()) == 1
    assert client.get("/api/leads", params={"stage": "construction"}).json() == []
    assert client.get("/api/stats").json()["leads_with_owner"] == 1
    projects = client.get("/api/projects").json()
    assert projects[0]["lead_count"] == 1 and projects[0]["geometry"] == LINE
    leads = client.get("/api/leads", params={"project_id": projects[0]["id"]}).json()
    assert leads[0]["owner"] == "Jane Roe"
    assert client.patch("/api/leads/%d" % leads[0]["id"], json={"status": "contacted"}).status_code == 200
    assert client.patch("/api/leads/%d" % leads[0]["id"], json={"status": "bogus"}).status_code == 400
    assert client.patch("/api/leads/999", json={"status": "new"}).status_code == 404
    assert "Jane Roe" in client.get("/api/leads.csv", params={"status": "contacted"}).text


def test_arcgis_row_joins_fields_and_builtin_catalog_loads():
    layer = {"counties": ["TEST"], "fields": {"apn": "PIN", "owner": ["O1", "O2"],
                                               "mailing_address": ["A1", "CITY", "ZIP"], "market_value": "V"}}
    row = parcels._arcgis_row(layer, {"PIN": " 12-3 ", "O1": "ROE JANE &", "O2": " ROE JOHN ", "A1": "1 MAIN ST  ",
                                      "CITY": "", "ZIP": 17046.0, "V": " 1200"}, True)
    assert row["apn"] == "12-3" and row["owner"] == "ROE JANE & ROE JOHN" and row["county"] == "TEST"
    assert row["mailing_address"] == "1 MAIN ST, 17046" and row["market_value"] == 1200.0
    assert parcels._arcgis_row(layer, {"PIN": None}, True) is None

    from edradar.config import load_config
    layers = load_config("config.example.yaml")["parcel_layers"]
    assert layers[0]["name"] == "Philadelphia OPA" and not layers[-1].get("counties")
    assert len({l["counties"][0] for l in layers if l.get("counties")}) >= 34
    assert all(l["fields"].get("apn") and l["fields"].get("owner") for l in layers[1:-1])


def test_static_export(tmp_path):
    path = str(tmp_path / "t.db")
    seeded_db(path)
    from edradar.web.app import export_static
    counts = export_static({"database": path}, str(tmp_path / "site"))
    assert counts == {"projects": 1, "leads": 1}
    data = json.loads((tmp_path / "site" / "data.json").read_text())
    assert data["leads"][0]["owner"] == "Jane Roe" and data["stats"]["generated"]
    assert "EDRADAR_STATIC = true" in (tmp_path / "site" / "index.html").read_text()


def test_owner_kind_and_address_parsing():
    from edradar.outreach import owner_kind, parse_address
    assert owner_kind("COMMONWEALTH OF PA") == "government"
    assert owner_kind("FRONT GATE ENTERPRISES LLC") == "company"
    assert owner_kind("OSMAN DENNE L & TONYA MARIE") == "individual"
    assert owner_kind(None) == "unknown"
    assert parse_address("C/O A SMITH, 486 PIKE RD, JOHNSTOWN, PA, 15909") == {
        "line1": "486 PIKE RD", "line2": "C/O A SMITH", "city": "JOHNSTOWN", "state": "PA", "zip": "15909"}
    assert parse_address("125 CORNELL RD, BALA CYNWYD PA 19004, 19004")["city"] == "BALA CYNWYD"
    assert parse_address("221 HOLYOKE RD BUTLER PA 16001")["line1"] == "221 HOLYOKE RD"
    for bad in ("HARRISBURG, PA, 17100", "1242 CORNWALL RD, LEBANON, PA", "", None,
                "925 W GEORGIA ST STE 800, VANCOUVER BC, V6C 3L2 CANADA"):
        assert parse_address(bad) is None


def test_postcard_and_email_flows(tmp_path, monkeypatch):
    import httpx
    from edradar import outreach

    cfg = {"outreach": {"firm": {"name": "Test Law", "address_line1": "1 Court St", "city": "Erie", "state": "PA",
                                 "zip": "16501", "phone": "555-0100"},
                        "postcard": {"advertising_label": True},
                        "apollo": {"sequence_id": "seq1", "email_account_id": "box1", "from_email": "a@b.test"}}}
    lead = {"id": 7, "owner": "ROE HOLDINGS LLC", "mailing_address": "9 OAK ST, ERIE, PA, 16501",
            "address": "12 MAIN ST", "apn": "A-1", "project": "Widen X (MPMS 1)", "stage": "planning"}

    monkeypatch.delenv("LOB_API_KEY", raising=False)
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    assert outreach.status(cfg)["postcard"]["mode"] == "off" and not outreach.status(cfg)["email"]["enabled"]
    check = outreach.postcard_check(cfg, lead)
    assert check["blocker"] is None and "ADVERTISEMENT" in check["back"] and "Widen X is in planning" in check["front"]
    assert outreach.postcard_check(cfg, dict(lead, owner="CITY OF ERIE"))["blocker"]
    assert outreach.postcard_check(cfg, dict(lead, mailing_address="ERIE PA"))["blocker"]

    sent = {}

    def lob(request):
        sent["body"] = request.content.decode()
        sent["idem"] = request.headers["Idempotency-Key"]
        return httpx.Response(200, json={"id": "psc_1", "url": "https://lob.test/proof.pdf"})

    monkeypatch.setenv("LOB_API_KEY", "test_abc")
    out = outreach.send_postcard(cfg, lead, httpx.Client(transport=httpx.MockTransport(lob)))
    assert out == {"external_id": "psc_1", "mode": "test", "proof_url": "https://lob.test/proof.pdf",
                   "expected_delivery": None}
    assert "use_type=marketing" in sent["body"] and "9+OAK+ST" in sent["body"] and sent["idem"] == "edradar-lead-7"

    calls = []

    def apollo(request):
        calls.append(request.url.path)
        assert request.headers["X-Api-Key"] == "k"
        if request.url.path.endswith("/mixed_people/api_search"):
            return httpx.Response(200, json={"people": [{"id": "p1", "first_name": "Jane", "last_name": "Roe",
                                                         "title": "Owner", "organization": {"name": "Roe Holdings"}}]})
        if request.url.path.endswith("/people/match"):
            return httpx.Response(200, json={"person": {"first_name": "Jane", "last_name": "Roe", "name": "Jane Roe",
                                                        "email": "jane@roe.test"}})
        if request.url.path.endswith("/contacts"):
            return httpx.Response(200, json={"contact": {"id": "c1"}})
        return httpx.Response(200, json={"contacts": []})

    monkeypatch.setenv("APOLLO_API_KEY", "k")
    ap = outreach.Apollo(cfg, httpx.Client(transport=httpx.MockTransport(apollo)))
    assert ap.find_contacts(lead)[0]["name"] == "Jane Roe"
    assert ap.enroll(lead, "p1")["email"] == "jane@roe.test"
    assert calls[-1].endswith("/emailer_campaigns/seq1/add_contact_ids")
    try:
        ap.find_contacts(dict(lead, owner="SMITH JOHN"))
        assert False
    except ValueError:
        pass

    # API layer: preview works, sending is refused cleanly while the service is off, and sends are logged once.
    path = str(tmp_path / "t.db")
    db = seeded_db(path)
    db.conn.execute("UPDATE parcels SET mailing_address='9 OAK ST, ERIE, PA, 16501'")
    db.commit()
    client = TestClient(create_app(dict(cfg, database=path)))
    lead_id = client.get("/api/leads").json()[0]["id"]
    assert client.get("/api/leads/%d/postcard" % lead_id).json()["to"]["city"] == "ERIE"
    monkeypatch.delenv("LOB_API_KEY")
    r = client.post("/api/leads/%d/postcard" % lead_id)
    assert r.status_code == 409 and "LOB_API_KEY" in r.json()["detail"]
    db.log_outreach(lead_id, "postcard", "test", "psc_1", {})
    row = client.get("/api/leads").json()[0]
    assert row["postcard_at"] and row["status"] == "contacted"
    assert client.post("/api/leads/%d/postcard" % lead_id).status_code == 409


def test_score_breakdown_adds_up():
    for likelihood, stage, cost in ((95, "planning", 60e6), (40, "design", None), (100, "planning", 700e6),
                                    (30, "construction", 2e6), (85, "row_acquisition", 12e6)):
        score = C.overall_score(likelihood, stage, cost)
        parts = C.score_breakdown(likelihood, stage, cost, score)
        assert abs(sum(p["points"] for p in parts) - score) <= 0.5, (likelihood, stage, parts)
    assert C.score_breakdown(90, "planning", None, 0)[-1]["label"] == "Not in Pennsylvania"


def test_parcel_detail(tmp_path, monkeypatch):
    import httpx
    from edradar import parceldetail as pd

    square = [[[-75.0, 40.0], [-75.0, 40.001], [-74.999, 40.001], [-74.999, 40.0], [-75.0, 40.0]]]
    m = pd._ring_metrics(square)
    assert 2.2 < m["acres"] < 2.5 and 1250 < m["perimeter_feet"] < 1300 and len(m["sides"]) == 4

    bg = pd.background({"attributes": {"OBJECTID": 5, "LANDUSE": "Farm", "DEED_ACRES": 8.98, "LastSaleDa": 1645056000000,
                                        "SALEPRICE": 340556, "ZONING": "A1", "Shape_Area": 9.1}})
    facts = {f["label"]: f["value"] for f in bg["facts"]}
    assert facts["Land use"] == "Farm" and facts["Zoning"] == "A1" and facts["Last sale price"] == "$340,556"
    assert facts["Last sale date"] == "Feb 17, 2022" and all(r["field"] != "OBJECTID" for r in bg["record"])

    lead = {"source": "penndot", "project_type": "highway", "match_reason": "abuts_alignment"}
    assert '"Department of Transportation"' in pd.case_query(lead) and "widening" in pd.case_query(lead)
    monkeypatch.delenv("COURTLISTENER_TOKEN", raising=False)
    off = pd.cases(lead)
    assert off["results"] == [] and "courtlistener.com/?" in off["search_url"] and "COURTLISTENER_TOKEN" in off["why"]

    def cl(request):
        assert request.headers["Authorization"] == "Token t"
        return httpx.Response(200, json={"results": [{"caseName": "Roe v. PennDOT", "court": "Commonwealth Court",
                                                       "dateFiled": "2019-03-01", "citation": ["203 A.3d 1"],
                                                       "absolute_url": "/opinion/1/roe/",
                                                       "opinions": [{"snippet": "a <mark>partial taking</mark> of land"}]}]})
    monkeypatch.setenv("COURTLISTENER_TOKEN", "t")
    on = pd.cases(lead, httpx.Client(transport=httpx.MockTransport(cl)))
    assert on["results"][0]["citation"] == "203 A.3d 1" and on["results"][0]["snippet"] == "a partial taking of land"

    path = str(tmp_path / "t.db")
    db = seeded_db(path)
    db.conn.execute("UPDATE parcels SET geometry=?, attributes=?", (json.dumps(square), json.dumps({"LANDUSE": "Farm"})))
    db.commit()
    client = TestClient(create_app({"database": path, "parcel_layers": [], "user_agent": "t"}))
    lead_id = client.get("/api/leads").json()[0]["id"]
    d = client.get("/api/leads/%d/detail" % lead_id).json()
    assert d["boundary"]["rings"] == square and d["background"]["facts"][0]["value"] == "Farm" and d["related"] == []


def test_timeline_and_owner_profile():
    from datetime import date
    from edradar import parceldetail as pd

    t = C.timeline("design", {"program": "Four Year Plan", "LET_DATE_DISPLAY": "10-18-29"}, today=date(2026, 11, 16))
    assert t["position"] == 3 and t["of"] == 6 and t["let_date"] == "2029-10-18" and t["months_to_letting"] == 35
    assert [s["state"] for s in t["steps"]] == ["done", "done", "current", "upcoming", "upcoming", "upcoming"]
    assert C.timeline("planning", {"LET_DATE_DISPLAY": "12-25-50"})["let_date"] is None     # placeholder year
    assert C.timeline("unknown")["position"] is None

    lead = {"owner": "ROE JANE", "address": "12 MAIN ST", "mailing_address": "12 MAIN STREET, ERIE, PA, 16501",
            "market_value": 100000}
    o = pd.owner_profile(lead, [{"label": "Last sale date", "value": "Feb 17, 2022"}], [{"market_value": 50000}])
    assert o["kind"] == "Individual" and o["location"] == "Pennsylvania" and "lives or operates" in o["occupancy"]
    assert o["owned_since"] == "Feb 17, 2022" and o["parcels_in_list"] == 2 and o["holdings_value"] == 150000
    away = pd.owner_profile(dict(lead, owner="ROE HOLDINGS LLC", mailing_address="9 OAK AVE, CARY, NC, 27519"), [], [])
    assert away["kind_key"] == "company" and away["location"] == "Out of state (NC)" and "absentee" in away["occupancy"]


def test_campaign_filter_and_snapshot(tmp_path, monkeypatch):
    import httpx
    from edradar import outreach
    from edradar.web.app import export_static

    path = str(tmp_path / "t.db")
    db = seeded_db(path)
    pid = db.rows("SELECT id FROM projects")[0]["id"]
    people = [("B-1", "ROE HOLDINGS LLC", "9 OAK ST, ERIE, PA, 16501", 90), ("B-2", "ROE HOLDINGS LLC", "9 OAK ST, ERIE, PA, 16501", 88),
              ("B-3", "CITY OF ERIE", "1 STATE ST, ERIE, PA, 16501", 95), ("B-4", "DOE JOHN", "ERIE PA", 93),
              ("B-5", "POE ANN", "4 ELM RD, ERIE, PA, 16502", 70)]
    for apn, owner, mail, score in people:
        db.upsert_lead(pid, db.upsert_parcel({"layer": "t", "apn": apn, "owner": owner, "mailing_address": mail}), 5.0,
                       "abuts_alignment", score)
    db.conn.execute("UPDATE parcels SET mailing_address='1 MAIN ST, ERIE, PA, 16501' WHERE apn='A-1'")
    db.commit()
    assert db.refresh_outreach_flags() >= 5
    kinds = {r["apn"]: (r["owner_kind"], r["mailable"]) for r in db.rows("SELECT * FROM parcels")}
    assert kinds["B-1"] == ("company", 1) and kinds["B-3"] == ("government", 1) and kinds["B-4"] == ("individual", 0)

    cfg = {"database": path, "parcel_layers": [], "user_agent": "t",
           "outreach": {"firm": {"name": "Test Law", "address_line1": "1 Court St", "city": "Erie", "state": "PA", "zip": "16501"},
                        "postcard": {}, "apollo": {}}}
    # one card per owner and address; public bodies, unreadable addresses and low scores are left out
    picked = [r["owner"] for r in outreach.campaign_leads(db, 80)]
    assert picked == ["ROE HOLDINGS LLC", "Jane Roe"]
    monkeypatch.delenv("LOB_API_KEY", raising=False)
    dry = outreach.run_campaign(cfg, db, 80)
    assert dry["dry_run"] and dry["eligible"] == 2 and dry["sent"] == []
    try:
        outreach.run_campaign(cfg, db, 80, dry_run=False)
        assert False
    except ValueError:
        pass
    monkeypatch.setenv("LOB_API_KEY", "test_x")
    lob = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"id": "psc_9"})))
    res = outreach.run_campaign(cfg, db, 80, limit=1, dry_run=False, client=lob)
    assert len(res["sent"]) == 1 and res["sent"][0]["owner"] == "ROE HOLDINGS LLC"
    # the same owner's second parcel is not mailed again
    assert [r["owner"] for r in outreach.campaign_leads(db, 80)] == ["Jane Roe"]

    client = TestClient(create_app(cfg))
    emails = client.get("/api/leads", params={"outreach": "email"}).json()
    assert {r["owner"] for r in emails} == {"ROE HOLDINGS LLC"}
    assert {r["owner"] for r in client.get("/api/leads", params={"outreach": "postcard"}).json()} == {"Jane Roe", "POE ANN"}
    assert len(client.get("/api/leads", params={"outreach": "contacted"}).json()) == 1
    preview = client.post("/api/postcards/campaign", json={"min_score": 80}).json()
    assert preview["dry_run"] and preview["eligible"] == 1

    apollo = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"people": []})))
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    assert outreach.scan_email_leads(cfg, db, client=apollo) == {"owners_checked": 1, "owners_with_contacts": 0}
    assert client.get("/api/leads", params={"outreach": "email"}).json() == []      # nobody found, so not an email lead

    counts = export_static(cfg, str(tmp_path / "site"))
    snap = json.loads((tmp_path / "site" / "data.json").read_text())
    row = next(r for r in snap["leads"] if r["owner"] == "Jane Roe")
    assert counts["leads"] == 6 and row["owner_kind"] == "individual" and row["own"][0] == "Pennsylvania" and "facts" in row


def test_outreach_template(tmp_path):
    from edradar import outreach

    path = str(tmp_path / "t.db")
    db = seeded_db(path)
    cfg = {"database": path, "parcel_layers": [], "user_agent": "t", "outreach": {"firm": {}, "postcard": {}, "apollo": {}}}
    client = TestClient(create_app(cfg))
    t = client.get("/api/outreach/template").json()
    assert "{{firm_name}}" in t["letter"] and t["firm"]["name"] == "" and "owner_name" in t["fields"]
    t["firm"].update(name="Duerr Law, P.C.", attorney="J. Duerr", phone="555-0100", address_line1="1 Court St", city="Erie",
                     state="PA", zip="16501")
    t["postcard_headline"] = "{{firm_name}} can help with {{property}}."
    saved = client.put("/api/outreach/template", json=t).json()
    assert saved["firm"]["name"] == "Duerr Law, P.C." and outreach.status(cfg)["firm_ready"]

    lead = {"id": 1, "owner": "ROE JANE", "address": "12 MAIN ST", "apn": "A-1", "parcel_county": "ERIE",
            "project": "Widen X (MPMS 1)", "stage": "design", "agency": "PennDOT District 01",
            "mailing_address": "12 MAIN ST, ERIE, PA, 16501"}
    v = outreach.merge_values(cfg, lead)
    assert v["property"] == "your property at 12 Main St" and v["project_name"] == "Widen X" and v["owner_name"] == "Roe Jane"
    letter = outreach.render(saved["letter"], v)
    assert "Duerr Law, P.C. represents" in letter and "{{" not in letter and "J. Duerr" in letter
    card = outreach.postcard_html(cfg, lead)
    assert "Duerr Law, P.C. can help with your property at 12 Main St." in card["front"] and "1 Court St" in card["back"]
    # a reopened app (and the command line) pick the saved template up again
    fresh = {"database": path, "outreach": {"firm": {}, "postcard": {}, "apollo": {}}}
    outreach.apply_saved(fresh, DB(path))
    assert fresh["outreach"]["firm"]["name"] == "Duerr Law, P.C."

    db.save_contacts("Jane Roe", [{"person_id": "p1", "name": "Jane Roe", "title": "Owner", "email": None}])
    db.save_contacts("Jane Roe", [{"person_id": "p1", "name": "Jane Roe", "title": "Owner", "email": "j@roe.test"}])
    assert db.contacts_for("Jane Roe")[0]["email"] == "j@roe.test"
    lead_id = client.get("/api/leads").json()[0]["id"]
    db.conn.execute("UPDATE parcels SET geometry='[[[0,0],[0,1],[1,1],[0,0]]]'"); db.commit()
    assert client.get("/api/leads/%d/detail" % lead_id).json()["contacts"][0]["name"] == "Jane Roe"


def test_skip_trace_and_usage(tmp_path, monkeypatch):
    import httpx
    from edradar import skiptrace

    path = str(tmp_path / "t.db")
    db = seeded_db(path)
    db.conn.execute("UPDATE parcels SET owner='ROE, JANE', mailing_address='12 MAIN ST, ERIE, PA, 16501'")
    db.commit()
    db.conn.execute("UPDATE parcels SET owner_kind=NULL"); db.commit(); db.refresh_outreach_flags()
    cfg = {"database": path, "parcel_layers": [], "user_agent": "t", "billing": {"skip_trace": {"cost": 0.25, "price": 1.0}},
           "outreach": {"firm": {}, "postcard": {}, "apollo": {}}}
    client = TestClient(create_app(cfg))
    lead = client.get("/api/leads").json()[0]

    req = skiptrace.build_request(lead)
    assert req["requests"][0]["propertyAddress"] == {"street": "12 MAIN ST", "city": "ERIE", "state": "PA", "zip": "16501"}
    assert req["requests"][0]["name"] == {"first": "Jane", "last": "Roe"} and req["options"] == {"includeTCPABlacklistedPhones": False}
    assert "name" not in skiptrace.build_request(dict(lead, owner="OSMAN DENNE L & TONYA MARIE"))["requests"][0]
    assert skiptrace.build_request(dict(lead, mailing_address="ERIE PA", parcel_county="ERIE"))["requests"][0]["apn"] == "A-1"
    assert skiptrace.build_request(dict(lead, mailing_address="ERIE PA", parcel_county=None)) is None

    monkeypatch.delenv("BATCHDATA_API_KEY", raising=False)
    r = client.post("/api/leads/%d/skip-trace" % lead["id"])
    assert r.status_code == 409 and "BATCHDATA_API_KEY" in r.json()["detail"]
    assert skiptrace.trace_batch(cfg, db, 0) == {"dry_run": True, "owners": 1, "matched": 0, "failed": 0, "max_cost": 0.25, "max_billed": 1.0}

    calls = []

    def api(request):
        calls.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json={"results": {"persons": [{
            "name": {"first": "jane", "last": "roe"}, "litigator": False, "meta": {"matched": True},
            "emails": [{"email": "jane@roe.test"}],
            "phoneNumbers": [{"number": "8145550101", "type": "Land Line", "score": 80, "reachable": True},
                             {"number": "8145550100", "type": "Mobile", "score": 100, "reachable": True}]}]}})

    monkeypatch.setenv("BATCHDATA_API_KEY", "k")
    http = httpx.Client(transport=httpx.MockTransport(api))
    out = skiptrace.trace(cfg, db, lead, http)
    assert out["matched"] and out["name_match"] and out["phones"][0]["number"] == "8145550100" and out["emails"] == ["jane@roe.test"]
    assert skiptrace.trace(cfg, db, lead, http)["cached"] and len(calls) == 1          # never paid for twice
    assert not skiptrace.parse_response("ROE, JANE", {"results": {"persons": [{"name": {"last": "Smith"}, "emails": [{"email": "x@y.z"}]}]}})["name_match"]
    assert skiptrace.parse_response("X", {"results": {"persons": []}})["matched"] is False

    usage = client.get("/api/usage").json()
    assert usage["all_time"] == [{"kind": "skip_trace", "count": 1, "cost": 0.25, "billed": 1.0, "margin": 0.75}]
    assert "skip_trace" in client.get("/api/usage.csv").text
    assert client.get("/api/leads/%d/detail" % lead["id"]).json()["skiptrace"]["emails"] == ["jane@roe.test"]
    assert len(client.get("/api/leads", params={"outreach": "traced"}).json()) == 1
    assert client.get("/api/outreach/status").json()["skiptrace"] == {"enabled": True, "why": None, "price": 1.0, "cost": 0.25}

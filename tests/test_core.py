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
    assert pipeline.lead_score(80, "within_buffer") == 70          # 15 position points become 5
    assert pipeline.lead_score(95, "named_in_document") == 95
    assert pipeline.lead_score(0, "abuts_alignment") == 0
    db = DB(":memory:")
    db.upsert_signal({"source": "penndot", "external_id": "9", "title": "T", "raw": {}})
    pid = db.save_project(db.unclassified_signals()[0]["id"], {"name": "T", "stage": "planning", "acquisition_likelihood": 95,
                                                                "score": 96, "est_cost": 60e6}, "rules")
    db.upsert_lead(pid, db.upsert_parcel({"layer": "t", "apn": "Z"}), None, "within_buffer", 72)
    db.commit()
    assert pipeline.rescore(db) == {"projects_changed": 1, "leads_changed": 1}
    assert db.rows("SELECT p.score ps, l.score ls FROM leads l JOIN projects p ON p.id=l.project_id") == [{"ps": 97, "ls": 87}]


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
    layers = [l for l in load_config("config.example.yaml")["parcel_layers"] if not l.get("state")]      # Pennsylvania's own
    assert layers[0]["name"] == "Philadelphia OPA" and not layers[-1].get("counties")
    assert len({l["counties"][0] for l in layers if l.get("counties")}) >= 34
    assert all(l["fields"].get("apn") and l["fields"].get("owner") for l in layers[1:-1])


def test_static_export(tmp_path):
    path = str(tmp_path / "t.db")
    seeded_db(path)
    from edradar.web.app import export_static
    counts = export_static({"database": path, "site": {"domain": "example.test"}}, str(tmp_path / "site"))
    assert (tmp_path / "site" / "CNAME").read_text() == "example.test\n"
    home = (tmp_path / "site" / "index.html").read_text()
    assert "for eminent domain attorneys" in home and "{{" not in home and (tmp_path / "site" / "signup.html").exists()
    assert (tmp_path / "site" / "auth-config.js").read_text() == 'window.ET_AUTH = {"url": "", "key": "", "google": false};\n'
    assert (tmp_path / "site" / "login.html").exists() and (tmp_path / "site" / "account.html").exists()
    blog = (tmp_path / "site" / "blog" / "index.html").read_text()
    post = (tmp_path / "site" / "blog" / "how-leads-are-scored.html").read_text()
    assert "how-leads-are-scored.html" in blog and "<h2>Need for private land: up to 60 points</h2>" in post and "{{" not in post
    assert "https://example.test/blog/how-leads-are-scored.html" in (tmp_path / "site" / "sitemap.xml").read_text()
    assert "Sitemap: https://example.test/sitemap.xml" in (tmp_path / "site" / "robots.txt").read_text()
    assert (tmp_path / "site" / "site.css").exists() and (tmp_path / "site" / "img" / "overview.jpg").exists()
    assert counts == {"projects": 1, "leads": 1}
    data = json.loads((tmp_path / "site" / "app" / "data.json").read_text())
    assert data["leads"][0]["owner"] == "Jane Roe" and data["stats"]["generated"]
    page = (tmp_path / "site" / "app" / "index.html").read_text()
    assert "EDRADAR_STATIC = true" in page and "EDRADAR_BUILD" in page and "data.json?v=" in page


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
    assert "use_type=marketing" in sent["body"] and "9+OAK+ST" in sent["body"] and sent["idem"].startswith("et-test-7-")
    assert "flex" not in check["front"] + check["back"] and "inset" not in check["front"] + check["back"]

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
    db.log_outreach(lead_id, "postcard", "test", "psc_1", {})        # a test proof is not a mailing
    row = client.get("/api/leads").json()[0]
    assert row["postcard_at"] is None and row["postcard_test_at"] and row["status"] == "new"
    db.log_outreach(lead_id, "postcard", "sent", "psc_2", {})
    row = client.get("/api/leads").json()[0]
    assert row["postcard_at"] and row["status"] == "contacted"
    assert client.post("/api/leads/%d/postcard" % lead_id).status_code == 409


def test_score_is_a_100_point_rubric():
    # new roadway (95), planning, $60M: 57 + 15 + 10 + 15
    assert C.overall_score(95, "planning", 60e6) == 97
    # bridge replacement (40), design, no budget: 24 + 12 + 0 + 15
    assert C.overall_score(40, "design", None) == 51
    assert C.overall_score(100, "planning", 1e9) == 100 and C.overall_score(0, "construction", None) == 15
    for likelihood, stage, cost in ((95, "planning", 60e6), (40, "design", None), (100, "planning", 700e6),
                                    (30, "construction", 2e6), (85, "row_acquisition", 12e6), (60, "unknown", 0)):
        score = C.overall_score(likelihood, stage, cost)
        parts = C.score_breakdown(likelihood, stage, cost, score)
        assert [x["key"] for x in parts] == ["need", "stage", "size", "position"]
        assert sum(x["points"] for x in parts) == score and sum(x["max"] for x in parts) == 100
        assert all(isinstance(x["points"], int) and 0 <= x["points"] <= x["max"] for x in parts)
    assert C.score_breakdown(90, "planning", None, 0)[0]["key"] == "outside"
    assert C.lead_points(97, "within_buffer") == 87 and C.lead_points(97, "abuts_alignment") == 97



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
    lob = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"id": "psc_9"})))
    monkeypatch.setenv("LOB_API_KEY", "test_x")
    res = outreach.run_campaign(cfg, db, 80, limit=1, dry_run=False, client=lob)
    assert len(res["sent"]) == 1 and res["mode"] == "test"
    assert len(outreach.campaign_leads(db, 80)) == 2           # a test proof does not use up the owner
    monkeypatch.setenv("LOB_API_KEY", "live_x")                # (mocked: nothing is really mailed)
    res = outreach.run_campaign(cfg, db, 80, limit=1, dry_run=False, client=lob)
    assert len(res["sent"]) == 1 and res["sent"][0]["owner"] == "ROE HOLDINGS LLC"
    # the same owner's second parcel is not mailed again
    assert [r["owner"] for r in outreach.campaign_leads(db, 80)] == ["Jane Roe"]

    client = TestClient(create_app(cfg))
    emails = client.get("/api/leads", params={"outreach": "email"}).json()
    assert {r["owner"] for r in emails} == {"ROE HOLDINGS LLC"}
    assert {r["owner"] for r in client.get("/api/leads", params={"outreach": "postcard"}).json()} == {"Jane Roe", "POE ANN"}
    assert len(client.get("/api/leads", params={"outreach": "contacted"}).json()) == 1
    hub = client.get("/api/outreach/hub").json()
    assert hub["sent"] == {"postcards": 1, "test_proofs": 1, "emails": 0, "skip_traces": 0}
    assert hub["ready"]["postcard_owners"] == 2 and hub["ready"]["email_companies"] == 1
    assert {a["status"] for a in hub["activity"]} == {"sent", "test"} and hub["activity"][0]["owner"]
    assert hub["usage"]["all_time"][0]["kind"] == "postcard" and hub["template"]["firm"]["name"] == "Test Law"
    preview = client.post("/api/postcards/campaign", json={"min_score": 80}).json()
    assert preview["dry_run"] and preview["eligible"] == 1

    apollo = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"people": []})))
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    assert outreach.scan_email_leads(cfg, db, client=apollo) == {"owners_checked": 1, "owners_with_contacts": 0}
    assert client.get("/api/leads", params={"outreach": "email"}).json() == []      # nobody found, so not an email lead

    counts = export_static(cfg, str(tmp_path / "site"))
    snap = json.loads((tmp_path / "site" / "app" / "data.json").read_text())
    row = next(r for r in snap["leads"] if r["owner"] == "Jane Roe")
    assert snap["hub"]["status"]["postcard"]["enabled"] is False and snap["hub"]["contacts"] == []
    assert snap["hub"]["sent"]["postcards"] == 1 and all(a["proof_url"] is None for a in snap["hub"]["activity"])
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
    assert outreach.merge_values(cfg, dict(lead, project="S 7th St Extension (Study (MPMS 104995)"))["project_name"] == "S 7th St Extension"
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


def test_env_file_supplies_keys(tmp_path, monkeypatch):
    from edradar.config import load_env_file

    f = tmp_path / ".env"
    f.write_text("# keys\nLOB_API_KEY=test_123\nEMPTY\nQUOTED='abc'\n")
    monkeypatch.delenv("LOB_API_KEY", raising=False)
    monkeypatch.setenv("QUOTED", "already-set")
    load_env_file(str(f))
    import os
    assert os.environ["LOB_API_KEY"] == "test_123" and os.environ["QUOTED"] == "already-set"
    monkeypatch.delenv("LOB_API_KEY")


def test_client_lists_run_postcards_and_email(tmp_path, monkeypatch):
    import httpx
    from edradar import outreach

    path = str(tmp_path / "t.db")
    db = seeded_db(path)
    pid = db.rows("SELECT id FROM projects")[0]["id"]
    for apn, owner, mail, score in [("B-1", "ROE HOLDINGS LLC", "9 OAK ST, ERIE, PA, 16501", 90),
                                    ("B-2", "ROE HOLDINGS LLC", "9 OAK ST, ERIE, PA, 16501", 88),
                                    ("B-3", "CITY OF ERIE", "1 STATE ST, ERIE, PA, 16501", 95),
                                    ("B-5", "POE ANN", "4 ELM RD, ERIE, PA, 16502", 70)]:
        db.upsert_lead(pid, db.upsert_parcel({"layer": "t", "apn": apn, "owner": owner, "mailing_address": mail}), 5.0,
                       "abuts_alignment", score)
    db.refresh_outreach_flags()
    cfg = {"database": path, "parcel_layers": [], "user_agent": "t",
           "outreach": {"firm": {"name": "Test Law", "address_line1": "1 Court St", "city": "Erie", "state": "PA", "zip": "16501"},
                        "postcard": {}, "apollo": {"sequence_id": "s1", "email_account_id": "m1", "from_email": "a@b.c"}}}
    monkeypatch.delenv("LOB_API_KEY", raising=False)
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    client = TestClient(create_app(cfg))
    ids = {r["apn"]: r["id"] for r in client.get("/api/leads").json()}
    made = client.post("/api/lists", json={"name": "Erie owners", "lead_ids": [ids["B-1"], ids["B-2"], ids["B-3"]]}).json()
    assert made["owners"] == 2 and made["postcard_ready"] == 1 and made["email_ready"] == 1
    again = client.post("/api/lists", json={"name": "Erie owners", "lead_ids": [ids["B-5"], ids["B-1"]]}).json()
    assert again["id"] == made["id"] and again["leads"] == 4 and again["postcard_ready"] == 2     # same name adds, no duplicates
    assert len(client.get("/api/leads", params={"list_id": made["id"]}).json()) == 4
    assert client.post("/api/lists", json={"name": "  ", "lead_ids": []}).status_code == 422

    dry = client.post("/api/lists/%d/campaign" % made["id"], json={}).json()
    assert dry["dry_run"] and [r["owner"] for r in dry["postcard"]["recipients"]] == ["ROE HOLDINGS LLC", "POE ANN"]
    assert [r["owner"] for r in dry["email"]["recipients"]] == ["ROE HOLDINGS LLC"]
    assert dry["skipped"] == [{"lead_id": ids["B-3"], "owner": "CITY OF ERIE", "why": "Public body"}]
    assert not dry["postcard"]["enabled"] and not dry["email"]["enabled"]
    # with nothing connected, a real run sends nothing rather than failing half way
    off = outreach.run_list(cfg, db, made["id"], dry_run=False)
    assert off["postcard"]["sent"] == [] and off["email"]["sent"] == []

    def handler(req):
        if "lob.com" in str(req.url):
            return httpx.Response(200, json={"id": "psc_1"})
        if req.url.path.endswith("/mixed_people/api_search"):
            return httpx.Response(200, json={"people": [{"id": "p1", "first_name": "Rae", "last_name": "Roe", "title": "Owner"}]})
        if req.url.path.endswith("/people/match"):
            return httpx.Response(200, json={"person": {"id": "p1", "name": "Rae Roe", "email": "rae@roe.com", "title": "Owner"}})
        return httpx.Response(200, json={"contact": {"id": "c1"}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("LOB_API_KEY", "live_x")          # (mocked: nothing is really mailed)
    monkeypatch.setenv("APOLLO_API_KEY", "k")
    res = outreach.run_list(cfg, db, made["id"], dry_run=False, client=http)
    assert len(res["postcard"]["sent"]) == 2 and res["email"]["sent"][0]["email"] == "rae@roe.com"
    after = outreach.list_summaries(db)[0]
    assert after["postcard_ready"] == 0 and after["email_ready"] == 0 and after["contacted"] == 2
    rerun = outreach.run_list(cfg, db, made["id"], dry_run=False, client=http)
    assert rerun["postcard"]["sent"] == [] and rerun["email"]["sent"] == []          # nobody is contacted twice

    assert client.post("/api/lists/%d/remove" % made["id"], json={"lead_ids": [ids["B-5"]]}).json()["leads"] == 3
    assert client.delete("/api/lists/%d" % made["id"]).json() == {"deleted": made["id"]}
    assert client.get("/api/lists").json() == [] and client.post("/api/lists/%d/campaign" % made["id"], json={}).status_code == 404


def test_private_data_upload(tmp_path, monkeypatch):
    import gzip
    import httpx
    from edradar.web.app import export_static

    path = str(tmp_path / "t.db")
    seeded_db(path)
    cfg = {"database": path, "site": {"supabase_url": "https://x.supabase.co", "supabase_anon_key": "pub"}}
    seen = {}

    def handler(req):
        seen["path"], seen["auth"], seen["body"] = req.url.path, req.headers["authorization"], req.content
        return httpx.Response(200, json={"Key": "app-data/data.json.gz"})

    monkeypatch.setenv("SUPABASE_SECRET_KEY", "sb_secret_test_zz9")
    export_static(cfg, str(tmp_path / "site"), upload_client=httpx.Client(transport=httpx.MockTransport(handler)))
    page = (tmp_path / "site" / "app" / "index.html").read_text()
    assert seen["path"] == "/storage/v1/object/app-data/states/pennsylvania.json.gz" and seen["auth"] == "Bearer sb_secret_test_zz9"
    assert "leads" in json.loads(gzip.decompress(seen["body"]))
    assert not (tmp_path / "site" / "app" / "data.json").exists() and "EDRADAR_PRIVATE = true" in page
    assert "sb_secret_test_zz9" not in page and "login.html?next=app" in page
    assert 'meta.access !== "active"' in page and 'EDRADAR_STATES = ["pennsylvania"]' in page
    # a failed upload falls back to the open file, so the page never points at data that is not there
    bad = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(403, json={"error": "no"})))
    export_static(cfg, str(tmp_path / "site2"), upload_client=bad)
    assert (tmp_path / "site2" / "app" / "data.json").exists()
    assert "EDRADAR_PRIVATE = true" not in (tmp_path / "site2" / "app" / "index.html").read_text()


def test_state_dot_source_and_classifier(tmp_path):
    from edradar.classify import classify_dot
    from edradar.parcels import pick_layer
    from edradar.sources import dot_arcgis
    from edradar.web.app import STATE_CODES, STATE_NAMES, US_STATES

    assert len(US_STATES) == 50 and len(STATE_CODES) == 50 and STATE_NAMES["NH"] == "New Hampshire"
    layer = {"state": "oh", "name": "ODOT projects", "key": "odot", "url": "https://x/FeatureServer/0", "agency": "Ohio DOT",
             "fields": {"id": "PID", "name": "PROJECT_NAME", "description": "DESCR", "work_type": ["WORK", "TYPE"],
                        "cost": "COST", "status": "PHASE", "let_date": "LET", "county": "COUNTY"}}
    attrs = {"PID": 77, "PROJECT_NAME": "FRA-70 widening", "DESCR": "Add lanes on I-70 from A to B", "WORK": None,
             "TYPE": "Major Widening", "COST": "$12,500,000", "PHASE": "Preliminary Engineering", "LET": 1924992000000,
             "COUNTY": "Franklin County"}
    sig = dot_arcgis.to_signal(layer, attrs, {"paths": [[[-83.0, 39.9], [-82.9, 39.95]]]})
    assert sig["source"] == "dot:OH" and sig["state"] == "OH" and sig["external_id"] == "odot:77" and sig["county"] == "FRANKLIN"
    assert sig["raw"]["_cost"] == 12500000.0 and sig["raw"]["_let"].startswith("2031-") and "Major Widening" in sig["body"]
    out = classify_dot(dict(sig, raw=json.dumps(sig["raw"]), geometry=json.dumps(sig["geometry"])))
    assert out["acquisition_likelihood"] == 85 and out["stage"] == "design" and out["project_type"] == "highway" and out["score"] >= 80
    paving = dot_arcgis.to_signal(layer, dict(attrs, PID=78, TYPE="Resurfacing", PROJECT_NAME="FRA-33 paving", DESCR="Mill and overlay"), None)
    assert classify_dot(paving)["acquisition_likelihood"] <= 10
    row = classify_dot(dot_arcgis.to_signal(layer, dict(attrs, PID=80, TYPE="86:RIGHT OF WAY", PHASE=None), None))
    assert row["acquisition_likelihood"] >= 95 and row["stage"] == "row_acquisition"
    lanes = classify_dot(dot_arcgis.to_signal(layer, dict(attrs, PID=81, TYPE="Statewide Highway", PROJECT_NAME="I-440", DESCR="Construct 2 lanes in each direction."), None))
    assert lanes["acquisition_likelihood"] >= 75
    built = dot_arcgis.to_signal(layer, dict(attrs, PID=79, PHASE="Under Construction"), None)
    assert classify_dot(built)["stage"] == "construction"

    db = DB(str(tmp_path / "s.db"))
    assert db.upsert_signal(sig) and db.rows("SELECT state FROM signals")[0]["state"] == "OH"
    layers = [{"name": "PA county", "counties": ["FRANKLIN"]}, {"name": "Ohio statewide", "state": "OH"}]
    assert pick_layer(layers, "FRANKLIN", "OH")["name"] == "Ohio statewide"       # same county name, different state
    assert pick_layer(layers, "FRANKLIN", "PA")["name"] == "PA county" and pick_layer(layers, "ADAMS", "NJ") is None


def test_state_files_become_sources(tmp_path, monkeypatch):
    from edradar import config

    files = [{"state": "Ohio", "code": "OH", "summary": "ok",
              "projects": [{"name": "ODOT", "url": "https://x/0", "verified": True, "fields": {"id": "PID", "name": "N"}},
                           {"name": "guess", "url": "https://x/1", "verified": False, "fields": {"id": "PID"}}],
              "parcels_statewide": {"name": "OH parcels", "url": "https://p/0", "verified": True, "fields": {"apn": "PIN", "owner": "OWNER"}},
              "parcels_counties": [{"county": "Franklin", "url": "https://f/0", "verified": True, "fields": {"apn": "ID", "owner": "OWN"}}]},
             {"state": "Texas", "code": "TX", "projects": [], "parcels_statewide": {
                 "name": "TX parcels", "url": "https://t/0", "verified": True, "restrictions": "no solicitation",
                 "fields": {"apn": "ID", "owner": "OWN"}}, "parcels_counties": []}]
    monkeypatch.setattr(config, "state_files", lambda: files)
    cfg = {"sources": {}, "parcel_layers": [{"name": "PA county", "counties": ["FRANKLIN"]}]}
    config.add_states(cfg)
    layers = cfg["sources"]["dot_arcgis"]["layers"]
    assert [x["url"] for x in layers] == ["https://x/0"] and layers[0]["state"] == "OH"          # unverified layers are never used
    names = [x["name"] for x in cfg["parcel_layers"]]
    assert names == ["PA county", "OH Franklin County", "OH statewide parcels"]                  # the restricted Texas layer is held back
    cov = {r["code"]: r for r in config.coverage(cfg)}
    assert len(cov) == 50 and sum(r["counties"] for r in cov.values()) == 3142          # every county in the country is on the list
    assert cov["OH"]["owners"] == "statewide" and cov["OH"]["counties_owners"] == cov["OH"]["counties"] == 88
    assert cov["TX"]["owners"] == "none" and cov["TX"]["held"] == "TX parcels" and cov["TX"]["counties_owners"] == 0
    assert cov["PA"]["projects"] and not cov["TX"]["projects"] and not cov["CA"]["researched"]


def test_federal_notices_are_filed_by_state():
    from edradar.sources.federal_register import state_of

    assert state_of("Notice of Intent for the I-11 corridor in Clark County, Nevada") == "NV"
    assert state_of("Transmission line from West Virginia to Virginia") == "WV"
    assert state_of("Meeting in Washington, DC on pipeline safety") is None
    assert state_of("Corps permit, Puget Sound, Washington") == "WA" and state_of("A notice naming no place") is None

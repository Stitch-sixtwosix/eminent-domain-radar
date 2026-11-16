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

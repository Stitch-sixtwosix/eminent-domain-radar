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

"""SQLite storage. Dependency-free so it runs anywhere (laptop, GitHub Actions, a small VM)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = """
-- Raw items pulled from a source (a PennDOT project, a Federal Register notice, a council bill).
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    url TEXT,
    agency TEXT,
    county TEXT,
    municipality TEXT,
    published TEXT,
    geometry TEXT,              -- Esri JSON in WGS84 ({"paths": ...} or {"x":..,"y":..}) when the source provides it
    raw TEXT,
    fetched_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source, external_id)
);

-- One row per signal after classification.
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    signal_id INTEGER NOT NULL UNIQUE REFERENCES signals(id),
    name TEXT,
    project_type TEXT,          -- highway, bridge, interchange, transit, rail, pipeline, transmission, water, utility, redevelopment, other
    stage TEXT,                 -- planning, environmental_review, design, row_acquisition, condemnation_filed, construction, unknown
    acquisition_likelihood INTEGER,  -- 0-100: will this project need private land?
    score INTEGER,              -- 0-100 overall lead value
    summary TEXT,
    est_cost REAL,
    contact TEXT,               -- agency project manager, when published
    locations TEXT,             -- JSON list of place strings to geocode (text sources)
    parcel_ids_mentioned TEXT,  -- JSON list of parcel IDs found in the text
    geometry TEXT,              -- Esri JSON in WGS84 (copied from signal or built by geocoding)
    lat REAL,
    lon REAL,
    engine TEXT,
    matched_at TEXT,
    classified_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS parcels (
    id INTEGER PRIMARY KEY,
    layer TEXT NOT NULL,
    apn TEXT NOT NULL,
    county TEXT,
    address TEXT,
    owner TEXT,
    mailing_address TEXT,
    market_value REAL,
    acres REAL,
    attributes TEXT,
    UNIQUE(layer, apn)
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    parcel_id INTEGER NOT NULL REFERENCES parcels(id),
    distance_m REAL,
    match_reason TEXT,           -- named_in_document, within_buffer
    score INTEGER,
    status TEXT DEFAULT 'new',   -- new, reviewing, contacted, retained, rejected
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(project_id, parcel_id)
);

CREATE INDEX IF NOT EXISTS idx_leads_score ON leads(score DESC);
CREATE INDEX IF NOT EXISTS idx_projects_score ON projects(score DESC);
"""


def _json(v: Any) -> Optional[str]:
    return None if v is None else json.dumps(v)


class DB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    # ---- signals ----
    def upsert_signal(self, s: Dict[str, Any]) -> bool:
        """Insert or refresh a signal; returns True if it was new.

        A refreshed signal whose title/body changed is re-queued for classification.
        """
        params = {k: s.get(k) for k in ("source", "external_id", "title", "body", "url", "agency",
                                          "county", "municipality", "published")}
        params["geometry"] = _json(s.get("geometry"))
        params["raw"] = _json(s.get("raw"))
        existing = self.conn.execute(
            "SELECT id, title, body FROM signals WHERE source=? AND external_id=?",
            (params["source"], params["external_id"]),
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """INSERT INTO signals (source, external_id, title, body, url, agency, county, municipality,
                                        published, geometry, raw)
                   VALUES (:source, :external_id, :title, :body, :url, :agency, :county, :municipality,
                           :published, :geometry, :raw)""",
                params,
            )
            self.conn.commit()
            return True
        if (existing["title"], existing["body"]) != (params["title"], params["body"]):
            self.conn.execute(
                """UPDATE signals SET title=:title, body=:body, url=:url, agency=:agency, county=:county,
                     municipality=:municipality, published=:published, geometry=:geometry, raw=:raw,
                     updated_at=datetime('now') WHERE source=:source AND external_id=:external_id""",
                params,
            )
            self.conn.execute("DELETE FROM projects WHERE signal_id=? AND NOT EXISTS "
                              "(SELECT 1 FROM leads WHERE leads.project_id=projects.id)", (existing["id"],))
            self.conn.commit()
        return False

    def unclassified_signals(self, limit: int = 100000) -> List[sqlite3.Row]:
        return self.conn.execute(
            """SELECT s.* FROM signals s LEFT JOIN projects p ON p.signal_id = s.id
               WHERE p.id IS NULL ORDER BY s.published DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    # ---- projects ----
    def save_project(self, signal_id: int, c: Dict[str, Any], engine: str) -> int:
        self.conn.execute(
            """INSERT INTO projects (signal_id, name, project_type, stage, acquisition_likelihood, score, summary,
                                     est_cost, contact, locations, parcel_ids_mentioned, geometry, lat, lon, engine)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(signal_id) DO UPDATE SET name=excluded.name, project_type=excluded.project_type,
                 stage=excluded.stage, acquisition_likelihood=excluded.acquisition_likelihood, score=excluded.score,
                 summary=excluded.summary, est_cost=excluded.est_cost, contact=excluded.contact,
                 locations=excluded.locations, parcel_ids_mentioned=excluded.parcel_ids_mentioned,
                 geometry=COALESCE(excluded.geometry, projects.geometry), lat=COALESCE(excluded.lat, projects.lat),
                 lon=COALESCE(excluded.lon, projects.lon), engine=excluded.engine, classified_at=datetime('now')""",
            (
                signal_id, c.get("name"), c.get("project_type"), c.get("stage"), c.get("acquisition_likelihood"),
                c.get("score"), c.get("summary"), c.get("est_cost"), c.get("contact"),
                json.dumps(c.get("locations") or []), json.dumps(c.get("parcel_ids_mentioned") or []),
                _json(c.get("geometry")), c.get("lat"), c.get("lon"), engine,
            ),
        )
        self.conn.commit()
        return self.conn.execute("SELECT id FROM projects WHERE signal_id=?", (signal_id,)).fetchone()["id"]

    def set_geometry(self, project_id: int, geometry: Dict[str, Any], lat: float, lon: float) -> None:
        self.conn.execute("UPDATE projects SET geometry=?, lat=?, lon=? WHERE id=?",
                          (json.dumps(geometry), lat, lon, project_id))
        self.conn.commit()

    def projects_needing_geocode(self, min_score: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, s.county AS signal_county, s.municipality AS signal_municipality
               FROM projects p JOIN signals s ON s.id=p.signal_id
               WHERE p.score >= ? AND p.geometry IS NULL AND p.locations != '[]' ORDER BY p.score DESC""",
            (min_score,),
        ).fetchall()

    def projects_to_match(self, min_score: int, limit: int) -> List[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, s.county FROM projects p JOIN signals s ON s.id=p.signal_id
               WHERE p.score >= ? AND p.matched_at IS NULL
                 AND (p.geometry IS NOT NULL OR p.parcel_ids_mentioned != '[]')
               ORDER BY p.score DESC LIMIT ?""",
            (min_score, limit),
        ).fetchall()

    def mark_matched(self, project_id: int) -> None:
        self.conn.execute("UPDATE projects SET matched_at=datetime('now') WHERE id=?", (project_id,))
        self.conn.commit()

    # ---- parcels & leads ----
    def upsert_parcel(self, p: Dict[str, Any]) -> int:
        row = {k: p.get(k) for k in ("layer", "apn", "county", "address", "owner", "mailing_address",
                                     "market_value", "acres")}
        row["attributes"] = json.dumps(p.get("attributes") or {})
        self.conn.execute(
            """INSERT INTO parcels (layer, apn, county, address, owner, mailing_address, market_value, acres, attributes)
               VALUES (:layer, :apn, :county, :address, :owner, :mailing_address, :market_value, :acres, :attributes)
               ON CONFLICT(layer, apn) DO UPDATE SET county=excluded.county, address=excluded.address,
                 owner=excluded.owner, mailing_address=excluded.mailing_address,
                 market_value=excluded.market_value, acres=excluded.acres, attributes=excluded.attributes""",
            row,
        )
        return self.conn.execute(
            "SELECT id FROM parcels WHERE layer=? AND apn=?", (p["layer"], p["apn"])
        ).fetchone()["id"]

    def upsert_lead(self, project_id: int, parcel_id: int, distance_m: Optional[float], reason: str, score: int) -> None:
        self.conn.execute(
            """INSERT INTO leads (project_id, parcel_id, distance_m, match_reason, score) VALUES (?,?,?,?,?)
               ON CONFLICT(project_id, parcel_id) DO UPDATE SET
                 distance_m=MIN(COALESCE(leads.distance_m, 1e12), COALESCE(excluded.distance_m, 1e12)),
                 match_reason=CASE WHEN excluded.score > leads.score THEN excluded.match_reason
                                   ELSE leads.match_reason END,
                 score=MAX(leads.score, excluded.score)""",
            (project_id, parcel_id, distance_m, reason, score),
        )

    def update_lead(self, lead_id: int, status: Optional[str], notes: Optional[str]) -> None:
        if status is not None:
            self.conn.execute("UPDATE leads SET status=? WHERE id=?", (status, lead_id))
        if notes is not None:
            self.conn.execute("UPDATE leads SET notes=? WHERE id=?", (notes, lead_id))
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def rows(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

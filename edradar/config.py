from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

DEFAULTS: Dict[str, Any] = {
    "database": "data/edradar.db",
    "user_agent": "edradar/0.1 (eminent-domain lead research)",
    "sources": {},
    "classifier": {"engine": "auto", "model": "claude-opus-5-5"},
    "geocoder": {"url": "https://nominatim.openstreetmap.org/search", "state": "Pennsylvania"},
    "parcel_layers": [],
    "outreach": {"firm": {}, "postcard": {"advertising_label": True,
                              "auto": {"enabled": False, "min_score": 90, "max_per_run": 25, "counties": []}}, "apollo": {}},
    "matching": {"min_project_score": 60, "abut_m": 20, "buffer_m": 75, "point_buffer_m": 150,
                 "max_parcels_per_project": 400, "projects_per_run": 150},
}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_env_file(path: str = ".env") -> None:
    """Read KEY=VALUE lines from a private .env file (never committed) so API keys need not live in the shell."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def state_files() -> list:
    import json

    out = []
    for f in sorted((Path(__file__).parent / "data" / "states").glob("*.json")):
        try:
            out.append(json.loads(f.read_text()))
        except ValueError as e:
            print("  ignoring %s: %s" % (f.name, e))
    return out


def _usable(item: Any, need_owner: bool = False) -> bool:
    """A researched layer is used only if it was verified with a live query and carries no unreviewed
    restriction on commercial use. A human sets "approved": true to clear a restriction."""
    if not isinstance(item, dict) or not item.get("verified") or not item.get("url"):
        return False
    if item.get("restrictions") and not item.get("approved"):
        return False
    return bool((item.get("fields") or {}).get("owner")) if need_owner else True


def add_states(cfg: Dict[str, Any]) -> None:
    """Turn each researched state file (edradar/data/states/XX.json) into a project source and parcel layers."""
    dot_layers, parcel_layers = [], []
    for st in state_files():
        code = (st.get("code") or "").upper()
        if len(code) != 2 or code == "PA":            # Pennsylvania has its own reader and county catalog
            continue
        for i, p in enumerate(st.get("projects") or []):
            if _usable(p) and p.get("fields"):
                dot_layers.append({"state": code, "name": p.get("name") or "%s projects" % code, "key": "%s%d" % (code.lower(), i),
                                   "url": p["url"], "fields": p["fields"], "where": p.get("where"), "stage": p.get("stage"),
                                   "max_records": p.get("max_records"),
                                   "agency": p.get("agency") or "%s DOT" % st.get("state", code), "info_url": p.get("info_url")})
        sw = st.get("parcels_statewide")
        counties = [c for c in (st.get("parcels_counties") or []) if _usable(c, need_owner=True)]
        for c in counties:                            # a county's own layer is preferred over the statewide one
            parcel_layers.append({"state": code, "name": "%s %s County" % (code, str(c.get("county", "")).title()),
                                  "provider": "arcgis", "counties": [str(c["county"]).upper()], "url": c["url"], "fields": c["fields"]})
        if _usable(sw) and (sw.get("fields") or {}).get("apn"):
            parcel_layers.append({"state": code, "name": "%s statewide parcels" % code, "provider": "arcgis",
                                  "url": sw["url"], "fields": sw["fields"]})
    extra = Path(__file__).parent / "data" / "sources" / "layers.json"      # regional, city and utility project maps
    if extra.exists():
        import json

        dot_layers += json.loads(extra.read_text())
    councils = Path(__file__).parent / "data" / "sources" / "legistar_clients.json"   # councils verified to answer
    if councils.exists():
        import json

        leg = cfg.setdefault("sources", {}).setdefault("legistar", {})
        have = {c.get("slug") for c in leg.get("clients") or []}
        leg["clients"] = (leg.get("clients") or []) + [c for c in json.loads(councils.read_text()) if c["slug"] not in have]
    if dot_layers:
        src = cfg.setdefault("sources", {}).setdefault("dot_arcgis", {})
        src.setdefault("enabled", True)
        src["layers"] = (src.get("layers") or []) + dot_layers
    cfg["parcel_layers"] = (cfg.get("parcel_layers") or []) + parcel_layers


def coverage(cfg: Dict[str, Any]) -> list:
    """Every state, counted county by county: is there a project source, and how many counties have owner records."""
    import json

    counties = json.loads((Path(__file__).parent / "data" / "us_counties.json").read_text())
    names = {"PA": "Pennsylvania"}
    files = {(st.get("code") or "").upper(): st for st in state_files()}
    pa_owner = len({c.upper() for layer in cfg.get("parcel_layers") or [] if not layer.get("state")
                    for c in layer.get("counties") or []})
    rows = []
    for code in sorted(counties):
        total, st = len(counties[code]), files.get(code)
        if code == "PA":
            rows.append({"code": "PA", "state": "Pennsylvania", "researched": True, "projects": True, "counties": total,
                         "counties_projects": total, "counties_owners": min(pa_owner, total), "owners": "%d counties" % pa_owner,
                         "held": None, "note": "PennDOT program plus federal notices and Pittsburgh legislation"})
            continue
        if not st:
            rows.append({"code": code, "state": code, "researched": False, "projects": False, "counties": total,
                         "counties_projects": 0, "counties_owners": 0, "owners": "none", "held": None, "note": "Not researched yet"})
            continue
        sw, by_county = st.get("parcels_statewide"), st.get("parcels_counties") or []
        ok_counties = {str(c.get("county", "")).upper() for c in by_county if _usable(c, need_owner=True)}
        statewide = _usable(sw, need_owner=True)
        has_projects = any(_usable(p) for p in st.get("projects") or [])
        held = [x.get("name") or x.get("county") for x in [sw] + by_county
                if isinstance(x, dict) and ((x.get("restrictions") and not x.get("approved")) or x.get("held"))]
        rows.append({"code": code, "state": st.get("state") or code, "researched": True, "projects": has_projects, "counties": total,
                     "counties_projects": total if has_projects else 0,
                     "counties_owners": min(total, int((sw or {}).get("counties_covered") or total)) if statewide
                     else min(len(ok_counties), total),
                     "owners": ("statewide" if not (sw or {}).get("counties_covered") else "%d counties in one state file" % sw["counties_covered"])
                     if statewide else ("%d counties" % len(ok_counties) if ok_counties else "none"),
                     "held": ", ".join(str(h) for h in held if h) or None, "note": st.get("summary")})
    return sorted(rows, key=lambda r: r["state"])


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    load_env_file()
    path = path or os.environ.get("EDRADAR_CONFIG") or "config.yaml"
    p = Path(path)
    if not p.exists():
        p = Path("config.example.yaml")
    data = yaml.safe_load(p.read_text()) if p.exists() else {}
    cfg = _merge(DEFAULTS, data or {})
    if cfg.get("use_builtin_pa_layers", True):
        # Order: the user's county layers, then the built-in county catalog, then statewide fallbacks.
        builtin = yaml.safe_load((Path(__file__).parent / "data" / "pa_parcel_layers.yaml").read_text())
        mine = cfg.get("parcel_layers") or []
        covered = {c.upper() for layer in mine for c in layer.get("counties") or []}
        builtin = [b for b in builtin if b["counties"][0] not in covered]
        cfg["parcel_layers"] = ([x for x in mine if x.get("counties")] + builtin
                                + [x for x in mine if not x.get("counties")])
    add_states(cfg)
    if os.environ.get("EDRADAR_DB"):
        cfg["database"] = os.environ["EDRADAR_DB"]
    return cfg


OWNER_FIELDS = [("owner", "owner name"), ("mailing_address", "mailing address"), ("address", "property address"),
                ("market_value", "value"), ("acres", "acreage"), ("apn", "parcel number")]
PROJECT_FIELDS = [("name", "name"), ("description", "description"), ("work_type", "type of work"), ("status", "status"),
                  ("cost", "cost"), ("let_date", "letting date"), ("date", "date"), ("county", "county")]


def _reports(fields: Any, known: list) -> list:
    """Which of the things we look for a source actually publishes, in plain words."""
    f = fields if isinstance(fields, dict) else {}
    alias = {"mailing_address": ("mailing_address", "mail", "mail_address", "mailing"), "address": ("address", "situs", "site_address"),
             "market_value": ("market_value", "value", "assessed_value", "total_value")}
    return [label for key, label in known if any(f.get(k) for k in alias.get(key, (key,)))]


def registry(cfg: Dict[str, Any], db: Any = None) -> Dict[str, Any]:
    """Every source the system knows about, in use or not: what it is, where it lives, how it reports its data,
    whether it is on, why not if it is off, and what has been loaded from it. Plus every county with no owner file."""
    import json

    loaded: Dict[Any, Any] = {}
    reads: Dict[Any, Any] = {}
    parcels: Dict[str, int] = {}
    if db is not None:
        for r in db.rows("SELECT source, CASE WHEN instr(external_id, ':') > 0 THEN substr(external_id, 1, instr(external_id, ':') - 1) "
                         "ELSE '' END AS k, COUNT(*) AS n, MAX(fetched_at) AS at FROM signals GROUP BY 1, 2"):
            loaded[(r["source"], r["k"])] = (r["n"], r["at"])
            n0, at0 = loaded.get(r["source"], (0, ""))
            loaded[r["source"]] = (n0 + r["n"], max(at0 or "", r["at"] or ""))
        parcels = {r["layer"]: r["n"] for r in db.rows("SELECT layer, COUNT(*) AS n FROM parcels GROUP BY 1")}
        try:
            for r in db.rows("SELECT source, key, tried_at, ok_at, records, error FROM source_reads"):
                reads[(r["source"], r["key"]) if r["key"] else r["source"]] = r
        except Exception:                # a database from before reads were recorded
            pass
    out = []

    def add(kind, state, name, url, how, reports, status, why=None, records=None, at=None, covers=None, flt=None, read=None):
        # "updated" is when the source last answered, once that is recorded; before that, when its newest record arrived
        row = {"kind": kind, "state": state, "name": name, "url": url, "how": how, "reports": reports, "status": status,
               "why": why, "loaded": records or 0, "updated": (read or {}).get("ok_at") or at, "covers": covers, "filter": flt}
        if kind == "projects":
            row.update(newest=at, tried=(read or {}).get("tried_at"), error=(read or {}).get("error"), measured=bool(read))
        out.append(row)

    src = cfg.get("sources") or {}
    active = {l.get("key") for l in (src.get("dot_arcgis") or {}).get("layers") or []}
    for l in (src.get("dot_arcgis") or {}).get("layers") or []:
        prefix = l.get("source_prefix") or "dot"
        n, at = loaded.get(("%s:%s" % (prefix, l["state"].upper()), l.get("key")), (0, None))
        add("projects", l["state"].upper(), l.get("name"), l.get("url"), "Map service, queried record by record",
            _reports(l.get("fields"), PROJECT_FIELDS), "on", None, n, at,
            l.get("coverage") or ("statewide" if prefix == "dot" else None),
            l.get("where") or "none: every record is loaded",
            read=reads.get(("%s:%s" % (prefix, l["state"].upper()), l.get("key") or l["name"][:24])))
    n, at = loaded.get("penndot", (0, None))
    for l in (src.get("penndot") or {}).get("layers") or []:
        add("projects", "PA", "PennDOT: %s" % (l.get("name") if isinstance(l, dict) else l), (l.get("url") if isinstance(l, dict) else None),
            "Map service, queried record by record", ["name", "description", "type of work", "status", "cost", "letting date", "county"],
            "on", None, n, at, "statewide", read=reads.get("penndot"))
    n, at = loaded.get("federal_register", (0, None))
    add("projects", "US", "Federal Register notices", "https://www.federalregister.gov/api/v1/documents.json", "Search API, notice text read for the project",
        ["name", "description", "agency", "date"], "on" if (src.get("federal_register") or {}).get("enabled") else "off", None, n, at, "nationwide")
    for c in (src.get("legistar") or {}).get("clients") or []:
        n, at = loaded.get("legistar:%s" % c.get("slug"), (0, None))
        add("projects", (c.get("state") or "PA").upper(), "%s council legislation" % (c.get("name") or c.get("slug")),
            "https://webapi.legistar.com/v1/%s/matters" % c.get("slug"), "Legislation API, matter text read for takings and acquisitions",
            ["name", "description", "status", "date"], "on", None, n, at, c.get("name"))
    for st in state_files():
        code = (st.get("code") or "").upper()
        for p in st.get("projects") or []:
            if isinstance(p, dict) and not (_usable(p) and p.get("fields")) and code != "PA":
                add("projects", code, p.get("name"), p.get("url"), "Map service", _reports(p.get("fields"), PROJECT_FIELDS), "off",
                    p.get("held") or ("not confirmed by a live query" if not p.get("verified") else "no usable field mapping"))
        entries = [("statewide", st.get("parcels_statewide"))] + [(str(c.get("county") or "").title() + " County", c)
                                                                    for c in st.get("parcels_counties") or [] if isinstance(c, dict)]
        for where, item in entries:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            layer = "%s statewide parcels" % code if where == "statewide" else "%s %s" % (code, where)
            if item.get("held"):
                status, why = "off", str(item["held"])
            elif not item.get("verified"):
                status, why = "off", "not confirmed by a live query"
            elif item.get("restrictions") and not item.get("approved"):
                status, why = "held", str(item["restrictions"])
            elif not (item.get("fields") or {}).get("owner"):
                status, why = "off", "publishes no owner name"
            else:
                status, why = "on", item.get("approved_note")
            add("owners", code, item.get("name") or layer, item.get("url"), "Parcel map service, queried around each project",
                _reports(item.get("fields"), OWNER_FIELDS), status, why, parcels.get(layer), None,
                ("%d counties" % item["counties_covered"]) if item.get("counties_covered") else where)
    for l in cfg.get("parcel_layers") or []:
        if not l.get("state"):                       # Pennsylvania's county catalog
            add("owners", "PA", l.get("name"), l.get("url"), "Parcel service (%s), queried around each project" % (l.get("provider") or "arcgis"),
                _reports(l.get("fields"), OWNER_FIELDS) or ["owner name", "mailing address", "property address"], "on", None,
                parcels.get(l.get("name")), None, ", ".join(str(c).title() for c in l.get("counties") or []))
    counties = json.loads((Path(__file__).parent / "data" / "us_counties.json").read_text())
    names = {(st.get("code") or "").upper(): st.get("state") for st in state_files()}
    names["PA"] = "Pennsylvania"
    gaps = []
    for code in sorted(counties):
        on = [s for s in out if s["kind"] == "owners" and s["state"] == code and s["status"] == "on"]
        whole = any(s["covers"] == "statewide" for s in on)
        partial = next((s["covers"] for s in on if str(s["covers"]).endswith("counties") and "statewide" in str(s["name"]).lower()), None)
        have = set()
        for s in on:
            for c in str(s["covers"] or "").split(","):
                have.add(c.replace(" County", "").strip().upper())
        missing = [] if (whole or partial) else [c for c in counties[code] if c.upper() not in have]
        n_missing = max(0, len(counties[code]) - int(str(partial).split()[0])) if partial and not whole else len(missing)
        gaps.append({"code": code, "state": names.get(code) or code, "counties": len(counties[code]), "missing_count": n_missing,
                     "missing": [c.title() for c in missing], "partial_statewide": partial,
                     "projects_on": sum(1 for s in out if s["kind"] == "projects" and s["state"] == code and s["status"] == "on"),
                     "statewide_projects": any(s["kind"] == "projects" and s["state"] == code and s["status"] == "on"
                                               and s["covers"] == "statewide" for s in out)})
    return {"sources": out, "gaps": gaps}

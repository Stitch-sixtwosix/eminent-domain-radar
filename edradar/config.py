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
    if dot_layers:
        src = cfg.setdefault("sources", {}).setdefault("dot_arcgis", {})
        src.setdefault("enabled", True)
        src["layers"] = (src.get("layers") or []) + dot_layers
    cfg["parcel_layers"] = (cfg.get("parcel_layers") or []) + parcel_layers


def coverage(cfg: Dict[str, Any]) -> list:
    """What each state has: a project source, owner records statewide or by county, and anything held back."""
    rows = [{"code": "PA", "state": "Pennsylvania", "projects": True, "owners": "35 counties",
             "held": None, "note": "PennDOT program plus federal notices and Pittsburgh legislation"}]
    for st in state_files():
        code = (st.get("code") or "").upper()
        if len(code) != 2 or code == "PA":
            continue
        sw, counties = st.get("parcels_statewide"), st.get("parcels_counties") or []
        ok_counties = [c for c in counties if _usable(c, need_owner=True)]
        held = [x.get("name") or x.get("county") for x in [sw] + counties if isinstance(x, dict) and x.get("restrictions") and not x.get("approved")]
        rows.append({"code": code, "state": st.get("state") or code,
                     "projects": any(_usable(p) for p in st.get("projects") or []),
                     "owners": "statewide" if _usable(sw, need_owner=True) else ("%d counties" % len(ok_counties) if ok_counties else "none"),
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

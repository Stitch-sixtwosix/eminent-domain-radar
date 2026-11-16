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
    "outreach": {"firm": {}, "postcard": {"advertising_label": True}, "apollo": {}},
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


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
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
    if os.environ.get("EDRADAR_DB"):
        cfg["database"] = os.environ["EDRADAR_DB"]
    return cfg

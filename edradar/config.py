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
    if os.environ.get("EDRADAR_DB"):
        cfg["database"] = os.environ["EDRADAR_DB"]
    return cfg

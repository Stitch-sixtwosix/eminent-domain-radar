"""Tiny geometry helpers over Esri-JSON-style dicts in WGS84: {"paths": [...]}, {"points": [...]}."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def coords(geometry: Optional[Dict[str, Any]]) -> List[List[float]]:
    if not geometry:
        return []
    if "paths" in geometry:
        return [pt for path in geometry["paths"] for pt in path]
    if "points" in geometry:
        return list(geometry["points"])
    if "x" in geometry and "y" in geometry:
        return [[geometry["x"], geometry["y"]]]
    return []


def center(geometry: Optional[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
    """(lat, lon) of the middle of the geometry's bounding box."""
    pts = coords(geometry)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return ((min(ys) + max(ys)) / 2.0, (min(xs) + max(xs)) / 2.0)


def is_linear(geometry: Optional[Dict[str, Any]]) -> bool:
    return bool(geometry and geometry.get("paths"))


def thin(geometry: Dict[str, Any], max_vertices: int = 600) -> Dict[str, Any]:
    """Drop vertices evenly so very long alignments stay within request size limits."""
    if not is_linear(geometry):
        return geometry
    total = sum(len(p) for p in geometry["paths"])
    if total <= max_vertices:
        return geometry
    step = int(math.ceil(total / float(max_vertices)))
    paths = []
    for p in geometry["paths"]:
        kept = p[::step]
        if kept[-1] != p[-1]:
            kept.append(p[-1])
        if len(kept) >= 2:
            paths.append(kept)
    return {"paths": paths}


def to_geojson(geometry: Dict[str, Any]) -> Dict[str, Any]:
    if is_linear(geometry):
        return {"type": "MultiLineString", "coordinates": geometry["paths"]}
    return {"type": "MultiPoint", "coordinates": coords(geometry)}

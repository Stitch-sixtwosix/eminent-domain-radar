"""Tiny geometry helpers over Esri-JSON-style dicts in WGS84: {"paths": [...]}, {"points": [...]}."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple


def coords(geometry: Optional[Dict[str, Any]]) -> List[List[float]]:
    """Every [x, y] in the geometry. Sources differ, so polygons ("rings") are read too, and any point that is
    not a pair of finite numbers (text, NaN, a point outside the globe) is dropped instead of crashing a run."""
    if not geometry:
        return []
    if "paths" in geometry:
        raw = [pt for path in geometry["paths"] or [] for pt in path or []]
    elif "rings" in geometry:
        raw = [pt for ring in geometry["rings"] or [] for pt in ring or []]
    elif "points" in geometry:
        raw = list(geometry["points"] or [])
    elif "x" in geometry and "y" in geometry:
        raw = [[geometry["x"], geometry["y"]]]
    else:
        return []
    out: List[List[float]] = []
    for pt in raw:
        try:
            x, y = float(pt[0]), float(pt[1])
        except (TypeError, ValueError, IndexError):
            continue
        if math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -90 <= y <= 90:
            out.append([x, y])
    return out


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

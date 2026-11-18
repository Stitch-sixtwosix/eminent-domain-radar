"""Work out which county a project is in from its map position, using the Census Bureau's county boundaries.

Many project lists give a location but no county. Owner files are chosen by county, so without one a project
can never be matched to parcels even where the county publishes them.
"""
from typing import Any, Dict, List, Optional, Tuple

URL = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1/query"
FIPS = {"AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10", "DC": "11",
        "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
        "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29", "MT": "30",
        "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
        "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49",
        "VT": "50", "VA": "51", "WA": "53", "WV": "54", "WI": "55", "WY": "56"}

Shape = Tuple[str, Tuple[float, float, float, float], List[List[List[float]]]]


def shapes(features: List[Dict[str, Any]]) -> List[Shape]:
    out = []
    for f in features:
        rings = (f.get("geometry") or {}).get("rings") or []
        name = ((f.get("attributes") or {}).get("BASENAME") or "").strip().upper()
        pts = [p for r in rings for p in r]
        if name and pts:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            out.append((name, (min(xs), min(ys), max(xs), max(ys)), rings))
    return out


def inside(rings: List[List[List[float]]], x: float, y: float) -> bool:
    """Even-odd test over every ring, which handles holes and counties made of several pieces."""
    hit = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
            j = i
    return hit


def find(state_shapes: List[Shape], lon: float, lat: float) -> Optional[str]:
    for name, (x0, y0, x1, y1), rings in state_shapes:
        if x0 <= lon <= x1 and y0 <= lat <= y1 and inside(rings, lon, lat):
            return name
    return None


def fetch(http: Any, state: str) -> List[Shape]:
    data = http.get_json(URL, {"where": "STATE='%s'" % FIPS[state], "outFields": "BASENAME", "returnGeometry": "true",
                               "outSR": 4326, "maxAllowableOffset": 0.001, "f": "json"})
    return shapes(data.get("features") or [])


def fill(db: Any, http: Any) -> int:
    """Give a county to every located project that has none. Returns how many were filled."""
    rows = db.rows("SELECT s.id AS signal_id, s.state, p.lat, p.lon FROM projects p JOIN signals s ON s.id = p.signal_id "
                   "WHERE (s.county IS NULL OR s.county = '') AND p.lat IS NOT NULL AND p.lon IS NOT NULL")
    by_state: Dict[str, List[Any]] = {}
    for r in rows:
        if (r["state"] or "").upper() in FIPS:
            by_state.setdefault(r["state"].upper(), []).append(r)
    total = 0
    for state in sorted(by_state):
        try:
            state_shapes = fetch(http, state)
        except Exception as e:
            print("  counties for %s: %s" % (state, str(e)[:120]))
            continue
        done = 0
        for r in by_state[state]:
            county = find(state_shapes, r["lon"], r["lat"])
            if county:
                db.conn.execute("UPDATE signals SET county=? WHERE id=?", (county, r["signal_id"]))
                done += 1
        db.conn.commit()
        total += done
        print("  %s: county found for %d of %d located projects" % (state, done, len(by_state[state])))
    return total

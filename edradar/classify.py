"""Turn a raw signal into a scored project.

Three engines:
  * PennDOT records are structured, so they are scored by transparent rules (no API cost).
  * Free-text signals (Federal Register, council legislation) go to Claude when credentials exist.
  * A keyword engine is the offline fallback for free text.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import geo

PROJECT_TYPES = ["highway", "bridge", "interchange", "transit", "rail", "pipeline", "transmission", "water",
                 "utility", "redevelopment", "airport", "other"]
STAGES = ["planning", "environmental_review", "design", "row_acquisition", "condemnation_filed",
          "construction", "unknown"]

# How early a stage is. Earlier stages are worth more as leads: owners have not been approached yet.
# ---------------------------------------------------------------- the 100-point score
# Every score is whole points from four components that add up to at most 100:
#   1. Need for private land   0-60   how likely this kind of project is to take land (likelihood x 0.6)
#   2. How early it is         0-15   earlier stages score higher: owners have not been approached yet
#   3. Size of the project     0-10   from the published budget
#   4. Where the parcel sits   0-15   on the alignment or named in the document 15, nearby 5
# A project's own score assumes a parcel on the alignment (the full 15), so it equals the score of its best leads.
MAX_POINTS = {"need": 60, "stage": 15, "size": 10, "position": 15}
STAGE_POINTS = {"planning": 15, "environmental_review": 15, "design": 12, "row_acquisition": 8,
                "condemnation_filed": 4, "unknown": 6, "construction": 0}
POSITION_POINTS = {"named_in_document": 15, "abuts_alignment": 15, "within_buffer": 5}
POSITION_LABEL = {"named_in_document": "Parcel is named in the source document",
                  "abuts_alignment": "Parcel abuts the mapped alignment (within 20 m)",
                  "within_buffer": "Parcel is nearby but not abutting (20 to 75 m)"}


def need_points(likelihood: Optional[int]) -> int:
    return max(0, min(60, int(round((likelihood or 0) * 0.6))))


def size_points(est_cost: Optional[float]) -> int:
    if not est_cost:
        return 0
    return 10 if est_cost >= 50e6 else 7 if est_cost >= 10e6 else 4 if est_cost >= 1e6 else 0


def size_label(est_cost: Optional[float]) -> str:
    if not est_cost:
        return "No budget published"
    return ("Budget of $50M or more" if est_cost >= 50e6 else "Budget of $10M to $50M" if est_cost >= 10e6
            else "Budget of $1M to $10M" if est_cost >= 1e6 else "Budget under $1M")


# PennDOT improvement type -> (likelihood that private land is needed, project type)
PENNDOT_RULES: Dict[str, Tuple[int, str]] = {
    "New Roadway": (95, "highway"),
    "New Interchange": (95, "interchange"),
    "Relocation/Realignment": (90, "highway"),
    "Add Lane": (85, "highway"),
    "Widen": (85, "highway"),
    "New Bridge": (75, "bridge"),
    "Interchange Improvement": (70, "interchange"),
    "Park and Ride Lot": (70, "transit"),
    "Intersection Improvement": (60, "highway"),
    "Add Turning Lane": (60, "highway"),
    "Reconstruct": (45, "highway"),
    "Bridge Replacement": (40, "bridge"),
    "Transit System Improvement": (35, "transit"),
    "Intermodal Services": (35, "transit"),
    "Corridor Safety Improvement": (35, "highway"),
    "Slides Correction": (35, "highway"),
    "Safety Improvement": (30, "highway"),
    "New Traffic Signal": (25, "highway"),
    "Pedestrian Facilities": (25, "other"),
    "Bicycle Facilities/Services": (20, "other"),
    "Drainage Improvement": (20, "other"),
}

PROGRAM_STAGE = {"Twelve Year Program": "planning", "Under Development": "planning",
                 "Four Year Plan": "design", "Anticipated": "row_acquisition"}


def clamp(n: float) -> int:
    return max(0, min(100, int(round(n))))


def overall_score(likelihood: int, stage: str, est_cost: Optional[float] = None, in_pa: bool = True) -> int:
    """Project score out of 100: the three project components plus the full 15 for a parcel on the alignment."""
    if not in_pa:
        return 0
    return need_points(likelihood) + STAGE_POINTS.get(stage, STAGE_POINTS["unknown"]) + size_points(est_cost) \
        + MAX_POINTS["position"]


def lead_points(project_score: int, reason: str) -> int:
    """Lead score out of 100: swap the project's assumed 15 position points for this parcel's actual position."""
    if project_score <= 0:
        return 0
    return max(0, project_score - MAX_POINTS["position"] + POSITION_POINTS.get(reason, 5))


STAGE_LABEL = {"planning": "Planning: the earliest stage", "environmental_review": "Environmental review: still early",
               "design": "In design", "row_acquisition": "Right-of-way acquisition already under way",
               "condemnation_filed": "Final acquisition: condemnations filed or due", "construction": "Bid or under construction: the land is already taken",
               "unknown": "Stage not published"}


STAGE_ORDER = [
    ("planning", "Planning", "On the long-range program. No design yet; owners have usually not been approached."),
    ("environmental_review", "Environmental review", "Alternatives and impacts are studied; the footprint starts to firm up."),
    ("design", "Design", "Engineering sets the exact right-of-way lines. Plans showing each affected parcel follow."),
    ("row_acquisition", "Right-of-way acquisition", "Appraisals and written offers go to owners."),
    ("condemnation_filed", "Condemnation", "A declaration of taking is filed for parcels not settled."),
    ("construction", "Construction", "The contract is let and work begins."),
]
STAGE_RANK = {k: i for i, (k, _, _) in enumerate(STAGE_ORDER)}


def _months_until(day: Optional[str], today: Optional[Any] = None) -> Optional[int]:
    from datetime import date, datetime

    for fmt in ("%Y-%m-%d", "%Y%m%d", "%m-%d-%y", "%m/%d/%Y"):
        try:
            d = datetime.strptime(str(day or "")[:10], fmt).date()
        except ValueError:
            continue
        if d.year >= 2045:           # agencies use far-off dates to mean "not scheduled"
            return None
        today = today or date.today()
        return (d.year - today.year) * 12 + d.month - today.month
    return None


def stage_from_schedule(let_date: Optional[str], let_actual: bool = False, building: bool = False,
                        today: Optional[Any] = None) -> Optional[str]:
    """The stage an agency's own bid date implies, or None when it publishes no usable date.

    A highway agency cannot open construction bids until it holds the right-of-way, so the bid ("let") date dates the
    land work: once bid, the land is taken; in the last year and a half before, offers are out and declarations of
    taking are being filed; from about three and a half years out, right-of-way plans are done and appraisals start.
    The program a project is listed under (a four-year plan, a long-range plan) says none of this, and was wrong often.
    """
    if building or let_actual:
        return "construction"
    months = _months_until(let_date, today)
    if months is None:
        return None
    return ("construction" if months <= 0 else "condemnation_filed" if months <= 18
            else "row_acquisition" if months <= 42 else "design" if months <= 72 else None)


def later_stage(a: Optional[str], b: Optional[str]) -> str:
    """Whichever of two stages is further along. Work does not move backwards."""
    return max((a or "unknown", b or "unknown"), key=lambda s: STAGE_RANK.get(s, -1))


def timeline(stage: Optional[str], raw: Optional[Dict[str, Any]] = None, today: Optional[Any] = None) -> Dict[str, Any]:
    """Where a project sits between first planning and construction, plus the letting date when PennDOT publishes one."""
    from datetime import date, datetime

    keys = [k for k, _, _ in STAGE_ORDER]
    at = keys.index(stage) if stage in keys else None
    steps = [{"key": k, "label": label, "about": about,
              "state": "unknown" if at is None else "done" if i < at else "current" if i == at else "upcoming"}
             for i, (k, label, about) in enumerate(STAGE_ORDER)]
    out: Dict[str, Any] = {"steps": steps, "position": None if at is None else at + 1, "of": len(keys),
                           "program": (raw or {}).get("program"), "status": (raw or {}).get("PROJ_STATUS_DESC"),
                           "let_date": None, "months_to_letting": None}
    r = raw or {}
    let = r.get("LET_DATE") or r.get("LET_DATE_DISPLAY") or r.get("_let")
    months = _months_until(let, today)
    if months is not None:
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%m-%d-%y", "%m/%d/%Y"):
            try:
                out["let_date"] = datetime.strptime(str(let)[:10], fmt).date().isoformat()
                break
            except ValueError:
                continue
        out["months_to_letting"] = months
    bid = stage == "construction" or "A" in (r.get("LET_IND"), r.get("NTP_IND")) or r.get("UNDER_CONSTRUCTION") == "Y"
    out["let_actual"] = bid
    # What the stage rests on, so a reader can judge it. Only a court filing proves a condemnation; none is read here.
    out["basis"] = ("The agency reports this contract as bid or under construction." if bid
                    else "Estimated from the agency's construction bid date. Right-of-way must be in hand before bids open, so "
                         "offers and condemnations come in the months before it. Not confirmed against court filings."
                    if months is not None and months <= 72
                    else "Taken from the program the agency lists this project under. No bid date is published, so this is "
                         "the least certain kind of stage.")
    return out


def score_breakdown(likelihood: Optional[int], stage: Optional[str], est_cost: Optional[float],
                    score: Optional[int]) -> List[Dict[str, Any]]:
    """The four components of a project score, each with its points and its maximum. They sum to the score."""
    if score == 0:
        return [{"key": "outside", "label": "Not scored: the notice does not affect land in Pennsylvania",
                 "points": 0, "max": 100}]
    stage = stage if stage in STAGE_POINTS else "unknown"
    return [
        {"key": "need", "label": "Need for private land", "detail": "This kind of project is rated %d of 100 for taking "
         "private land; 60%% of that rating counts" % (likelihood or 0), "points": need_points(likelihood), "max": 60},
        {"key": "stage", "label": "How early it is", "detail": STAGE_LABEL.get(stage, "Stage unknown"),
         "points": STAGE_POINTS[stage], "max": 15},
        {"key": "size", "label": "Size of the project", "detail": size_label(est_cost), "points": size_points(est_cost), "max": 10},
        {"key": "position", "label": "Where the parcel sits", "detail": "A parcel on the alignment",
         "points": MAX_POINTS["position"], "max": 15},
    ]


# ---------------------------------------------------------------- PennDOT rules
def classify_penndot(signal: Dict[str, Any]) -> Dict[str, Any]:
    raw = json.loads(signal["raw"]) if isinstance(signal.get("raw"), str) else (signal.get("raw") or {})
    imp = raw.get("IMPROVEMENT_CODE_DESC") or ""
    likelihood, ptype = PENNDOT_RULES.get(imp, (10, "other"))
    text = "%s %s" % (signal.get("title") or "", signal.get("body") or "")
    # Free-text hints inside the description nudge the likelihood.
    if re.search(r"right[- ]of[- ]way|\bROW\b|acquisition|realign|relocat|new alignment|bypass|widen", text, re.I):
        likelihood = min(100, likelihood + 8)
    stage = later_stage(PROGRAM_STAGE.get(raw.get("program") or "", "unknown"), stage_from_schedule(
        raw.get("LET_DATE") or raw.get("LET_DATE_DISPLAY"), "A" in (raw.get("LET_IND"), raw.get("NTP_IND")),
        raw.get("UNDER_CONSTRUCTION") == "Y"))
    cost = raw.get("PHS_EST_COST_AM")
    geometry = json.loads(signal["geometry"]) if isinstance(signal.get("geometry"), str) else signal.get("geometry")
    c = geo.center(geometry)
    contact = ", ".join(str(x) for x in (raw.get("PRJCT_MNGR_CONTACT_NM"), raw.get("PRJCT_MNGR_EMAIL"),
                                         raw.get("PRJCT_MNGR_NO")) if x)
    if "placeholder" in contact.lower():  # PennDOT fills unassigned projects with a dummy manager
        contact = ""
    parts = [imp, raw.get("PRJCT_GRPHY_DC"), "Program: %s" % raw.get("program"),
             "Status: %s" % raw.get("PROJ_STATUS_DESC") if raw.get("PROJ_STATUS_DESC") else None,
             "Let: %s" % raw.get("LET_DATE_DISPLAY") if raw.get("LET_DATE_DISPLAY") else None]
    return {
        "name": signal.get("title"),
        "project_type": ptype,
        "stage": stage,
        "acquisition_likelihood": likelihood,
        "score": overall_score(likelihood, stage, cost),
        "summary": ". ".join(p for p in parts if p),
        "est_cost": cost,
        "contact": contact or None,
        "locations": [],
        "parcel_ids_mentioned": [],
        "geometry": geometry,
        "lat": c[0] if c else None,
        "lon": c[1] if c else None,
    }


# ---------------------------------------------------------------- keyword engine
KEYWORDS: List[Tuple[str, int]] = [
    (r"declaration of taking", 95), (r"eminent domain", 90), (r"condemn(?:ation|ing|\b)", 85),
    (r"acquisition of (?:real )?propert", 75), (r"right[- ]of[- ]way acquisition", 80),
    (r"acquire[^.]{0,60}(?:parcel|propert|easement|right[- ]of[- ]way)", 70),
    (r"permanent easement|temporary construction easement", 65),
    (r"new alignment|bypass|widening|additional lanes?|new interchange", 60),
    (r"pipeline|transmission line|certificate of public convenience", 55),
    (r"environmental impact statement", 45), (r"environmental assessment", 35),
    (r"redevelopment (?:area|plan)|blight", 45), (r"easement", 30),
]
TYPE_HINTS = [("pipeline", r"pipeline|natural gas|compressor"), ("transmission", r"transmission line|kV\b"),
              ("rail", r"\brail|amtrak|railroad"), ("transit", r"transit|SEPTA|busway|light rail"),
              ("interchange", r"interchange"), ("bridge", r"bridge"),
              ("highway", r"highway|\bSR \d|\bI-\d|route \d|roadway|turnpike|street"),
              ("water", r"water|sewer|stormwater|flood|dam\b|levee"),
              ("redevelopment", r"redevelopment|blight"), ("airport", r"airport|runway")]
PA_HINT = re.compile(r"pennsylvania|\bPA\b|philadelphia|pittsburgh|allegheny|harrisburg|penndot|"
                     r"pennsylvania turnpike|lehigh|susquehanna|delaware river", re.I)
# Allegheny County lot-and-block (e.g. 23-L-145 or 0023-L-00145) and Philadelphia OPA (9 digits).
APN_PATTERNS = [re.compile(r"\b\d{1,4}-[A-Z]-\d{1,5}(?:-\d{1,4}){0,2}\b"), re.compile(r"\b\d{9}\b")]
ADDRESS = re.compile(r"\b\d{1,5} (?:[NSEW]\.? )?[A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+){0,2} "
                     r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Way|Drive|Dr|Lane|Ln|Pike|Highway)\b")


# Any state DOT's project record: what the work is decides how likely it is to need private land.
DOT_WORK = [
    (r"right[- ]of[- ]way|\brow\b|r/w", 95, "highway"),
    (r"new location|convert non-freeway|grade,? drain", 90, "highway"),
    (r"new (?:road|route|highway|alignment|location|corridor|connector|interchange|freeway|expressway)|bypass|\bextension\b|extend ", 95, "highway"),
    (r"new interchange", 95, "interchange"),
    (r"realign|relocat", 90, "highway"),
    (r"construct(?:ion of)? (?:\w+ ){0,3}lanes|multi-?lan|upgrade (?:\w+ ){0,6}to (?:a |an )?(?:freeway|expressway|interstate|interchange|superstreet)|superstreet|super[- ]?2|reduced conflict", 85, "highway"),
    (r"widen|add(?:ed|ing|itional)? (?:travel |through |general purpose )?lanes?|capacity|duali[sz]|(?:four|six|4|6)[- ]lan(?:e|ing)|two[- ]way left|passing lane|auxiliary lane", 85, "highway"),
    (r"grade separation|overpass|underpass|new bridge|flyover", 75, "bridge"),
    (r"interchange", 70, "interchange"),
    (r"park[- ](?:and|&|n)[- ]ride|transit (?:center|station|facility)|rail (?:station|extension)", 65, "transit"),
    (r"roundabout|intersection|turn(?:ing)? lanes?|channeliz", 60, "highway"),
    (r"reconstruct|rebuild|moderni[sz]", 45, "highway"),
    (r"bridge (?:replace|reconstruct)|replace (?:the )?bridge|culvert replace", 40, "bridge"),
    (r"shoulder|safety|slide|slope|rockfall|curve", 30, "highway"),
    (r"sidewalk|pedestrian|bike|bicycl|trail|shared[- ]use|multi[- ]?use|ada\b", 25, "other"),
    (r"signal|lighting|drainage|culvert|bridge (?:rehab|repair|preserv|deck|paint)", 18, "other"),
    (r"resurfac|overlay|pavement|repav|mill|seal|preserv|rehabilitat|striping|marking|guardrail|guiderail|sign(?:s|ing)\b|maint|\bits\b|landscap|study|planning", 5, "other"),
]
DOT_STAGE = [
    (r"work program|long range program", "planning"),      # Georgia's programs of future work, not work under way
    (r"\brw\b", "row_acquisition"),                        # Ohio lists the phases funded in the STIP period
    (r"pre-?litigation|apprais|offer|relocation|right of entry|hold for project", "row_acquisition"),
    (r"condemn|litigation|special commissioner|trial phase|judgment", "condemnation_filed"),
    (r"complete|closed|finished|open to traffic|construct|under contract|awarded|\blet\b|active work|in progress", "construction"),
    (r"right[- ]of[- ]way|\brow\b|r/w|acquisition|utilit", "row_acquisition"),
    (r"design|engineer|\bpe\b|plans?\b|ps&e|final", "design"),
    (r"environment|nepa|scoping|study|concept|alternativ", "environmental_review"),
    (r"plan|program|proposed|candidate|future|stip|\btip\b|long[- ]range|unfunded|funded|scheduled|develop|pending|not started", "planning"),
]


def classify_dot(signal: Dict[str, Any]) -> Dict[str, Any]:
    raw = json.loads(signal["raw"]) if isinstance(signal.get("raw"), str) else (signal.get("raw") or {})
    work = "%s. %s" % (raw.get("_work_type") or "", signal.get("title") or "")
    text = "%s %s" % (work, signal.get("body") or "")
    likelihood, ptype = 10, "other"
    for pat, weight, kind in DOT_WORK:          # the stated type of work first, then the description
        if re.search(pat, work, re.I):
            likelihood, ptype = weight, kind
            break
    else:
        for pat, weight, kind in DOT_WORK:
            if re.search(pat, text, re.I):
                likelihood, ptype = max(10, weight - 10), kind
                break
    if likelihood >= 30 and re.search(r"right[- ]of[- ]way|\bROW\b|acquisition|new alignment|relocat", text, re.I):
        likelihood = min(100, likelihood + 8)
    if raw.get("_likelihood"):                  # a list of parcels the agency is acquiring needs no inference
        likelihood, ptype = int(raw["_likelihood"]), ("highway" if ptype == "other" else ptype)
    status = str(raw.get("_status") or "")
    # A program of future work (a STIP, a five-year plan) is "planning" unless its status says something more specific.
    stage = next((st for pat, st in DOT_STAGE if re.search(pat, status, re.I)), raw.get("_stage") or ("planning" if not status else "unknown"))
    if stage in ("planning", "unknown", "design") and re.search(r"right[- ]of[- ]way|\brow\b|r/w", str(raw.get("_work_type") or ""), re.I):
        stage = "row_acquisition"              # the job itself is the land purchase
    stage = later_stage(stage, stage_from_schedule(raw.get("_let")))
    cost = raw.get("_cost")
    geometry = json.loads(signal["geometry"]) if isinstance(signal.get("geometry"), str) else signal.get("geometry")
    c = geo.center(geometry)
    return {
        "name": signal.get("title"), "project_type": ptype, "stage": stage, "acquisition_likelihood": likelihood,
        "score": overall_score(likelihood, stage, cost), "summary": signal.get("body") or signal.get("title"),
        "est_cost": cost, "contact": None, "locations": [], "parcel_ids_mentioned": [],
        "geometry": geometry, "lat": c[0] if c else None, "lon": c[1] if c else None,
    }


def classify_keywords(signal: Dict[str, Any]) -> Dict[str, Any]:
    text = "%s. %s" % (signal.get("title") or "", signal.get("body") or "")
    likelihood = 0
    for pat, weight in KEYWORDS:
        if re.search(pat, text, re.I):
            likelihood = max(likelihood, weight)
    # Disposals and vacations are the opposite of a taking.
    if re.search(r"\b(?:sale|sell|convey(?:ance)?|vacat\w+) (?:of|to)\b", text, re.I) and likelihood < 85:
        likelihood = int(likelihood * 0.5)
    ptype = next((t for t, pat in TYPE_HINTS if re.search(pat, text, re.I)), "other")
    if re.search(r"declaration of taking|condemn|eminent domain", text, re.I):
        stage = "condemnation_filed"
    elif re.search(r"acquisition|acquire|easement", text, re.I):
        stage = "row_acquisition"
    elif re.search(r"environmental (?:impact|assessment)|scoping|notice of intent", text, re.I):
        stage = "environmental_review"
    else:
        stage = "unknown"
    # Federal Register results are already full-text filtered to Pennsylvania by the search terms.
    # A signal filed under another state came from that state's own council or agency, so it is in scope.
    in_pa = (bool(signal.get("county")) or signal.get("source") == "federal_register"
             or bool(PA_HINT.search(text)) or (signal.get("state") or "PA") != "PA")
    apns = sorted({m.group(0) for p in APN_PATTERNS for m in p.finditer(text)})
    locations = sorted({m.group(0) for m in ADDRESS.finditer(text)})[:8]
    return {
        "name": (signal.get("title") or "")[:200],
        "project_type": ptype,
        "stage": stage,
        "acquisition_likelihood": likelihood,
        "score": overall_score(likelihood, stage, None, in_pa),
        "summary": (signal.get("body") or signal.get("title") or "")[:600],
        "locations": locations,
        "parcel_ids_mentioned": apns,
    }


# ---------------------------------------------------------------- Claude engine
SCHEMA = {
    "type": "object",
    "properties": {
        "in_pennsylvania": {"type": "boolean",
                            "description": "True only if land inside Pennsylvania is affected."},
        "name": {"type": "string", "description": "Short project name."},
        "project_type": {"type": "string", "enum": PROJECT_TYPES},
        "stage": {"type": "string", "enum": STAGES},
        "acquisition_likelihood": {
            "type": "integer",
            "description": "0-100 likelihood that a condemnor will need to acquire private real property or "
                           "easements for this project (by negotiation or eminent domain).",
        },
        "summary": {"type": "string", "description": "Two or three sentences for an attorney: what is being "
                                                     "built, by whom, where, and what land is at stake."},
        "locations": {"type": "array", "items": {"type": "string"},
                      "description": "Up to 8 geocodable places in Pennsylvania: street addresses, "
                                     "intersections, or 'Township, County' names. No state suffix."},
        "parcel_ids_mentioned": {"type": "array", "items": {"type": "string"},
                                 "description": "Parcel / tax IDs quoted verbatim in the text."},
    },
    "required": ["in_pennsylvania", "name", "project_type", "stage", "acquisition_likelihood", "summary",
                 "locations", "parcel_ids_mentioned"],
    "additionalProperties": False,
}

SYSTEM = """You screen public notices for a Pennsylvania eminent domain law firm that represents property \
owners (condemnees). For each notice, judge whether a government body, authority, or utility with \
condemnation power (PennDOT, the Turnpike Commission, municipalities, redevelopment authorities, SEPTA, \
pipeline and transmission companies with FERC or PUC certificates) is likely to take private land or \
easements, and how early in the process it is.

Score acquisition_likelihood on the notice's substance. New alignments, widenings, new interchanges, \
pipelines, transmission lines and explicit acquisition or condemnation authorizations score high. \
Maintenance, studies with no footprint, sales or vacations of public land, withdrawals and rescissions \
score low. Notices that do not touch Pennsylvania get in_pennsylvania=false.

Only list locations and parcel IDs that the text supports; do not guess."""


class ClaudeClassifier:
    def __init__(self, model: str):
        import anthropic  # imported lazily so the keyword engine works without the SDK

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model

    def classify(self, signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        user = "Source: %s\nAgency: %s\nPublished: %s\nTitle: %s\n\n%s" % (
            signal.get("source"), signal.get("agency"), signal.get("published"),
            signal.get("title"), signal.get("body") or "")
        resp = self.client.beta.messages.create(
            model=self.model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if resp.stop_reason in ("refusal", "max_tokens"):
            return None
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            return None
        d = json.loads(text)
        likelihood = clamp(d["acquisition_likelihood"])
        return {
            "name": d["name"][:200],
            "project_type": d["project_type"],
            "stage": d["stage"],
            "acquisition_likelihood": likelihood,
            "score": overall_score(likelihood, d["stage"], None, d["in_pennsylvania"]),
            "summary": d["summary"],
            "locations": d["locations"][:8],
            "parcel_ids_mentioned": d["parcel_ids_mentioned"][:50],
        }


def make_text_classifier(cfg: Dict[str, Any]):
    """Returns (engine_name, fn). Falls back to keywords when Claude is unavailable."""
    engine = cfg.get("engine", "auto")
    if engine in ("auto", "claude"):
        try:
            cc = ClaudeClassifier(cfg.get("model", "claude-opus-5-5"))
        except Exception as e:
            if engine == "claude":
                raise
            print("  Claude unavailable (%s); using keyword classifier for text sources." % e)
        else:
            state = {"ok": True}

            def run(signal: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
                if state["ok"]:
                    try:
                        out = cc.classify(signal)
                        if out is not None:
                            return "claude", out
                    except (cc.anthropic.AuthenticationError, cc.anthropic.PermissionDeniedError,
                            TypeError) as e:
                        # No usable credentials: stop calling the API for the rest of this run.
                        if engine == "claude":
                            raise
                        state["ok"] = False
                        print("  Claude credentials not usable (%s); using keywords." % type(e).__name__)
                    except cc.anthropic.RateLimitError:
                        print("  Claude rate limited; keyword fallback for signal %s" % signal.get("id"))
                    except cc.anthropic.APIStatusError as e:
                        print("  Claude error %s; keyword fallback for signal %s" % (e.status_code,
                                                                                    signal.get("id")))
                    except cc.anthropic.APIConnectionError:
                        print("  Claude unreachable; keyword fallback for signal %s" % signal.get("id"))
                return "keywords", classify_keywords(signal)

            return run
    return lambda signal: ("keywords", classify_keywords(signal))

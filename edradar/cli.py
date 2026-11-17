from __future__ import annotations

import argparse

from . import pipeline
from .config import load_config
from .db import DB


def main() -> None:
    ap = argparse.ArgumentParser(prog="edradar", description="Pennsylvania eminent domain lead finder")
    ap.add_argument("-c", "--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="pull new signals from sources")
    p.add_argument("--source", choices=sorted(pipeline.SOURCES))
    sub.add_parser("classify", help="score unclassified signals")
    sub.add_parser("geocode", help="geocode text-sourced projects")
    p = sub.add_parser("match", help="find parcels around scored projects")
    p.add_argument("--limit", type=int, help="projects to match this run")
    p = sub.add_parser("run", help="ingest + classify + geocode + match")
    p.add_argument("--limit", type=int, help="projects to match this run")
    sub.add_parser("stats", help="show counts")
    p = sub.add_parser("serve", help="start the dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("postcards", help="mail one postcard per eligible owner (dry run unless --send)")
    p.add_argument("--min-score", type=int, default=90)
    p.add_argument("--county")
    p.add_argument("--limit", type=int, default=25, help="most cards to mail in this batch")
    p.add_argument("--send", action="store_true", help="actually send; without it the batch is only listed")
    p = sub.add_parser("find-emails", help="ask Apollo which company owners have contacts (search only, no credits)")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("skip-trace", help="find phones and emails for individual owners (dry run unless --run)")
    p.add_argument("--min-score", type=int, default=90)
    p.add_argument("--limit", type=int, default=25, help="most owners to look up in this batch")
    p.add_argument("--run", action="store_true", help="actually look them up; without it only the count and cost are shown")
    sub.add_parser("usage", help="billable usage: lookups and postcards, cost and client price")
    p = sub.add_parser("export", help="write a read-only static copy of the dashboard")
    p.add_argument("--out", default="site")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.cmd == "serve":
        import uvicorn

        from .web.app import create_app

        uvicorn.run(create_app(cfg), host=args.host, port=args.port)
        return
    if args.cmd == "export":
        from .web.app import export_static

        print("wrote %s: %s" % (args.out, export_static(cfg, args.out)))
        return
    db = DB(cfg["database"])
    from . import outreach as _outreach

    _outreach.apply_saved(cfg, db)     # the template edited in the dashboard applies to command-line sends too
    if args.cmd == "ingest":
        pipeline.ingest(cfg, db, args.source)
    elif args.cmd == "classify":
        pipeline.classify(cfg, db)
    elif args.cmd == "geocode":
        pipeline.geocode(cfg, db)
    elif args.cmd == "match":
        pipeline.match(cfg, db, args.limit)
    elif args.cmd == "run":
        pipeline.run_all(cfg, db, args.limit)
    elif args.cmd == "postcards":
        from . import outreach

        res = outreach.run_campaign(cfg, db, args.min_score, None, args.county, args.limit, dry_run=not args.send)
        for r in res["recipients"]:
            print("  %3d  %-40s %s" % (r["score"], (r["owner"] or "")[:40], r["mailing_address"]))
        if res["dry_run"]:
            print("%d owners would be mailed (Lob mode: %s). Add --send to mail them." % (res["eligible"], res["mode"]))
        else:
            print("%d sent in %s mode, %d failed" % (len(res["sent"]), res["mode"], len(res["failed"])))
            for f in res["failed"]:
                print("  failed: %s: %s" % (f["owner"], f["why"]))
    elif args.cmd == "skip-trace":
        from . import skiptrace

        res = skiptrace.trace_batch(cfg, db, args.min_score, args.limit, dry_run=not args.run)
        if res["dry_run"]:
            print("%d owners would be looked up: up to $%.2f cost, $%.2f billed to the client. Add --run to do it."
                  % (res["owners"], res["max_cost"], res["max_billed"]))
        else:
            print("%d owners looked up, %d matched, %d failed" % (res["owners"], res["matched"], res["failed"]))
    elif args.cmd == "usage":
        from . import skiptrace

        for label, rows in skiptrace.usage_summary(db).items():
            print(label.replace("_", " "))
            for r in rows or [{"kind": "nothing yet", "count": 0, "cost": 0, "billed": 0, "margin": 0}]:
                print("  %-12s %5d  cost $%8.2f  billed $%8.2f  margin $%8.2f" % (r["kind"], r["count"], r["cost"] or 0,
                                                                                  r["billed"] or 0, r["margin"] or 0))
    elif args.cmd == "find-emails":
        from . import outreach

        print(outreach.scan_email_leads(cfg, db, args.limit))
    elif args.cmd == "stats":
        for table in ("signals", "projects", "parcels", "leads"):
            print("%-9s %d" % (table, db.rows("SELECT COUNT(*) n FROM %s" % table)[0]["n"]))
        for r in db.rows("SELECT s.source, COUNT(*) n, SUM(p.score >= 60) hot FROM projects p "
                         "JOIN signals s ON s.id=p.signal_id GROUP BY s.source"):
            print("  %-20s %5d projects, %5d scoring 60+" % (r["source"], r["n"], r["hot"] or 0))


if __name__ == "__main__":
    main()

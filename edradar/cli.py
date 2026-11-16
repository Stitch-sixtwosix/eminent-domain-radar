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
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.cmd == "serve":
        import uvicorn

        from .web.app import create_app

        uvicorn.run(create_app(cfg), host=args.host, port=args.port)
        return
    db = DB(cfg["database"])
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
    elif args.cmd == "stats":
        for table in ("signals", "projects", "parcels", "leads"):
            print("%-9s %d" % (table, db.rows("SELECT COUNT(*) n FROM %s" % table)[0]["n"]))
        for r in db.rows("SELECT s.source, COUNT(*) n, SUM(p.score >= 60) hot FROM projects p "
                         "JOIN signals s ON s.id=p.signal_id GROUP BY s.source"):
            print("  %-20s %5d projects, %5d scoring 60+" % (r["source"], r["n"], r["hot"] or 0))


if __name__ == "__main__":
    main()

"""python -m leadengine.cli serve|maintain|create-owner|classify"""
from __future__ import annotations

import argparse
import json


def main() -> None:
    ap = argparse.ArgumentParser(prog="leadengine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--demo", action="store_true", help="seed synthetic demo data (agents, projects, spend, leads, a viewing) first")
    c = sub.add_parser("classify")
    c.add_argument("text")
    sub.add_parser("maintain", help="platform mode: SLA escalation + retention purge for every workspace (run from cron)")
    o = sub.add_parser("create-owner", help="platform mode: create a verified owner and workspace (password from OWNER_PASSWORD or a prompt)")
    o.add_argument("email")
    o.add_argument("--name", required=True)
    o.add_argument("--agency", required=True)
    args = ap.parse_args()

    if args.cmd == "classify":
        from .nlp.intent import classify
        print(json.dumps(classify(args.text).to_dict(), ensure_ascii=False, indent=2))
        return
    from .config import Settings
    settings = Settings()
    if args.cmd in ("maintain", "create-owner"):
        return operator_command(args, settings)
    import uvicorn
    if settings.control_db:                                  # platform mode: accounts + one database per workspace
        from .platform.app import create_platform_app
        if args.demo:
            raise SystemExit("--demo is single-tenant only; unset CONTROL_DB, or sign in and seed from the workspace.")
        try:
            app = create_platform_app(settings)
        except ValueError as e:
            raise SystemExit(f"error: {e}")
    else:
        from .api import create_app
        app = create_app(settings)
        if args.demo:
            from .demo import seed_demo
            print("seeded:", seed_demo(app.state.pipeline))
    uvicorn.run(app, host=args.host, port=args.port)


def operator_command(args, settings) -> None:
    import getpass
    import os
    import sys
    if not settings.control_db:
        raise SystemExit("error: this command needs platform mode: set CONTROL_DB, DATA_DIR and ENCRYPTION_KEY")
    from .platform.maintenance import create_owner, maintain
    try:
        if args.cmd == "maintain":
            results, errors = maintain(settings)
            print(json.dumps({"workspaces": len(results) + len(errors), "results": results, "errors": errors}, indent=2))
            if errors:
                sys.exit(1)
            return
        password = os.environ.get("OWNER_PASSWORD") or getpass.getpass("Password (12+ characters): ")
        uid, tid = create_owner(settings, args.email, password, args.name, args.agency)
        print(f"created owner {args.email} (user {uid}, workspace {tid})")
    except ValueError as e:
        raise SystemExit(f"error: {e}")


if __name__ == "__main__":
    main()

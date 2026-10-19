"""python -m leadengine.cli serve|demo|classify"""
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
    args = ap.parse_args()

    if args.cmd == "classify":
        from .nlp.intent import classify
        print(json.dumps(classify(args.text).to_dict(), ensure_ascii=False, indent=2))
        return
    import uvicorn
    from .api import create_app
    app = create_app()
    if args.demo:
        from .demo import seed_demo
        print("seeded:", seed_demo(app.state.pipeline))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()

import json
import logging
import sys

from .config import Config


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    cfg = Config()
    if cmd == "serve":
        from .server import serve
        serve(cfg)
    elif cmd in ("sync", "sync-full"):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        from .mapping import Mapping
        m = Mapping(cfg)
        m.sync(force_full=cmd == "sync-full")
        print(json.dumps(m.stats(), indent=2))
    elif cmd == "stats":
        from .mapping import Mapping
        print(json.dumps(Mapping(cfg).stats(), indent=2))
    else:
        sys.exit(f"usage: python -m bridge [serve|sync|sync-full|stats]")


if __name__ == "__main__":
    main()

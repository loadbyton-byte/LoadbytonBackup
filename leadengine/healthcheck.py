"""Container health probe: exit 0 if the app answers /healthz (platform mode) or /health (single-tenant)."""
import sys
import urllib.request

for path in ("/healthz", "/health"):
    try:
        if urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=4).status == 200:
            sys.exit(0)
    except Exception:
        pass
sys.exit(1)

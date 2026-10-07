"""Share the console from this PC through a Cloudflare Tunnel.

    python scripts/up.py          # the stack, as usual
    python scripts/share.py       # build the console, serve it, open the tunnel
    python scripts/share.py --stop

This PC becomes the server. The built console is served by `vite preview` on
port 4173, which forwards the console's own calls to the API on port 8000, and
`cloudflared` gives that one address a public HTTPS hostname. One address for
the console and its API is what D101b asks for: the session cookie stays
first-party and the live alert feed keeps working.

Only people in a browser get through. Every machine route authenticates with an
X-API-Key, a browser using the console never sends one, and the preview server
refuses any request that carries a key (frontend/vite.config.js): the
development keys are public in this repository. The simulator keeps reaching
the API directly on 127.0.0.1:8000.

Without a Cloudflare account this opens a *quick tunnel*: free, no login, and a
new random https://<words>.trycloudflare.com address every time it starts. A
fixed address needs a named tunnel on a domain you own (see the README).

The public address lasts while this PC is on, awake and online. Synthetic data
only (D101a): the demo bank's customers are simulated, and real customer data
must never be loaded into a stack reachable from the internet.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
LOGS = REPO_ROOT / "logs"
STATE = LOGS / "share.json"
PREVIEW_PORT = 4173
URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def say(what: str, detail: str) -> None:
    print(f"  {what:<34}{detail}")


def find_cloudflared() -> str | None:
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in (r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
                      r"C:\Program Files\cloudflared\cloudflared.exe",
                      os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\cloudflared.exe")):
        if Path(candidate).exists():
            return candidate
    return None


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return r.status < 500
    except (urllib.error.URLError, OSError):
        return False


def spawn(name: str, args: list[str], cwd: Path) -> int:
    """Detached, so the share outlives this script, like up.py's services."""
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / f"{name}.log", "wb")
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(args, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True).pid


def stop() -> None:
    try:
        pids = json.loads(STATE.read_text(encoding="utf-8")).get("pids", {})
    except (OSError, ValueError):
        pids = {}
    for name, pid in pids.items():
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            subprocess.run(["kill", str(pid)], capture_output=True)
        say(name, f"stopped ({pid})")
    STATE.unlink(missing_ok=True)
    if not pids:
        say("share", "nothing was running")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stop", action="store_true", help="close the tunnel and stop serving the console")
    parser.add_argument("--no-build", action="store_true", help="serve the last build as it is")
    args = parser.parse_args()
    print("\nRisk Radar - sharing from this PC\n")
    if args.stop:
        stop()
        return

    cloudflared = find_cloudflared()
    if not cloudflared:
        sys.exit("  cloudflared is not installed: winget install --id Cloudflare.cloudflared")
    if not http_ok("http://127.0.0.1:8000/health"):
        sys.exit("  the API is not running on port 8000: start the stack with python scripts/up.py")
    if STATE.exists():
        stop()

    npm = "npm.cmd" if os.name == "nt" else "npm"
    if not args.no_build:
        say("console", "building...")
        built = subprocess.run([npm, "run", "build"], cwd=str(FRONTEND), capture_output=True, text=True)
        if built.returncode:
            sys.exit(built.stdout[-2000:] + built.stderr[-2000:])
        say("console", "built")

    pids = {"preview": spawn("preview", [npm, "run", "preview"], FRONTEND)}
    for _ in range(40):
        if http_ok(f"http://127.0.0.1:{PREVIEW_PORT}/"):
            break
        time.sleep(0.5)
    else:
        sys.exit(f"  the console did not start on {PREVIEW_PORT}; see {LOGS / 'preview.log'}")
    say("console", f"serving on {PREVIEW_PORT}")

    pids["tunnel"] = spawn("tunnel", [cloudflared, "tunnel", "--no-autoupdate", "--url",
                                      f"http://127.0.0.1:{PREVIEW_PORT}"], REPO_ROOT)
    STATE.write_text(json.dumps({"pids": pids}), encoding="utf-8")
    address = None
    for _ in range(60):
        match = URL.search((LOGS / "tunnel.log").read_text(encoding="utf-8", errors="replace"))
        if match:
            address = match.group(0)
            break
        time.sleep(1)
    if not address:
        sys.exit(f"  the tunnel did not report an address; see {LOGS / 'tunnel.log'}")
    STATE.write_text(json.dumps({"pids": pids, "url": address}), encoding="utf-8")

    print(f"\n  public address    {address}")
    print("  it can take up to a minute before the address answers everywhere\n")
    print("  who gets in      anyone with the address reaches the login page; every")
    print("                   account needs its password and MFA code (python scripts/codes.py)")
    print("  stays up while   this PC is on, awake and online")
    print("  stop             python scripts/share.py --stop\n")


if __name__ == "__main__":
    main()

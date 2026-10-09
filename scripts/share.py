"""Share the console from this PC through a Cloudflare Tunnel.

    python scripts/up.py          # the stack, as usual
    python scripts/share.py       # build the console, serve it, open the tunnel
    python scripts/share.py --stop

People open the console on Vercel (riskradar-ml.vercel.app); Vercel forwards
its API calls to this PC through the tunnel. Each start writes the tunnel's new
address into frontend/vercel.json on the branch Vercel deploys from (through
the GitHub API, as whoever `gh` is logged in as) and Vercel redeploys; pass
--vercel to also repoint the shared riskradar-ml.vercel.app at this tunnel.
Without it the tunnel address is yours alone, so several people can demo at
the same time: the Vercel site holds one pointer for the whole team, and
repointing it takes the live link away from whoever else is using it.

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


REPO = os.environ.get("RISKRADAR_GITHUB_REPO", "binocularsx/riskradar")
VERCEL_BRANCH = os.environ.get("RISKRADAR_VERCEL_BRANCH", "integration")
VERCEL_URL = os.environ.get("RISKRADAR_VERCEL_URL", "https://riskradar-ml.vercel.app")
VERCEL_FILE = "frontend/vercel.json"
DESTINATION = re.compile(r"https://[^/\"]+(?=/(?:v1/:path\*|health)\")")


def point_vercel(address: str) -> bool:
    """Make the Vercel site forward its API calls to this tunnel.

    The console on Vercel calls /v1 and /health on its own address, and
    frontend/vercel.json forwards those to the API. A quick tunnel's address
    changes every time it starts, so the file on the branch Vercel deploys from
    is updated through the GitHub API (no local checkout is touched) and Vercel
    redeploys on its own. Nothing is written when the address is already right.
    """
    gh = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
    path = f"repos/{REPO}/contents/{VERCEL_FILE}"
    got = subprocess.run([gh, "api", f"{path}?ref={VERCEL_BRANCH}"], capture_output=True, text=True)
    if got.returncode:
        say("vercel", f"could not read {VERCEL_FILE}: {got.stderr.strip()[:200]}")
        return False
    meta = json.loads(got.stdout)
    import base64

    current = base64.b64decode(meta["content"]).decode("utf-8")
    updated = DESTINATION.sub(address, current)
    if updated == current:
        say("vercel", "already forwards to this tunnel")
        return True
    put = subprocess.run(
        [gh, "api", "--method", "PUT", path,
         "-f", "message=Point Vercel at the current tunnel address (scripts/share.py)",
         "-f", f"content={base64.b64encode(updated.encode('utf-8')).decode('ascii')}",
         "-f", f"sha={meta['sha']}", "-f", f"branch={VERCEL_BRANCH}"],
        capture_output=True, text=True)
    if put.returncode:
        say("vercel", f"could not update {VERCEL_FILE}: {put.stderr.strip()[:200]}")
        return False
    say("vercel", f"pointed at the tunnel on {VERCEL_BRANCH}; redeploying")
    return True


def wait_for_vercel(address: str, timeout_s: int = 300) -> bool:
    """Wait until the Vercel site's /health is answered by this tunnel's API."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if http_ok(f"{VERCEL_URL}/health") and http_ok(f"{address}/health"):
            # Both answer; the Vercel one only can once the new deployment is live.
            return True
        time.sleep(10)
    return False


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
    parser.add_argument("--vercel", action="store_true",
                        help="also repoint the shared Vercel site at this tunnel, taking the live link from anyone else using it")
    parser.add_argument("--no-vercel", action="store_true", help=argparse.SUPPRESS)
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
    say("tunnel", address)

    if args.vercel:
        for _ in range(30):  # a new quick tunnel takes a few seconds to answer
            if http_ok(f"{address}/health"):
                break
            time.sleep(2)
        if point_vercel(address):
            say("vercel", "waiting for the new deployment...")
            ready = wait_for_vercel(address)
            say("vercel", "live" if ready else f"not answering yet; check {VERCEL_URL} in a minute")

    # The tunnel address is a complete demo on its own: the preview server it
    # points at proxies /v1 and /health to this machine's API, so the console and
    # the API share one origin (D101b) without Vercel in the picture at all.
    if args.vercel:
        print(f"\n  open the console  {VERCEL_URL}")
        print("  (Vercel serves the console and forwards its API calls to this PC)")
        print("  the shared link now points here, so nobody else's demo is live\n")
    else:
        print(f"\n  open the console  {address}")
        print("  (this address is yours alone - the shared Vercel link is untouched,")
        print("   so somebody else can demo at the same time; --vercel repoints it)\n")
    print("  who gets in      anyone with the address reaches the login page; every")
    print("                   account needs its password and MFA code (python scripts/codes.py)")
    print("  stays up while   this PC is on, awake and online")
    print("  stop             python scripts/share.py --stop\n")


if __name__ == "__main__":
    main()

r"""Set a fresh machine up so the demo runs. `scripts\setup.cmd`

Why this exists
---------------
`up.py` is the everyday path and assumes everything is already installed: the
virtualenv, the console's packages, PostgreSQL, the database. A machine that has
only pulled the repository has none of that, and the first symptom is a path
error from `demo.cmd` that says nothing about which piece is missing.

This installs and checks each piece in order, and says which one it is when it
cannot go on. It is safe to run again: every step looks before it acts, and it
will not replace a database that already holds data.

    scripts\setup.cmd                       # set up, restore fixtures\handover.dump if present
    scripts\setup.cmd --dump D:\risk.dump   # restore a snapshot from somewhere else
    scripts\setup.cmd --no-data             # an empty desk: users and rules only
    scripts\setup.cmd --check               # report what is missing, change nothing
    scripts\setup.cmd --pg-zip D:\pg.zip    # use a PostgreSQL zip you already have

Then `scripts\demo.cmd`.

Two things it will not do for you, and says so rather than guessing:

* Install Python 3.12+ or Node 20+. Those belong to the machine, not the repo.
* Invent the tokenisation pepper when it restores a snapshot. Every customer
  token in the snapshot was made with the original pepper (D9c), so a different
  one orphans every behavioural baseline. Copy the original `.env` across.

It runs in two stages. Stage one needs only a bare Python: it creates the
virtualenv and installs packages. Stage two re-runs this file inside that
virtualenv, because everything after that (PostgreSQL, the database) uses the
project's own settings and code.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
FRONTEND = REPO_ROOT / "frontend"
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
BOOTSTRAP_SQL = REPO_ROOT / "backend" / "migrations" / "0000_bootstrap.sql"
FIXTURES = REPO_ROOT / "fixtures"
HANDOVER = FIXTURES / "handover.dump"
IDENTITIES = FIXTURES / "core_identities.csv"
ARTIFACTS = REPO_ROOT / "ml" / "artifacts"

# The same locations, with the same overrides, as up.py and snapshot.py.
PG_BIN = Path(os.environ.get("RISKRADAR_PG_BIN", Path.home() / ".local" / "pgsql" / "bin"))
PGDATA = Path(os.environ.get("RISKRADAR_PGDATA", Path.home() / ".local" / "riskradar-pgdata"))
PG_TOOLS = ("pg_ctl", "initdb", "pg_restore", "pg_dump", "createdb", "dropdb")

# The portable build the project was developed on (16.4). EnterpriseDB publishes
# the zip over HTTPS; there is no checksum to pin, so it is checked structurally.
PG_ZIP_URL = "https://get.enterprisedb.com/postgresql/postgresql-16.4-1-windows-x64-binaries.zip"
SUPERUSER_PASSWORD = os.environ.get("RISKRADAR_PG_SUPERUSER_PASSWORD", "riskradar_dev_superuser_pw")


def say(step: str, detail: str = "") -> None:
    print(f"  {step:<28}{detail}", flush=True)


def stop(message: str, hint: str = "") -> None:
    print(f"\n  Stopped: {message}")
    for line in hint.splitlines():
        print(f"    {line}")
    print()
    sys.exit(1)


def run(cmd: list, what: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a step with its output held back, and show it only if the step fails."""
    result = subprocess.run([str(c) for c in cmd], cwd=str(REPO_ROOT), env=env,
                            capture_output=True, text=True)
    if result.returncode:
        print(result.stdout[-2500:])
        print(result.stderr[-2500:], file=sys.stderr)
        stop(f"{what} failed (exit {result.returncode}); the output above says why.")
    return result


# ---------------------------------------------------------------------------
# What this machine has
# ---------------------------------------------------------------------------


def node_major() -> int | None:
    node = shutil.which("node")
    if not node:
        return None
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    found = re.match(r"v(\d+)", out)
    return int(found.group(1)) if found else None


def npm_path() -> str | None:
    return shutil.which("npm.cmd") or shutil.which("npm")


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


def find_gh() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    candidate = r"C:\Program Files\GitHub CLI\gh.exe"
    return candidate if Path(candidate).exists() else None


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def bootstrap_passwords() -> tuple[str, str]:
    """The role passwords the first migration creates. The app must be told the same."""
    sql = BOOTSTRAP_SQL.read_text(encoding="utf-8")
    migrate = re.search(r"ROLE riskradar_migrate LOGIN PASSWORD '([^']*)'", sql)
    app = re.search(r"ROLE riskradar_app LOGIN PASSWORD '([^']*)'", sql)
    if not (migrate and app):
        stop(f"could not read the database role passwords from {BOOTSTRAP_SQL.name}.")
    return migrate.group(1), app.group(1)


def decide_data(args: argparse.Namespace) -> tuple[str, Path | None]:
    """Restore a snapshot, or start from an empty desk. Decided the same way in both stages."""
    if args.no_data:
        return "seed", None
    if args.dump:
        dump = Path(args.dump).expanduser()
        if not dump.exists():
            stop(f"the snapshot {dump} does not exist.")
        return "restore", dump
    if HANDOVER.exists():
        return "restore", HANDOVER
    return "seed", None


# ---------------------------------------------------------------------------
# Stage one: needs only a bare Python
# ---------------------------------------------------------------------------


def prerequisites(mode: str) -> list[str]:
    problems: list[str] = []
    say("python", sys.version.split()[0])
    # pip unpacks some files with ~115-character paths under .venv, and Windows stops
    # at 260 unless long paths are switched on. The failure surfaces deep in pip as a
    # missing file, so say it here, where it can still be acted on.
    if len(str(REPO_ROOT)) > 120:
        say("location", f"this folder's path is {len(str(REPO_ROOT))} characters; Windows can fail to install the\n"
            + " " * 30 + "Python packages when it is this long. Move the repo somewhere short, e.g. C:\\risk")
    if sys.version_info < (3, 12):
        problems.append("Python 3.12 or newer is needed.\nwinget install Python.Python.3.12")

    major = node_major()
    if major is None or not npm_path():
        say("node", "MISSING")
        problems.append("Node.js 20 or newer is not installed.\nwinget install OpenJS.NodeJS.LTS")
    elif major < 20:
        say("node", f"v{major} is too old")
        problems.append(f"Node.js {major} is too old; 20 or newer is needed.\nwinget install OpenJS.NodeJS.LTS")
    else:
        say("node", f"v{major}")

    # Only needed to publish the demo, so a warning rather than a stop.
    say("cloudflared", "found" if find_cloudflared() else "missing - needed to share the demo\n"
        + " " * 30 + "winget install --id Cloudflare.cloudflared")
    gh = find_gh()
    if not gh:
        say("github cli", "missing - only needed for demo.cmd --vercel\n"
            + " " * 30 + "winget install --id GitHub.cli   then   gh auth login")
    else:
        signed_in = subprocess.run([gh, "auth", "status"], capture_output=True).returncode == 0
        say("github cli", "signed in" if signed_in else "installed, not signed in - run: gh auth login")

    if not ENV_FILE.exists() and (PGDATA / "PG_VERSION").exists():
        # Re-cloning the repo to a new folder loses .env but not the database, which
        # lives under the home folder. A fresh pepper would then sit beside tokens made
        # with the old one, and nothing would complain: every baseline is just orphaned.
        problems.append(
            f"A database already exists at {PGDATA}, but there is no .env.\n"
            "A new pepper would orphan every behavioural baseline in it.\n"
            "Copy back the .env that goes with it, or start clean by pointing\n"
            "RISKRADAR_PGDATA at an empty folder before running this again.")
    if mode == "restore" and not ENV_FILE.exists():
        problems.append(
            "There is no .env, and a snapshot is about to be restored.\n"
            "The tokens in the snapshot were made with the original machine's\n"
            "RISKRADAR_HMAC_PEPPER, so a new one would orphan every baseline.\n"
            "Copy .env from the machine the snapshot came from, then run this again.\n"
            "(Or use --no-data for an empty desk, which can have its own.)")
    return problems


def ensure_env(check: bool) -> None:
    migrate_pw, app_pw = bootstrap_passwords()
    if ENV_FILE.exists():
        env = read_env(ENV_FILE)
        if not env.get("RISKRADAR_HMAC_PEPPER"):
            stop(".env has no RISKRADAR_HMAC_PEPPER.", "Add one, or delete .env and run this again.")
        if (env.get("RISKRADAR_DB_MIGRATE_PASSWORD") != migrate_pw
                or env.get("RISKRADAR_DB_APP_PASSWORD") != app_pw):
            say(".env", "kept - but its database passwords differ from the ones the first\n"
                + " " * 30 + "migration creates, so the app will be refused. Set\n"
                + " " * 30 + "RISKRADAR_DB_MIGRATE_PASSWORD and RISKRADAR_DB_APP_PASSWORD to match\n"
                + " " * 30 + "backend/migrations/0000_bootstrap.sql, or ALTER the two roles.")
        else:
            say(".env", "present")
        return
    if check:
        say(".env", "MISSING - would be created")
        return
    # .env.example carries placeholders, and "change-me" is not what the roles are
    # created with, so copying it as it stands gives an app that cannot log in.
    wanted = {
        "RISKRADAR_DB_MIGRATE_PASSWORD": migrate_pw,
        "RISKRADAR_DB_APP_PASSWORD": app_pw,
        "RISKRADAR_HMAC_PEPPER": secrets.token_urlsafe(32),
        # The example says 3, sized for a production box (D87a). A demo laptop runs one:
        # it is what the desk was built and rehearsed on, and a single process is also
        # one less thing to be left running after the demo is stopped.
        "RISKRADAR_API_WORKERS": "1",
    }
    lines = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        key = line.partition("=")[0].strip()
        lines.append(f"{key}={wanted[key]}" if key in wanted and "=" in line else line)
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    say(".env", "created, with a new pepper (this desk starts empty)")


def stage_one(args: argparse.Namespace, mode: str) -> None:
    ensure_env(args.check)

    if VENV_PYTHON.exists():
        say("virtualenv", "present")
    elif args.check:
        say("virtualenv", "MISSING - would be created")
    else:
        say("virtualenv", "creating...")
        run([sys.executable, "-m", "venv", REPO_ROOT / ".venv"], "creating the virtualenv")

    if args.check:
        if VENV_PYTHON.exists():
            probe = subprocess.run([str(VENV_PYTHON), "-c", "import fastapi, psycopg, pyotp, uvicorn"],
                                   capture_output=True)
            say("python packages", "installed" if probe.returncode == 0 else "MISSING - would be installed")
        say("console packages", "installed" if (FRONTEND / "node_modules").exists()
            else "MISSING - would be installed")
        return

    say("python packages", "installing (a minute or two the first time)...")
    run([VENV_PYTHON, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
        "installing the Python packages")
    say("console packages", "installing...")
    run([npm_path(), "install", "--no-audit", "--no-fund", "--prefix", FRONTEND],
        "installing the console's packages")


# ---------------------------------------------------------------------------
# Stage two: inside the virtualenv, with the project's own settings
# ---------------------------------------------------------------------------


def have_postgres() -> bool:
    return all((PG_BIN / f"{tool}.exe").exists() for tool in PG_TOOLS)


def download(url: str, dest: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=60) as response, open(dest, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done, shown = 0, -1
            while chunk := response.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total and done * 10 // total != shown:
                    shown = done * 10 // total
                    say("", f"{done * 100 // total:>3}%  {done / 1e6:,.0f} of {total / 1e6:,.0f} MB")
    except (urllib.error.URLError, OSError) as exc:
        stop(f"could not download PostgreSQL ({exc}).",
             f"Download it in a browser:\n{url}\nthen run this again with:  --pg-zip <the file>")


def install_postgres(pg_zip: str | None) -> None:
    target = PG_BIN.parent
    if target.exists() and any(target.iterdir()):
        stop(f"{target} exists but is not a complete PostgreSQL.",
             "Delete that folder and run this again, or point RISKRADAR_PG_BIN at a real install.")
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(pg_zip).expanduser() if pg_zip else Path(scratch) / "postgresql.zip"
        if not pg_zip:
            say("postgresql", "downloading the portable build (~323 MB)...")
            download(PG_ZIP_URL, archive)
        elif not archive.exists():
            stop(f"the zip {archive} does not exist.")
        say("postgresql", "unpacking...")
        try:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(Path(scratch) / "unpacked")
        except zipfile.BadZipFile:
            stop(f"{archive.name} is not a valid zip file.", "The download may have been cut short; try again.")
        found = next((p for p in (Path(scratch) / "unpacked").rglob("pg_ctl.exe")), None)
        if not found:
            stop("that zip does not contain a PostgreSQL build (no pg_ctl.exe).")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()  # empty (checked above); move() would otherwise nest the build inside it
        shutil.move(str(found.parent.parent), str(target))
    if not have_postgres():
        stop(f"PostgreSQL was unpacked to {target} but is missing some tools.")
    say("postgresql", f"installed at {target}")


def init_cluster() -> None:
    PGDATA.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(suffix=".pw")
    os.close(handle)  # an open handle would stop Windows deleting the file afterwards
    pwfile = Path(name)
    try:
        pwfile.write_text(SUPERUSER_PASSWORD, encoding="ascii")
        run([PG_BIN / "initdb.exe", "-D", PGDATA, "-U", "postgres", "--auth-local=trust",
             "--auth-host=scram-sha-256", f"--pwfile={pwfile}", "-E", "UTF8"], "creating the database cluster")
    finally:
        pwfile.unlink(missing_ok=True)
    say("database cluster", f"created at {PGDATA}")


def database_has_users(settings) -> bool | None:
    import psycopg

    try:
        with psycopg.connect(settings.migrate_dsn, connect_timeout=5) as conn:
            return conn.execute("SELECT count(*) FROM users").fetchone()[0] > 0
    except Exception:
        return None


def final_checks(settings) -> int:
    """What the demo will quietly be without, said now instead of discovered mid-demo.

    Returns how many things are missing, so the closing message can be honest.
    """
    missing = 0
    import psycopg
    import psycopg.rows

    try:
        with psycopg.connect(settings.app_dsn, connect_timeout=5, row_factory=psycopg.rows.dict_row) as conn:
            row = conn.execute("SELECT version, artifact_path FROM model_versions WHERE is_active LIMIT 1").fetchone()
    except Exception:
        row = None
    if not row:
        missing += 1
        say("model", "NONE ACTIVE - scoring runs on rules only until one is promoted")
    else:
        path = Path(row["artifact_path"] or "")
        path = path if path.is_absolute() else REPO_ROOT / path
        if path.exists():
            say("model", f"{row['version']} ready")
        else:
            missing += 1
            say("model", f"MISSING FILE {path.name} - copy it from the other machine's ml/artifacts folder,\n"
                + " " * 30 + "or the API runs on rules only")
    if IDENTITIES.exists():
        say("customer file", "present")
    else:
        missing += 1
        say("customer file", "MISSING - copy fixtures/core_identities.csv from the other machine")
    return missing


def stage_two(args: argparse.Namespace, mode: str, dump: Path | None) -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    sys.path.insert(0, str(SCRIPTS))
    from riskradar.config import settings

    import up

    s = settings()

    if have_postgres():
        say("postgresql", f"found at {PG_BIN}")
    elif args.check:
        say("postgresql", "MISSING - would be downloaded (~323 MB)")
    else:
        install_postgres(args.pg_zip)

    initialised = (PGDATA / "PG_VERSION").exists()
    if initialised:
        say("database cluster", "present")
    elif args.check:
        say("database cluster", "MISSING - would be created")
    else:
        init_cluster()

    if args.check:
        if not (have_postgres() and initialised):
            say("database", "(checked once PostgreSQL is in place)")
            return
        if not up.listening(s.db_port):
            say("database", f"PostgreSQL is not running on {s.db_port} - up.py or demo.cmd starts it")
            return
        has_users = database_has_users(s)
        say("database", {None: "not created yet", False: "created, empty", True: "has data"}[has_users])
        final_checks(s)
        return

    up.start_postgres(s.db_port)
    run([VENV_PYTHON, SCRIPTS / "migrate.py"], "creating the database")
    say("database", "ready")

    if database_has_users(s):
        say("data", "the database already holds data - left exactly as it is\n"
            + " " * 30 + "(scripts/snapshot.py --restore replaces it, deliberately)")
    elif mode == "restore":
        say("data", f"restoring {dump.name} - a few minutes...")
        import snapshot

        snapshot.restore(dump, 4, stop_services=False)
        run([VENV_PYTHON, SCRIPTS / "migrate.py"], "bringing the restored database up to date")
        say("data", "restored")
    else:
        run([VENV_PYTHON, SCRIPTS / "seed.py"], "seeding the users and rules")
        import demo_reset

        demo_reset.register_trained_model()
        say("data", "seeded with users and rules; the desk has no cases yet")

    missing = final_checks(s)
    if missing:
        print(f"\n  Set up, but {missing} thing(s) above are still missing. Copy them across from the")
        print("  machine the snapshot came from; the demo will run without them but not properly.")
        print("\n  Then start the demo with:\n")
    else:
        print("\n  Ready. Start the demo with:\n")
    print("      scripts\\demo.cmd             your own address, nobody else affected")
    print("      scripts\\demo.cmd --vercel    also take the shared riskradar-ml.vercel.app link")
    print("\n  MFA codes:  scripts\\codes.cmd\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Set a fresh machine up so the demo runs")
    parser.add_argument("--dump", help="restore this snapshot (default: fixtures/handover.dump if present)")
    parser.add_argument("--no-data", action="store_true", help="start with an empty desk instead of restoring")
    parser.add_argument("--check", action="store_true", help="report what is missing and change nothing")
    parser.add_argument("--pg-zip", help="a PostgreSQL binaries zip you already have, instead of downloading one")
    parser.add_argument("--stage2", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    mode, dump = decide_data(args)

    if args.stage2:
        stage_two(args, mode, dump)
        return

    print("\nRisk Radar - " + ("checking this machine\n" if args.check else "setting up this machine\n"))
    problems = prerequisites(mode)
    if problems:
        print()
        stop(f"{len(problems)} thing(s) need doing first.",
             "\n\n".join(f"{n}. {text}".replace("\n", "\n   ") for n, text in enumerate(problems, 1)))
    if mode == "restore":
        say("snapshot", f"will restore {dump.name} ({dump.stat().st_size / 1e6:,.0f} MB)")
    else:
        say("snapshot", "none - starting with an empty desk" + (
            "" if args.no_data else " (put a .dump at fixtures/handover.dump to restore one)"))

    stage_one(args, mode)

    if not VENV_PYTHON.exists() or not ENV_FILE.exists():
        print("\n  (the remaining checks need the virtualenv and .env first)\n")
        return
    result = subprocess.run([str(VENV_PYTHON), str(Path(__file__).resolve()), "--stage2", *sys.argv[1:]])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()

"""Manage the per-user macOS HTTP server service (separate from ingestion)."""
import argparse
import os
import plistlib
import socket
import subprocess
import sys
from pathlib import Path

LABEL = "com.user.token-tracker.server"
ROOT = Path(__file__).resolve().parent.parent
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
DOMAIN = f"gui/{os.getuid()}"
TARGET = f"{DOMAIN}/{LABEL}"


def definition():
    logs = Path.home() / "Library/Logs"
    return {
        "Label": LABEL,
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-m", "uvicorn",
                             "tracker.api:app", "--host", "127.0.0.1", "--port", "8732"],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "token-tracker-server.log"),
        "StandardErrorPath": str(logs / "token-tracker-server.error.log"),
    }


def loaded():
    return subprocess.run(["launchctl", "print", TARGET], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def launch(*args):
    subprocess.run(["launchctl", *args], check=True)


def free_port():
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", 8732))
        except OSError:
            raise SystemExit("Port 8732 is in use. Stop the existing server through its launcher first.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=["install", "start", "stop", "restart", "status", "uninstall"])
    ap.add_argument("--dry-run", action="store_true", help="Print the generated plist without changing anything")
    args = ap.parse_args()
    if args.dry_run:
        print(plistlib.dumps(definition()).decode())
        return
    if sys.platform != "darwin":
        raise SystemExit("This service manager requires macOS.")
    if args.action == "status":
        print("Running/loaded" if loaded() else "Not loaded")
        return
    if args.action in ("stop", "uninstall"):
        if loaded():
            launch("bootout", TARGET)
        if args.action == "uninstall" and PLIST.exists():
            PLIST.unlink()
        print("Service stopped" if args.action == "stop" else "Service uninstalled")
        return
    if args.action == "install":
        python = ROOT / ".venv/bin/python"
        if not python.exists():
            raise SystemExit("Run uv sync in the project first.")
        subprocess.run([str(python), "-c", "import uvicorn, tracker.api"], cwd=ROOT, check=True)
        if not loaded():
            free_port()
        if PLIST.exists():
            existing = plistlib.loads(PLIST.read_bytes())
            if existing.get("WorkingDirectory") != str(ROOT):
                raise SystemExit("A service from another checkout exists; uninstall it there first.")
        if loaded():
            launch("bootout", TARGET)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        (Path.home() / "Library/Logs").mkdir(parents=True, exist_ok=True)
        temporary = PLIST.with_suffix(".plist.tmp")
        temporary.write_bytes(plistlib.dumps(definition()))
        temporary.replace(PLIST)
        launch("enable", TARGET)
    elif not PLIST.exists():
        raise SystemExit("Install the service first with make server-service.")
    if loaded():
        if args.action == "restart":
            launch("kickstart", "-k", TARGET)
    else:
        free_port()
        launch("bootstrap", DOMAIN, str(PLIST))
    print("Server service enabled at http://127.0.0.1:8732")


if __name__ == "__main__":
    main()

"""Start/stop the free, ephemeral service on the same GitHub Actions runner."""

import argparse
import os
import subprocess
import time
from pathlib import Path

import requests


IMAGE = "ghcr.io/flaresolverr/flaresolverr:v3.5.2"
CONTAINER = "topgg-free-flaresolverr"
URL = "http://127.0.0.1:8191"


def docker(*args, timeout=30):
    return subprocess.run(["docker", *args], check=True, timeout=timeout,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop():
    try:
        docker("rm", "--force", "--volumes", CONTAINER)
    except (OSError, subprocess.SubprocessError):
        pass


def start():
    bridge = Path(__file__).with_name("flaresolverr_bridge.py").resolve()
    stop()
    docker("pull", IMAGE, timeout=180)
    docker("run", "--detach", "--name", CONTAINER, "--network", "host",
           "--shm-size", "512m", "--env", "HOST=127.0.0.1", "--env", "LOG_LEVEL=warn",
           "--env", "LOG_HTML=false", "--env", "HEADLESS=true",
           "--env", "DISABLE_MEDIA=false", "--env", "LANG=en_US",
           "--mount", f"type=bind,src={bridge},dst=/bridge/flaresolverr_bridge.py,readonly",
           IMAGE, "/usr/local/bin/python", "-u", "/bridge/flaresolverr_bridge.py")
    deadline = time.monotonic() + 120
    with requests.Session() as session:
        session.trust_env = False
        while time.monotonic() < deadline:
            try:
                response = session.get(URL + "/health", timeout=2)
                if response.ok and response.json().get("status") == "ok":
                    return
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1)
    raise RuntimeError("Local FlareSolverr startup timed out")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("start", "stop"))
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args()
    if args.command == "stop":
        stop()
        return 0
    try:
        start()
    except Exception as exc:
        stop()
        print("Free FlareSolverr unavailable (" + type(exc).__name__ + "); ordinary Chrome remains available")
        return 1 if args.required else 0
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
            env.write("FLARESOLVERR_URL=" + URL + "\n")
    print("Free local FlareSolverr ready; no API key or paid service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

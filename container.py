#!/usr/bin/env python3
"""Run the Glovebox MCP server inside its Docker container. Standard library only, so it works on
a machine that has Docker and nothing else of Glovebox installed (macOS, Windows, a bare Linux).

Why a long-lived container and `docker exec`, not `docker run` per session: instances outlive an
agent's session and several agents share one desktop, each with its own display. A container
per session would kill the apps with the session and give every agent a separate, colliding copy.

  python container.py            start the container if needed, then serve MCP over stdio
                                 (what GLOVEBOX_MODE=container makes server.py do)
  python container.py up         start (or create) the container; print where to watch it
  python container.py down       stop and remove the container (the data folder stays)
  python container.py status     JSON: state, image, view URL, data folder
  python container.py build      docker build the local image from this folder

Environment:
  GLOVEBOX_CONTAINER   container name                     (default: glovebox)
  GLOVEBOX_IMAGE       image to run                       (default: glovebox-mcp:local, else the
                                                           published image for this version)
  GLOVEBOX_DATA        host folder for files/ + profiles/ (default: ./container-data next to this file)
  GLOVEBOX_VIEW_PORT   host port of the web viewer        (default: 6080, bound to 127.0.0.1)
  GLOVEBOX_DESKTOP_SIZE, GLOVEBOX_VNC_PASSWORD            passed to a NEWLY created container
Everything is logged to stderr: stdout belongs to the MCP protocol.
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _version import __version__  # noqa: E402

NAME = os.environ.get("GLOVEBOX_CONTAINER", "glovebox")
LOCAL_IMAGE = "glovebox-mcp:local"
REGISTRY_IMAGE = f"ghcr.io/segentic-lab/glovebox-mcp:{__version__}"
PORT = os.environ.get("GLOVEBOX_VIEW_PORT", "6080")
DATA = os.environ.get("GLOVEBOX_DATA") or os.path.join(HERE, "container-data")
VIEW_URL = f"http://localhost:{PORT}/vnc.html?autoconnect=1&resize=scale"
# settings of the launcher itself or of a host install — meaningless (or wrong) inside the container
NOT_FORWARDED = {"GLOVEBOX_MODE", "GLOVEBOX_HOST_DISPLAY", "GLOVEBOX_CONTAINER", "GLOVEBOX_IMAGE", "GLOVEBOX_DATA",
                 "GLOVEBOX_VIEW_PORT", "GLOVEBOX_DESKTOP_SIZE", "GLOVEBOX_VNC_PASSWORD", "GLOVEBOX_IN_CONTAINER",
                 "GLOVEBOX_VIEW_URL"}


class Problem(RuntimeError):
    """Something the person has to fix; the message names the step."""


def log(msg: str) -> None:
    print(f"[glovebox] {msg}", file=sys.stderr, flush=True)


def docker(*args, timeout=120, check=False):
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise Problem("Docker is not installed (or not on PATH) — install Docker Desktop / docker.io, "
                      "or run Glovebox natively with ./install.sh") from None
    except subprocess.TimeoutExpired:
        raise Problem(f"`docker {' '.join(args[:2])}` did not answer within {timeout} s — is the Docker daemon running?") from None
    if check and r.returncode != 0:
        raise Problem(f"`docker {' '.join(args[:3])}` failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def state():
    """'running' | 'exited' | 'created' | 'paused' | … | None (no such container)."""
    r = docker("inspect", "-f", "{{.State.Status}}", NAME, timeout=30)
    if r.returncode != 0:
        err = (r.stderr or "").lower()
        if "no such" in err or "not found" in err:
            return None
        raise Problem(f"cannot talk to Docker: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def image_exists(image: str) -> bool:
    return docker("image", "inspect", image, timeout=30).returncode == 0


def pick_image() -> str:
    explicit = os.environ.get("GLOVEBOX_IMAGE")
    for image in ([explicit] if explicit else [LOCAL_IMAGE, REGISTRY_IMAGE]):
        if image_exists(image):
            return image
    wanted = explicit or REGISTRY_IMAGE
    log(f"image {wanted} is not on this machine — pulling it …")
    if docker("pull", wanted, timeout=900).returncode == 0:
        return wanted
    raise Problem(f"no Glovebox image: {wanted} could not be pulled and {LOCAL_IMAGE} is not built. "
                  f"Build it once: `docker build -t {LOCAL_IMAGE} \"{HERE}\"` (or ./install.sh container)")


def run_args(image: str) -> list:
    for sub in ("files", "profiles"):
        os.makedirs(os.path.join(DATA, sub), exist_ok=True)   # created by us, so not owned by root
    args = ["run", "-d", "--name", NAME, "--restart", "unless-stopped", "--shm-size", "1g",
            "-p", f"127.0.0.1:{PORT}:6080",
            "-v", f"{os.path.join(DATA, 'files')}:/app/files",
            "-v", f"{os.path.join(DATA, 'profiles')}:/app/profiles"]
    for k in ("GLOVEBOX_DESKTOP_SIZE", "GLOVEBOX_VNC_PASSWORD"):
        if os.environ.get(k):
            args += ["-e", f"{k}={os.environ[k]}"]
    if PORT != "6080":
        args += ["-e", f"GLOVEBOX_VIEW_URL={VIEW_URL}"]
    return args + [image]


def ensure_running() -> str:
    """Start (or create) the container and wait until its desktop answers. Returns what was done."""
    st = state()
    did = "already running"
    if st == "running":
        pass
    elif st is None:
        image = pick_image()
        log(f"creating container '{NAME}' from {image} (data: {DATA})")
        docker(*run_args(image), timeout=180, check=True)
        did = "created"
    else:
        if st == "paused":
            docker("unpause", NAME, check=True)
        else:
            log(f"starting container '{NAME}' (was {st})")
            docker("start", NAME, check=True)
        did = "started"
    deadline = time.time() + 40
    while time.time() < deadline:
        if docker("exec", NAME, "xdpyinfo", "-display", ":0", timeout=20).returncode == 0:
            if did != "already running":
                log(f"desktop ready — watch it at {VIEW_URL}")
            return did
        if state() != "running":
            break
        time.sleep(0.5)
    tail = docker("logs", "--tail", "20", NAME, timeout=20)
    raise Problem(f"container '{NAME}' did not bring its desktop up: {(tail.stderr or tail.stdout).strip()[-400:]}")


def server_cmd() -> list:
    """The command an MCP client runs: the server's stdio, inside the container."""
    env = []
    for k, v in sorted(os.environ.items()):
        if k.startswith("GLOVEBOX_") and k not in NOT_FORWARDED:
            env += ["-e", f"{k}={v}"]
    return ["docker", "exec", "-i", *env, NAME, "glovebox-server"]


def exec_server() -> None:
    """Hand this process's stdio to the server in the container. Does not return."""
    try:
        ensure_running()
    except Problem as e:
        log(str(e))
        sys.exit(2)
    cmd = server_cmd()
    if os.name == "nt":                      # no real exec on Windows: stay as a transparent parent
        sys.exit(subprocess.call(cmd))
    os.execvp(cmd[0], cmd)


def main(argv) -> int:
    what = argv[0] if argv else "serve"
    try:
        if what == "serve":
            exec_server()
        elif what == "up":
            did = ensure_running()
            print(json.dumps({"ok": True, "container": NAME, "did": did, "view_url": VIEW_URL, "data": DATA,
                              "mcp": {"command": "docker", "args": ["exec", "-i", NAME, "glovebox-server"]}}, indent=2))
        elif what == "down":
            if state() is None:
                print(json.dumps({"ok": True, "container": NAME, "did": "nothing (no such container)"}))
            else:
                docker("rm", "-f", NAME, check=True)
                print(json.dumps({"ok": True, "container": NAME, "did": "removed", "data_kept": DATA}))
        elif what == "status":
            st = state()
            image = docker("inspect", "-f", "{{.Config.Image}}", NAME, timeout=30).stdout.strip() if st else None
            print(json.dumps({"ok": True, "container": NAME, "state": st, "image": image, "view_url": VIEW_URL,
                              "data": DATA, "docker": shutil.which("docker")}, indent=2))
        elif what == "build":
            log(f"building {LOCAL_IMAGE} from {HERE} (several minutes the first time) …")
            return subprocess.call(["docker", "build", "-t", LOCAL_IMAGE, *argv[1:], HERE], stdout=sys.stderr)
        else:
            print(__doc__, file=sys.stderr)
            return 1
    except Problem as e:
        log(str(e))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

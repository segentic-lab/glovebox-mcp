"""container.py — the launcher of container mode. Docker itself is faked: these tests pin what
is asked of it, not whether an image builds (tests/container_smoke.py does that, in CI)."""
import json
import subprocess

import pytest

import container


class FakeDocker:
    """Answers `docker …` calls from a table: {first words of the command: (returncode, stdout, stderr)}."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, *args, timeout=120, check=False):
        self.calls.append(args)
        for key, (rc, out, err) in self.table.items():
            if args[:len(key)] == key:
                if check and rc != 0:
                    raise container.Problem(err)
                return subprocess.CompletedProcess(args, rc, out, err)
        raise AssertionError(f"unexpected docker call: {args}")


def test_missing_docker_is_a_named_problem(monkeypatch):
    def boom(*a, **kw):
        raise FileNotFoundError
    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(container.Problem, match="Docker is not installed"):
        container.docker("ps")


def test_state_tells_absent_from_broken(monkeypatch):
    monkeypatch.setattr(container, "docker", FakeDocker({("inspect",): (1, "", "Error: No such object: glovebox")}))
    assert container.state() is None
    monkeypatch.setattr(container, "docker", FakeDocker({("inspect",): (1, "", "Cannot connect to the Docker daemon")}))
    with pytest.raises(container.Problem, match="cannot talk to Docker"):
        container.state()
    monkeypatch.setattr(container, "docker", FakeDocker({("inspect",): (0, "running\n", "")}))
    assert container.state() == "running"


def test_a_running_container_is_left_alone(monkeypatch):
    fake = FakeDocker({("inspect",): (0, "running\n", ""), ("exec",): (0, "", "")})
    monkeypatch.setattr(container, "docker", fake)
    assert container.ensure_running() == "already running"
    assert not any(c[0] in ("run", "start", "pull") for c in fake.calls)


def test_a_stopped_container_is_started_not_recreated(monkeypatch):
    states = iter(["exited", "running", "running"])
    fake = FakeDocker({("start",): (0, "", ""), ("exec",): (0, "", "")})

    def docker(*args, **kw):
        if args[0] == "inspect":
            return subprocess.CompletedProcess(args, 0, next(states) + "\n", "")
        return fake(*args, **kw)
    monkeypatch.setattr(container, "docker", docker)
    assert container.ensure_running() == "started"
    assert ("start", container.NAME) in fake.calls and not any(c[0] == "run" for c in fake.calls)


def test_a_new_container_keeps_its_ports_and_data_on_this_machine(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "DATA", str(tmp_path / "data"))
    monkeypatch.setenv("GLOVEBOX_VNC_PASSWORD", "hunter2")
    args = container.run_args("glovebox-mcp:local")
    assert args[:2] == ["run", "-d"] and args[-1] == "glovebox-mcp:local"
    assert f"127.0.0.1:{container.PORT}:6080" in args            # the viewer is never published to the network
    assert f"{tmp_path / 'data' / 'files'}:/app/files" in args and (tmp_path / "data" / "profiles").is_dir()
    assert "GLOVEBOX_VNC_PASSWORD=hunter2" in args
    import os
    assert f"GLOVEBOX_UID={os.getuid()}" in args                 # the sandbox user writes the data folder as you
    assert not any(a.startswith("--privileged") or a == "host" for a in args)


def test_image_choice_prefers_a_local_build_and_names_the_fix(monkeypatch):
    monkeypatch.delenv("GLOVEBOX_IMAGE", raising=False)
    monkeypatch.setattr(container, "image_exists", lambda image: image == container.LOCAL_IMAGE)
    assert container.pick_image() == container.LOCAL_IMAGE
    monkeypatch.setattr(container, "image_exists", lambda image: False)
    monkeypatch.setattr(container, "docker", FakeDocker({("pull",): (1, "", "denied")}))
    with pytest.raises(container.Problem) as e:
        container.pick_image()
    assert "docker build -t glovebox-mcp:local" in str(e.value)
    monkeypatch.setattr(container, "docker", FakeDocker({("pull",): (0, "", "")}))
    assert container.pick_image() == container.REGISTRY_IMAGE


def test_server_command_forwards_only_settings_that_mean_something_inside(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("GLOVEBOX_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("GLOVEBOX_VISION", "basic")
    monkeypatch.setenv("GLOVEBOX_MODE", "container")              # would recurse inside the container
    monkeypatch.setenv("GLOVEBOX_HOST_DISPLAY", ":0")             # the HOST's display means nothing in there
    cmd = container.server_cmd()
    assert cmd == ["docker", "exec", "-i", "-e", "GLOVEBOX_VISION=basic", container.NAME, "glovebox-server"]


def test_cli_status_is_json_and_touches_nothing(monkeypatch, capsys):
    fake = FakeDocker({("inspect", "-f", "{{.State.Status}}"): (0, "running\n", ""),
                       ("inspect", "-f", "{{.Config.Image}}"): (0, "glovebox-mcp:local\n", ""),
                       ("inspect", "-f", "{{range .Mounts}}{{.Destination}} {{end}}"): (0, "/app/files /app/profiles \n", ""),
                       ("exec", container.NAME, "printenv"): (0, "full\n", "")})
    monkeypatch.setattr(container, "docker", fake)
    assert container.main(["status"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["state"] == "running" and d["image"] == "glovebox-mcp:local" and "vnc.html" in d["view_url"]
    assert d["apps"] == "full" and d["vision"] == "ocr"
    assert all(c[0] == "inspect" or c[:3] == ("exec", container.NAME, "printenv") for c in fake.calls)


def test_vision_sharing_is_opt_in_and_mounts_only_the_share_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(container, "DATA", str(tmp_path / "data"))
    monkeypatch.setenv("GLOVEBOX_VISION_SOCKET", str(tmp_path / "rt" / "glovebox-vision.sock"))
    monkeypatch.delenv("GLOVEBOX_CONTAINER_VISION", raising=False)
    args = container.run_args("glovebox-mcp:local")
    assert not any(container.VISION_MOUNT in a for a in args) and "GLOVEBOX_VISION_REMOTE=1" not in args
    monkeypatch.setenv("GLOVEBOX_CONTAINER_VISION", "host")
    monkeypatch.setattr(container.sys, "platform", "linux")
    args = container.run_args("glovebox-mcp:local")
    share = tmp_path / "rt" / "glovebox-vision-share"
    assert f"{share}:{container.VISION_MOUNT}:ro" in args          # read-only, and not the runtime dir itself
    assert share.is_dir() and oct(share.stat().st_mode & 0o777) == "0o700"
    assert f"GLOVEBOX_VISION_SOCKET={container.VISION_MOUNT}/vision.sock" in args and "GLOVEBOX_VISION_REMOTE=1" in args
    monkeypatch.setattr(container.sys, "platform", "darwin")
    with pytest.raises(container.Problem, match="needs a Linux host"):
        container.run_args("glovebox-mcp:local")
    monkeypatch.setenv("GLOVEBOX_CONTAINER_VISION", "cloud")
    with pytest.raises(container.Problem, match="use 'host'"):
        container.run_args("glovebox-mcp:local")


def test_the_host_socket_setting_never_leaks_into_the_container(monkeypatch):
    """Inside, the socket path is the mount; forwarding the host's path would point at nothing."""
    for k in list(__import__("os").environ):
        if k.startswith("GLOVEBOX_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("GLOVEBOX_VISION_SOCKET", "/run/user/1000/glovebox-vision.sock")
    monkeypatch.setenv("GLOVEBOX_CONTAINER_VISION", "host")
    monkeypatch.setenv("GLOVEBOX_APPS", "full")
    assert container.server_cmd() == ["docker", "exec", "-i", container.NAME, "glovebox-server"]


def test_install_refuses_anything_but_package_names(monkeypatch):
    monkeypatch.setattr(container, "docker", FakeDocker({}))
    monkeypatch.setattr(container.subprocess, "call", lambda *a, **kw: pytest.fail("nothing may run"))
    for bad in ([], ["gimp; rm -rf /"], ["$(id)"], ["a b"]):
        assert container.main(["install", *bad]) == 2


def test_container_mode_hands_over_before_the_heavy_imports():
    """GLOVEBOX_MODE=container must work on a machine without the server's dependencies: the
    hand-over sits above the third-party imports and never runs on a plain import."""
    import os
    src = open(os.path.join(os.path.dirname(container.__file__), "server.py"), encoding="utf-8").read()
    assert src.index("container.exec_server()") < src.index("from mcp.server.mcpserver import MCPServer")
    assert '__name__ == "__main__" and os.environ.get("GLOVEBOX_MODE"' in src

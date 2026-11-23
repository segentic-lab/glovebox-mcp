"""install.sh — the interactive questions, driven with piped answers on faked machines.

`--plan` makes the installer print what it decided and stop, so nothing is ever installed here.
Each test runs a COPY of the script in a temp folder with a PATH that holds only stubs, so the
answers do not depend on this machine's GPU, Docker or installed models."""
import os
import shutil
import subprocess

import pytest

from conftest import REPO

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="bash missing")
TOOLS = ("bash", "dirname", "head", "tr", "sed", "cat", "env")


def machine(tmp_path, os_name="Linux", vram=None, docker=True, native_local=False, claude=True):
    """A fake machine: returns (script path, env)."""
    root = tmp_path / "glovebox"
    root.mkdir()
    shutil.copy(os.path.join(REPO, "install.sh"), root / "install.sh")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for t in TOOLS:
        os.symlink(shutil.which(t), bin_ / t)

    def stub(name, body):
        p = bin_ / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)
    stub("uname", f'[ "$1" = "-m" ] && echo x86_64 || echo {os_name}')
    if vram is not None:
        stub("nvidia-smi", f'case "$1" in *name*) echo "Test GPU";; *) echo "{vram}";; esac')
    if docker:
        stub("docker", "exit 0")
    if native_local:
        (root / ".venv" / "bin").mkdir(parents=True)
        py = root / ".venv" / "bin" / "python"
        py.write_text("#!/bin/sh\n")
        py.chmod(0o755)
        (root / "OmniParser" / "weights").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    if claude:
        (home / ".claude").mkdir()
    return str(root / "install.sh"), {"PATH": str(bin_), "HOME": str(home), "GLOVEBOX_INSTALL_ASK": "1"}


def run(script, env, answers="", *args):
    return subprocess.run(["bash", script, "--plan", *args], input=answers, env=env, capture_output=True, text=True, timeout=30)


def plan(r):
    line = next(l for l in r.stdout.splitlines() if l.startswith("plan: "))
    return dict(kv.split("=", 1) for kv in line[len("plan: "):].split())


def test_without_a_terminal_nothing_is_asked(tmp_path):
    script, env = machine(tmp_path, vram=16384)
    env.pop("GLOVEBOX_INSTALL_ASK")
    r = run(script, env)                                # stdin is a pipe, not a terminal
    assert r.returncode == 0 and "Where should the sandbox run" not in r.stdout
    assert plan(r)["mode"] == "auto" and "command: ./install.sh auto" in r.stdout


def test_enter_everywhere_is_the_old_default(tmp_path):
    script, env = machine(tmp_path, vram=16384)
    r = run(script, env, "\n\n\n\n\n")
    p = plan(r)
    assert r.returncode == 0 and p == {"mode": "auto", "apps": "browser", "container_vision": "ocr", "skill": "1",
                                       "screenparser": "1", "dev": "0"}
    assert "here that is 'local'" in r.stdout and "ScreenParser detector" in r.stdout
    assert "Plan — the same as running:  ./install.sh auto" in r.stdout


def test_end_of_input_takes_the_defaults_instead_of_hanging(tmp_path):
    script, env = machine(tmp_path, vram=None)
    r = run(script, env, "")
    assert r.returncode == 0 and plan(r)["mode"] == "auto" and "here that is 'basic'" in r.stdout


def test_container_with_full_apps_shared_vision_and_no_skill(tmp_path):
    script, env = machine(tmp_path, vram=16384, native_local=True)
    r = run(script, env, "2\n3\n2\nn\ny\n")
    p = plan(r)
    assert p["mode"] == "container" and p["apps"] == "full" and p["container_vision"] == "host" and p["skill"] == "0"
    assert "command: ./install.sh container --apps full --vision host --no-skill" in r.stdout


def test_options_this_machine_cannot_run_are_refused_and_asked_again(tmp_path):
    script, env = machine(tmp_path, vram=4096, native_local=False)     # a 4 GB GPU, no native install
    r = run(script, env, "1\n2\n3\n\n\n")                              # local -> refused -> basic
    assert "needs an NVIDIA GPU with at least 6 GB VRAM" in r.stdout and "not available on this machine" in r.stdout
    assert plan(r)["mode"] == "basic" and "ScreenParser detector" not in r.stdout
    r = run(script, env, "2\n1\n2\n1\n\n\n")                           # container, host vision -> refused -> OCR
    assert "needs a native './install.sh local'" in r.stdout
    p = plan(r)
    assert p["mode"] == "container" and p["container_vision"] == "ocr"


def test_a_mac_is_offered_only_the_container(tmp_path):
    script, env = machine(tmp_path, os_name="Darwin", claude=False)
    r = run(script, env, "1\n2\n\n\n\n")
    assert "the native sandbox needs Linux (X11); this is Darwin" in r.stdout
    assert plan(r)["mode"] == "container" and "Claude Code skill" not in r.stdout     # no ~/.claude: not asked
    r = run(script, env, "")                                           # the default there is the container
    assert plan(r)["mode"] == "container"


def test_nothing_possible_says_what_to_install(tmp_path):
    script, env = machine(tmp_path, os_name="Darwin", docker=False)
    r = run(script, env, "")
    assert r.returncode == 1 and "Install Docker Desktop" in r.stdout and "plan:" not in r.stdout


def test_saying_no_at_the_end_installs_nothing(tmp_path):
    script, env = machine(tmp_path, vram=16384)
    r = run(script, env, "1\n3\n\nn\n")
    assert r.returncode == 0 and "Nothing was installed." in r.stdout and "plan:" not in r.stdout


def test_garbage_answers_give_up_instead_of_looping(tmp_path):
    script, env = machine(tmp_path, vram=16384)
    r = run(script, env, "x\n9\n0\nyes\n-1\n")
    assert r.returncode == 1 and "please answer with a number from 1 to 2" in r.stdout
    assert "nothing was installed" in r.stdout.lower() and "plan:" not in r.stdout


def test_a_named_mode_or_yes_skips_the_questions(tmp_path):
    script, env = machine(tmp_path, vram=16384)
    for args, mode in ((("--yes",), "auto"), (("basic",), "basic"), (("container", "--apps", "basic"), "container")):
        r = run(script, env, "", *args)
        assert "Where should the sandbox run" not in r.stdout and plan(r)["mode"] == mode, args
    r = run(script, env, "\n\n\n\n\n", "--dev", "--no-skill")           # flags alone still ask, and are kept
    p = plan(r)
    assert "Where should the sandbox run" in r.stdout and p["dev"] == "1" and p["skill"] == "0"
    assert "Claude Code skill" not in r.stdout and "--no-skill --dev" in r.stdout

"""host_bridge: the clipboard sync policy (no X server needed) and the keymap helpers."""
import os

import pytest

import host_bridge as hb


class Fake:
    """Two fake clipboards; None = empty/non-text."""
    def __init__(self, host=None, inst=None):
        self.host, self.inst = host, inst
        self.sync = hb.ClipboardSync(lambda: self.host, lambda: self.inst,
                                     self._wh, self._wi)
    def _wh(self, t): self.host = t
    def _wi(self, t): self.inst = t


def test_seed_from_host_then_both_directions():
    f = Fake(host="from host", inst="stale")
    assert f.sync.step() == "seed" and f.inst == "from host"
    assert f.sync.step() == "idle"
    f.inst = "agent copied"                       # app/agent copied in the instance
    assert f.sync.step() == "to_host" and f.host == "agent copied"
    f.host = "person copied"                      # person copied on the host
    assert f.sync.step() == "to_instance" and f.inst == "person copied"
    assert f.sync.copied == {"to_instance": 2, "to_host": 1}


def test_empty_or_non_text_never_overwrites():
    f = Fake(host=None, inst="keep me")
    assert f.sync.step() == "idle" and f.inst == "keep me" and f.sync.last == "keep me"
    f.host = "x"
    assert f.sync.step() == "to_instance"
    f.host = None                                 # host now holds an image
    assert f.sync.step() == "idle" and f.inst == "x"
    f.inst = None                                 # instance app cleared it
    assert f.sync.step() == "idle" and f.host is None


def test_instance_change_wins_a_simultaneous_change():
    f = Fake(host="a", inst="a")
    f.sync.step()
    f.host, f.inst = "h", "i"
    assert f.sync.step() == "to_host" and f.host == "i" and f.inst == "i"


def test_same_text_on_both_sides_is_quietly_adopted():
    f = Fake(host="a", inst="a")
    assert f.sync.step() == "idle"
    f.host = f.inst = "b"                        # e.g. the previous bridge instance synced it
    assert f.sync.step() == "idle" and f.sync.last == "b"
    f.host = "c"
    assert f.sync.step() == "to_instance"


def test_layout_from_dump():
    assert hb.layout_from_dump('xkb_keymap {\n xkb_symbols "pc_si_inet(evdev)" {\n') == "si"
    assert hb.layout_from_dump('xkb_symbols "pc_si(nodeadkeys)_inet(evdev)_group(alt_shift_toggle)" {') == "si(nodeadkeys)"
    assert hb.layout_from_dump('xkb_symbols "pc_us_inet(evdev)" {') == "us"
    assert hb.layout_from_dump('xkb_symbols "pc+si+inet(evdev)" {') == "si"          # fresh server naming
    assert hb.layout_from_dump('xkb_symbols "pc+de(nodeadkeys)+inet(evdev)+group(alt)" {') == "de(nodeadkeys)"
    assert hb.layout_from_dump("nothing here") == ""


def test_requested_layout(monkeypatch):
    monkeypatch.delenv("GLOVEBOX_KEYBOARD", raising=False)
    assert hb.requested_layout() == ("", "")
    monkeypatch.setenv("GLOVEBOX_KEYBOARD", "si")
    assert hb.requested_layout() == ("si", "")
    monkeypatch.setenv("GLOVEBOX_KEYBOARD", "de(nodeadkeys)")
    assert hb.requested_layout() == ("de", "nodeadkeys")
    monkeypatch.setenv("GLOVEBOX_KEYBOARD", "si,us")
    assert hb.requested_layout() == ("si,us", "")
    monkeypatch.setenv("GLOVEBOX_KEYBOARD", "rm -rf /")   # not a layout name -> ignored
    assert hb.requested_layout() == ("", "")


def test_lock_probe_without_bridge(tmp_path, monkeypatch):
    monkeypatch.setattr(hb, "TMP", str(tmp_path))
    assert hb.running(42) is False
    assert hb.lock_path(42) == os.path.join(str(tmp_path), "bridge_42.lock")
    assert hb.lock_path(":7").endswith("bridge_7.lock")

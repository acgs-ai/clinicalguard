"""Audit log path resolution: default, override, and fail-loud on unwritable.

Constitutional Hash: 608508a9bd224290
"""

from __future__ import annotations

import importlib
import os
import stat
import sys
from pathlib import Path

import pytest

from clinicalguard.agent import DEFAULT_AUDIT_LOG_NAME, resolve_audit_log_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CLINICALGUARD_AUDIT_LOG", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def test_default_uses_xdg_state_home_not_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = resolve_audit_log_path()
    assert path == tmp_path / "clinicalguard" / DEFAULT_AUDIT_LOG_NAME
    assert path.parent.is_dir()
    assert not str(path).startswith("/tmp/clinicalguard_audit")


def test_default_falls_back_to_home_local_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = resolve_audit_log_path()
    assert path == tmp_path / ".local" / "state" / "clinicalguard" / DEFAULT_AUDIT_LOG_NAME
    assert path.parent.is_dir()


def test_env_override_wins_and_creates_directory(tmp_path, monkeypatch):
    target = tmp_path / "data" / "custom_audit.json"
    monkeypatch.setenv("CLINICALGUARD_AUDIT_LOG", str(target))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "ignored"))
    assert resolve_audit_log_path() == target
    assert target.parent.is_dir()
    assert not (tmp_path / "ignored").exists()


def test_unwritable_directory_fails_loudly(tmp_path, monkeypatch):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("CLINICALGUARD_AUDIT_LOG", str(blocker / "sub" / "audit.json"))
    with pytest.raises(RuntimeError, match="not writable"):
        resolve_audit_log_path()


def test_explicit_environ_mapping_is_used(tmp_path):
    target = tmp_path / "a.json"
    assert resolve_audit_log_path({"CLINICALGUARD_AUDIT_LOG": str(target)}) == target


def _import_main():
    sys.modules.pop("clinicalguard.main", None)
    return importlib.import_module("clinicalguard.main")


def test_main_module_is_wired_to_resolved_path(tmp_path, monkeypatch):
    target = tmp_path / "wired" / "audit.json"
    monkeypatch.setenv("CLINICALGUARD_AUDIT_LOG", str(target))
    main = _import_main()
    try:
        assert Path(main._audit_log_path) == target
        assert target.parent.is_dir()
    finally:
        sys.modules.pop("clinicalguard.main", None)


def test_main_module_import_fails_when_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("CLINICALGUARD_AUDIT_LOG", str(blocker / "audit.json"))
    with pytest.raises(RuntimeError, match="not writable"):
        _import_main()
    sys.modules.pop("clinicalguard.main", None)


# --- the configured FILE (not just its directory) must be writable ---------------

_needs_non_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses file permission checks; cannot make a path unwritable"
)


def _use(monkeypatch, path):
    monkeypatch.setenv("CLINICALGUARD_AUDIT_LOG", str(path))


def test_directory_as_audit_path_fails_loudly(tmp_path, monkeypatch):
    _use(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="not writable") as excinfo:
        resolve_audit_log_path()
    assert str(tmp_path) in str(excinfo.value)
    assert "CLINICALGUARD_AUDIT_LOG" in str(excinfo.value)


@_needs_non_root
def test_existing_read_only_file_fails_loudly(tmp_path, monkeypatch):
    log = tmp_path / "audit.json"
    log.write_text("precious")
    log.chmod(0o400)
    _use(monkeypatch, log)
    with pytest.raises(RuntimeError, match="not writable"):
        resolve_audit_log_path()
    assert log.read_text() == "precious"


def test_dangling_symlink_fails_loudly(tmp_path, monkeypatch):
    link = tmp_path / "audit.json"
    link.symlink_to(tmp_path / "missing" / "audit.json")
    _use(monkeypatch, link)
    with pytest.raises(RuntimeError, match="not writable"):
        resolve_audit_log_path()
    assert not (tmp_path / "missing").exists()


@_needs_non_root
def test_symlink_into_unwritable_directory_fails_loudly(tmp_path, monkeypatch):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        link = tmp_path / "audit.json"
        link.symlink_to(locked / "audit.json")
        _use(monkeypatch, link)
        with pytest.raises(RuntimeError, match="not writable"):
            resolve_audit_log_path()
    finally:
        locked.chmod(0o700)


@_needs_non_root
def test_symlink_to_read_only_file_fails_loudly(tmp_path, monkeypatch):
    target = tmp_path / "real.json"
    target.write_text("precious")
    target.chmod(0o400)
    link = tmp_path / "audit.json"
    link.symlink_to(target)
    _use(monkeypatch, link)
    with pytest.raises(RuntimeError, match="not writable"):
        resolve_audit_log_path()


def test_symlink_to_writable_file_is_accepted_unchanged(tmp_path, monkeypatch):
    target = tmp_path / "real.json"
    target.write_text("precious")
    link = tmp_path / "audit.json"
    link.symlink_to(target)
    _use(monkeypatch, link)
    assert resolve_audit_log_path() == link
    assert target.read_text() == "precious"


def test_existing_log_content_and_mode_are_untouched(tmp_path, monkeypatch):
    log = tmp_path / "audit.json"
    log.write_bytes(b'{"entries": [{"id": "HC-1"}]} trailing bytes')
    log.chmod(0o644)
    before = log.stat()
    _use(monkeypatch, log)
    resolve_audit_log_path()
    after = log.stat()
    assert log.read_bytes() == b'{"entries": [{"id": "HC-1"}]} trailing bytes'
    assert stat.S_IMODE(after.st_mode) == 0o644
    assert after.st_mtime_ns == before.st_mtime_ns
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audit.json"]  # no probe file left


def test_no_probe_file_left_when_creating(tmp_path, monkeypatch):
    target = tmp_path / "d" / "audit.json"
    _use(monkeypatch, target)
    resolve_audit_log_path()
    assert sorted(p.name for p in target.parent.iterdir()) == ["audit.json"]


# --- permissions -----------------------------------------------------------------


def test_created_directory_and_file_are_private(tmp_path, monkeypatch):
    target = tmp_path / "new" / "audit.json"
    _use(monkeypatch, target)
    resolve_audit_log_path()
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_existing_directory_mode_is_left_alone(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o755)
    target = data / "audit.json"
    _use(monkeypatch, target)
    resolve_audit_log_path()
    assert stat.S_IMODE(data.stat().st_mode) == 0o755
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_created_empty_log_restores_without_warning(tmp_path, monkeypatch, caplog):
    from clinicalguard.agent import ClinicalGuardApp

    target = tmp_path / "audit.json"
    _use(monkeypatch, target)
    path = resolve_audit_log_path()
    with caplog.at_level("WARNING"):
        guard = ClinicalGuardApp.create(audit_log_path=path)
    assert len(guard.audit_log) == 0
    assert "Could not restore" not in caplog.text


def test_persisted_log_keeps_private_mode(tmp_path, monkeypatch):
    from clinicalguard.agent import ClinicalGuardApp

    target = tmp_path / "audit.json"
    _use(monkeypatch, target)
    guard = ClinicalGuardApp.create(audit_log_path=resolve_audit_log_path())
    guard._persist(guard.audit_log)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert '"entries"' in target.read_text()


# --- path normalisation and home lookup -------------------------------------------


def test_relative_override_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLINICALGUARD_AUDIT_LOG", "rel/audit.json")
    path = resolve_audit_log_path()
    assert path.is_absolute()
    assert path == Path(os.getcwd()) / "rel" / "audit.json"


def test_missing_home_gives_actionable_error(monkeypatch):
    def _no_home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))
    with pytest.raises(RuntimeError, match="CLINICALGUARD_AUDIT_LOG"):
        resolve_audit_log_path()


def test_agent_card_reports_package_version():
    import clinicalguard
    from clinicalguard.agent import _AGENT_CARD

    assert _AGENT_CARD["version"] == clinicalguard.__version__


# --- non-regular files and blank values --------------------------------------------


def test_dev_null_is_rejected(monkeypatch):
    _use(monkeypatch, "/dev/null")
    with pytest.raises(RuntimeError, match="not a regular file") as excinfo:
        resolve_audit_log_path()
    assert "/dev/null" in str(excinfo.value)
    assert "CLINICALGUARD_AUDIT_LOG" in str(excinfo.value)


def test_fifo_is_rejected_without_blocking(tmp_path, monkeypatch):
    # The file type is checked before the path is opened, so no reader is needed
    # and this cannot hang. The test never opens the FIFO itself.
    fifo = tmp_path / "audit.fifo"
    os.mkfifo(fifo)
    _use(monkeypatch, fifo)
    with pytest.raises(RuntimeError, match="not a regular file"):
        resolve_audit_log_path()


def test_symlink_to_device_is_rejected(tmp_path, monkeypatch):
    link = tmp_path / "audit.json"
    link.symlink_to("/dev/null")
    _use(monkeypatch, link)
    with pytest.raises(RuntimeError, match="not a regular file"):
        resolve_audit_log_path()


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t\n"])
def test_blank_variable_falls_back_to_default(tmp_path, monkeypatch, blank):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLINICALGUARD_AUDIT_LOG", blank)
    monkeypatch.chdir(tmp_path)
    path = resolve_audit_log_path()
    assert path == tmp_path / "state" / "clinicalguard" / DEFAULT_AUDIT_LOG_NAME
    assert not any(p.name.strip() == "" for p in tmp_path.iterdir())  # no blank-named file in cwd


def test_surrounding_whitespace_is_stripped(tmp_path, monkeypatch):
    target = tmp_path / "audit.json"
    _use(monkeypatch, f"  {target}  ")
    assert resolve_audit_log_path() == target

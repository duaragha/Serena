"""The ACL check that kept the brain from starting.

Windows rewrites child ACLs when a directory's inheritance changes, and the
brain's state directory holds the Fleet worktrees, so the enforce call outgrew
its ten second budget and every start ended in FATAL. Reading the ACL back is
cheap at any size, so these tests pin the read path and both of its answers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from core import brain_lifetime

PRINCIPAL = "raghavsgamingpc\\raghav"
STATE = Path("C:\\Users\\ragha\\.local\\state\\serena")

PRIVATE = f"""{STATE} {PRINCIPAL}:(OI)(CI)(F)
                                   NT AUTHORITY\\SYSTEM:(OI)(CI)(F)
                                   BUILTIN\\Administrators:(OI)(CI)(F)

Successfully processed 1 files; Failed processing 0 files
"""

INHERITED = f"""{STATE} {PRINCIPAL}:(I)(OI)(CI)(F)
                                   NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)

Successfully processed 1 files; Failed processing 0 files
"""

STRANGER = f"""{STATE} {PRINCIPAL}:(OI)(CI)(F)
                                   RAGHAVSGAMINGPC\\guest:(OI)(CI)(R)

Successfully processed 1 files; Failed processing 0 files
"""


def _icacls(monkeypatch, stdout: str, returncode: int = 0):
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(brain_lifetime.subprocess, "run", fake_run)
    return calls


def test_an_already_private_directory_is_not_rewritten(monkeypatch):
    calls = _icacls(monkeypatch, PRIVATE)
    assert brain_lifetime._windows_acl_is_private(STATE, PRINCIPAL) is True
    # One read, and crucially no /inheritance:r write.
    assert len(calls) == 1
    assert "/inheritance:r" not in calls[0]


def test_an_inherited_entry_means_it_must_be_enforced(monkeypatch):
    _icacls(monkeypatch, INHERITED)
    assert brain_lifetime._windows_acl_is_private(STATE, PRINCIPAL) is False


def test_an_unexpected_principal_means_it_must_be_enforced(monkeypatch):
    _icacls(monkeypatch, STRANGER)
    assert brain_lifetime._windows_acl_is_private(STATE, PRINCIPAL) is False


def test_a_failed_or_empty_read_never_reports_private(monkeypatch):
    _icacls(monkeypatch, "", returncode=5)
    assert brain_lifetime._windows_acl_is_private(STATE, PRINCIPAL) is False
    _icacls(monkeypatch, "Successfully processed 1 files; Failed processing 0 files\n")
    assert brain_lifetime._windows_acl_is_private(STATE, PRINCIPAL) is False


def test_a_read_that_hangs_is_not_taken_as_private(monkeypatch):
    def boom(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 20)

    monkeypatch.setattr(brain_lifetime.subprocess, "run", boom)
    assert brain_lifetime._windows_acl_is_private(STATE, PRINCIPAL) is False


def test_the_enforce_budget_is_far_above_the_old_ten_seconds():
    """Ten seconds is what the Fleet worktrees outgrew."""

    assert brain_lifetime.ACL_WRITE_TIMEOUT_SECONDS >= 120
    assert brain_lifetime.ACL_READ_TIMEOUT_SECONDS <= 30

"""Regression: profile alias scan must not be O(profiles * wrappers)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import profiles as profiles_mod


def test_profile_alias_map_scans_wrappers_once(tmp_path, monkeypatch):
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    prof_root = tmp_path / ".hermes" / "profiles"
    prof_root.mkdir(parents=True)
    (prof_root / "alpha").mkdir()
    (prof_root / "beta").mkdir()
    (wrapper_dir / "alpha").write_text("#!/bin/sh\nhermes -p alpha\n", encoding="utf-8")
    (wrapper_dir / "custom-beta").write_text("#!/bin/sh\nhermes -p beta\n", encoding="utf-8")

    monkeypatch.setattr(profiles_mod, "_get_wrapper_dir", lambda: wrapper_dir)
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: prof_root)
    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: tmp_path / ".hermes")
    profiles_mod.invalidate_profile_alias_cache()

    assert profiles_mod.find_alias_for_profile("alpha") == "alpha"
    assert profiles_mod.find_alias_for_profile("beta") == "custom-beta"

    # Many lookups should stay fast (cached map, not re-scan per profile).
    t0 = time.perf_counter()
    for _ in range(200):
        profiles_mod.find_alias_for_profile("beta")
    assert time.perf_counter() - t0 < 0.5


def test_list_profiles_for_roster_faster_than_full_list(monkeypatch):
    """Roster listing skips gateway probes (smoke; not a timing SLA)."""
    monkeypatch.setattr(profiles_mod, "invalidate_profile_alias_cache", lambda: None)
    roster = profiles_mod.list_profiles_for_roster()
    assert roster
    assert all(not p.gateway_running for p in roster)
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from scripts import acceptance_school_model as acceptance


def test_prepare_database_creates_only_a_new_acceptance_database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    target = tmp_path / "data" / "acceptance" / "run.db"

    assert acceptance.prepare_database(target) == target.resolve()
    with sqlite3.connect(target) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='inventory_items'"
        ).fetchone()[0] == 1


def test_prepare_database_rejects_paths_outside_acceptance_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    target = tmp_path / "data" / "production.db"

    with pytest.raises(ValueError, match="under data/acceptance"):
        acceptance.prepare_database(target)

    assert not target.exists()


def test_prepare_database_never_modifies_an_existing_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    target = tmp_path / "data" / "acceptance" / "existing.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"preserve this file")

    with pytest.raises(ValueError, match="existing files are never modified"):
        acceptance.prepare_database(target)

    assert target.read_bytes() == b"preserve this file"


@pytest.mark.parametrize("name", ["database.sqlite", "database"])
def test_prepare_database_requires_db_extension(tmp_path: Path, monkeypatch, name: str):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    target = tmp_path / "data" / "acceptance" / name

    with pytest.raises(ValueError, match="must use the .db extension"):
        acceptance.prepare_database(target)

    assert not target.exists()

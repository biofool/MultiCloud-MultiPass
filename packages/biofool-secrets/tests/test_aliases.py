"""Alias-registry tests — bundled data + override merge + project scoping."""

from __future__ import annotations

import json

from biofool_secrets import aliases


def test_bundled_registry_loads():
    reg = aliases.registry()
    assert reg["version"] == 1
    assert "BRAVE_SEARCH_API_KEY" in reg["aliases"]
    assert "story_graph" in reg["project_aliases"]
    assert "AIRichardMoon" in reg["project_aliases"]


def test_global_aliases_available_without_project():
    assert "BRAVE_API_KEY" in aliases.aliases_for("BRAVE_SEARCH_API_KEY")


def test_google_api_key_not_globally_aliased():
    # The documented hazard: GOOGLE_API_KEY must not leak into every repo.
    assert aliases.aliases_for("GOOGLE_API_KEY") == []


def test_project_id_alias_stays_wsf_scoped():
    # GCP_PROJECT_ID↔GOOGLE_CLOUD_PROJECT is valid in WSF but a repo that
    # points the two vars at different projects must not cross-resolve.
    assert aliases.aliases_for("GCP_PROJECT_ID") == []
    assert aliases.aliases_for("GCP_PROJECT_ID", project="WorldStudioFinder") == [
        "GOOGLE_CLOUD_PROJECT"
    ]


def test_project_scoping():
    assert aliases.aliases_for("GOOGLE_API_KEY", project="WorldStudioFinder") == [
        "MOVEMENT_ARTS_GOOGLE_API_KEY", "HOBBY_LOBBY_GOOGLE_API_KEY",
    ]
    # story_graph's disambiguation: geocode falls back ONLY to the
    # Cloud-console key, never to the Gemini GOOGLE_API_KEY.
    sg = aliases.aliases_for("GOOGLE_GEOCODE_API_KEY", project="story_graph")
    assert sg == ["MOVEMENT_ARTS_GOOGLE_API_KEY"]


def test_override_file_merges(tmp_path, monkeypatch):
    override = tmp_path / "extra.json"
    override.write_text(json.dumps({
        "aliases": {"MY_KEY": ["MY_KEY_ALT"]},
        "project_aliases": {"custom": {"K": ["K_ALT"]}},
    }))
    monkeypatch.setenv("BIOFOOL_SECRETS_ALIASES_FILE", str(override))
    aliases.reset()
    assert "MY_KEY_ALT" in aliases.aliases_for("MY_KEY")
    assert aliases.aliases_for("K", project="custom") == ["K_ALT"]
    # bundled entries survive the merge
    assert "BRAVE_API_KEY" in aliases.aliases_for("BRAVE_SEARCH_API_KEY")


def test_missing_override_warns_not_silent(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("BIOFOOL_SECRETS_ALIASES_FILE", str(tmp_path / "nope.json"))
    aliases.reset()
    import logging
    with caplog.at_level(logging.WARNING):
        aliases.aliases_for("BRAVE_SEARCH_API_KEY")
    assert "not found" in caplog.text

"""resolve_token tests — fake values only."""

from __future__ import annotations

import pytest

from biofool_secrets import resolve_token, token, token_candidates
from biofool_secrets.cli import main

ALL = ("AIKI_CFT", "AIKI_CFT_dup", "CLOUDFLARE_API_TOKEN", "CFT")


@pytest.fixture(autouse=True)
def env_only(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "env")
    for n in ALL:
        monkeypatch.delenv(n, raising=False)


def test_candidate_order():
    assert token_candidates("AIKI") == list(ALL)


@pytest.mark.parametrize("bad", ["", "aiki", "AI-KI", "1AB", "A B"])
def test_invalid_prefix(bad):
    with pytest.raises(ValueError):
        token_candidates(bad)


def test_none_when_nothing_set():
    assert resolve_token("AIKI") is None


def test_only_cft(monkeypatch):
    monkeypatch.setenv("CFT", "fake-cft")
    assert resolve_token("AIKI") == ("CFT", "fake-cft")


def test_generic_beats_cft(monkeypatch):
    monkeypatch.setenv("CFT", "fake-cft")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-generic")
    assert resolve_token("AIKI") == ("CLOUDFLARE_API_TOKEN", "fake-generic")


def test_dup_beats_generic(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fake-generic")
    monkeypatch.setenv("AIKI_CFT_dup", "fake-dup")
    assert resolve_token("AIKI") == ("AIKI_CFT_dup", "fake-dup")


def test_prefixed_beats_all(monkeypatch):
    for n in ALL:
        monkeypatch.setenv(n, f"fake-{n}")
    assert resolve_token("AIKI") == ("AIKI_CFT", "fake-AIKI_CFT")


def test_uppercase_dup_is_not_accepted(monkeypatch):
    monkeypatch.setenv("AIKI_CFT_DUP", "fake")
    assert resolve_token("AIKI") is None


@pytest.mark.parametrize("blank", ["", "   ", "\r\n", '""', "''"])
def test_blank_prefixed_falls_through(monkeypatch, blank):
    monkeypatch.setenv("AIKI_CFT", blank)
    monkeypatch.setenv("CFT", "fake-cft")
    assert resolve_token("AIKI") == ("CFT", "fake-cft")


def test_value_is_cleaned(monkeypatch):
    monkeypatch.setenv("AIKI_CFT", '  "fake-quoted"\r\n')
    assert resolve_token("AIKI") == ("AIKI_CFT", "fake-quoted")


def test_custom_generic(monkeypatch):
    monkeypatch.setenv("OTHER", "fake-other")
    assert resolve_token("AIKI", generic=("OTHER",)) == ("OTHER", "fake-other")


def test_parse_env_file(tmp_path):
    f = tmp_path / "env"
    f.write_bytes(
        b"# AIKI_CFT=commented\r\n"
        b"export AIKI_CFT='fake-a'\r\n"
        b"AIKI_CFT=\r\n"
        b"AIKI_CFT=fake-later\r\n"
        b"  CFT = \"fake-b\"  \r\n"
        b"EMPTY=\n"
        b"not an assignment\n"
    )
    assert token.parse_env_file(str(f)) == {"AIKI_CFT": "fake-a", "CFT": "fake-b"}


def test_parse_env_file_missing(tmp_path):
    assert token.parse_env_file(str(tmp_path / "nope")) == {}


def test_env_file_used_after_process_env(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("AIKI_CFT=fake-file\nCFT=fake-file-cft\n")
    assert resolve_token("AIKI", env_file=str(f)) == ("AIKI_CFT", "fake-file")
    # process env wins over the file, even for a lower-priority name
    monkeypatch.setenv("CFT", "fake-env-cft")
    assert resolve_token("AIKI", env_file=str(f)) == ("CFT", "fake-env-cft")


def test_two_names_different_values_first_name_wins(monkeypatch):
    monkeypatch.setenv("AIKI_CFT", "fake-one")
    monkeypatch.setenv("AIKI_CFT_dup", "fake-two")
    assert resolve_token("AIKI") == ("AIKI_CFT", "fake-one")


def test_gsm_pass(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "auto")
    seen = []

    def fake_access(name, project=None):
        seen.append(name)
        return " fake-gsm\n" if name == "AIKI_CFT_dup" else None

    monkeypatch.setattr("biofool_secrets.gsm.access", fake_access)
    assert resolve_token("AIKI") == ("AIKI_CFT_dup", "fake-gsm")
    assert seen == ["AIKI_CFT", "AIKI_CFT_dup"]


def test_env_backend_skips_gsm(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GSM must not be called")

    monkeypatch.setattr("biofool_secrets.gsm.access", boom)
    assert resolve_token("AIKI") is None


def test_cli_token(monkeypatch, capsys):
    monkeypatch.setenv("AIKI_CFT_dup", "fake-dup")
    assert main(["token", "AIKI"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "fake-dup"
    assert out.err.strip() == "resolved_from=AIKI_CFT_dup"
    assert main(["token", "AIKI", "--name-only"]) == 0
    assert capsys.readouterr().out.strip() == "AIKI_CFT_dup"


def test_cli_token_errors(capsys):
    assert main(["token", "AIKI"]) == 1
    assert main(["token", "bad"]) == 2

"""CLI tests — resolve/list/env/check subcommands."""

from __future__ import annotations

from biofool_secrets import cli


def test_resolve_mask(monkeypatch, capsys):
    monkeypatch.setenv("MY_KEY", "value123")
    assert cli.main(["resolve", "MY_KEY", "--mask"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == "<set:8>"


def test_resolve_missing_exit_1(capsys):
    assert cli.main(["resolve", "NOPE"]) == 1
    assert "not resolved" in capsys.readouterr().err


def test_env_dry_run_masks_and_flags_missing(monkeypatch, capsys):
    monkeypatch.setenv("HAVE_IT", "abc")
    monkeypatch.setenv("SECRETS_BACKEND", "env")
    rc = cli.main(["env", "HAVE_IT", "GONE", "--dry-run"])
    out = capsys.readouterr()
    assert rc == 1
    assert "HAVE_IT=<set:3>" in out.out
    assert "MISSING" in out.out
    assert "WARNING" in out.err
    # resolved value never printed
    assert "abc" not in out.out


def test_env_writes_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("W1", "secret-value")
    target = tmp_path / "out.env"
    rc = cli.main(["env", "W1", "--output", str(target)])
    assert rc == 0
    assert target.read_text().strip() == "W1=secret-value"


def test_env_keys_file_export_prefix_and_missing(monkeypatch, tmp_path, capsys):
    kf = tmp_path / "keys.env"
    kf.write_text("export GOOD=1\n# comment\n\nBAD=x\n")
    monkeypatch.setenv("GOOD", "v")
    monkeypatch.setenv("SECRETS_BACKEND", "env")
    rc = cli.main(["env", "--keys-file", str(kf), "--dry-run"])
    out = capsys.readouterr()
    assert rc == 1  # BAD unresolved
    assert "GOOD=<set:1>" in out.out
    assert "export" not in out.out


def test_env_keys_file_not_found_exit_2(capsys):
    assert cli.main(["env", "--keys-file", "/nonexistent/x"]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_check_report_names_only(monkeypatch, capsys):
    monkeypatch.setenv("BRAVE_API_KEY", "topsecret")
    assert cli.main(["check", "BRAVE_SEARCH_API_KEY"]) == 0
    out = capsys.readouterr().out
    assert "env:BRAVE_API_KEY" in out
    assert "topsecret" not in out

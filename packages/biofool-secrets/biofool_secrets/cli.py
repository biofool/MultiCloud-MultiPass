"""``python -m biofool_secrets`` — thin CLI over the resolver.

Subcommands:

  resolve NAME   Resolve a secret; prints the VALUE to stdout so scripts
                 can do ``TOKEN=$(biofool-secrets resolve X)``. Source is
                 reported on stderr (``--show-source``). ``--mask`` prints
                 only ``<set:N>`` instead of the value.
  list           List secret NAMES in the configured project (never values).
  env KEY...     bootstrap_env replacement: resolve each key and emit
                 ``KEY=value`` lines — to stdout, or ``--output FILE``.
                 ``--dry-run`` prints ``KEY=<set:N>``/``KEY=<missing>``.
  check NAME     Names-only resolution report (candidates, source, no value).
  token PREFIX   Resolve <PREFIX>_CFT -> <PREFIX>_CFT_dup -> CLOUDFLARE_API_TOKEN
                 -> CFT. Prints the VALUE; the winning NAME goes to stderr.
                 ``--name-only`` prints just the name.
"""

from __future__ import annotations

import argparse
import json
import sys

from biofool_secrets import resolver, token


def _cmd_resolve(args: argparse.Namespace) -> int:
    value = resolver.resolve_secret(
        args.name, project=args.project, secret_project=args.secret_project,
    )
    if not value:
        print(f"{args.name}: not resolved", file=sys.stderr)
        return 1
    if args.show_source:
        report = resolver.resolution_report(
            args.name, project=args.project, secret_project=args.secret_project,
        )
        print(f"resolved_from={report['resolved_from']}", file=sys.stderr)
    print(f"<set:{len(value)}>" if args.mask else value)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    for name in resolver.list_secret_names(args.secret_project):
        print(name)
    return 0


def _cmd_env(args: argparse.Namespace) -> int:
    keys = list(args.keys)
    if args.keys_file:
        try:
            with open(args.keys_file) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[len("export "):].lstrip()
                    keys.append(line.split("=", 1)[0].strip())
        except OSError as exc:
            print(f"ERROR: cannot read keys file {args.keys_file}: {exc}", file=sys.stderr)
            return 2
    lines: list[str] = []
    missing: list[str] = []
    for key in keys:
        value = resolver.resolve_secret(
            key, project=args.project, secret_project=args.secret_project,
        )
        if value:
            lines.append(f"{key}=<set:{len(value)}>" if args.dry_run else f"{key}={value}")
        else:
            missing.append(key)
            lines.append(f"# {key}=  # MISSING — did not resolve")
    for key in missing:
        print(f"WARNING: {key} did not resolve", file=sys.stderr)
    output = "\n".join(lines) + "\n"
    if args.output and not args.dry_run:
        with open(args.output, "w") as fh:
            fh.write(output)
        print(f"wrote {len(keys)} keys to {args.output}", file=sys.stderr)
    else:
        print(output, end="")
    return 1 if missing else 0


def _cmd_check(args: argparse.Namespace) -> int:
    report = resolver.resolution_report(
        args.name, project=args.project, secret_project=args.secret_project,
    )
    print(json.dumps(report, indent=2))
    return 0


def _cmd_token(args: argparse.Namespace) -> int:
    try:
        found = token.resolve_token(
            args.prefix, env_file=args.env_file, secret_project=args.secret_project,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not found:
        print(f"{args.prefix}: no token resolved", file=sys.stderr)
        return 1
    name, value = found
    print(f"resolved_from={name}", file=sys.stderr)
    print(name if args.name_only else value)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="biofool-secrets",
        description="Canonical biofool secret resolution (env → GSM → Proton).",
    )
    p.add_argument("--project", help="alias-registry project scope")
    p.add_argument("--secret-project", help="GCP project id for GSM lookups")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("resolve", help="resolve one secret (prints the value)")
    r.add_argument("name")
    r.add_argument("--mask", action="store_true", help="print <set:N> instead of the value")
    r.add_argument("--show-source", action="store_true", help="report source on stderr")
    r.set_defaults(func=_cmd_resolve)

    li = sub.add_parser("list", help="list secret names (never values)")
    li.set_defaults(func=_cmd_list)

    e = sub.add_parser("env", help="resolve keys and emit KEY=value lines")
    e.add_argument("keys", nargs="*")
    e.add_argument("--keys-file", help="file of key names (or KEY=… lines)")
    e.add_argument("--output", help="write to file instead of stdout")
    e.add_argument("--dry-run", action="store_true", help="mask values, write nothing")
    e.set_defaults(func=_cmd_env)

    c = sub.add_parser("check", help="names-only resolution report")
    c.add_argument("name")
    c.set_defaults(func=_cmd_check)

    t = sub.add_parser("token", help="resolve a prefixed Cloudflare token")
    t.add_argument("prefix", help="project prefix, e.g. AIKI")
    t.add_argument("--env-file", help="also read NAME=value lines from this file")
    t.add_argument("--name-only", action="store_true", help="print the winning name, not the value")
    t.set_defaults(func=_cmd_token)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

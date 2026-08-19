#!/usr/bin/env bash
#
# Wrapper: lives one directory above the scraper-intl repo.
# First argument is the Terraform directory (relative to repo root or absolute).
# Remaining arguments are passed through to scraper-intl/deploy_quantum_aikido_terraform.sh
#
# Example (Americas-ish UTC offsets via TZ geo-filter):
#   ./deploy_quantum_aikido_terraform.sh terraform-test --geo-filter='TZ:{UTC-8,UTC-7,UTC-6,UTC-5,UTC-4,UTC-9}' --extra-args=--only-new
#
# Override repo location:
#   QUANTUM_AIKIDO_REPO=/path/to/scraper-intl ./deploy_quantum_aikido_terraform.sh terraform-test --quick
#

set -euo pipefail

WRAPPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${QUANTUM_AIKIDO_REPO:-$WRAPPER_DIR/scraper-intl}"
INNER="$REPO/deploy_quantum_aikido_terraform.sh"

if [[ ! -f "$INNER" ]]; then
    echo "Error: inner deploy script not found: $INNER"
    echo "Set QUANTUM_AIKIDO_REPO to your scraper-intl checkout."
    exit 1
fi

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <terraform-dir> [options for inner deploy script...]"
    echo ""
    echo "  <terraform-dir>  Path relative to repo root (e.g. terraform-test, terraform)"
    echo "                     or an absolute path to the Terraform working directory."
    echo ""
    echo "Example:"
    echo "  $0 terraform-test --geo-filter='TZ:{UTC-8,UTC-7,UTC-6,UTC-5,UTC-4,UTC-9}' --extra-args=--only-new"
    exit 1
fi

TFD="$1"
shift

if [[ "$TFD" = /* ]]; then
    TF_ABS="$(cd "$TFD" && pwd)"
else
    TF_ABS="$(cd "$REPO/$TFD" && pwd)"
fi

if ! compgen -G "$TF_ABS/*.tf" > /dev/null; then
    echo "Error: expected at least one *.tf file under: $TF_ABS"
    exit 1
fi

exec bash "$INNER" --terraform-dir="$TF_ABS" "$@"

#!/usr/bin/env bash
# Secret scan over the whole git history with gitleaks and .gitleaks.toml. CI runs
# it on every push; run it locally before pushing. Without gitleaks it skips locally and
# fails in CI (CI set), so a missing scanner can't pass for a clean scan.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v gitleaks >/dev/null 2>&1; then
	if [ -n "${CI:-}" ]; then
		echo "gitleaks is not installed" >&2
		exit 1
	fi
	echo "gitleaks not installed: secret scan skipped (brew install gitleaks)"
	exit 0
fi

if gitleaks git --help >/dev/null 2>&1; then # 8.19 and later
	exec gitleaks git --config .gitleaks.toml --redact --no-banner .
fi
exec gitleaks detect --config .gitleaks.toml --redact --no-banner --source .

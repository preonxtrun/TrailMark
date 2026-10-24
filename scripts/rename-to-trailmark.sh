#!/usr/bin/env bash
# One-shot rename: codegraph → trailmark (package, CLI, markers, state dir, crates).
# Run from the repo root AFTER any running campaign/agent has finished.
set -euo pipefail

OLD=codegraph
NEW=trailmark

echo "==> git mv src/$OLD src/$NEW"
git mv "src/$OLD" "src/$NEW"

echo "==> rewriting imports, strings, markers, state dir"
# source + tests + bench + docs + config; word-ish boundaries to keep other words intact
grep -rl "$OLD" src bench tests docs README.md SPEC.md pyproject.toml crates tools \
  --include="*.py" --include="*.rs" --include="*.toml" --include="*.md" --include="*.sh" \
  | while read -r f; do
      sed -i "s/${OLD}/${NEW}/g; s/${OLD^}/${NEW^}/g; s/${OLD^^}/${NEW^^}/g" "$f"
    done

echo "==> crate dirs"
git mv crates/core crates/"$NEW"-core 2>/dev/null || mv crates/core crates/"$NEW"-core
git mv crates/cli crates/"$NEW"-cli 2>/dev/null || mv crates/cli crates/"$NEW"-cli
sed -i "s/name = \"${OLD}-core\"/name = \"${NEW}-core\"/; s/name = \"${OLD}-cli\"/name = \"${NEW}-cli\"/" \
  crates/"$NEW"-core/Cargo.toml crates/"$NEW"-cli/Cargo.toml
sed -i "s/${OLD}-core/${NEW}-core/g; s/${OLD}-cli/${NEW}-cli/g; s/${OLD}_core/${NEW}_core/g; s/${OLD}_cli/${NEW}_cli/g" \
  crates/"$NEW"-cli/Cargo.toml crates/"$NEW"-cli/src/*.rs crates/"$NEW"-cli/src/**/*.rs 2>/dev/null || true

echo "==> reinstall + verify"
export PATH="$HOME/.local/bin:$PATH"
uv sync --extra dev --extra bench
uv run pytest -q
uv run ruff check src bench tests
export PATH="$HOME/.cargo/bin:$PATH"
cargo build --release
cargo test --workspace

echo "==> rename complete: $NEW (CLI: $NEW, state dir: .$NEW/, markers: <!-- $NEW:start v1 -->)"

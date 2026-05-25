#!/usr/bin/env bash
# Bundle the Tier-1 Modulatio docs into a single DOCX + PDF for GitHub-side distribution
# (until the Modulatio docs site is hosted).
#
# Output:
#   docs/dist/Modulatio-Docs-docx
#   docs/dist/Modulatio-Docs-pdf       (if weasyprint is available)
#
# Requires: the project venv activated (provides bundled pandoc via pypandoc).
# Optional: weasyprint (`uv pip install weasyprint`) for PDF output.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCS_DIR="$REPO_ROOT/docs"
DIST_DIR="$DOCS_DIR/dist"
VERSION="${MODULATIO_DOCS_VERSION:-v0.1.0}"
TIMESTAMP="$(date '+%Y-%m-%d')"

# Locate bundled pandoc via pypandoc (no system pandoc needed).
PANDOC="$(python -c 'import pypandoc; print(pypandoc.get_pandoc_path())' 2>/dev/null)"
if [[ -z "${PANDOC}" || ! -x "${PANDOC}" ]]; then
  echo "ERROR: pandoc not found. Activate the venv (source .venv/bin/activate) and ensure pypandoc is installed." >&2
  exit 1
fi
echo "Using pandoc: ${PANDOC}"

mkdir -p "$DIST_DIR"

# Section order — matches mkdocs.yml nav.
SECTIONS=(
  "$DOCS_DIR/index.md"
  "$DOCS_DIR/getting-started/install.md"
  "$DOCS_DIR/getting-started/quickstart.md"
  "$DOCS_DIR/getting-started/wizard.md"
  "$DOCS_DIR/concepts.md"
  "$DOCS_DIR/providers.md"
  "$DOCS_DIR/agents.md"
  "$DOCS_DIR/plan-lifecycle.md"
  "$DOCS_DIR/reference/cli.md"
  "$DOCS_DIR/troubleshooting.md"
)

# Build a temp combined markdown with a title page + each section demoted.
# Each section is wrapped in a horizontal rule + page break so DOCX/PDF
# render section boundaries cleanly.
TMP_MD="$(mktemp /tmp/modulatio-docs-XXXXXX.md)"
trap 'rm -f "$TMP_MD"' EXIT

cat > "$TMP_MD" <<EOF
---
title: "Modulatio Documentation"
subtitle: "${VERSION}"
date: "${TIMESTAMP}"
toc: true
toc-depth: 3
---

# Modulatio Documentation

**Version: ${VERSION}**
**Built: ${TIMESTAMP}**

A multi-model agent framework for running long, high-stakes projects with real quality control.

This document bundles the Tier-1 user documentation into one file. For the navigable
web version (when hosted), see the project README for the docs URL.

\\newpage

EOF

for SECTION in "${SECTIONS[@]}"; do
  if [[ ! -f "$SECTION" ]]; then
    echo "WARNING: missing section: $SECTION" >&2
    continue
  fi
  # shift-heading-level-by isn't applied via concatenation so we manually add an extra '#'
  # to every line that starts with one or more '#' (markdown headings) before joining.
  # This is conservative — only matches lines that begin with '#' followed by space or '#'.
  awk '
    /^#+ / { print "#" $0; next }
    { print }
  ' "$SECTION" >> "$TMP_MD"
  echo "" >> "$TMP_MD"
  echo "\\newpage" >> "$TMP_MD"
  echo "" >> "$TMP_MD"
done

# DOCX build — always works.
DOCX_OUT="$DIST_DIR/Modulatio-Docs-${VERSION}.docx"
echo "Building DOCX -> $DOCX_OUT"
"$PANDOC" "$TMP_MD" \
  --from=markdown+pipe_tables+yaml_metadata_block \
  --to=docx \
  --output="$DOCX_OUT" \
  --toc \
  --toc-depth=3 \
  --standalone

# PDF build — best-effort via weasyprint.
PDF_OUT="$DIST_DIR/Modulatio-Docs-${VERSION}.pdf"
if python -c "import weasyprint" 2>/dev/null; then
  echo "Building PDF  -> $PDF_OUT  (via weasyprint)"
  "$PANDOC" "$TMP_MD" \
    --from=markdown+pipe_tables+yaml_metadata_block \
    --to=pdf \
    --pdf-engine=weasyprint \
    --output="$PDF_OUT" \
    --toc \
    --toc-depth=3 \
    --standalone
else
  echo "Skipping PDF: weasyprint not installed."
  echo "  To enable: uv pip install weasyprint"
fi

echo ""
echo "Done. Artifacts:"
ls -la "$DIST_DIR"

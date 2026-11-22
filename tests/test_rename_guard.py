"""The rename is only complete if retired identifiers cannot creep back in."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RETIRED = re.compile(
    r"\bSEC_A\b|\bSEC_B\b|\bcam_main\b|\bcam_sec_a\b|\bcam_sec_b\b|"
    r"\bSECONDARY_A\b|\bSECONDARY_B\b|(?<![-a-z])approach-main(?![-a-z])|approach-sec-a|approach-sec-b"
)
MAIN_AS_APPROACH = re.compile(
    r'"approach"\s*:\s*"MAIN"|approach:\s*MAIN\b|approach="MAIN"|'
    r"_camera\([^\n]*\"MAIN\""
)
ALLOW = {
    "docs/MIGRATION_RENAME.md",  # the migration record itself
    "docs/FOLLOWUP_AUDIT.md",  # historical pre-rename audit
    "app/config.py",  # migration error text and retired-name set
    "tests/test_config.py",  # deliberately exercises the rejection path
    "tests/test_rename_guard.py",
}
SKIP = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "logs",
    "docs/ui",
}
SUFFIXES = {".py", ".md", ".yaml", ".yml", ".txt", ".ini"}


def test_no_retired_identifiers_outside_the_allowlist():
    hits: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        if any(part in SKIP for part in path.parts):
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOW:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            if RETIRED.search(line) or MAIN_AS_APPROACH.search(line):
                hits.append(f"{relative}:{number}: {line.strip()}")
    assert not hits, "retired identifiers found:\n" + "\n".join(hits)


def test_example_config_uses_only_the_new_layout():
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert not RETIRED.search(text)
    assert not MAIN_AS_APPROACH.search(text)
    assert "approaches: [MAIN_A, MAIN_B]" in text
    assert "approaches: [SEC]" in text

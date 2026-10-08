"""Audit this repository against the Homework 5 required tree.

    python audit_repo.py

Checks that every required file exists, that .gitignore protects .env while still
allowing both databases through, that .env.example carries placeholders rather
than a real key, and that no secret is about to be committed.

Exits non-zero if anything fails.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PASS: list[str] = []
FAIL: list[str] = []
WARN: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  <- {detail}" if detail else ""))
    return ok


def warn(label: str, detail: str = "") -> None:
    WARN.append(label)
    print(f"  [WARN] {label}" + (f"  <- {detail}" if detail else ""))


REQUIRED = [
    "AI_prompts.md",
    "requirements.txt",
    ".env.example",
    ".gitignore",
    ".mcp.json",
    "README.md",
    "data/campus_customs.db",
    "data/campus_customs_new.db",
    "mcp_server/server.py",
    "mcp_server/README.md",
    "backend/main.py",
    "backend/models.py",
    "backend/prompts/boss.md",
    "backend/prompts/inventory.md",
    "backend/prompts/accounting.md",
    "backend/prompts/facilities.md",
    "backend/prompts/customer_service.md",
    "output/harness.md",
    "output/mcp_smoke.json",
    "output/desk_tickets.html",
    "output/design.md",
    "output/resolved_tickets.json",
    "output/resolved_board.html",
    "output/audit_trail.json",
    "output/github_url.txt",
]


def main() -> int:
    print(f"Auditing {ROOT.name}/ against the Homework 5 required tree\n")

    print("1. Required files")
    for rel in REQUIRED:
        path = ROOT / rel
        size = f"{path.stat().st_size:,} bytes" if path.is_file() else "MISSING"
        check(rel, path.is_file(), size)

    print("\n2. frontend/ present and buildable")
    check("frontend/package.json", (ROOT / "frontend/package.json").is_file())
    check("frontend/src/App.tsx", (ROOT / "frontend/src/App.tsx").is_file())
    check("frontend/vite.config.ts", (ROOT / "frontend/vite.config.ts").is_file())
    check(
        "frontend/node_modules not committed",
        not (ROOT / "frontend/node_modules").exists(),
        "rebuilt with npm install",
    )

    print("\n3. Secrets")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    check("'.env' is gitignored", re.search(r"^\.env\s*$", gitignore, re.M) is not None)
    check(
        "'.env.example' is explicitly un-ignored",
        "!.env.example" in gitignore,
    )
    check("no real .env in the repo", not (ROOT / ".env").exists())

    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    check(
        "PORTKEY_API_KEY is a placeholder",
        "PORTKEY_API_KEY=your_key_here" in example,
        next((l for l in example.splitlines() if l.startswith("PORTKEY_API_KEY")), "?"),
    )
    # A Portkey key is a long opaque token; a placeholder is not.
    leaked = [
        line
        for line in example.splitlines()
        if "=" in line
        and not line.strip().startswith("#")
        and len(line.split("=", 1)[1].strip()) > 40
    ]
    check("no long opaque value looks like a real key", not leaked, str(leaked))

    print("\n4. Databases ship with the repo")
    for rel in ("data/campus_customs.db", "data/campus_customs_new.db"):
        path = ROOT / rel
        if not path.is_file():
            check(f"{rel} readable", False, "missing")
            continue
        conn = sqlite3.connect(path)
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ))
        conn.close()
        check(f"{rel} has all 9 tables", len(tables) == 9, f"{len(tables)} tables")

    seed = sqlite3.connect(ROOT / "data/campus_customs.db")
    work = sqlite3.connect(ROOT / "data/campus_customs_new.db")
    seed_balance = seed.execute("SELECT balance FROM cash_accounts").fetchone()[0]
    work_balance = work.execute("SELECT balance FROM cash_accounts").fetchone()[0]
    work_payments = work.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    seed.close()
    work.close()
    check("seed opens at 3400.00", seed_balance == 3400.0, str(seed_balance))
    check(
        "working copy holds the completed run (160.00, 2 payments)",
        work_balance == 160.0 and work_payments == 2,
        f"balance {work_balance}, {work_payments} payments",
    )

    # Would git actually include them?
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "data/campus_customs.db", "data/campus_customs_new.db"],
            cwd=ROOT, capture_output=True, text=True,
        ).stdout.strip()
        check("neither database is gitignored", ignored == "", ignored or "both tracked")
    except FileNotFoundError:
        warn("git not available; could not confirm ignore rules")

    print("\n5. Content sanity")
    prompts = list((ROOT / "backend/prompts").glob("*.md"))
    check("exactly 5 agent prompt files", len(prompts) == 5,
          ", ".join(sorted(p.name for p in prompts)))

    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    check("campus-customs-ops registered in .mcp.json",
          "campus-customs-ops" in mcp.get("mcpServers", {}))

    trail = json.loads((ROOT / "output/audit_trail.json").read_text(encoding="utf-8"))
    check("audit_trail.json is a valid JSON array", isinstance(trail, list), f"{len(trail)} events")

    resolved = json.loads((ROOT / "output/resolved_tickets.json").read_text(encoding="utf-8"))
    ids = [t["ticket_id"] for t in resolved.get("tickets", [])]
    check("resolved_tickets.json covers 101/102/103", sorted(ids) == [101, 102, 103], str(ids))
    check("ending balance recorded as 160.0",
          resolved["cash_reconciliation"]["ending_balance"] == 160.0)

    url_file = (ROOT / "output/github_url.txt").read_text(encoding="utf-8").strip()
    first = url_file.splitlines()[0].strip()
    check("github_url.txt starts with a URL", first.startswith("https://github.com/"), first)
    if "PLACEHOLDER" in url_file:
        warn("github_url.txt is still a placeholder", "replace after pushing")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle, label in [
        ("cp data/campus_customs.db data/campus_customs_new.db", "README documents the DB reset"),
        ("mcp_server/server.py", "README documents launching the MCP server"),
        ("uvicorn main:app --reload --port 8000", "README documents the uvicorn command"),
        ("npm run dev", "README documents the frontend"),
        ("Run the three tickets", "README documents running the three tickets"),
    ]:
        check(label, needle in readme)

    print(f"\n{'=' * 64}\n{len(PASS)} passed, {len(FAIL)} failed, {len(WARN)} warnings")
    if FAIL:
        print("FAILED: " + "; ".join(FAIL))
    if WARN:
        print("WARNINGS: " + "; ".join(WARN))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

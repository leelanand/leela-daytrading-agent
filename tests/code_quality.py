"""
Code Quality Scanner — Alpaca Agent
Runs pylint, flake8, and radon (complexity) across all source files.
Outputs tests/reports/code_quality.json. Exits 0=pass, 1=below threshold.

Run: python tests/code_quality.py
"""
import sys, json, subprocess
from pathlib import Path
from datetime import datetime

AGENT_DIR = Path(r"C:\Users\leela\leela-daytrading-agent")
REPORTS   = AGENT_DIR / "tests" / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)
PYTHON    = sys.executable

# Source files to scan (exclude tests/, migrations, generated)
SOURCE_FILES = [
    f for f in AGENT_DIR.glob("*.py")
    if f.name not in ("setup.py",) and not f.name.startswith("_")
]
SOURCE_PATHS = [str(f) for f in SOURCE_FILES]

PYLINT_PASS_SCORE = 7.0   # out of 10 — flag if below this
MAX_COMPLEXITY    = 15     # cyclomatic complexity threshold

results: dict = {
    "agent": "alpaca",
    "timestamp": datetime.now().isoformat(),
    "files_scanned": len(SOURCE_FILES),
    "pylint": {},
    "flake8": {},
    "radon": {},
    "summary": {},
}


def _run(args: list[str], cwd: Path = AGENT_DIR) -> tuple[str, str, int]:
    r = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=120)
    return r.stdout, r.stderr, r.returncode


print(f"\n{'='*60}")
print(f"  Code Quality Scanner — Alpaca Agent ({len(SOURCE_FILES)} files)")
print(f"{'='*60}")

# ── pylint ────────────────────────────────────────────────────────────────────
print("\n[1] pylint")
try:
    stdout, stderr, rc = _run(
        [PYTHON, "-m", "pylint", "--output-format=json",
         "--disable=C0114,C0115,C0116,W0511,R0903,W0212,W0621",
         "--max-line-length=120"] + SOURCE_PATHS
    )
    # Also get the score line
    score_stdout, _, _ = _run(
        [PYTHON, "-m", "pylint",
         "--disable=C0114,C0115,C0116,W0511,R0903,W0212,W0621",
         "--max-line-length=120"] + SOURCE_PATHS
    )
    score = 0.0
    for line in score_stdout.splitlines():
        if "Your code has been rated at" in line:
            try:
                score = float(line.split("at")[1].split("/")[0].strip())
            except Exception:
                pass

    try:
        issues = json.loads(stdout) if stdout.strip() else []
    except Exception:
        issues = []

    by_type = {}
    for issue in issues:
        t = issue.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1

    results["pylint"] = {
        "score": round(score, 2),
        "pass": score >= PYLINT_PASS_SCORE,
        "threshold": PYLINT_PASS_SCORE,
        "issue_count": len(issues),
        "by_type": by_type,
        "top_issues": [
            {"file": i.get("path",""), "line": i.get("line",0),
             "msg": i.get("message",""), "id": i.get("message-id","")}
            for i in issues[:10]
        ],
    }
    status = "PASS" if score >= PYLINT_PASS_SCORE else "WARN"
    print(f"  Score: {score:.1f}/10  [{status}]  issues={len(issues)}  breakdown={by_type}")
except Exception as e:
    results["pylint"] = {"error": str(e)}
    print(f"  ERROR: {e}")

# ── flake8 ────────────────────────────────────────────────────────────────────
print("\n[2] flake8")
try:
    stdout, stderr, rc = _run(
        [PYTHON, "-m", "flake8",
         "--max-line-length=120",
         "--extend-ignore=E501,W503,E203,W291,W293,E302,E303",
         "--format=%(path)s:%(row)d:%(col)d: %(code)s %(text)s"]
        + SOURCE_PATHS
    )
    lines  = [l for l in stdout.splitlines() if l.strip()]
    errors = [l for l in lines if ": E" in l]
    warns  = [l for l in lines if ": W" in l]
    results["flake8"] = {
        "total_issues": len(lines),
        "errors": len(errors),
        "warnings": len(warns),
        "pass": len(errors) == 0,
        "top_issues": lines[:10],
    }
    status = "PASS" if len(errors) == 0 else "WARN"
    print(f"  [{status}]  total={len(lines)}  errors={len(errors)}  warnings={len(warns)}")
except Exception as e:
    results["flake8"] = {"error": str(e)}
    print(f"  ERROR: {e}")

# ── radon (cyclomatic complexity) ─────────────────────────────────────────────
print("\n[3] radon — cyclomatic complexity")
try:
    stdout, stderr, rc = _run(
        [PYTHON, "-m", "radon", "cc", "--json", "--min=B"] + SOURCE_PATHS
    )
    try:
        radon_data = json.loads(stdout) if stdout.strip() else {}
    except Exception:
        radon_data = {}

    complex_fns = []
    max_found   = 0
    for filepath, fns in radon_data.items():
        for fn in fns:
            c = fn.get("complexity", 0)
            max_found = max(max_found, c)
            if c >= MAX_COMPLEXITY:
                complex_fns.append({
                    "file": Path(filepath).name,
                    "name": fn.get("name"),
                    "complexity": c,
                    "rank": fn.get("rank"),
                })

    results["radon"] = {
        "max_complexity": max_found,
        "threshold": MAX_COMPLEXITY,
        "pass": max_found < MAX_COMPLEXITY,
        "high_complexity_count": len(complex_fns),
        "high_complexity_fns": complex_fns[:10],
    }
    status = "PASS" if max_found < MAX_COMPLEXITY else "WARN"
    print(f"  [{status}]  max_complexity={max_found}  functions_above_{MAX_COMPLEXITY}={len(complex_fns)}")
    for fn in complex_fns[:5]:
        print(f"    {fn['file']}:{fn['name']}  complexity={fn['complexity']} ({fn['rank']})")
except Exception as e:
    results["radon"] = {"error": str(e)}
    print(f"  ERROR: {e}")

# ── Summary ───────────────────────────────────────────────────────────────────
pylint_ok  = results["pylint"].get("pass", False)
flake8_ok  = results["flake8"].get("pass", False)
radon_ok   = results["radon"].get("pass", False)
overall_ok = pylint_ok and flake8_ok and radon_ok

results["summary"] = {
    "pylint_pass":  pylint_ok,
    "flake8_pass":  flake8_ok,
    "radon_pass":   radon_ok,
    "overall_pass": overall_ok,
    "overall":      "PASS" if overall_ok else "WARN",
    "pylint_score": results["pylint"].get("score", 0),
}

print(f"\n{'='*60}")
print(f"  pylint  : {'PASS' if pylint_ok else 'WARN'}  ({results['pylint'].get('score',0):.1f}/10)")
print(f"  flake8  : {'PASS' if flake8_ok else 'WARN'}  ({results['flake8'].get('errors',0)} errors)")
print(f"  radon   : {'PASS' if radon_ok  else 'WARN'}  (max complexity={results['radon'].get('max_complexity',0)})")
print(f"  OVERALL : {'PASS' if overall_ok else 'WARN'}")
print(f"{'='*60}")

(REPORTS / "code_quality.json").write_text(json.dumps(results, indent=2))
print(f"Report: {REPORTS / 'code_quality.json'}")
sys.exit(0 if overall_ok else 1)

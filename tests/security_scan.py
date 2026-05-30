"""
Security Scanner + SCA — Alpaca Agent
Runs bandit (SAST) and pip-audit (SCA/CVE) across the codebase.
Outputs tests/reports/security_scan.json. Exits 0=clean, 1=issues found.

Run: python tests/security_scan.py
"""
import sys, json, subprocess
from pathlib import Path
from datetime import datetime

AGENT_DIR = Path(r"C:\Users\leela\leela-daytrading-agent")
REPORTS   = AGENT_DIR / "tests" / "reports"
REPORTS.mkdir(parents=True, exist_ok=True)
PYTHON    = sys.executable

SOURCE_FILES = [str(f) for f in AGENT_DIR.glob("*.py")
                if not f.name.startswith("_")]

# Severity levels that cause a FAIL (LOW is warning only)
FAIL_ON_SEVERITY = {"MEDIUM", "HIGH"}

results: dict = {
    "agent": "alpaca",
    "timestamp": datetime.now().isoformat(),
    "bandit": {},
    "pip_audit": {},
    "summary": {},
}


def _run(args: list[str], cwd: Path = AGENT_DIR) -> tuple[str, str, int]:
    r = subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=180)
    return r.stdout, r.stderr, r.returncode


print(f"\n{'='*60}")
print(f"  Security Scanner — Alpaca Agent")
print(f"{'='*60}")

# ── bandit (SAST) ─────────────────────────────────────────────────────────────
print("\n[1] bandit — static application security testing")
try:
    stdout, stderr, rc = _run(
        [PYTHON, "-m", "bandit", "--format", "json",
         "--skip", "B101,B603,B607",   # assert, subprocess (needed for trading), partial-path
         "-r"] + SOURCE_FILES
    )
    try:
        report = json.loads(stdout) if stdout.strip() else {}
    except Exception:
        report = {}

    issues   = report.get("results", [])
    metrics  = report.get("metrics", {})
    by_sev   = {}
    for iss in issues:
        sev = iss.get("issue_severity", "LOW")
        by_sev[sev] = by_sev.get(sev, 0) + 1

    blocking = [i for i in issues if i.get("issue_severity") in FAIL_ON_SEVERITY]

    results["bandit"] = {
        "total_issues": len(issues),
        "by_severity": by_sev,
        "blocking_count": len(blocking),
        "pass": len(blocking) == 0,
        "blocking_issues": [
            {
                "file": Path(i.get("filename","")).name,
                "line": i.get("line_number",0),
                "severity": i.get("issue_severity"),
                "confidence": i.get("issue_confidence"),
                "test_id": i.get("test_id"),
                "issue": i.get("issue_text",""),
            }
            for i in blocking[:15]
        ],
        "low_severity_count": by_sev.get("LOW", 0),
    }
    status = "PASS" if len(blocking) == 0 else "FAIL"
    print(f"  [{status}]  total={len(issues)}  by_severity={by_sev}  blocking={len(blocking)}")
    for b in blocking[:5]:
        sev   = b.get("issue_severity", "?")
        fname = Path(b.get("filename", "?")).name
        lineno = b.get("line_number", 0)
        text  = b.get("issue_text", "")[:80]
        print(f"    [{sev}] {fname}:{lineno} — {text}")
except Exception as e:
    results["bandit"] = {"error": str(e), "pass": False}
    print(f"  ERROR: {e}")

# ── pip-audit (SCA / CVE) ─────────────────────────────────────────────────────
print("\n[2] pip-audit — software composition analysis (CVEs)")
try:
    stdout, stderr, rc = _run(
        [PYTHON, "-m", "pip_audit", "--format", "json", "--skip-editable"]
    )
    try:
        audit_data = json.loads(stdout) if stdout.strip() else {}
    except Exception:
        audit_data = {}

    # pip-audit JSON schemas vary by version — handle all known shapes
    if isinstance(audit_data, list):
        vulns = audit_data
    elif isinstance(audit_data, dict):
        vulns = (audit_data.get("vulnerabilities")
                 or audit_data.get("dependencies")
                 or [])
    else:
        vulns = []
    # Only keep entries that actually have CVEs reported
    vuln_entries = [v for v in vulns
                    if isinstance(v, dict) and v.get("vulns")]
    # pip-audit vuln objects: {"id": "...", "fix_versions": [...], "aliases": [...]}
    # They don't carry severity — treat any reported CVE as blocking
    high_vulns = vuln_entries

    results["pip_audit"] = {
        "total_vulnerabilities": len(vuln_entries),
        "high_severity_count":   len(high_vulns),
        "pass": len(high_vulns) == 0,
        "vulnerabilities": [
            {
                "package": v.get("name") or v.get("package"),
                "version": v.get("version") or v.get("installed_version"),
                "vuln_ids": [a.get("id") if isinstance(a, dict) else str(a)
                             for a in (v.get("vulns") or [])],
            }
            for v in vuln_entries[:10]
        ],
    }
    status = "PASS" if len(high_vulns) == 0 else "FAIL"
    print(f"  [{status}]  total_CVEs={len(vuln_entries)}  blocking={len(high_vulns)}")
    for v in vuln_entries[:5]:
        pkg = v.get("name") or v.get("package", "?")
        ver = v.get("version") or v.get("installed_version", "?")
        ids = [a.get("id") if isinstance(a, dict) else str(a) for a in (v.get("vulns") or [])]
        print(f"    {pkg}=={ver}  {ids}")
except Exception as e:
    results["pip_audit"] = {"error": str(e), "pass": False}
    print(f"  ERROR: {e}")

# ── Summary ───────────────────────────────────────────────────────────────────
bandit_ok   = results["bandit"].get("pass",     False)
pip_ok      = results["pip_audit"].get("pass",  False)
overall_ok  = bandit_ok and pip_ok

results["summary"] = {
    "bandit_pass":    bandit_ok,
    "pip_audit_pass": pip_ok,
    "overall_pass":   overall_ok,
    "blocking_issues": results["bandit"].get("blocking_count", 0),
    "high_cves":       results["pip_audit"].get("high_severity_count", 0),
}

print(f"\n{'='*60}")
print(f"  bandit    : {'PASS' if bandit_ok else 'FAIL'}  ({results['bandit'].get('blocking_count',0)} blocking issues)")
print(f"  pip-audit : {'PASS' if pip_ok    else 'FAIL'}  ({results['pip_audit'].get('high_severity_count',0)} high/critical CVEs)")
print(f"  OVERALL   : {'PASS' if overall_ok else 'FAIL'}")
print(f"{'='*60}")

(REPORTS / "security_scan.json").write_text(json.dumps(results, indent=2))
print(f"Report: {REPORTS / 'security_scan.json'}")
sys.exit(0 if overall_ok else 1)

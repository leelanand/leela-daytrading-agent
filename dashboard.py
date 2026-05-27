"""
Trading Day Dashboard — http://localhost:8765
Serves a live status page for the Alpaca and IBKR trading agents.
Auto-refreshes every 30 seconds via JavaScript fetch.

Run:  python dashboard.py
Open: http://localhost:8765
"""
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

BST  = ZoneInfo("Europe/London")
ET   = ZoneInfo("America/New_York")
PORT = 8765

ALPACA_DIR = Path(__file__).parent
IBKR_DIR   = Path(r"C:\Users\leela\leela-ibkr-agent")

# BST schedule: (hhmm, label, event_key, window)
SCHEDULE = [
    (1400, "Pre-market research",              "RESEARCH",     "pre"),
    (1433, "Prescan  — no orders placed",      "PRESCAN",      "pre"),
    (1448, "First scan — first trade possible","SCAN_1",       "trading"),
    (1500, "Scan loop every 5 min",            "SCANNING",     "trading"),
    (1700, "Midday block — no new entries",    "MIDDAY_START", "blocked"),
    (1800, "Afternoon session — trading resumes","MIDDAY_END", "trading"),
    (2030, "Stop new scans — last entry cutoff","SCAN_STOP",   "closing"),
    (2044, "Force close all positions",        "FORCE_CLOSE",  "closing"),
    (2055, "Verify flat",                      "VERIFIED",     "eod"),
    (2100, "EOD report",                       "REPORT",       "eod"),
    (2115, "Performance dashboard",            "PERFORMANCE",  "eod"),
]

LOG_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+(\S+)\s*(\S*)\s*(.*)"
)


# ── Data collection ───────────────────────────────────────────────────────────

def _now_bst() -> datetime:
    return datetime.now(BST)


def _hhmm(dt: datetime) -> int:
    return dt.hour * 100 + dt.minute


def _read_audit(directory: Path) -> list[dict]:
    today   = date.today().isoformat()
    entries = []
    try:
        for line in (directory / "audit.log").read_text(encoding="utf-8", errors="replace").splitlines():
            if today not in line:
                continue
            m = LOG_RE.match(line)
            if m:
                entries.append({
                    "ts":     m.group(1),
                    "action": m.group(2),
                    "symbol": m.group(3),
                    "detail": m.group(4)[:200],
                    "raw":    line[:200],
                })
    except Exception:
        pass
    return entries


def _action_set(audit: list[dict]) -> set[str]:
    return {e["action"] for e in audit}


def _last_scan_time(audit: list[dict]) -> str:
    scans = [e["ts"] for e in audit if e["action"] in
             ("SCAN_START", "SCAN_DONE", "PRESCAN_DONE")]
    return scans[-1][11:16] if scans else ""


def _trades_today(db_path: Path) -> list[dict]:
    today = date.today().isoformat()
    try:
        con  = sqlite3.connect(db_path)
        rows = con.execute(
            "SELECT symbol,shares,entry,exit_price,pnl,pnl_pct,reason,ts "
            "FROM trades WHERE date=? ORDER BY ts",
            (today,),
        ).fetchall()
        con.close()
        return [
            {"symbol": r[0], "shares": r[1], "entry": r[2], "exit": r[3],
             "pnl": r[4], "pnl_pct": r[5], "reason": r[6], "ts": r[7]}
            for r in rows
        ]
    except Exception:
        return []


def _research_done() -> bool:
    today = date.today().isoformat()
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            rc = json.loads((d / "research_cache.json").read_text())
            if rc.get("generated_at", "")[:10] == today:
                return True
        except Exception:
            pass
    return False


def _research_symbols() -> tuple[int, int]:
    """Returns (total, claude_scored)."""
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            rc  = json.loads((d / "research_cache.json").read_text())
            sym = rc.get("symbols", {})
            total  = len(sym)
            scored = sum(1 for v in sym.values()
                         if v.get("research_brief", "").startswith("Not") is False
                         and v.get("research_brief"))
            return total, scored
        except Exception:
            pass
    return 0, 0


def _claude_eff() -> dict:
    today = date.today().isoformat()
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            f = d / "claude_effectiveness.jsonl"
            if not f.exists():
                continue
            records = [
                json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines()
                if ln.strip() and json.loads(ln).get("date") == today
            ]
            if not records:
                continue
            total      = len(records)
            rejects    = sum(1 for r in records if r.get("local_only"))
            hits       = sum(1 for r in records if r.get("cache_hit"))
            scored     = sum(1 for r in records if not r.get("local_only") and not r.get("cache_hit"))
            changed    = sum(1 for r in records if r.get("claude_changed_decision"))
            return {
                "total": total, "rejects": rejects,
                "hits": hits, "scored": scored, "changed": changed,
            }
        except Exception:
            pass
    return {}


def _perf_today() -> dict:
    today = date.today().isoformat()
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            p = json.loads((d / "performance.json").read_text())
            if p.get("date") == today:
                return p
        except Exception:
            pass
    return {}


def _candidates_today() -> tuple[int, int]:
    """Returns (tradeable, watchlist) from candidates.json."""
    today = date.today().isoformat()
    for d in (ALPACA_DIR, IBKR_DIR):
        try:
            f = d / "candidates.json"
            if f.stat().st_mtime and date.fromtimestamp(f.stat().st_mtime).isoformat() == today:
                cands = json.loads(f.read_text())
                t = sum(1 for c in cands if c.get("tradeable"))
                w = sum(1 for c in cands if c.get("watchlist"))
                return t, w
        except Exception:
            pass
    return 0, 0


def _ibkr_log_tail() -> list[str]:
    try:
        lines = (IBKR_DIR / "trading_day.log").read_text(encoding="utf-8", errors="replace").splitlines()
        return [l for l in lines if l.strip()][-8:]
    except Exception:
        return []


# ── Schedule status ───────────────────────────────────────────────────────────

def _schedule_rows(audit: list[dict]) -> list[dict]:
    now       = _now_bst()
    hhmm_now  = _hhmm(now)
    actions   = _action_set(audit)
    today     = date.today().isoformat()

    # Derive booleans
    research_ok     = _research_done()
    prescan_ok      = "PRESCAN_DONE"    in actions
    prescan_skipped = "PRESCAN_SKIPPED" in actions
    scan_skipped    = "SCAN_SKIPPED"    in actions
    force_ok        = "FORCE_CLOSE"     in actions
    verified_ok     = "VERIFIED" in actions or "EOD_VERIFIED" in actions
    perf_ok         = bool(_perf_today())
    scan_count      = sum(1 for e in audit if e["action"] in ("SCAN_DONE", "SCAN_START"))
    last_scan       = _last_scan_time(audit)
    any_order       = "ORDER_PLACED" in actions
    regime_entry    = next((e for e in audit if e["action"] == "REGIME_DETECTED"), None)
    regime_note     = ""
    if regime_entry:
        try:
            raw = regime_entry["raw"]
            d = json.loads(raw[raw.index("{"):])
            regime_note = d.get("regime", "")
        except Exception:
            pass

    # candidates info
    t_count, w_count = _candidates_today()

    rows = []
    for hhmm, label, key, window in SCHEDULE:
        past = hhmm_now >= hhmm
        if key == "RESEARCH":
            if research_ok:
                status, note = "done", "cache fresh"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "PRESCAN":
            if prescan_ok:
                status, note = "done", f"{t_count}T {w_count}W"
            elif prescan_skipped:
                status, note = "skip", f"regime={regime_note}"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "SCAN_1":
            if scan_count > 0:
                status, note = "done", f"last {last_scan}"
            elif scan_skipped:
                status, note = "skip", f"regime={regime_note}"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "SCANNING":
            in_midday = 1700 <= hhmm_now < 1800
            if force_ok:
                status, note = "done", "closed"
            elif scan_count > 0 and not in_midday and hhmm_now < 2030:
                status, note = "active", f"{scan_count} scans  {'📦 order' if any_order else ''}"
            elif in_midday:
                status, note = "active", "midday block"
            elif past:
                status, note = "pending", ""
            else:
                status, note = "pending", ""

        elif key == "MIDDAY_START":
            status = "active" if 1700 <= hhmm_now < 1800 else ("done" if past else "pending")
            note   = ""

        elif key == "MIDDAY_END":
            status = "done" if past else ("active" if hhmm_now >= 1700 else "pending")
            note   = ""

        elif key == "SCAN_STOP":
            status = "done" if past else "pending"
            note   = ""

        elif key == "FORCE_CLOSE":
            if force_ok:
                status, note = "done", "all flat"
            elif past:
                status, note = "warn", "expected by now"
            else:
                status, note = "pending", ""

        elif key == "VERIFIED":
            if verified_ok:
                status, note = "done", "positions=0"
            elif past:
                status, note = "warn", ""
            else:
                status, note = "pending", ""

        elif key == "REPORT":
            if perf_ok:
                p = _perf_today()
                pnl = p.get("total_pnl", 0)
                status = "done"
                note   = f"P&L ${pnl:+.2f}"
            elif past:
                status, note = "warn", ""
            else:
                status, note = "pending", ""

        elif key == "PERFORMANCE":
            if perf_ok:
                status, note = "done", "dashboard written"
            elif past:
                status, note = "warn", ""
            else:
                status, note = "pending", ""

        else:
            status, note = "pending", ""

        # ET time
        h, m  = divmod(hhmm, 100)
        et_h  = (h - 5) % 24
        et    = f"{et_h:02d}:{m:02d}"

        rows.append({
            "bst":    f"{h:02d}:{m:02d}",
            "et":     et,
            "label":  label,
            "status": status,
            "note":   note,
            "window": window,
        })

    return rows


# ── JSON API ──────────────────────────────────────────────────────────────────

def _build_status() -> dict:
    now_bst = _now_bst()
    now_et  = datetime.now(ET)
    audit   = _read_audit(ALPACA_DIR)
    trades  = _trades_today(ALPACA_DIR / "daytrades.db")
    eff     = _claude_eff()
    perf    = _perf_today()
    ibkr_log = _ibkr_log_tail()
    res_total, res_scored = _research_symbols()

    total_pnl = sum(t["pnl"] for t in trades) if trades else 0.0
    wins      = sum(1 for t in trades if t["pnl"] > 0)
    losses    = sum(1 for t in trades if t["pnl"] <= 0)

    # Recent audit events (last 12 distinct actions, skip noise)
    SKIP = {"LOW_VOLUME_MODE", "MARKET_REGIME"}
    recent = [e for e in audit if e["action"] not in SKIP][-12:]

    return {
        "now_bst":    now_bst.strftime("%H:%M:%S BST"),
        "now_et":     now_et.strftime("%H:%M:%S ET"),
        "today":      date.today().isoformat(),
        "schedule":   _schedule_rows(audit),
        "trades":     trades,
        "total_pnl":  round(total_pnl, 2),
        "wins":       wins,
        "losses":     losses,
        "claude_eff": eff,
        "research":   {"total": res_total, "claude_scored": res_scored},
        "recent_log": recent,
        "ibkr_log":   ibkr_log,
        "perf":       {k: perf.get(k) for k in
                       ("win_rate", "profit_factor", "expectancy", "trades")
                       if k in perf},
    }


# ── HTTP handler ──────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Leela Trading Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d1117; color: #e6edf3; font-family: 'Cascadia Code', 'Fira Mono', monospace; font-size: 13px; padding: 16px; }
  h1 { font-size: 18px; color: #58a6ff; margin-bottom: 4px; }
  .subtitle { color: #8b949e; font-size: 12px; margin-bottom: 16px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; }
  .card h2 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
  table { width: 100%; border-collapse: collapse; }
  th { color: #8b949e; font-weight: normal; text-align: left; padding: 4px 8px; font-size: 11px; }
  td { padding: 5px 8px; border-top: 1px solid #21262d; }
  tr:first-child td { border-top: none; }
  .done    { color: #3fb950; }
  .active  { color: #58a6ff; }
  .warn    { color: #d29922; }
  .skip    { color: #a371f7; }
  .pending { color: #484f58; }
  .dot-done    { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #3fb950; margin-right: 6px; }
  .dot-active  { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #58a6ff; margin-right: 6px; animation: pulse 1.5s infinite; }
  .dot-warn    { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #d29922; margin-right: 6px; }
  .dot-skip    { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #a371f7; margin-right: 6px; }
  .dot-pending { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #30363d; margin-right: 6px; }
  .win-trading { color: #3fb950; font-size: 10px; font-weight: bold; letter-spacing: .5px; }
  .win-blocked { color: #f85149; font-size: 10px; font-weight: bold; letter-spacing: .5px; }
  .win-closing { color: #d29922; font-size: 10px; font-weight: bold; letter-spacing: .5px; }
  .win-pre     { color: #8b949e; font-size: 10px; letter-spacing: .5px; }
  .win-eod     { color: #8b949e; font-size: 10px; letter-spacing: .5px; }
  tr.row-blocked td { background: #1a1010; }
  tr.row-trading td { background: #0d1a0d; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
  .pnl-pos { color: #3fb950; }
  .pnl-neg { color: #f85149; }
  .pnl-zero { color: #8b949e; }
  .log-entry { font-size: 11px; color: #8b949e; padding: 2px 0; border-top: 1px solid #21262d; }
  .log-entry:first-child { border-top: none; }
  .log-action { color: #e6edf3; }
  .log-sym { color: #79c0ff; }
  .header-bar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
  .time-display { font-size: 22px; color: #e6edf3; }
  .time-et { font-size: 14px; color: #8b949e; margin-left: 12px; }
  .refresh-info { font-size: 11px; color: #484f58; }
  #last-refresh { color: #58a6ff; }
  .metric { display: inline-block; margin-right: 20px; }
  .metric-val { font-size: 20px; }
  .metric-label { font-size: 10px; color: #8b949e; text-transform: uppercase; }
  .current-row td { background: #1c2128; }
  .full-width { grid-column: 1 / -1; }
</style>
</head>
<body>
<div class="header-bar">
  <div>
    <h1>Leela Trading Dashboard</h1>
    <div class="subtitle">Alpaca paper + IBKR — auto-refresh every 30s</div>
  </div>
  <div style="text-align:right">
    <div class="time-display" id="clock-bst">--:-- BST</div>
    <div class="time-et" id="clock-et">--:-- ET</div>
    <div class="refresh-info">Last update: <span id="last-refresh">—</span></div>
  </div>
</div>

<div class="grid">

  <!-- Schedule -->
  <div class="card full-width">
    <h2>Trading Day Schedule</h2>
    <table>
      <tr><th>BST</th><th>ET</th><th>Window</th><th>Event</th><th>Status</th><th>Notes</th></tr>
      <tbody id="schedule-body"></tbody>
    </table>
  </div>

  <!-- P&L -->
  <div class="card">
    <h2>Today's Trades</h2>
    <div id="pnl-summary" style="margin-bottom:10px"></div>
    <table>
      <tr><th>Symbol</th><th>Shares</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Reason</th></tr>
      <tbody id="trades-body"></tbody>
    </table>
    <div id="no-trades" style="color:#484f58;font-size:12px;display:none">No trades today yet.</div>
  </div>

  <!-- Claude Effectiveness -->
  <div class="card">
    <h2>Claude Effectiveness</h2>
    <div id="claude-stats"></div>
  </div>

  <!-- Recent log -->
  <div class="card full-width">
    <h2>Recent Agent Activity</h2>
    <div id="log-entries"></div>
  </div>

</div>

<script>
let currentBSThhmm = 0;

function hhmm(dt) { return dt.getHours() * 100 + dt.getMinutes(); }

function tickClock() {
  const now = new Date();
  // BST = UTC+1
  const bst = new Date(now.getTime() + 60*60*1000);
  const et  = new Date(now.getTime() - 4*60*60*1000); // EDT UTC-4
  document.getElementById('clock-bst').textContent =
    bst.toISOString().substring(11,19) + ' BST';
  document.getElementById('clock-et').textContent =
    et.toISOString().substring(11,19) + ' ET';
  currentBSThhmm = bst.getUTCHours() * 100 + bst.getUTCMinutes();
}

function statusIcon(s) {
  return `<span class="dot-${s}"></span>`;
}

function statusText(s, note) {
  const labels = {done:'Done', active:'Active', warn:'⚠ Late', skip:'⏭ Skipped', pending:'Pending'};
  return `<span class="${s}">${statusIcon(s)}${labels[s]||s}</span>${note ? ' <span style="color:#8b949e">'+note+'</span>' : ''}`;
}

function pnlClass(v) {
  return v > 0 ? 'pnl-pos' : v < 0 ? 'pnl-neg' : 'pnl-zero';
}

function render(data) {
  // Schedule
  const sched = document.getElementById('schedule-body');
  sched.innerHTML = '';
  const SCHED_HHMM = [1400,1433,1448,1500,1700,1800,2030,2044,2055,2100,2115];
  data.schedule.forEach((row, i) => {
    const tr = document.createElement('tr');
    const isCurrent = (i < data.schedule.length - 1)
      ? (currentBSThhmm >= SCHED_HHMM[i] && currentBSThhmm < SCHED_HHMM[i+1])
      : (currentBSThhmm >= SCHED_HHMM[i]);
    const WIN_LABELS = {
      trading: '<span class="win-trading">&#9654; TRADING</span>',
      blocked: '<span class="win-blocked">&#9940; NO TRADES</span>',
      closing: '<span class="win-closing">&#9209; CLOSING</span>',
      pre:     '<span class="win-pre">PRE-MKT</span>',
      eod:     '<span class="win-eod">EOD</span>',
    };
    if (isCurrent) tr.className = 'current-row';
    else if (row.window === 'blocked') tr.className = 'row-blocked';
    else if (row.window === 'trading') tr.className = 'row-trading';
    const winBadge = WIN_LABELS[row.window] || '';
    tr.innerHTML = '<td>' + row.bst + '</td>'
      + '<td style="color:#8b949e">' + row.et + '</td>'
      + '<td>' + winBadge + '</td>'
      + '<td>' + row.label + '</td>'
      + '<td>' + statusText(row.status, '') + '</td>'
      + '<td style="color:#8b949e">' + (row.note || '') + '</td>';
    sched.appendChild(tr);
  });

  // P&L summary
  const pnlCls = pnlClass(data.total_pnl);
  document.getElementById('pnl-summary').innerHTML = `
    <span class="metric"><span class="metric-val ${pnlCls}">$${data.total_pnl >= 0 ? '+' : ''}${data.total_pnl.toFixed(2)}</span><div class="metric-label">P&amp;L</div></span>
    <span class="metric"><span class="metric-val">${data.wins}W / ${data.losses}L</span><div class="metric-label">Trades</div></span>
  `;

  // Trades table
  const tb = document.getElementById('trades-body');
  const noTrades = document.getElementById('no-trades');
  tb.innerHTML = '';
  if (data.trades.length === 0) {
    noTrades.style.display = 'block';
  } else {
    noTrades.style.display = 'none';
    data.trades.forEach(t => {
      const cls = pnlClass(t.pnl);
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td class="log-sym">${t.symbol}</td>
        <td>${t.shares}</td>
        <td>$${t.entry.toFixed(2)}</td>
        <td>${t.exit ? '$'+t.exit.toFixed(2) : '—'}</td>
        <td class="${cls}">$${t.pnl >= 0?'+':''}${t.pnl.toFixed(2)}</td>
        <td style="color:#8b949e;font-size:11px">${t.reason||''}</td>`;
      tb.appendChild(tr);
    });
  }

  // Claude effectiveness
  const eff = data.claude_eff;
  const res = data.research;
  let claude_html = '';
  if (!eff || !eff.total) {
    claude_html = '<div style="color:#484f58">No data yet — starts after first scan.</div>';
  } else {
    const eligible = eff.total - eff.rejects;
    const cacheRate = eligible > 0 ? Math.round(eff.hits / eligible * 100) : 0;
    const changeRate = eff.scored > 0 ? Math.round(eff.changed / eff.scored * 100) : 0;
    claude_html = `
      <table>
        <tr><td>Candidates seen</td><td style="color:#e6edf3">${eff.total}</td></tr>
        <tr><td>Local rejects</td><td class="pending">${eff.rejects} (no Claude)</td></tr>
        <tr><td>Cache hits</td><td class="done">${eff.hits}</td></tr>
        <tr><td>Claude scored</td><td class="active">${eff.scored}</td></tr>
        <tr><td>Cache hit rate</td><td class="done">${cacheRate}%</td></tr>
        <tr><td>Decision changes</td><td class="${changeRate>20?'active':'pending'}">${eff.changed} (${changeRate}%)</td></tr>
      </table>
      <div style="margin-top:8px;color:#8b949e;font-size:11px">Research: ${res.claude_scored} of ${res.total} symbols sent to Claude</div>`;
  }
  document.getElementById('claude-stats').innerHTML = claude_html;

  // Recent log
  const logDiv = document.getElementById('log-entries');
  const SKIP = new Set(['LOW_VOLUME_MODE','MARKET_REGIME']);
  logDiv.innerHTML = '';
  data.recent_log.forEach(e => {
    const d = document.createElement('div');
    d.className = 'log-entry';
    d.innerHTML = `<span style="color:#484f58">${e.ts.substring(11,16)}</span>  <span class="log-action">${e.action}</span>  <span class="log-sym">${e.symbol||''}</span>  <span>${e.detail.substring(0,120)}</span>`;
    logDiv.appendChild(d);
  });
  if (data.recent_log.length === 0) {
    logDiv.innerHTML = '<div style="color:#484f58">No activity yet today.</div>';
  }

  document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
}

async function refresh() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    render(data);
  } catch(e) {
    document.getElementById('last-refresh').textContent = 'error — retrying';
  }
}

setInterval(tickClock, 1000);
tickClock();
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            try:
                data = _build_status()
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # suppress access log noise


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dashboard running → http://localhost:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

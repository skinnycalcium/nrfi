"""
nrfi_email.py — Run the daily NRFI report and email it via Resend.

Required env vars:
  RESEND_API_KEY  — https://resend.com (free 3000/mo, no card)
  EMAIL_TO        — your inbox
  EMAIL_FROM      — verified sender (e.g., "NRFI <nrfi@yourdomain.com>")
                    Or use Resend's sandbox: "onboarding@resend.dev"

Optional:
  ODDS_API_KEY, BANKROLL, KELLY_FRACTION  — same as nrfi_daily.py

Usage:
  python nrfi_email.py              # today
  python nrfi_email.py 2026-04-18   # specific date
"""
from __future__ import annotations

import os
import sys
import base64
from datetime import date, datetime

import requests

from nrfi_daily import (
    fetch_schedule, fetch_nrfi_odds, build_game, score, render_html,
)

VERDICT_RANK = {"BET": 0, "LEAN": 1, "PASS": 2, "NO LINE": 3}


def build_summary_email(games: list, target: str) -> tuple[str, str]:
    bets = [g for g in games if g.verdict == "BET"]
    leans = [g for g in games if g.verdict == "LEAN"]

    rows = []
    for g in (bets + leans)[:12]:
        sign = "+" if (g.market_line or 0) > 0 else ""
        line_str = f"{sign}{g.market_line}" if g.market_line else "—"
        edge_str = f"{g.edge*100:+.1f}%" if g.market_line else "—"
        stake_str = f"${g.stake:,.0f}" if g.kelly_pct > 0 else "—"
        verdict_color = "#15803d" if g.verdict == "BET" else "#d97706"
        rows.append(f"""
          <tr style="border-bottom:1px solid #eee;">
            <td style="padding:10px 8px;"><strong style="color:{verdict_color};font-size:11px;letter-spacing:0.1em;">{g.verdict}</strong></td>
            <td style="padding:10px 8px;font-size:13px;">{g.away_team} @ {g.home_team}</td>
            <td style="padding:10px 8px;font-family:'SF Mono',monospace;font-size:13px;text-align:right;">{g.p_nrfi*100:.1f}%</td>
            <td style="padding:10px 8px;font-family:'SF Mono',monospace;font-size:13px;text-align:right;">{line_str}</td>
            <td style="padding:10px 8px;font-family:'SF Mono',monospace;font-size:13px;text-align:right;color:{verdict_color};"><strong>{edge_str}</strong></td>
            <td style="padding:10px 8px;font-family:'SF Mono',monospace;font-size:13px;text-align:right;">{stake_str}</td>
          </tr>
        """)

    table_or_empty = (
        f"""<table style="width:100%;border-collapse:collapse;margin-top:8px;">
          <thead>
            <tr style="border-bottom:2px solid #0a0a0a;text-align:left;">
              <th style="padding:10px 8px;font-size:10px;letter-spacing:0.15em;color:#666;">VERDICT</th>
              <th style="padding:10px 8px;font-size:10px;letter-spacing:0.15em;color:#666;">MATCHUP</th>
              <th style="padding:10px 8px;font-size:10px;letter-spacing:0.15em;color:#666;text-align:right;">P(NRFI)</th>
              <th style="padding:10px 8px;font-size:10px;letter-spacing:0.15em;color:#666;text-align:right;">LINE</th>
              <th style="padding:10px 8px;font-size:10px;letter-spacing:0.15em;color:#666;text-align:right;">EDGE</th>
              <th style="padding:10px 8px;font-size:10px;letter-spacing:0.15em;color:#666;text-align:right;">STAKE</th>
            </tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>"""
        if rows
        else '<p style="color:#999;font-size:14px;margin:24px 0;">No bets or leans on the slate today.</p>'
    )

    pages_url = os.getenv("PAGES_URL", "")
    pages_link = (
        f'<p style="margin:24px 0 0;color:#666;font-size:13px;">'
        f'Live dashboard: <a href="{pages_url}" style="color:#0a0a0a;">{pages_url}</a></p>'
        if pages_url else ""
    )

    body = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fafaf8;padding:32px 16px;margin:0;">
<div style="max-width:720px;margin:0 auto;background:white;border:1px solid #e5e5e5;padding:32px;">
  <div style="border-bottom:2px solid #0a0a0a;padding-bottom:16px;margin-bottom:8px;">
    <div style="font-family:'SF Mono',monospace;font-size:10px;letter-spacing:0.18em;color:#d97706;text-transform:uppercase;">First Inning Edge Engine</div>
    <h1 style="font-family:Georgia,serif;font-style:italic;font-size:36px;margin:4px 0 0;color:#0a0a0a;">NRFI · {target}</h1>
  </div>
  <p style="color:#666;font-size:13px;margin:12px 0 24px;">
    <strong style="color:#15803d;">{len(bets)} bets</strong> · 
    <strong style="color:#d97706;">{len(leans)} leans</strong> · 
    {len(games)} games scanned
  </p>
  {table_or_empty}
  {pages_link}
  <p style="margin:24px 0 0;color:#999;font-size:11px;border-top:1px solid #eee;padding-top:16px;">
    Full per-game dashboard attached. Quarter-Kelly stake sizing. Best line across U.S. books.
  </p>
</div>
</body></html>"""

    subject = f"NRFI · {target} · {len(bets)} bet{'s' if len(bets) != 1 else ''}, {len(leans)} lean{'s' if len(leans) != 1 else ''}"
    return subject, body


def send_via_resend(subject: str, body_html: str, attachments: list,
                    to_addr: str, from_addr: str) -> dict:
    api_key = os.environ["RESEND_API_KEY"]
    payload = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "html": body_html,
        "attachments": attachments,
    }
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}"},
        json=payload, timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    season = datetime.fromisoformat(target).year

    print(f"=== NRFI Email · {target} ===", file=sys.stderr)
    raw = fetch_schedule(target)
    odds = fetch_nrfi_odds()

    games = []
    for r in raw:
        g = build_game(r, season)
        if g:
            score(g, odds)
            games.append(g)

    games.sort(key=lambda g: (
        VERDICT_RANK[g.verdict],
        -g.edge if g.market_line else 0,
        -g.p_nrfi,
    ))

    # Write full dashboard for attachment + GitHub Pages
    report_html = render_html(games, target)
    report_file = f"nrfi_report_{target}.html"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_html)

    # Also write as latest.html for stable URL
    with open("latest.html", "w", encoding="utf-8") as f:
        f.write(report_html)

    # Build email
    subject, body = build_summary_email(games, target)
    encoded = base64.b64encode(report_html.encode("utf-8")).decode()
    attachments = [{"filename": f"nrfi_{target}.html", "content": encoded}]

    to_addr = os.environ["EMAIL_TO"]
    from_addr = os.environ.get("EMAIL_FROM", "NRFI <onboarding@resend.dev>")

    result = send_via_resend(subject, body, attachments, to_addr, from_addr)
    print(f"Sent → {to_addr} (id: {result.get('id', '?')})", file=sys.stderr)


if __name__ == "__main__":
    main()

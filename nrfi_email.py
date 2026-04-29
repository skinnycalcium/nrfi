"""
nrfi_email.py - Run the daily NRFI/YRFI scan and email the full slate.
Email body shows EVERY game (including TBD-pitcher games), with starter matchups.
"""
from __future__ import annotations

import os
import sys
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from nrfi_daily import (
    fetch_schedule, fetch_inning_odds, build_game, score, render_html,
    american_to_implied,
)

ET = ZoneInfo("America/New_York")

VERDICT_RANK = {"BET NRFI": 0, "BET YRFI": 0, "NO BET": 1, "NO LINE": 2, "PITCHER TBD": 3}


def build_summary_email(games: list, target: str) -> tuple[str, str]:
    bn = sum(1 for g in games if g.verdict == "BET NRFI")
    by = sum(1 for g in games if g.verdict == "BET YRFI")
    nb = sum(1 for g in games if g.verdict == "NO BET")
    nl = sum(1 for g in games if g.verdict == "NO LINE")
    tbd = sum(1 for g in games if g.verdict == "PITCHER TBD")

    rows = []
    for g in games:
        if g.verdict == "BET NRFI":
            color, label = "#15803d", "BET NRFI"
        elif g.verdict == "BET YRFI":
            color, label = "#1d4ed8", "BET YRFI"
        elif g.verdict == "NO BET":
            color, label = "#999", "NO BET"
        elif g.verdict == "PITCHER TBD":
            color, label = "#d97706", "PITCHER TBD"
        else:
            color, label = "#bbb", "NO LINE"

        # Game time in ET
        try:
            tlocal = datetime.fromisoformat(g.game_time_iso.replace("Z", "+00:00")).astimezone(ET)
            time_str = tlocal.strftime("%-I:%M %p ET")
        except Exception:
            time_str = ""

        # Pitcher matchup line
        pitchers = f"{g.away_pitcher.name} vs {g.home_pitcher.name}"

        if g.verdict == "PITCHER TBD":
            p_nrfi_str = "&mdash;"
            p_yrfi_str = "&mdash;"
            line_str = "&mdash;"
            edge_str = "&mdash;"
            stake_str = "&mdash;"
        else:
            p_nrfi_str = f"{g.p_nrfi*100:.0f}%"
            p_yrfi_str = f"{g.p_yrfi*100:.0f}%"
            if g.chosen_price is not None:
                sign = "+" if g.chosen_price > 0 else ""
                line_str = f"{g.side} {sign}{g.chosen_price}"
                edge_str = f"{g.chosen_edge*100:+.1f}%"
                stake_str = f"${g.stake:,.0f}" if g.kelly_pct > 0 else "&mdash;"
            else:
                line_str = "&mdash;"
                edge_str = "&mdash;"
                stake_str = "&mdash;"

        rows.append(f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:8px;vertical-align:top;"><strong style="color:{color};font-size:10px;letter-spacing:0.1em;">{label}</strong></td>
          <td style="padding:8px;font-size:12px;vertical-align:top;">
            <div><strong>{g.away_team} @ {g.home_team}</strong></div>
            <div style="color:#666;font-size:11px;margin-top:2px;">{pitchers}</div>
            <div style="color:#999;font-size:10px;margin-top:2px;">{time_str}</div>
          </td>
          <td style="padding:8px;font-family:monospace;font-size:12px;text-align:right;color:#15803d;vertical-align:top;">{p_nrfi_str}</td>
          <td style="padding:8px;font-family:monospace;font-size:12px;text-align:right;color:#1d4ed8;vertical-align:top;">{p_yrfi_str}</td>
          <td style="padding:8px;font-family:monospace;font-size:12px;text-align:right;vertical-align:top;">{line_str}</td>
          <td style="padding:8px;font-family:monospace;font-size:12px;text-align:right;color:{color};vertical-align:top;"><strong>{edge_str}</strong></td>
          <td style="padding:8px;font-family:monospace;font-size:12px;text-align:right;vertical-align:top;">{stake_str}</td>
        </tr>
        """)

    pages_url = os.getenv("PAGES_URL", "").strip()
    pages_link = (
        f'<p style="margin:24px 0 0;color:#666;font-size:13px;">'
        f'Live dashboard: <a href="{pages_url}" style="color:#0a0a0a;">{pages_url}</a></p>'
        if pages_url and pages_url != " " else ""
    )

    table = (
        f"""<table style="width:100%;border-collapse:collapse;margin-top:8px;">
          <thead>
            <tr style="border-bottom:2px solid #0a0a0a;text-align:left;">
              <th style="padding:8px;font-size:9px;letter-spacing:0.15em;color:#666;">VERDICT</th>
              <th style="padding:8px;font-size:9px;letter-spacing:0.15em;color:#666;">MATCHUP / PITCHERS</th>
              <th style="padding:8px;font-size:9px;letter-spacing:0.15em;color:#666;text-align:right;">P(NRFI)</th>
              <th style="padding:8px;font-size:9px;letter-spacing:0.15em;color:#666;text-align:right;">P(YRFI)</th>
              <th style="padding:8px;font-size:9px;letter-spacing:0.15em;color:#666;text-align:right;">PICK</th>
              <th style="padding:8px;font-size:9px;letter-spacing:0.15em;color:#666;text-align:right;">EDGE</th>
              <th style="padding:8px;font-size:9px;letter-spacing:0.15em;color:#666;text-align:right;">STAKE</th>
            </tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>"""
        if rows
        else '<p style="color:#999;font-size:14px;margin:24px 0;">No games on the slate today.</p>'
    )

    body = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#fafaf8;padding:32px 16px;margin:0;">
<div style="max-width:880px;margin:0 auto;background:white;border:1px solid #e5e5e5;padding:32px;">
  <div style="border-bottom:2px solid #0a0a0a;padding-bottom:16px;margin-bottom:8px;">
    <div style="font-family:monospace;font-size:10px;letter-spacing:0.18em;color:#d97706;text-transform:uppercase;">First Inning Edge Engine</div>
    <h1 style="font-family:Georgia,serif;font-style:italic;font-size:36px;margin:4px 0 0;color:#0a0a0a;">NRFI &middot; {target}</h1>
  </div>
  <p style="color:#666;font-size:13px;margin:12px 0 24px;">
    <strong style="color:#15803d;">{bn} bet NRFI</strong> &middot;
    <strong style="color:#1d4ed8;">{by} bet YRFI</strong> &middot;
    <span style="color:#999;">{nb} no bet &middot; {nl} no line &middot; {tbd} pitcher TBD</span> &middot;
    {len(games)} games scanned
  </p>
  {table}
  {pages_link}
  <p style="margin:24px 0 0;color:#999;font-size:11px;border-top:1px solid #eee;padding-top:16px;">
    Full per-game dashboard attached. Quarter-Kelly stake sizing. Best line across U.S. books.
    Edge threshold for BET: 4%. Pitcher TBD games shown for visibility but not graded.
  </p>
</div>
</body></html>"""

    subject = f"NRFI - {target} - {bn + by} bet{'s' if (bn+by) != 1 else ''} on {len(games)} games"
    return subject, body


def send_via_resend(subject, body_html, attachments, to_addr, from_addr):
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
    # Default to TODAY in US Eastern Time so the runner's UTC clock can never put us a day off.
    target = sys.argv[1] if len(sys.argv) > 1 else datetime.now(ET).date().isoformat()

    print(f"=== NRFI Email - {target} (ET) ===", file=sys.stderr)
    season = datetime.fromisoformat(target).year
    raw = fetch_schedule(target)
    odds = fetch_inning_odds()

    games = []
    for r in raw:
        g = build_game(r, season)
        if g:
            score(g, odds)
            games.append(g)

    games.sort(key=lambda g: (
        VERDICT_RANK.get(g.verdict, 9),
        -g.chosen_edge if g.chosen_price else 0,
        -g.p_nrfi,
    ))

    report_html = render_html(games, target)
    report_file = f"nrfi_report_{target}.html"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_html)

    with open("latest.html", "w", encoding="utf-8") as f:
        f.write(report_html)

    subject, body = build_summary_email(games, target)
    encoded = base64.b64encode(report_html.encode("utf-8")).decode()
    attachments = [{"filename": f"nrfi_{target}.html", "content": encoded}]

    to_addr = os.environ["EMAIL_TO"]
    from_addr = os.environ.get("EMAIL_FROM", "NRFI <onboarding@resend.dev>")

    result = send_via_resend(subject, body, attachments, to_addr, from_addr)
    print(f"Sent -> {to_addr} (id: {result.get('id', '?')})", file=sys.stderr)


if __name__ == "__main__":
    main()

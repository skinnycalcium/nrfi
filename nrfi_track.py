"""
nrfi_track.py — Persists daily picks and grades them against actual results.

Run order each morning:
  1. nrfi_email.py     (generates today's picks)
  2. nrfi_track.py     (saves today's picks + grades yesterday's)

Outputs:
  picks_history.json   — list of every pick ever made + result
  picks_summary.json   — running W/L, ROI, calibration buckets
  picks_today.json     — today's picks for the Slack bot to read
"""
from __future__ import annotations

import os
import sys
import json
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from nrfi_daily import (
    fetch_schedule, fetch_inning_odds, build_game, score, american_to_implied,
)

HISTORY_FILE = "picks_history.json"
SUMMARY_FILE = "picks_summary.json"
TODAY_FILE = "picks_today.json"


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def fetch_first_inning_runs(game_pk: int) -> Optional[int]:
    """Returns total runs scored in the 1st inning, or None if game not final."""
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
    try:
        r = requests.get(url, timeout=12).json()
    except Exception as e:
        print(f"  ! result fetch failed for {game_pk}: {e}", file=sys.stderr)
        return None

    status = r.get("gameData", {}).get("status", {}).get("abstractGameState", "")
    if status != "Final":
        return None

    innings = r.get("liveData", {}).get("linescore", {}).get("innings", [])
    if not innings:
        return None
    first = innings[0]
    away_runs = first.get("away", {}).get("runs", 0) or 0
    home_runs = first.get("home", {}).get("runs", 0) or 0
    return int(away_runs) + int(home_runs)


def grade_pick(pick: dict, runs_in_first: int) -> str:
    """Returns 'W', 'L', or 'PUSH'. PUSH never happens for 0.5 lines."""
    side = pick.get("side", "")
    if side == "NRFI":
        return "W" if runs_in_first == 0 else "L"
    elif side == "YRFI":
        return "W" if runs_in_first >= 1 else "L"
    return "PUSH"


def payout(stake: float, american: int, result: str) -> float:
    """Profit/loss in dollars given the bet's stake, line, and W/L."""
    if result == "L":
        return -stake
    if result == "PUSH":
        return 0.0
    if american > 0:
        return stake * (american / 100)
    return stake * (100 / abs(american))


def grade_yesterday(history: list) -> tuple[int, int]:
    """Find any ungraded BET picks from past dates, fetch results, mark W/L."""
    graded, attempted = 0, 0
    for p in history:
        if p.get("result") in ("W", "L", "PUSH"):
            continue
        if not p.get("verdict", "").startswith("BET"):
            continue
        # Only grade picks at least 3 hours old (let games finish)
        try:
            placed = datetime.fromisoformat(p["date"])
            if placed.date() >= date.today():
                continue
        except Exception:
            continue
        attempted += 1
        runs = fetch_first_inning_runs(p["game_pk"])
        if runs is None:
            continue
        result = grade_pick(p, runs)
        p["result"] = result
        p["runs_in_first"] = runs
        p["pnl"] = round(payout(p.get("stake", 0), p.get("price", -110), result), 2)
        graded += 1
    return graded, attempted


def compute_summary(history: list) -> dict:
    bets = [p for p in history if p.get("result") in ("W", "L", "PUSH")]
    wins = sum(1 for p in bets if p["result"] == "W")
    losses = sum(1 for p in bets if p["result"] == "L")
    pushes = sum(1 for p in bets if p["result"] == "PUSH")
    total_staked = sum(p.get("stake", 0) for p in bets)
    total_pnl = sum(p.get("pnl", 0) for p in bets)
    roi = (total_pnl / total_staked * 100) if total_staked > 0 else 0

    nrfi = [p for p in bets if p.get("side") == "NRFI"]
    yrfi = [p for p in bets if p.get("side") == "YRFI"]
    nrfi_w = sum(1 for p in nrfi if p["result"] == "W")
    yrfi_w = sum(1 for p in yrfi if p["result"] == "W")

    # Edge calibration buckets — were 4-7% edges actually profitable?
    buckets = {"<2%": [], "2-4%": [], "4-7%": [], "7-12%": [], "12%+": []}
    for p in bets:
        e = p.get("edge", 0) * 100
        if e < 2:
            buckets["<2%"].append(p)
        elif e < 4:
            buckets["2-4%"].append(p)
        elif e < 7:
            buckets["4-7%"].append(p)
        elif e < 12:
            buckets["7-12%"].append(p)
        else:
            buckets["12%+"].append(p)
    cal = {}
    for k, ps in buckets.items():
        if ps:
            n = len(ps)
            w = sum(1 for p in ps if p["result"] == "W")
            staked = sum(p.get("stake", 0) for p in ps)
            pnl = sum(p.get("pnl", 0) for p in ps)
            cal[k] = {
                "n": n, "wins": w, "win_pct": round(100 * w / n, 1),
                "roi": round(pnl / staked * 100, 1) if staked > 0 else 0,
                "pnl": round(pnl, 2),
            }

    return {
        "updated": datetime.now().isoformat(),
        "total_bets": len(bets),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_pct": round(100 * wins / max(1, wins + losses), 1),
        "total_staked": round(total_staked, 2),
        "total_pnl": round(total_pnl, 2),
        "roi_pct": round(roi, 2),
        "nrfi": {"n": len(nrfi), "wins": nrfi_w,
                 "win_pct": round(100 * nrfi_w / max(1, len(nrfi)), 1)},
        "yrfi": {"n": len(yrfi), "wins": yrfi_w,
                 "win_pct": round(100 * yrfi_w / max(1, len(yrfi)), 1)},
        "edge_calibration": cal,
    }


def serialize_game(g, target: str) -> dict:
    """Convert a Game object into a flat dict for storage / Slack bot."""
    return {
        "date": target,
        "game_pk": g.game_pk,
        "away_team": g.away_team,
        "home_team": g.home_team,
        "venue": g.venue,
        "park_factor": g.park_factor,
        "temp_f": g.temp_f,
        "wind_mph": g.wind_mph,
        "dome": g.dome,
        "away_pitcher": g.away_pitcher.name,
        "away_pitcher_1st_era": g.away_pitcher.era_1st,
        "away_pitcher_season_era": g.away_pitcher.season_era,
        "away_pitcher_k_pct": g.away_pitcher.k_pct,
        "home_pitcher": g.home_pitcher.name,
        "home_pitcher_1st_era": g.home_pitcher.era_1st,
        "home_pitcher_season_era": g.home_pitcher.season_era,
        "home_pitcher_k_pct": g.home_pitcher.k_pct,
        "p_nrfi": round(g.p_nrfi, 4),
        "p_yrfi": round(g.p_yrfi, 4),
        "fair_nrfi": g.fair_nrfi,
        "fair_yrfi": g.fair_yrfi,
        "verdict": g.verdict,
        "side": g.side,
        "price": g.chosen_price,
        "book": g.chosen_book,
        "edge": round(g.chosen_edge, 4) if g.chosen_price else 0,
        "kelly_pct": round(g.kelly_pct, 4),
        "stake": round(g.stake, 2),
        "result": None,         # filled in next day
        "runs_in_first": None,  # filled in next day
        "pnl": None,            # filled in next day
    }


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    season = datetime.fromisoformat(target).year

    print(f"=== NRFI Track · {target} ===", file=sys.stderr)

    # 1. Generate today's picks
    print("Generating today's slate...", file=sys.stderr)
    raw = fetch_schedule(target)
    odds = fetch_inning_odds()
    games = []
    for r in raw:
        g = build_game(r, season)
        if g:
            score(g, odds)
            games.append(g)

    today_picks = [serialize_game(g, target) for g in games]
    save_json(TODAY_FILE, today_picks)
    print(f"  Saved {len(today_picks)} picks to {TODAY_FILE}", file=sys.stderr)

    # 2. Append BET picks to history (only ones we'd actually bet)
    history = load_json(HISTORY_FILE, [])
    existing_keys = {f"{p['date']}_{p['game_pk']}" for p in history}
    new_count = 0
    for p in today_picks:
        if not p["verdict"].startswith("BET"):
            continue
        key = f"{p['date']}_{p['game_pk']}"
        if key in existing_keys:
            continue
        history.append(p)
        new_count += 1
    print(f"  Added {new_count} new BET picks to history", file=sys.stderr)

    # 3. Grade ungraded picks from prior days
    print("Grading prior picks...", file=sys.stderr)
    graded, attempted = grade_yesterday(history)
    print(f"  Graded {graded} of {attempted} attempted", file=sys.stderr)

    save_json(HISTORY_FILE, history)

    # 4. Compute and save summary
    summary = compute_summary(history)
    save_json(SUMMARY_FILE, summary)

    print(f"\n--- Track Record ---", file=sys.stderr)
    print(f"  {summary['wins']}-{summary['losses']} ({summary['win_pct']}%) · "
          f"${summary['total_pnl']:+.2f} on ${summary['total_staked']:.2f} staked · "
          f"ROI {summary['roi_pct']:+.2f}%", file=sys.stderr)
    print(f"  NRFI: {summary['nrfi']['wins']}/{summary['nrfi']['n']} "
          f"({summary['nrfi']['win_pct']}%)", file=sys.stderr)
    print(f"  YRFI: {summary['yrfi']['wins']}/{summary['yrfi']['n']} "
          f"({summary['yrfi']['win_pct']}%)", file=sys.stderr)


if __name__ == "__main__":
    main()

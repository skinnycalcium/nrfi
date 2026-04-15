"""
nrfi_daily.py — Automated NRFI scanner for the day's MLB slate.

Pulls schedule + probable pitchers (MLB Stats API), 1st-inning splits,
team hitting, weather (Open-Meteo), and NRFI lines (The Odds API),
runs the Poisson model, and writes an HTML dashboard ranked by edge.

Usage:
    export ODDS_API_KEY="your_key_here"   # optional but recommended
    export BANKROLL=5000                  # for Kelly stake sizing
    python nrfi_daily.py                  # today
    python nrfi_daily.py 2026-04-18       # specific date
"""
from __future__ import annotations

import os
import sys
import math
import json
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Optional

import requests

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
BANKROLL = float(os.getenv("BANKROLL", "5000"))
KELLY_FRACTION = float(os.getenv("KELLY_FRACTION", "0.25"))
EDGE_BET = 0.04
EDGE_LEAN = 0.015

# League averages (refresh annually)
LG_RPI = 0.52
LG_ERA = 4.30
LG_WOBA = 0.320
LG_K_PCT = 22.0
LG_BB_PCT = 8.0

REQUEST_TIMEOUT = 12

# -----------------------------------------------------------------------------
# STADIUM DATA — park factor, lat/lon, dome flag
# Names must match MLB Stats API venue.name exactly.
# -----------------------------------------------------------------------------
STADIUMS = {
    "Coors Field":              {"pf": 1.15, "lat": 39.7559, "lon": -104.9942, "dome": False},
    "Great American Ball Park": {"pf": 1.08, "lat": 39.0975, "lon": -84.5071,  "dome": False},
    "Yankee Stadium":           {"pf": 1.05, "lat": 40.8296, "lon": -73.9262,  "dome": False},
    "Fenway Park":              {"pf": 1.04, "lat": 42.3467, "lon": -71.0972,  "dome": False},
    "Rogers Centre":            {"pf": 1.03, "lat": 43.6414, "lon": -79.3894,  "dome": True},
    "Wrigley Field":            {"pf": 1.02, "lat": 41.9484, "lon": -87.6553,  "dome": False},
    "Chase Field":              {"pf": 1.02, "lat": 33.4453, "lon": -112.0667, "dome": True},
    "Citizens Bank Park":       {"pf": 1.02, "lat": 39.9061, "lon": -75.1665,  "dome": False},
    "Globe Life Field":         {"pf": 1.01, "lat": 32.7474, "lon": -97.0833,  "dome": True},
    "Dodger Stadium":           {"pf": 1.00, "lat": 34.0739, "lon": -118.2400, "dome": False},
    "Truist Park":              {"pf": 1.00, "lat": 33.8908, "lon": -84.4677,  "dome": False},
    "Oriole Park at Camden Yards": {"pf": 1.00, "lat": 39.2839, "lon": -76.6217, "dome": False},
    "Minute Maid Park":         {"pf": 1.00, "lat": 29.7572, "lon": -95.3556,  "dome": True},
    "Daikin Park":              {"pf": 1.00, "lat": 29.7572, "lon": -95.3556,  "dome": True},
    "Angel Stadium":            {"pf": 0.99, "lat": 33.8003, "lon": -117.8827, "dome": False},
    "Busch Stadium":            {"pf": 0.98, "lat": 38.6226, "lon": -90.1928,  "dome": False},
    "American Family Field":    {"pf": 0.98, "lat": 43.0280, "lon": -87.9712,  "dome": True},
    "PNC Park":                 {"pf": 0.97, "lat": 40.4469, "lon": -80.0057,  "dome": False},
    "Target Field":             {"pf": 0.97, "lat": 44.9817, "lon": -93.2776,  "dome": False},
    "Progressive Field":        {"pf": 0.97, "lat": 41.4962, "lon": -81.6852,  "dome": False},
    "Comerica Park":            {"pf": 0.97, "lat": 42.3390, "lon": -83.0485,  "dome": False},
    "Kauffman Stadium":         {"pf": 0.97, "lat": 39.0517, "lon": -94.4803,  "dome": False},
    "Citi Field":               {"pf": 0.96, "lat": 40.7571, "lon": -73.8458,  "dome": False},
    "Nationals Park":           {"pf": 0.96, "lat": 38.8730, "lon": -77.0074,  "dome": False},
    "loanDepot park":           {"pf": 0.95, "lat": 25.7781, "lon": -80.2197,  "dome": True},
    "George M. Steinbrenner Field": {"pf": 0.97, "lat": 27.9803, "lon": -82.5067, "dome": False},  # Rays 2025-26 temp
    "Sutter Health Park":       {"pf": 0.95, "lat": 38.5803, "lon": -121.5135, "dome": False},   # A's temp
    "Petco Park":               {"pf": 0.94, "lat": 32.7073, "lon": -117.1566, "dome": False},
    "T-Mobile Park":            {"pf": 0.93, "lat": 47.5914, "lon": -122.3325, "dome": False},
    "Rate Field":               {"pf": 0.92, "lat": 41.8299, "lon": -87.6338,  "dome": False},
    "Guaranteed Rate Field":    {"pf": 0.92, "lat": 41.8299, "lon": -87.6338,  "dome": False},
    "Oracle Park":              {"pf": 0.90, "lat": 37.7786, "lon": -122.3893, "dome": False},
}
DEFAULT_STADIUM = {"pf": 1.00, "lat": 0.0, "lon": 0.0, "dome": False}

# -----------------------------------------------------------------------------
# MODELS
# -----------------------------------------------------------------------------
@dataclass
class Pitcher:
    id: int
    name: str
    era_1st: float = LG_ERA
    season_era: float = LG_ERA
    k_pct: float = LG_K_PCT
    bb_pct: float = LG_BB_PCT
    starts_with_1st_data: int = 0

@dataclass
class Lineup:
    woba: float = LG_WOBA
    k_pct: float = LG_K_PCT

@dataclass
class Game:
    game_pk: int
    away_team: str
    home_team: str
    venue: str
    park_factor: float
    game_time_iso: str
    away_pitcher: Pitcher
    home_pitcher: Pitcher
    away_lineup: Lineup
    home_lineup: Lineup
    temp_f: float = 72.0
    wind_mph: float = 5.0
    wind_mod: float = 1.0
    dome: bool = False
    market_line: Optional[int] = None
    book: str = ""
    # computed
    xr_away: float = 0.0
    xr_home: float = 0.0
    p_zero_away: float = 0.0
    p_zero_home: float = 0.0
    p_nrfi: float = 0.0
    fair_line: int = 0
    edge: float = 0.0
    kelly_pct: float = 0.0
    stake: float = 0.0
    verdict: str = "NO LINE"

# -----------------------------------------------------------------------------
# DATA FETCH
# -----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

def safe_get(url: str, **kw) -> dict:
    try:
        r = requests.get(url, timeout=REQUEST_TIMEOUT, **kw)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"  ! GET failed: {url[:80]}... ({e})")
        return {}

def fetch_schedule(target_date: str) -> list:
    url = (f"https://statsapi.mlb.com/api/v1/schedule"
           f"?sportId=1&date={target_date}"
           f"&hydrate=probablePitcher,team,venue,linescore")
    data = safe_get(url)
    games = []
    for d in data.get("dates", []):
        games.extend(d.get("games", []))
    return games

def fetch_pitcher_stats(pid: int, season: int) -> dict:
    out = {"era": LG_ERA, "k_pct": LG_K_PCT, "bb_pct": LG_BB_PCT,
           "era_1st": LG_ERA, "starts_1st": 0}
    if not pid:
        return out

    # Season line
    season_url = (f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                  f"?stats=season&group=pitching&season={season}")
    sj = safe_get(season_url)
    for stat in sj.get("stats", []):
        for split in stat.get("splits", []):
            s = split.get("stat", {})
            try:
                out["era"] = float(s.get("era", LG_ERA))
                bf = max(1, int(s.get("battersFaced", 1)))
                out["k_pct"] = round(100 * int(s.get("strikeOuts", 0)) / bf, 1)
                out["bb_pct"] = round(100 * int(s.get("baseOnBalls", 0)) / bf, 1)
            except (ValueError, TypeError):
                pass

    # 1st-inning split (sitCode i01)
    split_url = (f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
                 f"?stats=statSplits&group=pitching&sitCodes=i01&season={season}")
    spj = safe_get(split_url)
    for stat in spj.get("stats", []):
        for split in stat.get("splits", []):
            s = split.get("stat", {})
            try:
                if "era" in s:
                    out["era_1st"] = float(s["era"])
                out["starts_1st"] = int(s.get("gamesStarted", 0))
            except (ValueError, TypeError):
                pass

    # If we got no 1st-inning data (early season), fall back to season ERA
    if out["era_1st"] == LG_ERA and out["era"] != LG_ERA:
        out["era_1st"] = out["era"]

    return out

def fetch_team_lineup_stats(team_id: int, season: int) -> dict:
    """
    Approximate top-of-order quality from team-level hitting.
    True top-3 wOBA ≈ team wOBA + ~0.025 (1-3 hitters typically run hot vs avg).
    """
    out = {"woba": LG_WOBA, "k_pct": LG_K_PCT}
    if not team_id:
        return out
    url = (f"https://statsapi.mlb.com/api/v1/teams/{team_id}/stats"
           f"?stats=season&group=hitting&season={season}")
    j = safe_get(url)
    for stat in j.get("stats", []):
        for split in stat.get("splits", []):
            s = split.get("stat", {})
            try:
                obp = float(s.get("obp", 0.320))
                slg = float(s.get("slg", 0.400))
                # FanGraphs-style wOBA approximation, then bump for top-of-order
                woba_est = (1.7 * obp + slg) / 2.7
                out["woba"] = round(min(0.430, max(0.260, woba_est + 0.025)), 3)
                pa = max(1, int(s.get("plateAppearances", 1)))
                out["k_pct"] = round(100 * int(s.get("strikeOuts", 0)) / pa, 1)
            except (ValueError, TypeError):
                pass
    return out

def fetch_weather(lat: float, lon: float, when_iso: str, dome: bool) -> dict:
    out = {"temp_f": 72.0, "wind_mph": 5.0, "wind_dir": 0.0}
    if dome or (lat == 0 and lon == 0):
        return out
    url = (f"https://api.open-meteo.com/v1/forecast"
           f"?latitude={lat}&longitude={lon}"
           f"&hourly=temperature_2m,wind_speed_10m,wind_direction_10m"
           f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
           f"&forecast_days=3")
    j = safe_get(url)
    hourly = j.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return out
    try:
        target = datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
        # Open-Meteo times are local-naive; treat as UTC offset 0 for matching purposes
        best_idx, best_diff = 0, float("inf")
        for i, t in enumerate(times):
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            diff = abs((dt - target).total_seconds())
            if diff < best_diff:
                best_diff, best_idx = diff, i
        out["temp_f"] = hourly["temperature_2m"][best_idx]
        out["wind_mph"] = hourly["wind_speed_10m"][best_idx]
        out["wind_dir"] = hourly["wind_direction_10m"][best_idx]
    except Exception as e:
        log(f"  ! weather parse: {e}")
    return out

def wind_modifier(mph: float, dome: bool) -> float:
    """
    Without per-park orientation data, we use magnitude only.
    Strong winds increase scoring variance; on average, mild positive bias.
    """
    if dome:
        return 1.00
    if mph >= 18:
        return 1.04
    if mph >= 12:
        return 1.02
    return 1.00

def fetch_nrfi_odds() -> dict:
    """
    Hits The Odds API for first-inning total runs market.
    Returns dict: 'AwayTeam @ HomeTeam' -> {'price': int, 'book': str}
    Picks the BEST (most generous) Under 0.5 price across books.
    """
    out = {}
    if not ODDS_API_KEY:
        log("  ! ODDS_API_KEY not set — skipping odds fetch")
        return out
    events_url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events"
                  f"?apiKey={ODDS_API_KEY}")
    events = safe_get(events_url)
    if not isinstance(events, list):
        return out

    for ev in events:
        eid = ev.get("id")
        away = ev.get("away_team", "")
        home = ev.get("home_team", "")
        if not (eid and away and home):
            continue
        odds_url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{eid}/odds"
                    f"?apiKey={ODDS_API_KEY}&regions=us&markets=totals_1st_1"
                    f"&oddsFormat=american")
        oj = safe_get(odds_url)
        best_price, best_book = None, ""
        for book in oj.get("bookmakers", []):
            for m in book.get("markets", []):
                if m.get("key") != "totals_1st_1":
                    continue
                for o in m.get("outcomes", []):
                    name = (o.get("name") or "").lower()
                    pt = o.get("point", 999)
                    if name == "under" and abs(pt - 0.5) < 0.01:
                        price = o.get("price")
                        if price is not None and (best_price is None or price > best_price):
                            best_price, best_book = price, book.get("title", "")
        if best_price is not None:
            out[f"{away} @ {home}"] = {"price": int(best_price), "book": best_book}
    return out

# -----------------------------------------------------------------------------
# MODEL
# -----------------------------------------------------------------------------
def compute_xr(sp_era1, sp_k, sp_bb, lu_woba, lu_k, park, weather, temp_f):
    p_factor = max(0.4, min(2.0, sp_era1 / LG_ERA))
    lu_factor = max(0.5, min(1.6, lu_woba / LG_WOBA))
    k_prem_p = (sp_k - LG_K_PCT) / 100
    k_prem_l = (lu_k - LG_K_PCT) / 100
    k_adj = max(0.7, min(1.2, 1 - 0.6 * k_prem_p - 0.4 * k_prem_l))
    bb_prem = (sp_bb - LG_BB_PCT) / 100
    bb_adj = max(0.9, min(1.2, 1 + 0.4 * bb_prem))
    temp_adj = 1 + ((temp_f - 70) / 10) * 0.02
    return LG_RPI * p_factor * lu_factor * k_adj * bb_adj * park * weather * temp_adj

def american_to_implied(a: int) -> float:
    return 100 / (a + 100) if a > 0 else abs(a) / (abs(a) + 100)

def prob_to_american(p: float) -> int:
    if p <= 0 or p >= 1:
        return 0
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)

def kelly_fraction(p: float, american: int, frac: float = 0.25) -> float:
    dec = (american / 100 + 1) if american > 0 else (100 / abs(american) + 1)
    b = dec - 1
    full = (b * p - (1 - p)) / b
    return max(0.0, full * frac)

# -----------------------------------------------------------------------------
# ORCHESTRATION
# -----------------------------------------------------------------------------
def build_game(raw: dict, season: int) -> Optional[Game]:
    teams = raw.get("teams", {})
    away_t, home_t = teams.get("away", {}), teams.get("home", {})
    away_team = away_t.get("team", {}).get("name", "")
    home_team = home_t.get("team", {}).get("name", "")
    away_id = away_t.get("team", {}).get("id", 0)
    home_id = home_t.get("team", {}).get("id", 0)
    venue = raw.get("venue", {}).get("name", "")
    away_pp = away_t.get("probablePitcher")
    home_pp = home_t.get("probablePitcher")

    if not (away_pp and home_pp):
        log(f"  - skip {away_team} @ {home_team}: TBD pitcher")
        return None

    a_pid, h_pid = away_pp.get("id"), home_pp.get("id")
    a_name, h_name = away_pp.get("fullName", "TBD"), home_pp.get("fullName", "TBD")
    log(f"  + {a_name:24s} @ {h_name:24s}  ({venue})")

    a_st = fetch_pitcher_stats(a_pid, season)
    h_st = fetch_pitcher_stats(h_pid, season)
    a_lu = fetch_team_lineup_stats(away_id, season)
    h_lu = fetch_team_lineup_stats(home_id, season)

    stadium = STADIUMS.get(venue, DEFAULT_STADIUM)
    wx = fetch_weather(stadium["lat"], stadium["lon"],
                       raw.get("gameDate", ""), stadium["dome"])
    wmod = wind_modifier(wx["wind_mph"], stadium["dome"])

    return Game(
        game_pk=raw.get("gamePk", 0),
        away_team=away_team, home_team=home_team,
        venue=venue, park_factor=stadium["pf"],
        game_time_iso=raw.get("gameDate", ""),
        away_pitcher=Pitcher(id=a_pid, name=a_name,
                             era_1st=a_st["era_1st"], season_era=a_st["era"],
                             k_pct=a_st["k_pct"], bb_pct=a_st["bb_pct"],
                             starts_with_1st_data=a_st["starts_1st"]),
        home_pitcher=Pitcher(id=h_pid, name=h_name,
                             era_1st=h_st["era_1st"], season_era=h_st["era"],
                             k_pct=h_st["k_pct"], bb_pct=h_st["bb_pct"],
                             starts_with_1st_data=h_st["starts_1st"]),
        away_lineup=Lineup(**a_lu),
        home_lineup=Lineup(**h_lu),
        temp_f=wx["temp_f"], wind_mph=wx["wind_mph"], wind_mod=wmod,
        dome=stadium["dome"],
    )

def score(g: Game, odds: dict) -> None:
    g.xr_away = compute_xr(g.home_pitcher.era_1st, g.home_pitcher.k_pct,
                           g.home_pitcher.bb_pct, g.away_lineup.woba,
                           g.away_lineup.k_pct, g.park_factor, g.wind_mod, g.temp_f)
    g.xr_home = compute_xr(g.away_pitcher.era_1st, g.away_pitcher.k_pct,
                           g.away_pitcher.bb_pct, g.home_lineup.woba,
                           g.home_lineup.k_pct, g.park_factor, g.wind_mod, g.temp_f)
    g.p_zero_away = math.exp(-g.xr_away)
    g.p_zero_home = math.exp(-g.xr_home)
    g.p_nrfi = g.p_zero_away * g.p_zero_home
    g.fair_line = prob_to_american(g.p_nrfi)

    market = odds.get(f"{g.away_team} @ {g.home_team}")
    if market:
        g.market_line = market["price"]
        g.book = market["book"]
        implied = american_to_implied(g.market_line)
        g.edge = g.p_nrfi - implied
        g.kelly_pct = kelly_fraction(g.p_nrfi, g.market_line, KELLY_FRACTION)
        g.stake = g.kelly_pct * BANKROLL
        if g.edge >= EDGE_BET:
            g.verdict = "BET"
        elif g.edge >= EDGE_LEAN:
            g.verdict = "LEAN"
        else:
            g.verdict = "PASS"

# -----------------------------------------------------------------------------
# HTML RENDER
# -----------------------------------------------------------------------------
def render_html(games: list, target_date: str) -> str:
    cards = []
    for g in games:
        verdict_class = {
            "BET":  "v-bet",
            "LEAN": "v-lean",
            "PASS": "v-pass",
            "NO LINE": "v-nolne",
        }.get(g.verdict, "v-nolne")

        market_block = ""
        if g.market_line is not None:
            sign = "+" if g.market_line > 0 else ""
            edge_class = "pos" if g.edge >= 0 else "neg"
            edge_sign = "+" if g.edge >= 0 else ""
            stake_str = f"${g.stake:,.0f} ({g.kelly_pct*100:.2f}%)" if g.kelly_pct > 0 else "$0"
            market_block = f"""
              <div class="market">
                <div class="m-row"><span>Market (best Under 0.5)</span><span class="mono">{sign}{g.market_line} · {g.book}</span></div>
                <div class="m-row"><span>Implied prob</span><span class="mono">{american_to_implied(g.market_line)*100:.1f}%</span></div>
                <div class="m-row"><span>Edge</span><span class="mono {edge_class}">{edge_sign}{g.edge*100:.1f}%</span></div>
                <div class="m-row"><span>Kelly stake (¼)</span><span class="mono">{stake_str}</span></div>
              </div>
            """
        else:
            market_block = '<div class="market"><div class="m-row"><span>Market</span><span class="mono dim">no line found</span></div></div>'

        wx_str = "Dome" if g.dome else f"{g.temp_f:.0f}°F · {g.wind_mph:.0f} mph"
        try:
            tlocal = datetime.fromisoformat(g.game_time_iso.replace("Z", "+00:00"))
            time_str = tlocal.strftime("%I:%M %p UTC").lstrip("0")
        except Exception:
            time_str = ""

        cards.append(f"""
        <article class="card">
          <header class="card-head">
            <div class="verdict {verdict_class}">{g.verdict}</div>
            <div class="time mono">{time_str}</div>
          </header>
          <h2 class="matchup"><span class="away-team">{g.away_team}</span> <span class="at">@</span> <span class="home-team">{g.home_team}</span></h2>
          <div class="venue">{g.venue} · PF {g.park_factor:.2f} · {wx_str}</div>

          <div class="big">
            <div class="big-label">P(NRFI)</div>
            <div class="big-val">{g.p_nrfi*100:.1f}%</div>
            <div class="big-sub">Fair: {'+' if g.fair_line > 0 else ''}{g.fair_line}</div>
          </div>

          <div class="grid2">
            <div class="col">
              <div class="col-name">{g.away_pitcher.name}</div>
              <div class="row"><span>1st ERA</span><span class="mono">{g.away_pitcher.era_1st:.2f}</span></div>
              <div class="row"><span>Season ERA</span><span class="mono">{g.away_pitcher.season_era:.2f}</span></div>
              <div class="row"><span>K% / BB%</span><span class="mono">{g.away_pitcher.k_pct:.0f} / {g.away_pitcher.bb_pct:.1f}</span></div>
              <div class="row"><span>vs lineup wOBA</span><span class="mono">{g.home_lineup.woba:.3f}</span></div>
              <div class="row"><span>xR1 (Home)</span><span class="mono">{g.xr_home:.3f}</span></div>
            </div>
            <div class="col">
              <div class="col-name">{g.home_pitcher.name}</div>
              <div class="row"><span>1st ERA</span><span class="mono">{g.home_pitcher.era_1st:.2f}</span></div>
              <div class="row"><span>Season ERA</span><span class="mono">{g.home_pitcher.season_era:.2f}</span></div>
              <div class="row"><span>K% / BB%</span><span class="mono">{g.home_pitcher.k_pct:.0f} / {g.home_pitcher.bb_pct:.1f}</span></div>
              <div class="row"><span>vs lineup wOBA</span><span class="mono">{g.away_lineup.woba:.3f}</span></div>
              <div class="row"><span>xR1 (Away)</span><span class="mono">{g.xr_away:.3f}</span></div>
            </div>
          </div>

          {market_block}
        </article>
        """)

    bet_count = sum(1 for g in games if g.verdict == "BET")
    lean_count = sum(1 for g in games if g.verdict == "LEAN")
    no_line_count = sum(1 for g in games if g.verdict == "NO LINE")

    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>NRFI Daily · {target_date}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@300;400;500;700&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0a0a0a; --panel: #131313; --edge: #1f1f1f;
    --ink: #f4f0e8; --dim: #8a847a; --faint: #4a4641;
    --amber: #f59e0b; --green: #4ade80; --red: #ef4444; --rule: #232323;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--ink);
    font-family: 'DM Sans', sans-serif; padding: 32px 24px 80px;
    background-image:
      radial-gradient(circle at 15% 10%, rgba(245,158,11,0.04) 0%, transparent 40%),
      radial-gradient(circle at 85% 90%, rgba(74,222,128,0.03) 0%, transparent 40%);
  }}
  .wrap {{ max-width: 1400px; margin: 0 auto; }}
  header.top {{ display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 1px solid var(--rule); padding-bottom: 20px; margin-bottom: 32px; }}
  .kicker {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.18em; color: var(--amber); text-transform: uppercase; }}
  h1 {{ font-family: 'Instrument Serif', serif; font-style: italic; font-weight: 400; font-size: 56px; line-height: 1; letter-spacing: -0.02em; margin-top: 4px; }}
  h1 .accent {{ color: var(--amber); }}
  .meta {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--dim); text-align: right; line-height: 1.7; }}

  .summary {{ display: flex; gap: 24px; margin-bottom: 28px; flex-wrap: wrap; }}
  .summary .pill {{ background: var(--panel); border: 1px solid var(--edge); padding: 12px 20px; }}
  .summary .pill .l {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; letter-spacing: 0.18em; color: var(--dim); text-transform: uppercase; }}
  .summary .pill .v {{ font-family: 'JetBrains Mono', monospace; font-size: 24px; margin-top: 4px; }}
  .summary .pill .v.hot {{ color: var(--green); }}
  .summary .pill .v.warn {{ color: var(--amber); }}

  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(420px, 1fr)); gap: 18px; }}

  .card {{ background: var(--panel); border: 1px solid var(--edge); padding: 22px; position: relative; }}
  .card-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
  .verdict {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; letter-spacing: 0.2em; padding: 4px 10px; border: 1px solid; }}
  .v-bet {{ color: var(--green); border-color: var(--green); background: rgba(74,222,128,0.08); }}
  .v-lean {{ color: var(--amber); border-color: var(--amber); background: rgba(245,158,11,0.08); }}
  .v-pass {{ color: var(--red); border-color: var(--red); background: rgba(239,68,68,0.06); }}
  .v-nolne {{ color: var(--dim); border-color: var(--rule); }}
  .time {{ font-size: 11px; color: var(--dim); }}

  .matchup {{ font-family: 'Instrument Serif', serif; font-style: italic; font-size: 28px; line-height: 1.1; font-weight: 400; }}
  .away-team {{ color: #e8d8b8; }}
  .home-team {{ color: #b8d8e8; }}
  .at {{ color: var(--faint); font-style: normal; font-family: 'JetBrains Mono', monospace; font-size: 16px; padding: 0 6px; }}
  .venue {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--dim); margin-top: 6px; padding-bottom: 16px; border-bottom: 1px solid var(--rule); }}

  .big {{ padding: 16px 0; border-bottom: 1px solid var(--rule); margin-bottom: 16px; }}
  .big-label {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; letter-spacing: 0.18em; color: var(--dim); text-transform: uppercase; }}
  .big-val {{ font-family: 'JetBrains Mono', monospace; font-weight: 300; font-size: 48px; line-height: 1; color: var(--amber); margin-top: 4px; }}
  .big-sub {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--dim); margin-top: 4px; }}

  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 16px; }}
  .col-name {{ font-family: 'DM Sans', sans-serif; font-weight: 500; font-size: 13px; color: var(--ink); padding-bottom: 6px; border-bottom: 1px dashed var(--rule); margin-bottom: 6px; }}
  .row {{ display: flex; justify-content: space-between; font-size: 11px; padding: 3px 0; color: var(--dim); }}
  .row .mono {{ color: var(--ink); font-family: 'JetBrains Mono', monospace; }}

  .market {{ padding-top: 14px; border-top: 1px solid var(--rule); }}
  .m-row {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--dim); padding: 4px 0; }}
  .m-row .mono {{ font-family: 'JetBrains Mono', monospace; color: var(--ink); }}
  .m-row .mono.pos {{ color: var(--green); }}
  .m-row .mono.neg {{ color: var(--red); }}
  .m-row .mono.dim {{ color: var(--faint); }}

  .empty {{ padding: 48px; text-align: center; color: var(--dim); border: 1px dashed var(--rule); }}
  .footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid var(--rule); font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--faint); text-align: center; letter-spacing: 0.1em; }}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <div class="kicker">First Inning Edge Engine · Daily Slate</div>
      <h1>NRFI<span class="accent">.</span> {target_date}</h1>
    </div>
    <div class="meta">
      GAMES SCANNED: {len(games)}<br/>
      GENERATED: {generated}<br/>
      MODEL: POISSON · v1.0
    </div>
  </header>

  <div class="summary">
    <div class="pill"><div class="l">Bet</div><div class="v hot">{bet_count}</div></div>
    <div class="pill"><div class="l">Lean</div><div class="v warn">{lean_count}</div></div>
    <div class="pill"><div class="l">No Line</div><div class="v">{no_line_count}</div></div>
    <div class="pill"><div class="l">Bankroll</div><div class="v">${BANKROLL:,.0f}</div></div>
  </div>

  <div class="cards">
    {''.join(cards) if cards else '<div class="empty">No games with confirmed pitchers for this date.</div>'}
  </div>

  <div class="footer">NRFI DAILY · GENERATED LOCALLY · NOT INVESTMENT ADVICE · GAMBLE RESPONSIBLY</div>
</div>
</body>
</html>"""

# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def main():
    target = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    season = datetime.fromisoformat(target).year

    log(f"=== NRFI Daily · {target} ===")
    log("Pulling schedule...")
    raw = fetch_schedule(target)
    log(f"  {len(raw)} games on slate")

    log("Pulling NRFI odds...")
    odds = fetch_nrfi_odds()
    log(f"  {len(odds)} games with NRFI lines")

    log("Building per-game projections...")
    games = []
    for r in raw:
        g = build_game(r, season)
        if g:
            score(g, odds)
            games.append(g)

    # Sort: BET edge desc, then LEAN edge, then by P(NRFI) desc
    rank = {"BET": 0, "LEAN": 1, "PASS": 2, "NO LINE": 3}
    games.sort(key=lambda g: (rank[g.verdict], -g.edge if g.market_line else 0, -g.p_nrfi))

    out_path = f"nrfi_report_{target}.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(render_html(games, target))
    log(f"\nWrote {out_path}")
    log("\nTop plays:")
    for g in games[:8]:
        if g.market_line is not None:
            sign = "+" if g.market_line > 0 else ""
            log(f"  {g.verdict:7s} {g.away_team[:14]:14s} @ {g.home_team[:14]:14s} "
                f"P={g.p_nrfi:.1%}  mkt={sign}{g.market_line}  edge={g.edge:+.1%}")
        else:
            log(f"  {g.verdict:7s} {g.away_team[:14]:14s} @ {g.home_team[:14]:14s} "
                f"P={g.p_nrfi:.1%}  (no line)")

if __name__ == "__main__":
    main()

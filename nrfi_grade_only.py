"""
nrfi_grade_only.py — Just fetches results and grades prior picks.
No new picks generated, no email sent. Runs in the late evening to catch
yesterday's results once all games are final.
"""
from __future__ import annotations

import sys
from datetime import datetime

from nrfi_track import (
    load_json, save_json, grade_yesterday, compute_summary,
    HISTORY_FILE, SUMMARY_FILE,
)


def main():
    print(f"=== NRFI Grade · {datetime.now().isoformat()} ===", file=sys.stderr)
    history = load_json(HISTORY_FILE, [])
    graded, attempted = grade_yesterday(history)
    print(f"  Graded {graded} of {attempted} ungraded picks", file=sys.stderr)

    save_json(HISTORY_FILE, history)
    summary = compute_summary(history)
    save_json(SUMMARY_FILE, summary)

    print(f"\n--- Track Record ---", file=sys.stderr)
    print(f"  {summary['wins']}-{summary['losses']} ({summary['win_pct']}%) · "
          f"${summary['total_pnl']:+.2f} on ${summary['total_staked']:.2f} staked · "
          f"ROI {summary['roi_pct']:+.2f}%", file=sys.stderr)


if __name__ == "__main__":
    main()

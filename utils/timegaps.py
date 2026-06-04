"""Find empty 1-hour buckets in a per-group time series.

Given a DataFrame with a datetime column and a grouping column (e.g.
``fab_name``), locate the 1-hour windows that contain zero rows for each
group. Each group is checked only across its own observed span
``[min_hour, max_hour]`` — leading/trailing gaps relative to some external
schedule are invisible by design, since there is no row to anchor them.

Timestamps are assumed already parsed to a single datetime dtype (tz-aware
KST is the house convention). ``NaT`` timestamps are dropped before
bucketing; rows whose group key is null are dropped by ``groupby`` as usual.
"""

import pandas as pd


def find_hourly_gaps(
    df: pd.DataFrame,
    time_col: str = "timestamp",
    group_col: str = "fab_name",
) -> dict[str, pd.DataFrame]:
    """Return the missing hourly buckets for each group that has any.

    For every distinct value in ``group_col``, build the full hourly grid
    from that group's earliest to latest floored hour and subtract the hours
    that actually carry rows. Groups with full coverage (no gaps) are omitted
    from the result.

    Each value is a DataFrame with columns ``[group_col, "missing_hour"]``,
    one row per empty hour, ``missing_hour`` carrying the same datetime dtype
    (including timezone) as the input.
    """
    hours = df[time_col].dropna().dt.floor("h")
    work = pd.DataFrame({group_col: df.loc[hours.index, group_col], "hour": hours})

    gaps: dict[str, pd.DataFrame] = {}
    for name, group in work.groupby(group_col):
        occupied = pd.DatetimeIndex(group["hour"].unique()).sort_values()
        grid = pd.date_range(occupied.min(), occupied.max(), freq="h")
        missing = grid.difference(occupied)
        if len(missing) == 0:
            continue
        gaps[name] = pd.DataFrame({group_col: name, "missing_hour": missing})
    return gaps


def collapse_gaps(
    gaps_df: pd.DataFrame,
    hour_col: str = "missing_hour",
) -> pd.DataFrame:
    """Fold consecutive missing hours into contiguous ranges.

    Takes one group's gap DataFrame (as produced by :func:`find_hourly_gaps`)
    and collapses runs of adjacent hours into ``[gap_start, gap_end,
    n_hours]`` rows. ``gap_end`` is the *inclusive* last missing hour of the
    run, so a 5-hour hole yields one row with ``n_hours == 5``.
    """
    ordered = gaps_df[hour_col].sort_values().reset_index(drop=True)
    if ordered.empty:
        return pd.DataFrame(columns=["gap_start", "gap_end", "n_hours"])

    # A new run starts wherever the step from the previous hour exceeds 1h.
    new_run = ordered.diff() != pd.Timedelta(hours=1)
    run_id = new_run.cumsum()

    rows = []
    for _, run in ordered.groupby(run_id):
        rows.append(
            {
                "gap_start": run.iloc[0],
                "gap_end": run.iloc[-1],
                "n_hours": len(run),
            }
        )
    return pd.DataFrame(rows)

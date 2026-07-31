"""Render publication frequency from an aggregate daily DA time series."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True, help="aggregate publication_trends CSV")
    parser.add_argument("--output", type=Path, required=True, help="destination PNG")
    parser.add_argument("--label", default="Новосибирск", help="timezone label already used to aggregate input dates")
    args = parser.parse_args()

    counts = Counter()
    with args.input.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            if row.get("effective_label") != "relevant":
                continue
            counts[date.fromisoformat(row["publication_day"])] += int(row["vacancy_count"])
    if not counts:
        raise SystemExit("input CSV contains no relevant publication dates")

    days = []
    current = min(counts)
    last_day = max(counts)
    while current <= last_day:
        days.append(current)
        current += timedelta(days=1)
    values = [counts[day] for day in days]

    figure, axis = plt.subplots(figsize=(16, 8))
    axis.bar(days, values, width=0.8, color="#2f6ea5", edgecolor="#1e4b70", linewidth=0.35)
    axis.set_title("Частота публикации релевантных вакансий")
    axis.set_xlabel(f"Дата публикации ({args.label})")
    axis.set_ylabel("Уникальные вакансии")
    axis.grid(axis="y", alpha=0.25)
    axis.set_axisbelow(True)
    locator = mdates.AutoDateLocator(minticks=6, maxticks=12)
    axis.xaxis.set_major_locator(locator)
    axis.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    axis.set_xlim(days[0] - timedelta(days=1), days[-1] + timedelta(days=1))
    axis.set_ylim(bottom=0)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.tight_layout()
    figure.savefig(args.output, dpi=180, bbox_inches="tight")
    print(args.output)


if __name__ == "__main__":
    main()

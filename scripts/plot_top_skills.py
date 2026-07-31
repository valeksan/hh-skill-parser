"""Render a vertical skill-frequency chart from an aggregate DA CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True, help="aggregate top_skills_rf.csv")
    parser.add_argument("--output", type=Path, required=True, help="destination PNG")
    parser.add_argument("--top", type=int, default=20, help="number of skills to draw")
    args = parser.parse_args()
    if args.top < 1:
        raise SystemExit("--top must be positive")

    with args.input.open(encoding="utf-8", newline="") as file:
        rows = sorted(csv.DictReader(file), key=lambda row: int(row["Count"]), reverse=True)[:args.top]
    if not rows:
        raise SystemExit("input CSV contains no skills")

    skills = [row["Skill"] for row in rows]
    counts = [int(row["Count"]) for row in rows]
    positions = list(range(len(rows)))

    figure, axis = plt.subplots(figsize=(18, 12))
    bars = axis.bar(positions, counts, color="#2f6ea5", width=0.72)
    axis.set_title("Наиболее востребованные навыки в релевантных вакансиях")
    axis.set_ylabel("Число уникальных вакансий")
    axis.set_xticks(positions)
    axis.set_xticklabels(skills, rotation=90, ha="center", va="top", fontsize=10)
    axis.tick_params(axis="x", pad=8)
    axis.set_xlim(-0.6, len(positions) - 0.4)
    axis.grid(axis="y", alpha=0.25)
    axis.set_axisbelow(True)
    for bar, count in zip(bars, counts):
        axis.text(bar.get_x() + bar.get_width() / 2, bar.get_height(), str(count), ha="center", va="bottom", fontsize=9)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    figure.subplots_adjust(bottom=0.42, top=0.93)
    figure.savefig(args.output, dpi=180, bbox_inches="tight")
    print(args.output)


if __name__ == "__main__":
    main()

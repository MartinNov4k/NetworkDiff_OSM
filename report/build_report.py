#!/usr/bin/env python3
"""Vygeneruje HTML přehled změn silniční sítě z CSV výstupů osm_network_diff.py.

Do přehledu vstupují pouze změny hran s confidence == "high".
U maxspeed se počítají jen změny, kde byla hodnota před i po
(doplnění chybějící rychlosti se nevyhodnocuje jako změna).

Použití:
    python report/build_report.py ways_changed.csv nodes_changed.csv \
        turn_restrictions.csv -o report/network_diff_report.html
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

# Hierarchie tříd komunikací od nejvyšší po nejnižší; linky sedí těsně pod rodičem.
HIGHWAY_RANK = [
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "unclassified",
    "residential",
    "living_street",
    "service",
]
RANK = {name: i for i, name in enumerate(HIGHWAY_RANK)}

CATEGORY_LABEL = {
    "classification": "Třída komunikace",
    "name": "Název",
    "speed": "Nejvyšší rychlost",
    "ref": "Číslo silnice",
    "lanes": "Počet jízdních pruhů",
    "oneway": "Jednosměrnost",
    "width": "Šířka",
    "access": "Přístupnost",
    "structure": "Most / tunel",
}


def read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def blank(value: str | None) -> bool:
    return value is None or not value.strip()


def num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_ways(rows: list[dict]) -> dict:
    high_all = [r for r in rows if r["confidence"] == "high"]
    # Doplnění dosud chybějící rychlosti (nebo její smazání) není změnou hodnoty –
    # do přehledu vstupují jen hrany, které měly maxspeed uvedený před i po.
    skipped_speed = [
        r for r in high_all
        if r["category"] == "speed" and (blank(r["speed_kph_old"]) or blank(r["speed_kph_new"]))
    ]
    high = [r for r in high_all if r not in skipped_speed]

    by_category = []
    for key, count in Counter(r["category"] for r in high).most_common():
        length = sum(num(r["length_m"]) or 0.0 for r in high if r["category"] == key)
        by_category.append({
            "key": key,
            "label": CATEGORY_LABEL.get(key, key),
            "count": count,
            "km": round(length / 1000, 1),
        })

    # --- třída komunikace -------------------------------------------------
    cls_rows = [r for r in high if r["category"] == "classification"]
    pair_counts = Counter((r["value_old"], r["value_new"]) for r in cls_rows)
    classes = sorted(
        {v for pair in pair_counts for v in pair},
        key=lambda c: RANK.get(c, len(RANK)),
    )
    matrix = [[pair_counts.get((old, new), 0) for new in classes] for old in classes]

    direction = Counter()
    for (old, new), count in pair_counts.items():
        old_rank, new_rank = RANK.get(old), RANK.get(new)
        if old_rank is None or new_rank is None:
            direction["unknown"] += count
        elif new_rank > old_rank:
            direction["down"] += count
        elif new_rank < old_rank:
            direction["up"] += count
        else:
            direction["same"] += count

    top_pairs = [
        {"old": old, "new": new, "count": count,
         "dir": "down" if RANK.get(new, 0) > RANK.get(old, 0) else "up"}
        for (old, new), count in pair_counts.most_common(12)
    ]

    # --- rychlosti --------------------------------------------------------
    speed_rows = []
    for r in high:
        if r["category"] != "speed":
            continue
        old, new = num(r["speed_kph_old"]), num(r["speed_kph_new"])
        speed_rows.append({
            "id": int(r["way_id"]),
            "name": r["name"] or "",
            "hw": r["highway"],
            "old": int(old),
            "new": int(new),
            "delta": int(new - old),
            "km": round((num(r["length_m"]) or 0.0) / 1000, 2),
        })
    speed_rows.sort(key=lambda r: (r["delta"], -r["km"]))
    transitions = [
        {"old": old, "new": new, "count": count}
        for (old, new), count in sorted(
            Counter((r["old"], r["new"]) for r in speed_rows).items(),
            key=lambda kv: (-kv[1], kv[0]),
        )
    ]
    speed = {
        "rows": speed_rows,
        "transitions": transitions,
        "skipped": len(skipped_speed),
        "slower": sum(1 for r in speed_rows if r["delta"] < 0),
        "faster": sum(1 for r in speed_rows if r["delta"] > 0),
        "km": round(sum(r["km"] for r in speed_rows), 1),
        "levels": sorted({v for r in speed_rows for v in (r["old"], r["new"])}),
    }

    # --- ostatní atributy -------------------------------------------------
    def pairs_for(category: str) -> list[dict]:
        counts = Counter(
            ((r["value_old"] or "—"), (r["value_new"] or "—"))
            for r in high if r["category"] == category
        )
        return [{"old": o, "new": n, "count": c} for (o, n), c in counts.most_common()]

    attrs = {key: pairs_for(key) for key in ("lanes", "oneway", "access", "width", "ref")}

    name_rows = [r for r in high if r["category"] == "name"]
    names = {
        "total": len(name_rows),
        "added": sum(1 for r in name_rows if blank(r["value_old"])),
        "removed": sum(1 for r in name_rows if blank(r["value_new"])),
        "examples": [
            {"id": int(r["way_id"]), "old": r["value_old"] or "—", "new": r["value_new"] or "—"}
            for r in name_rows if not blank(r["value_old"]) and not blank(r["value_new"])
        ][:14],
    }

    table = [
        {
            "id": int(r["way_id"]),
            "cat": r["category"],
            "attr": r["attribute"],
            "old": r["value_old"] or "—",
            "new": r["value_new"] or "—",
            "name": r["name"] or "",
            "hw": r["highway"],
            "km": round((num(r["length_m"]) or 0.0) / 1000, 2),
        }
        for r in high
    ]

    return {
        "total": len(high),
        "total_high": len(high_all),
        "total_all": len(rows),
        "skipped_speed": len(skipped_speed),
        "km": round(sum(num(r["length_m"]) or 0.0 for r in high) / 1000, 1),
        "by_category": by_category,
        "classification": {
            "classes": classes,
            "matrix": matrix,
            "direction": dict(direction),
            "top": top_pairs,
            "total": len(cls_rows),
        },
        "speed": speed,
        "attrs": attrs,
        "names": names,
        "table": table,
    }


def build_nodes(rows: list[dict]) -> dict:
    arms, control = [], []
    for r in rows:
        if r["change_type"] == "node_arms_changed":
            old, new = int(r["value_old"]), int(r["value_new"])
            arms.append({"id": int(r["node_id"]), "old": old, "new": new, "delta": new - old})
        else:
            control.append({
                "id": int(r["node_id"]),
                "attr": r["attribute"],
                "old": r["value_old"] or "—",
                "new": r["value_new"] or "—",
            })

    arm_pairs = Counter((a["old"], a["new"]) for a in arms)
    signals = {
        "removed": sum(1 for c in control if c["old"] == "traffic_signals" and c["new"] == "—"),
        "added": sum(1 for c in control if c["new"] == "traffic_signals" and c["old"] == "—"),
    }
    crossings = {
        "removed": sum(1 for c in control if c["old"] == "crossing" and c["new"] == "—"),
        "added": sum(1 for c in control if c["new"] == "crossing" and c["old"] == "—"),
    }
    return {
        "total": len(rows),
        "arms": {
            "total": len(arms),
            "more": sum(1 for a in arms if a["delta"] > 0),
            "less": sum(1 for a in arms if a["delta"] < 0),
            "pairs": [{"old": o, "new": n, "count": c} for (o, n), c in sorted(arm_pairs.items())],
            "rows": sorted(arms, key=lambda a: (-abs(a["delta"]), a["id"])),
        },
        "control": {
            "total": len(control),
            "signals": signals,
            "crossings": crossings,
            "rows": control,
        },
    }


def build_restrictions(rows: list[dict]) -> dict:
    def kind(value: str) -> str:
        return value.split("=", 1)[1] if "=" in value else (value or "—")

    added = [r for r in rows if r["change_type"] == "restriction_added"]
    removed = [r for r in rows if r["change_type"] == "restriction_removed"]
    changed = [r for r in rows if r["change_type"] == "restriction_changed"]

    types = sorted(
        {kind(r["restriction_new"] or r["restriction_old"]) for r in rows},
        key=lambda t: -sum(
            1 for r in rows if kind(r["restriction_new"] or r["restriction_old"]) == t
        ),
    )
    by_type = [
        {
            "type": t,
            "added": sum(1 for r in added if kind(r["restriction_new"]) == t),
            "removed": sum(1 for r in removed if kind(r["restriction_old"]) == t),
            "changed": sum(1 for r in changed if kind(r["restriction_new"]) == t),
        }
        for t in types
    ]

    retyped = [
        {"id": int(r["rel_id"]), "old": kind(r["restriction_old"]), "new": kind(r["restriction_new"])}
        for r in changed if r["restriction_old"] != r["restriction_new"]
    ]
    except_changed = [
        {"id": int(r["rel_id"]), "type": kind(r["restriction_new"]),
         "old": r["except_old"] or "—", "new": r["except_new"] or "—"}
        for r in changed if r["except_old"] != r["except_new"]
    ]
    return {
        "total": len(rows),
        "added": len(added),
        "removed": len(removed),
        "changed": len(changed),
        "by_type": by_type,
        "retyped": retyped,
        "except_changed": except_changed,
        "geometry_only": len(changed) - len(retyped) - len(except_changed),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ways")
    ap.add_argument("nodes")
    ap.add_argument("restrictions")
    ap.add_argument("-o", "--output", default="report/network_diff_report.html")
    ap.add_argument("--template", default=None)
    args = ap.parse_args()

    data = {
        "generated": date.today().isoformat(),
        "ways": build_ways(read_csv(Path(args.ways))),
        "nodes": build_nodes(read_csv(Path(args.nodes))),
        "restrictions": build_restrictions(read_csv(Path(args.restrictions))),
    }

    template_path = Path(args.template) if args.template else Path(__file__).with_name("template.html")
    html = template_path.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = html.replace("/*__DATA__*/null", payload)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"{out}  ({out.stat().st_size / 1024:.0f} kB)")


if __name__ == "__main__":
    main()

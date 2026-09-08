#!/usr/bin/env python3
"""Vygeneruje HTML přehled změn, které mají dopad na dopravní model.

Ze CSV výstupů osm_network_diff.py projdou jen změny, které mění chování sítě
v přiřazení – jednosměrnost, nejvyšší rychlost, počet pruhů, přístupnost,
řízení křižovatek a zákazy odbočení. Přetagování (třída komunikace, název,
číslo silnice, šířka) a pouhé doplnění dosud chybějícího tagu se vypouští;
kolik toho bylo, se v přehledu uvádí.

Nové a zaniklé komunikace (ways_added / ways_removed) se záměrně neřeší.

Volitelně se ke každé změně připojí historie z OSM (report/osm_history.py),
takže je vidět, kdo a v jakém changesetu ji provedl a jestli šlo o hromadnou
editaci.

Použití:
    python report/build_report.py ways_changed.csv nodes_changed.csv \
        turn_restrictions.csv [--history history.json] \
        -o report/network_diff_report.html
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date
from pathlib import Path

CATEGORY_LABEL = {
    "classification": "třída komunikace",
    "name": "název ulice",
    "ref": "číslo silnice",
    "width": "šířka vozovky",
    "structure": "most / tunel",
    "speed": "nejvyšší rychlost",
    "lanes": "počet jízdních pruhů",
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


def km(row: dict) -> float:
    return round((num(row.get("length_m", "")) or 0.0) / 1000, 2)


# ---------------------------------------------------------------------------
# výběr změn relevantních pro model
# ---------------------------------------------------------------------------

def split_ways(rows: list[dict]) -> tuple[dict, list[dict]]:
    """Rozdělí změny hran na kandidáty pro model a na odfiltrovaný zbytek."""
    high = [r for r in rows if r["confidence"] == "high"]
    dropped: Counter = Counter()

    def drop(row: dict, reason: str) -> None:
        dropped[reason] += 1

    speed, lanes, oneway, access = [], [], [], []
    for r in high:
        cat, old, new = r["category"], r["value_old"], r["value_new"]
        if cat == "speed":
            o, n = num(r["speed_kph_old"]), num(r["speed_kph_new"])
            if o is None or n is None:
                drop(r, "maxspeed jen doplněn nebo smazán")
                continue
            speed.append({**base(r), "old": int(o), "new": int(n), "delta": int(n - o)})
        elif cat == "lanes":
            if blank(old) or blank(new):
                drop(r, "lanes jen doplněny nebo smazány")
                continue
            lanes.append({**base(r), "old": old, "new": new})
        elif cat == "oneway":
            oneway.append({**base(r), "old": old or "—", "new": new or "—",
                           "closed": new.strip() == "yes"})
        elif cat == "access":
            access.append({**base(r), "old": old or "—", "new": new or "—",
                           "blocking": new.strip() in {"no", "private", "destination"}})
        else:
            drop(r, CATEGORY_LABEL.get(cat, cat))

    speed.sort(key=lambda r: (r["delta"], -r["km"]))
    oneway.sort(key=lambda r: (not r["closed"], r["name"] or "￿"))
    lanes.sort(key=lambda r: -r["km"])
    access.sort(key=lambda r: (not r["blocking"], r["name"] or "￿"))

    speed_pack = {
        "rows": speed,
        "transitions": [{"old": o, "new": nn, "count": c} for (o, nn), c in sorted(
            Counter((r["old"], r["new"]) for r in speed).items(), key=lambda kv: (-kv[1], kv[0]))],
        "levels": sorted({v for r in speed for v in (r["old"], r["new"])}),
        "slower": sum(1 for r in speed if r["delta"] < 0),
        "faster": sum(1 for r in speed if r["delta"] > 0),
        "km": round(sum(r["km"] for r in speed), 1),
    }
    candidates = {"speed": speed_pack, "oneway": oneway, "lanes": lanes, "access": access}
    excluded = [{"label": label, "count": count} for label, count in dropped.most_common()]
    return candidates, excluded


def base(r: dict) -> dict:
    return {"id": int(r["way_id"]), "name": r["name"] or "", "hw": r["highway"], "km": km(r)}


def build_nodes(rows: list[dict]) -> dict:
    arms, signals, other = [], [], []
    for r in rows:
        if r["change_type"] == "node_arms_changed":
            old, new = int(r["value_old"]), int(r["value_new"])
            arms.append({"id": int(r["node_id"]), "old": old, "new": new, "delta": new - old})
        elif r["value_old"] == "traffic_signals" or r["value_new"] == "traffic_signals":
            signals.append({"id": int(r["node_id"]),
                            "old": r["value_old"] or "—", "new": r["value_new"] or "—",
                            "added": r["value_new"] == "traffic_signals"})
        else:
            other.append({"id": int(r["node_id"]), "attr": r["attribute"],
                          "old": r["value_old"] or "—", "new": r["value_new"] or "—"})
    arms.sort(key=lambda a: (-abs(a["delta"]), -a["new"]))
    return {
        "arms": {
            "rows": arms,
            "more": sum(1 for a in arms if a["delta"] > 0),
            "less": sum(1 for a in arms if a["delta"] < 0),
            "pairs": [{"old": o, "new": n, "count": c} for (o, n), c in
                      sorted(Counter((a["old"], a["new"]) for a in arms).items(),
                             key=lambda kv: -kv[1])],
        },
        "signals": signals,
        "other": other,
    }


def build_restrictions(rows: list[dict]) -> dict:
    def kind(value: str) -> str:
        return value.split("=", 1)[1] if "=" in value else (value or "—")

    def pack(r: dict, change: str) -> dict:
        return {"id": int(r["rel_id"]), "change": change,
                "type": kind(r["restriction_new"] or r["restriction_old"]),
                "from": r["from_ways"], "via": r["via"], "to": r["to_ways"],
                "except": r["except_new"] or r["except_old"] or ""}

    added = [pack(r, "added") for r in rows if r["change_type"] == "restriction_added"]
    removed = [pack(r, "removed") for r in rows if r["change_type"] == "restriction_removed"]
    changed_rows = [r for r in rows if r["change_type"] == "restriction_changed"]

    retyped = [{**pack(r, "changed"), "old": kind(r["restriction_old"]), "new": kind(r["restriction_new"])}
               for r in changed_rows if r["restriction_old"] != r["restriction_new"]]
    except_changed = [{**pack(r, "changed"), "old": r["except_old"] or "—", "new": r["except_new"] or "—"}
                      for r in changed_rows if r["except_old"] != r["except_new"]]

    types = Counter(r["type"] for r in added + removed)
    by_type = [{"type": t,
                "added": sum(1 for r in added if r["type"] == t),
                "removed": sum(1 for r in removed if r["type"] == t)}
               for t, _ in types.most_common()]
    return {
        "added": added, "removed": removed,
        "retyped": retyped, "except_changed": except_changed,
        "by_type": by_type,
        "changed_members": len(changed_rows) - len(retyped) - len(except_changed),
        "total": len(rows),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ways")
    ap.add_argument("nodes")
    ap.add_argument("restrictions")
    ap.add_argument("--history", help="JSON z report/osm_history.py")
    ap.add_argument("--since", help="datum referenčního stavu (jen do hlavičky)")
    ap.add_argument("-o", "--output", default="report/network_diff_report.html")
    ap.add_argument("--template", default=None)
    args = ap.parse_args()

    way_rows = read_csv(Path(args.ways))
    candidates, excluded = split_ways(way_rows)
    nodes = build_nodes(read_csv(Path(args.nodes)))
    restrictions = build_restrictions(read_csv(Path(args.restrictions)))

    history = {}
    if args.history:
        history = json.loads(Path(args.history).read_text(encoding="utf-8")).get("objects", {})

    n_ways = (len(candidates["speed"]["rows"]) + len(candidates["oneway"])
              + len(candidates["lanes"]) + len(candidates["access"]))
    n_nodes = len(nodes["arms"]["rows"]) + len(nodes["signals"])
    n_rest = len(restrictions["added"]) + len(restrictions["removed"]) \
        + len(restrictions["retyped"]) + len(restrictions["except_changed"])

    data = {
        "generated": date.today().isoformat(),
        "since": args.since,
        "summary": {
            "diff_total": len(way_rows),
            "high": sum(1 for r in way_rows if r["confidence"] == "high"),
            "ways": n_ways, "nodes": n_nodes, "restrictions": n_rest,
            "total": n_ways + n_nodes + n_rest,
            "excluded": excluded,
            "excluded_total": sum(e["count"] for e in excluded),
        },
        "ways": candidates,
        "nodes": nodes,
        "restrictions": restrictions,
        "history": history,
    }

    template_path = Path(args.template) if args.template else Path(__file__).with_name("template.html")
    html = template_path.read_text(encoding="utf-8")
    html = html.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, separators=(",", ":")))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"{out}  ({out.stat().st_size / 1024:.0f} kB)  "
          f"{data['summary']['total']} položek k ověření, "
          f"{data['summary']['excluded_total']} odfiltrováno")


if __name__ == "__main__":
    main()

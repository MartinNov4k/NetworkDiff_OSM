#!/usr/bin/env python3
"""Dotáhne k vybraným změnám historii z OSM API – kdo, kdy a v jakém changesetu.

Diff porovnává dva *stavy* sítě, takže neumí odlišit reálnou změnu v terénu od
přetagování. Tenhle script k jednotlivým objektům stáhne jejich verze
(`/api/0.6/<typ>/<id>/history`), najde changeset, který sledovaný tag skutečně
změnil, a doplní o něm metadata (autor, komentář, editor, velikost changesetu).

Z toho vznikne rozlišení, které v samotném diffu chybí:

* **kampaň** – jeden changeset mění tentýž tag na mnoha objektech po celém
  území; skoro vždy úklid dat, ne změna v terénu,
* **hromadná editace** – changeset s velkým počtem změn,
* **mikrotagování** – StreetComplete / Every Door / MapRoulette; obvykle
  doplnění toho, co v terénu platilo už dřív (u maxspeed ale často jde
  o skutečně odečtenou značku),
* **lokální editace** – malý changeset na jednom místě, nejlepší kandidát na
  reálnou změnu,
* **beze změny tagu** – v historii objektu žádná odpovídající editace není;
  řádek v diffu pak vznikl zpracováním grafu (rozdělení nebo sloučení way),
  ne editací v OSM.

Používá jen standardní knihovnu. Odpovědi se cachují, takže opakovaný běh
nestahuje znovu totéž.

Použití:
    python report/osm_history.py vystup/ways_changed.csv \
        vystup/nodes_changed.csv vystup/turn_restrictions.csv \
        --since 2024-01-01 -o report/history.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from build_report import build_nodes, build_restrictions, read_csv, split_ways  # noqa: E402

API = "https://api.openstreetmap.org/api/0.6"
UA = "NetworkDiff_OSM/1.0 (+https://github.com/MartinNov4k/NetworkDiff_OSM)"

MICRO_EDITORS = ("streetcomplete", "every door", "everydoor", "maproulette", "osmose", "vespucci")

FLAG_LABEL = {
    "campaign": "kampaň",
    "bulk": "hromadná editace",
    "micro": "mikrotagování",
    "local": "lokální editace",
    "no_tag_edit": "beze změny tagu",
    "unknown": "nezjištěno",
}


# ---------------------------------------------------------------------------
# čistá logika (testovatelná bez sítě)
# ---------------------------------------------------------------------------

def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def baseline_index(versions: list[dict], since: datetime) -> int | None:
    """Index poslední verze, která platila k datu referenčního stavu."""
    idx = None
    for i, v in enumerate(versions):
        if parse_ts(v["timestamp"]) <= since:
            idx = i
    return idx


def find_tag_edit(versions: list[dict], since: datetime, attr: str) -> dict | None:
    """Poslední verze po `since`, ve které se hodnota `attr` skutečně změnila.

    Vrací None, pokud se tag v daném období vůbec neměnil – řádek v diffu pak
    nevznikl editací tohoto objektu.
    """
    versions = sorted(versions, key=lambda v: v["version"])
    start = baseline_index(versions, since)
    first = 0 if start is None else start
    hit = None
    for prev, cur in zip(versions[first:], versions[first + 1:]):
        if not cur.get("visible", True):
            hit = cur          # smazání objektu je taky změna
            continue
        if (prev.get("tags") or {}).get(attr) != (cur.get("tags") or {}).get(attr):
            hit = cur
    if hit is None and start is None and versions:
        return versions[0]     # objekt v období vznikl
    return hit


def geometry_changed(versions: list[dict], since: datetime) -> bool:
    versions = sorted(versions, key=lambda v: v["version"])
    start = baseline_index(versions, since)
    if start is None:
        return True
    ref = versions[start].get("nodes")
    for v in versions[start + 1:]:
        if v.get("nodes") != ref:
            return True
    return False


def classify(changeset: dict | None, object_hits: int,
             bulk_threshold: int = 100, campaign_threshold: int = 8) -> str:
    if changeset is None:
        return "unknown"
    if object_hits >= campaign_threshold:
        return "campaign"
    if (changeset.get("changes") or 0) >= bulk_threshold:
        return "bulk"
    editor = (changeset.get("created_by") or "").lower()
    if any(m in editor for m in MICRO_EDITORS):
        return "micro"
    return "local"


# ---------------------------------------------------------------------------
# stahování
# ---------------------------------------------------------------------------

class Api:
    def __init__(self, base: str, cache: Path, sleep: float = 0.4, verbose: bool = True):
        self.base, self.sleep, self.verbose = base.rstrip("/"), sleep, verbose
        self.cache_path = cache
        self.cache = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
        self.fetched = 0

    def get(self, path: str) -> dict | None:
        if path in self.cache:
            return self.cache[path]
        url = f"{self.base}/{path}"
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    payload = None
                    break
                if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError):
                if attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise
        self.cache[path] = payload
        self.fetched += 1
        if self.verbose and self.fetched % 25 == 0:
            print(f"  … {self.fetched} dotazů", file=sys.stderr)
            self.save()
        time.sleep(self.sleep)
        return payload

    def history(self, kind: str, osm_id: int) -> list[dict]:
        payload = self.get(f"{kind}/{osm_id}/history.json")
        return (payload or {}).get("elements", [])

    def changesets(self, ids: list[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        todo = sorted({i for i in ids if i})
        for i in range(0, len(todo), 100):
            batch = todo[i:i + 100]
            payload = self.get("changesets.json?changesets=" + ",".join(map(str, batch)))
            for cs in (payload or {}).get("changesets", []):
                tags = cs.get("tags") or {}
                out[cs["id"]] = {
                    "id": cs["id"], "user": cs.get("user", ""),
                    "created_at": cs.get("created_at", ""),
                    "changes": cs.get("changes_count", 0),
                    "comment": tags.get("comment", ""),
                    "created_by": tags.get("created_by", ""),
                    "source": tags.get("source", ""),
                }
        return out

    def save(self) -> None:
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")


def targets(ways_csv: Path, nodes_csv: Path, rest_csv: Path) -> list[tuple[str, str, int, str]]:
    """Seznam (klíč, typ, id, sledovaný tag) pro objekty relevantní pro model."""
    candidates, _ = split_ways(read_csv(ways_csv))
    nodes = build_nodes(read_csv(nodes_csv))
    rest = build_restrictions(read_csv(rest_csv))

    out: list[tuple[str, str, int, str]] = []
    for r in candidates["speed"]["rows"]:
        out.append((f"way/{r['id']}:maxspeed", "way", r["id"], "maxspeed"))
    for r in candidates["oneway"]:
        out.append((f"way/{r['id']}:oneway", "way", r["id"], "oneway"))
    for r in candidates["lanes"]:
        out.append((f"way/{r['id']}:lanes", "way", r["id"], "lanes"))
    for r in candidates["access"]:
        out.append((f"way/{r['id']}:access", "way", r["id"], "access"))
    for r in nodes["signals"]:
        out.append((f"node/{r['id']}:highway", "node", r["id"], "highway"))
    for r in nodes["arms"]["rows"]:
        out.append((f"node/{r['id']}:arms", "node", r["id"], "highway"))
    for r in (rest["added"] + rest["removed"] + rest["retyped"] + rest["except_changed"]):
        out.append((f"relation/{r['id']}", "relation", r["id"], "restriction"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ways")
    ap.add_argument("nodes")
    ap.add_argument("restrictions")
    ap.add_argument("--since", required=True, help="datum referenčního stavu, YYYY-MM-DD")
    ap.add_argument("-o", "--output", default="report/history.json")
    ap.add_argument("--cache", default="report/.osm_history_cache.json")
    ap.add_argument("--api", default=API)
    ap.add_argument("--sleep", type=float, default=0.4, help="pauza mezi dotazy [s]")
    ap.add_argument("--limit", type=int, help="zpracovat jen prvních N objektů (test)")
    ap.add_argument("--bulk-threshold", type=int, default=100)
    ap.add_argument("--campaign-threshold", type=int, default=8)
    args = ap.parse_args()

    since = parse_ts(args.since if "T" in args.since else args.since + "T00:00:00Z")
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    items = targets(Path(args.ways), Path(args.nodes), Path(args.restrictions))
    if args.limit:
        items = items[:args.limit]
    print(f"Objektů k dotažení: {len(items)}  (referenční stav {since.date()})", file=sys.stderr)

    api = Api(args.api, Path(args.cache), args.sleep)
    objects: dict[str, dict] = {}
    try:
        for key, kind, osm_id, attr in items:
            versions = api.history(kind, osm_id)
            if not versions:
                objects[key] = {"flag": "unknown", "versions_after": 0}
                continue
            after = [v for v in versions if parse_ts(v["timestamp"]) > since]
            edit = find_tag_edit(versions, since, attr)
            objects[key] = {
                "versions_after": len(after),
                "geom_changed": geometry_changed(versions, since) if kind == "way" else None,
                "changeset": edit.get("changeset") if edit else None,
                "user": edit.get("user") if edit else None,
                "ts": edit.get("timestamp") if edit else None,
                "deleted": bool(edit and not edit.get("visible", True)),
                "flag": None if edit else "no_tag_edit",
            }
    except KeyboardInterrupt:
        print("Přerušeno – ukládám, co je hotové.", file=sys.stderr)
    finally:
        api.save()

    cs_ids = [o["changeset"] for o in objects.values() if o.get("changeset")]
    hits: dict[int, int] = {}
    for cid in cs_ids:
        hits[cid] = hits.get(cid, 0) + 1
    print(f"Changesetů k dotažení: {len(set(cs_ids))}", file=sys.stderr)
    changesets = api.changesets(cs_ids)
    api.save()

    for obj in objects.values():
        if obj.get("flag") == "no_tag_edit":
            continue
        cs = changesets.get(obj.get("changeset"))
        obj["flag"] = classify(cs, hits.get(obj.get("changeset"), 0),
                               args.bulk_threshold, args.campaign_threshold)
        if cs:
            obj["comment"] = cs["comment"]
            obj["created_by"] = cs["created_by"]
            obj["changes"] = cs["changes"]

    campaigns = sorted(
        ({"id": cid, "objects": count, **changesets.get(cid, {})}
         for cid, count in hits.items() if count >= args.campaign_threshold),
        key=lambda c: -c["objects"])

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "since": since.isoformat(),
        "objects": objects,
        "campaigns": campaigns,
        "flag_counts": {FLAG_LABEL.get(f, f): sum(1 for o in objects.values() if o.get("flag") == f)
                        for f in FLAG_LABEL},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out}  ({len(objects)} objektů, {len(campaigns)} kampaní)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

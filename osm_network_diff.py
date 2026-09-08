#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
osm_network_diff.py
===================

Porovnání dvou stavů silniční sítě OSM (např. Praha) na úrovni jednotlivých
OSM way ID.  Jako referenční (starý) stav slouží GeoPackage vyexportovaný
z OSMnx (vrstvy ``nodes`` a ``edges``), aktuální stav si script stáhne sám
ve stejném rozsahu (bbox se čte přímo z toho GeoPackage).

Co script hlásí
---------------
1) Změny atributů komunikací (na úrovni OSM way):
   - ``maxspeed``  -> kategorie ``speed``      (změna rychlosti, vč. výpočtu km/h)
   - ``oneway``    -> kategorie ``oneway``     (vznik/zánik jednosměrky)
   - ``highway``   -> kategorie ``classification``
   - ``lanes``, ``access``, ``junction``, ``name``, ``bridge``, ``tunnel``, ``ref``, ``width``, ``area``
2) Nové a zaniklé komunikace (way_added / way_removed).
3) Změny v uzlech (křižovatkách): nový/zrušený semafor, kruhový objezd,
   stopka, dej přednost v jízdě, změna počtu ramen křižovatky.
4) Změny zákazů odbočení (OSM relace ``type=restriction``) přes Overpass API.
   Starý stav se čte z historických ("attic") dat Overpassu k datu snapshotu,
   protože v GeoPackage z OSMnx žádné relace zákazů odbočení nejsou.
5) Změny pruhových tagů řídících pohyby v křižovatce (``turn:lanes`` apod.),
   opět proti historickým datům Overpassu.

Výstupy (adresář ``--outdir``)
------------------------------
- ``zmeny.gpkg``            – vrstvy pro QGIS: ``ways_changed``, ``ways_added``,
                              ``ways_removed``, ``nodes_changed``,
                              ``turn_restrictions``, ``turn_lanes``
- ``*.csv``                 – stejná data jako tabulky (oddělovač ``;``, UTF-8 BOM)
- ``souhrn.md``             – souhrnná statistika

Použití
-------
    pip install -r requirements.txt
    python osm_network_diff.py --baseline osm_snapshot_11_2025.gpkg --outdir vystup

    # bez stahování (porovnání dvou vlastních GPKG):
    python osm_network_diff.py --baseline stary.gpkg --current novy.gpkg --outdir vystup

    # bez Overpassu (jen síť z OSMnx):
    python osm_network_diff.py --baseline osm_snapshot_11_2025.gpkg --no-overpass
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    sys.exit("Chybí pandas.  Nainstaluj: pip install -r requirements.txt")

try:
    import geopandas as gpd
except ImportError:  # pragma: no cover
    sys.exit("Chybí geopandas.  Nainstaluj: pip install -r requirements.txt")

from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.ops import linemerge, unary_union

# ---------------------------------------------------------------------------
# Konfigurace
# ---------------------------------------------------------------------------

#: atributy hran, které se porovnávají (pokud jsou v obou snapshotech)
DEFAULT_ATTRS = [
    "maxspeed",
    "oneway",
    "highway",
    "lanes",
    "junction",
    "access",
    "name",
    "bridge",
    "tunnel",
    "ref",
    "width",
    "area",
]

#: zařazení atributu do tematické kategorie (kvůli filtrování ve výstupu)
ATTR_CATEGORY = {
    "maxspeed": "speed",
    "oneway": "oneway",
    "highway": "classification",
    "lanes": "lanes",
    "junction": "junction",
    "access": "access",
    "name": "name",
    "bridge": "structure",
    "tunnel": "structure",
    "ref": "ref",
    "width": "width",
    "area": "other",
}

#: hodnoty ``highway`` na uzlu, které ovlivňují pohyby v křižovatce
NODE_CONTROL_VALUES = {
    "traffic_signals",
    "stop",
    "give_way",
    "mini_roundabout",
    "turning_circle",
    "turning_loop",
    "crossing",
    "motorway_junction",
}

#: tagy popisující povolené pohyby v křižovatce (dotahují se z Overpassu)
DEFAULT_TURN_TAGS = [
    "turn",
    "turn:lanes",
    "turn:lanes:forward",
    "turn:lanes:backward",
    "turn:lanes:both_ways",
    "oneway:conditional",
    "maxspeed:conditional",
]

OVERPASS_DEFAULT_URL = "https://overpass-api.de/api/interpreter"

#: slovní hodnoty maxspeed -> km/h
SPEED_ZONE_VALUES = {
    "cz:urban": 50.0,
    "cz:rural": 90.0,
    "cz:trunk": 110.0,
    "cz:motorway": 130.0,
    "cz:living_street": 20.0,
    "cz:pedestrian_zone": 20.0,
    "living_street": 20.0,
    "urban": 50.0,
    "rural": 90.0,
    "walk": 5.0,
}

USER_AGENT = "osm_network_diff.py (porovnani zmen site OSM)"


# ---------------------------------------------------------------------------
# Drobné pomůcky
# ---------------------------------------------------------------------------

_T0 = time.time()


def log(msg: str) -> None:
    """Průběžný výpis s časem od startu."""
    print(f"[{time.time() - _T0:7.1f}s] {msg}", flush=True)


def is_missing(value) -> bool:
    """True pro None / NaN / prázdný řetězec / prázdný seznam."""
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple, set)) and len(value) == 0:
        return True
    return False


def to_token_list(value) -> list[str]:
    """
    Rozloží hodnotu atributu na seznam textových tokenů.

    OSMnx u zjednodušených hran ukládá více hodnot jako seznam; v GeoPackage
    je pak uložený jeho textový zápis, např. ``"['50', '30']"``.
    """
    if is_missing(value):
        return []

    if isinstance(value, (list, tuple, set)):
        items = list(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
                items = list(parsed) if isinstance(parsed, (list, tuple, set)) else [parsed]
            except (ValueError, SyntaxError):
                items = [text]
        else:
            items = [text]
    else:
        items = [value]

    out: list[str] = []
    for item in items:
        if is_missing(item):
            continue
        if isinstance(item, (list, tuple, set)):
            out.extend(to_token_list(item))
            continue
        if isinstance(item, bool):
            out.append("yes" if item else "no")
            continue
        if isinstance(item, float) and item.is_integer():
            out.append(str(int(item)))
            continue
        out.append(str(item).strip())
    return [tok for tok in out if tok]


def normalize_attr(attr: str, value) -> str:
    """
    Vrátí kanonickou textovou podobu hodnoty atributu.

    Více hodnot se seřadí a spojí znakem ``|``, aby na pořadí nezáleželo
    (``['50','30']`` a ``['30','50']`` jsou tatáž hodnota).
    """
    tokens = to_token_list(value)

    if attr == "oneway":
        mapped = []
        for tok in tokens:
            low = tok.strip().lower()
            if low in {"true", "1", "yes", "-1"}:
                mapped.append("yes")
            elif low in {"false", "0", "no"}:
                mapped.append("no")
            else:
                mapped.append(low)
        tokens = mapped
    elif attr in {"bridge", "tunnel", "area", "junction", "access", "highway"}:
        tokens = [tok.strip().lower() for tok in tokens]

    return "|".join(sorted(set(tokens)))


def parse_speed_kph(token: str) -> float | None:
    """Převede jednu hodnotu ``maxspeed`` na km/h (None = nelze určit)."""
    text = token.strip().lower()
    if not text or text in {"none", "signals", "variable", "unknown"}:
        return None

    match = re.match(r"^(\d+(?:[.,]\d+)?)\s*(mph|km/?h|kph)?$", text)
    if match:
        value = float(match.group(1).replace(",", "."))
        if match.group(2) == "mph":
            value *= 1.60934
        return round(value, 1)

    if text in SPEED_ZONE_VALUES:
        return SPEED_ZONE_VALUES[text]

    match = re.search(r"zone:?(\d+)", text)
    if match:
        return float(match.group(1))
    return None


def speed_bounds(normalized: str) -> tuple[float | None, float | None]:
    """Z kanonické hodnoty maxspeed vrátí (min km/h, max km/h)."""
    speeds = [s for s in (parse_speed_kph(t) for t in normalized.split("|") if t) if s is not None]
    if not speeds:
        return None, None
    return min(speeds), max(speeds)


def as_multilinestring(geom):
    """Sjednotí geometrii do MultiLineString (kvůli jednotnému typu v GPKG)."""
    if geom is None or geom.is_empty:
        return None
    if isinstance(geom, LineString):
        return MultiLineString([geom])
    if isinstance(geom, MultiLineString):
        return geom
    parts = [g for g in getattr(geom, "geoms", []) if isinstance(g, LineString)]
    return MultiLineString(parts) if parts else None


def bbox_hash(bbox: tuple[float, float, float, float]) -> str:
    return hashlib.md5(("%.6f_%.6f_%.6f_%.6f" % bbox).encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Čtení snapshotu (GeoPackage z OSMnx)
# ---------------------------------------------------------------------------


def list_gpkg_layers(path: Path) -> list[str]:
    try:
        import pyogrio

        return [str(name) for name, _ in pyogrio.list_layers(path)]
    except Exception:
        try:
            import fiona

            return list(fiona.listlayers(str(path)))
        except Exception:
            return []


def pick_layer(layers: list[str], preferred: str, keywords: tuple[str, ...]) -> str:
    if preferred in layers:
        return preferred
    for layer in layers:
        if any(k in layer.lower() for k in keywords):
            return layer
    return preferred


def read_snapshot(path: Path, edges_layer: str | None, nodes_layer: str | None):
    """Načte vrstvy hran a uzlů z GeoPackage."""
    layers = list_gpkg_layers(path)
    edges_name = edges_layer or pick_layer(layers, "edges", ("edge", "hran", "line"))
    nodes_name = nodes_layer or pick_layer(layers, "nodes", ("node", "uzl", "point"))

    edges = gpd.read_file(path, layer=edges_name)
    try:
        nodes = gpd.read_file(path, layer=nodes_name)
    except Exception as exc:
        log(f"  ! vrstvu uzlů '{nodes_name}' se nepodařilo načíst ({exc}); uzly se porovnávat nebudou")
        nodes = gpd.GeoDataFrame(columns=["osmid", "geometry"], geometry="geometry", crs=edges.crs)

    if edges.crs is not None and edges.crs.to_epsg() != 4326:
        edges = edges.to_crs(4326)
    if len(nodes) and nodes.crs is not None and nodes.crs.to_epsg() != 4326:
        nodes = nodes.to_crs(4326)

    missing = [c for c in ("u", "v", "osmid") if c not in edges.columns]
    if missing:
        raise SystemExit(
            f"Vrstva hran '{edges_name}' v {path} nemá sloupce {missing}. "
            "Očekává se GeoPackage vyexportovaný z OSMnx (save_graph_geopackage)."
        )
    return edges, nodes


def bbox_from_gpkg(path: Path, edges: gpd.GeoDataFrame) -> tuple[float, float, float, float]:
    """
    Zjistí rozsah (W, S, E, N).  Primárně z metadat ``gpkg_contents``,
    jinak z obálky geometrií.
    """
    try:
        import sqlite3

        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT min_x, min_y, max_x, max_y FROM gpkg_contents "
                "WHERE min_x IS NOT NULL"
            ).fetchall()
        if rows:
            west = min(r[0] for r in rows)
            south = min(r[1] for r in rows)
            east = max(r[2] for r in rows)
            north = max(r[3] for r in rows)
            return (west, south, east, north)
    except Exception:
        pass

    west, south, east, north = edges.total_bounds
    return (float(west), float(south), float(east), float(north))


# ---------------------------------------------------------------------------
# Stažení aktuálního stavu (OSMnx)
# ---------------------------------------------------------------------------


def detect_graph_options(edges: gpd.GeoDataFrame) -> dict:
    """
    Odhadne, s jakými parametry OSMnx vznikl referenční GeoPackage.

    ``retain_all``  – graf je složen z více nesouvislých komponent
    ``simplify``    – hrany nesou seznam více OSM way ID (znak zjednodušení)

    Aktuální stav se pak stahuje stejně, jinak by u okrajů území vznikaly
    falešné přírůstky a úbytky.
    """
    simplified = bool(edges["osmid"].astype(str).str.startswith("[").any())

    parent: dict = {}

    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for u, v in zip(edges["u"].values, edges["v"].values):
        root_u, root_v = find(u), find(v)
        if root_u != root_v:
            parent[root_u] = root_v

    components = len({find(node) for node in list(parent)})
    return {"retain_all": components > 1, "simplify": simplified, "components": components}


def download_current_snapshot(
    bbox: tuple[float, float, float, float],
    network_type: str,
    simplify: bool,
    retain_all: bool,
    truncate_by_edge: bool,
    out_path: Path,
) -> Path:
    """Stáhne aktuální síť ve stejném rozsahu a uloží ji jako GeoPackage."""
    try:
        import osmnx as ox
    except ImportError:  # pragma: no cover
        raise SystemExit(
            "Chybí osmnx (potřebné pro stažení aktuálního stavu).\n"
            "Nainstaluj: pip install -r requirements.txt\n"
            "Nebo předej vlastní aktuální GeoPackage přes --current."
        )

    west, south, east, north = bbox
    log(
        f"Stahuji aktuální síť z OSM (network_type={network_type}, simplify={simplify}, "
        f"retain_all={retain_all}, truncate_by_edge={truncate_by_edge}) …"
    )
    log(f"  rozsah W={west:.6f} S={south:.6f} E={east:.6f} N={north:.6f}")

    ox.settings.use_cache = True
    ox.settings.log_console = False

    try:
        # OSMnx >= 2.0: bbox = (left, bottom, right, top)
        graph = ox.graph_from_bbox(
            bbox=(west, south, east, north),
            network_type=network_type,
            simplify=simplify,
            truncate_by_edge=truncate_by_edge,
            retain_all=retain_all,
        )
    except TypeError:
        # OSMnx 1.x: samostatné argumenty north, south, east, west
        graph = ox.graph_from_bbox(
            north=north,
            south=south,
            east=east,
            west=west,
            network_type=network_type,
            simplify=simplify,
            truncate_by_edge=truncate_by_edge,
            retain_all=retain_all,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    log(f"Ukládám aktuální stav do {out_path}")
    try:
        ox.io.save_graph_geopackage(graph, filepath=out_path, directed=True)
    except TypeError:
        ox.io.save_graph_geopackage(graph, filepath=out_path)
    return out_path


# ---------------------------------------------------------------------------
# Převod hran na tabulku podle OSM way ID
# ---------------------------------------------------------------------------


def build_way_table(edges: gpd.GeoDataFrame, attrs: list[str], label: str) -> pd.DataFrame:
    """
    Z hran grafu udělá tabulku indexovanou OSM way ID.

    OSMnx při zjednodušení slučuje více way do jedné hrany (``osmid`` je pak
    seznam) a naopak jedna way se dělí na více hran.  Hodnota atributu se
    proto pro každou way skládá takto:

    * pokud existuje hrana tvořená **jen touto** way, berou se hodnoty z ní
      (jednoznačné, ``confident=True``);
    * jinak se použije sjednocení hodnot ze sloučených hran a záznam se
      označí jako nejistý (``confident=False``) – u sloučených hran nelze
      hodnotu spolehlivě přiřadit konkrétní way.
    """
    log(f"Sestavuji tabulku po OSM way ({label}) z {len(edges)} hran …")

    use_attrs = [a for a in attrs if a in edges.columns]
    has_length = "length" in edges.columns
    has_key = "key" in edges.columns

    # hodnoty: way_id -> attr -> {"solo": set(), "merged": set()}
    values: dict[int, dict[str, dict[str, set]]] = {}
    geoms: dict[int, list] = {}
    lengths: dict[int, dict] = {}

    geom_col = edges.geometry.values
    records = edges.drop(columns=[edges.geometry.name]).to_dict("records")

    for idx, row in enumerate(records):
        way_ids = to_token_list(row.get("osmid"))
        if not way_ids:
            continue
        solo = len(way_ids) == 1
        geom = geom_col[idx]

        # klíč pro odstranění duplicit: obousměrná ulice má v grafu dvě
        # opačně orientované hrany se stejnou geometrií
        u_v = tuple(sorted((row.get("u"), row.get("v"))))
        edge_key = (u_v, row.get("key") if has_key else 0, round(float(row.get("length") or 0.0), 2))

        for raw_id in way_ids:
            try:
                way_id = int(raw_id)
            except (TypeError, ValueError):
                continue

            slot = values.setdefault(way_id, {})
            for attr in use_attrs:
                bucket = slot.setdefault(attr, {"solo": set(), "merged": set()})
                normalized = normalize_attr(attr, row.get(attr))
                bucket["solo" if solo else "merged"].add(normalized)

            if geom is not None and not geom.is_empty:
                geoms.setdefault(way_id, []).append(geom)
            if has_length:
                lengths.setdefault(way_id, {})[edge_key] = float(row.get("length") or 0.0)

    log(f"  nalezeno {len(values)} unikátních OSM way")

    rows = []
    for way_id, slot in values.items():
        record: dict = {"way_id": way_id}
        confident = True
        for attr in use_attrs:
            bucket = slot.get(attr, {"solo": set(), "merged": set()})
            source = bucket["solo"] if bucket["solo"] else bucket["merged"]
            if not bucket["solo"] and bucket["merged"]:
                confident = False
            tokens: set[str] = set()
            for value in source:
                tokens.update(t for t in value.split("|") if t)
            record[attr] = "|".join(sorted(tokens))
        record["confident"] = confident
        record["length_m"] = round(sum(lengths.get(way_id, {}).values()), 1)
        record["n_edges"] = len(geoms.get(way_id, []))
        rows.append(record)

    table = pd.DataFrame(rows).set_index("way_id")

    # geometrie way = sjednocení geometrií jejích hran
    merged_geoms = {}
    for way_id, parts in geoms.items():
        try:
            merged = unary_union(parts)
            merged = linemerge(merged) if merged.geom_type == "MultiLineString" else merged
        except Exception:
            merged = parts[0]
        merged_geoms[way_id] = as_multilinestring(merged)
    table["geometry"] = pd.Series(merged_geoms)
    return table


# ---------------------------------------------------------------------------
# Porovnání way
# ---------------------------------------------------------------------------


def tag_summary(row: pd.Series, attrs: list[str]) -> str:
    """Kompaktní přehled tagů pro nové/zaniklé way."""
    parts = []
    for attr in attrs:
        value = row.get(attr)
        if value:
            parts.append(f"{attr}={value}")
    return "; ".join(parts)


def diff_ways(
    old: pd.DataFrame,
    new: pd.DataFrame,
    attrs: list[str],
    inner_bbox,
) -> pd.DataFrame:
    """Porovná dvě tabulky way a vrátí dlouhý seznam změn (řádek = 1 změna)."""
    log("Porovnávám atributy komunikací …")

    compare_attrs = [a for a in attrs if a in old.columns and a in new.columns]
    skipped = [a for a in attrs if a not in compare_attrs]
    if skipped:
        log(f"  (přeskočeno – atribut není v obou snapshotech: {', '.join(skipped)})")

    old_ids = set(old.index)
    new_ids = set(new.index)
    common = old_ids & new_ids

    changes: list[dict] = []

    # --- změny atributů -----------------------------------------------------
    common_index = sorted(common)
    old_common = old.loc[common_index]
    new_common = new.loc[common_index]

    for attr in compare_attrs:
        left = old_common[attr].fillna("")
        right = new_common[attr].fillna("")
        differs = left != right
        if not differs.any():
            continue

        for way_id in left.index[differs]:
            old_value = left.at[way_id]
            new_value = right.at[way_id]
            confident = bool(old_common.at[way_id, "confident"]) and bool(
                new_common.at[way_id, "confident"]
            )

            record = {
                "way_id": int(way_id),
                "change_type": "attr_changed",
                "category": ATTR_CATEGORY.get(attr, "other"),
                "attribute": attr,
                "value_old": old_value,
                "value_new": new_value,
                "name": new_common.at[way_id, "name"] if "name" in new_common.columns else "",
                "highway": new_common.at[way_id, "highway"] if "highway" in new_common.columns else "",
                "length_m": float(new_common.at[way_id, "length_m"]),
                "confidence": "high" if confident else "low",
                "note": "" if confident else "hodnota odvozena ze sloučené hrany – ověřit ručně",
                "geometry": new_common.at[way_id, "geometry"],
            }

            if attr == "maxspeed":
                old_min, old_max = speed_bounds(old_value)
                new_min, new_max = speed_bounds(new_value)
                record["speed_kph_old"] = old_max
                record["speed_kph_new"] = new_max
                if old_max is not None and new_max is not None:
                    record["speed_delta"] = round(new_max - old_max, 1)
                if old_min is not None and new_min is not None and old_min != old_max:
                    record["note"] = (record["note"] + " ").strip() + \
                        f" (rozsah staré {old_min}-{old_max}, nové {new_min}-{new_max} km/h)"
            changes.append(record)

    # --- nové a zaniklé way -------------------------------------------------
    for way_id in sorted(new_ids - old_ids):
        row = new.loc[way_id]
        changes.append(_lifecycle_record(way_id, row, "way_added", compare_attrs, inner_bbox))

    for way_id in sorted(old_ids - new_ids):
        row = old.loc[way_id]
        changes.append(_lifecycle_record(way_id, row, "way_removed", compare_attrs, inner_bbox))

    if not changes:
        return pd.DataFrame(
            columns=[
                "way_id", "change_type", "category", "attribute", "value_old",
                "value_new", "name", "highway", "length_m", "confidence", "note", "geometry",
            ]
        )

    result = pd.DataFrame(changes)
    order = {"speed": 0, "oneway": 1, "junction": 2, "classification": 3, "lanes": 4}
    result["_sort"] = result["category"].map(lambda c: order.get(c, 9))
    result = result.sort_values(["_sort", "category", "way_id"]).drop(columns="_sort")
    return result.reset_index(drop=True)


def _lifecycle_record(way_id, row: pd.Series, change_type: str, attrs: list[str], inner_bbox) -> dict:
    """Řádek pro nově vzniklou / zaniklou way (vč. příznaku okraje území)."""
    geom = row.get("geometry")
    near_boundary = False
    if inner_bbox is not None and geom is not None:
        try:
            near_boundary = not geom.within(inner_bbox)
        except Exception:
            near_boundary = False

    note = ""
    confidence = "high"
    if near_boundary:
        confidence = "low"
        note = "leží u okraje zájmového území – může jít o rozdíl v ořezu, ne o reálnou změnu"
    elif not bool(row.get("confident", True)):
        confidence = "low"
        note = "atributy odvozeny ze sloučené hrany"

    return {
        "way_id": int(way_id),
        "change_type": change_type,
        "category": "topology",
        "attribute": "-",
        "value_old": tag_summary(row, attrs) if change_type == "way_removed" else "",
        "value_new": tag_summary(row, attrs) if change_type == "way_added" else "",
        "name": row.get("name", ""),
        "highway": row.get("highway", ""),
        "length_m": float(row.get("length_m", 0.0) or 0.0),
        "confidence": confidence,
        "note": note,
        "geometry": geom,
    }


# ---------------------------------------------------------------------------
# Porovnání uzlů (křižovatek)
# ---------------------------------------------------------------------------


def diff_nodes(old_nodes: gpd.GeoDataFrame, new_nodes: gpd.GeoDataFrame) -> pd.DataFrame:
    """Změny řízení křižovatek a počtu ramen."""
    if not len(old_nodes) or not len(new_nodes) or "osmid" not in old_nodes.columns:
        return pd.DataFrame()

    log("Porovnávám uzly (křižovatky) …")

    def prep(gdf: gpd.GeoDataFrame) -> pd.DataFrame:
        gdf = gdf.reset_index(drop=True)
        count = len(gdf)
        frame = pd.DataFrame(
            {
                "osmid": pd.to_numeric(gdf["osmid"], errors="coerce").values,
                "control": [normalize_attr("highway", v) for v in gdf["highway"]]
                if "highway" in gdf.columns
                else [""] * count,
                "junction": [normalize_attr("junction", v) for v in gdf["junction"]]
                if "junction" in gdf.columns
                else [""] * count,
                "street_count": pd.to_numeric(gdf["street_count"], errors="coerce").values
                if "street_count" in gdf.columns
                else [float("nan")] * count,
                "geometry": gdf.geometry.values,
            }
        )
        frame = frame.dropna(subset=["osmid"]).drop_duplicates(subset=["osmid"])
        frame["osmid"] = frame["osmid"].astype("int64")
        return frame.set_index("osmid")

    old_frame = prep(old_nodes)
    new_frame = prep(new_nodes)

    common = sorted(set(old_frame.index) & set(new_frame.index))
    rows: list[dict] = []

    for osmid in common:
        old_row = old_frame.loc[osmid]
        new_row = new_frame.loc[osmid]

        if old_row["control"] != new_row["control"]:
            relevant = (set(old_row["control"].split("|")) | set(new_row["control"].split("|"))) & NODE_CONTROL_VALUES
            if relevant:
                rows.append(
                    {
                        "node_id": int(osmid),
                        "change_type": "node_control_changed",
                        "category": "junction_control",
                        "attribute": "highway",
                        "value_old": old_row["control"],
                        "value_new": new_row["control"],
                        "geometry": new_row["geometry"],
                    }
                )

        if old_row["junction"] != new_row["junction"]:
            rows.append(
                {
                    "node_id": int(osmid),
                    "change_type": "node_junction_changed",
                    "category": "junction_control",
                    "attribute": "junction",
                    "value_old": old_row["junction"],
                    "value_new": new_row["junction"],
                    "geometry": new_row["geometry"],
                }
            )

        old_count, new_count = old_row["street_count"], new_row["street_count"]
        if pd.notna(old_count) and pd.notna(new_count) and old_count != new_count:
            rows.append(
                {
                    "node_id": int(osmid),
                    "change_type": "node_arms_changed",
                    "category": "junction_topology",
                    "attribute": "street_count",
                    "value_old": str(int(old_count)),
                    "value_new": str(int(new_count)),
                    "geometry": new_row["geometry"],
                }
            )

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Overpass – zákazy odbočení a pruhové tagy
# ---------------------------------------------------------------------------


class OverpassClientError(RuntimeError):
    """Chyba, kterou nemá smysl opakovat (špatný dotaz, nepodporovaná funkce serveru)."""


def overpass_request(url: str, query: str, cache_file: Path | None, refresh: bool, retries: int = 3) -> dict:
    """Odešle dotaz na Overpass API s jednoduchým cachováním a opakováním."""
    if cache_file and cache_file.exists() and not refresh:
        log(f"  cache: {cache_file.name}")
        return json.loads(cache_file.read_text(encoding="utf-8"))

    try:
        import requests
    except ImportError:  # pragma: no cover
        raise SystemExit("Chybí requests.  Nainstaluj: pip install -r requirements.txt")

    delay = 5
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                url,
                data={"data": query},
                timeout=600,
                headers={"User-Agent": USER_AGENT},
            )
            if response.status_code in (429, 502, 503, 504):
                raise RuntimeError(f"Overpass vrátil {response.status_code} (server je vytížený)")
            if 400 <= response.status_code < 500:
                # typicky chyba v dotazu nebo server neposkytuje historická data
                raise OverpassClientError(
                    f"Overpass vrátil {response.status_code}: {response.text[:300].strip()}"
                )
            response.raise_for_status()
            payload = response.json()
            if cache_file:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(payload), encoding="utf-8")
            return payload
        except OverpassClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - chceme opakovat na cokoliv síťového
            last_error = exc
            if attempt < retries:
                log(f"  ! pokus {attempt}/{retries} selhal ({exc}); čekám {delay}s")
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"Overpass API se nepodařilo zavolat: {last_error}")


def build_restriction_query(bbox, date: str | None, timeout: int = 600) -> str:
    west, south, east, north = bbox
    date_clause = f'[date:"{date}"]' if date else ""
    return (
        f"[out:json][timeout:{timeout}]{date_clause};\n"
        f'relation["type"~"^restriction"]({south},{west},{north},{east});\n'
        f"out body center;"
    )


def build_turn_tag_query(bbox, tags: list[str], date: str | None, timeout: int = 600) -> str:
    west, south, east, north = bbox
    date_clause = f'[date:"{date}"]' if date else ""
    parts = "\n".join(
        f'  way["highway"]["{tag}"]({south},{west},{north},{east});' for tag in tags
    )
    return f"[out:json][timeout:{timeout}]{date_clause};\n(\n{parts}\n);\nout tags center;"


def parse_restrictions(payload: dict) -> dict[int, dict]:
    """Z odpovědi Overpassu udělá slovník relace_id -> popis zákazu odbočení."""
    result: dict[int, dict] = {}
    for element in payload.get("elements", []):
        if element.get("type") != "relation":
            continue
        tags = element.get("tags", {}) or {}
        restriction_tags = {k: v for k, v in tags.items() if k.startswith("restriction")}
        members = element.get("members", []) or []

        def role_ids(role: str) -> list[int]:
            return sorted(m.get("ref") for m in members if m.get("role") == role and m.get("ref") is not None)

        center = element.get("center") or {}
        result[int(element["id"])] = {
            "rel_id": int(element["id"]),
            "restriction": "; ".join(f"{k}={v}" for k, v in sorted(restriction_tags.items())),
            "except": tags.get("except", ""),
            "from_ways": ",".join(str(i) for i in role_ids("from")),
            "via": ",".join(str(i) for i in role_ids("via")),
            "to_ways": ",".join(str(i) for i in role_ids("to")),
            "lat": center.get("lat"),
            "lon": center.get("lon"),
        }
    return result


def restriction_signature(record: dict) -> tuple:
    return (record["restriction"], record["from_ways"], record["via"], record["to_ways"], record["except"])


def diff_restrictions(old: dict[int, dict], new: dict[int, dict]) -> pd.DataFrame:
    """Porovná zákazy odbočení; přečíslované relace se stejným obsahem ignoruje."""
    old_ids, new_ids = set(old), set(new)

    added = {i for i in new_ids - old_ids}
    removed = {i for i in old_ids - new_ids}

    # relace mohla být smazána a znovu založena se stejným obsahem -> není to změna
    added_sig = {restriction_signature(new[i]): i for i in added}
    removed_sig = {restriction_signature(old[i]): i for i in removed}
    for signature in set(added_sig) & set(removed_sig):
        added.discard(added_sig[signature])
        removed.discard(removed_sig[signature])

    rows: list[dict] = []

    for rel_id in sorted(added):
        record = new[rel_id]
        rows.append(_restriction_row(rel_id, "restriction_added", None, record))

    for rel_id in sorted(removed):
        record = old[rel_id]
        rows.append(_restriction_row(rel_id, "restriction_removed", record, None))

    for rel_id in sorted(old_ids & new_ids):
        old_record, new_record = old[rel_id], new[rel_id]
        if restriction_signature(old_record) != restriction_signature(new_record):
            rows.append(_restriction_row(rel_id, "restriction_changed", old_record, new_record))

    return pd.DataFrame(rows)


def _restriction_row(rel_id: int, change_type: str, old_record, new_record) -> dict:
    source = new_record or old_record
    lat, lon = source.get("lat"), source.get("lon")
    return {
        "rel_id": rel_id,
        "change_type": change_type,
        "category": "turn_restriction",
        "restriction_old": (old_record or {}).get("restriction", ""),
        "restriction_new": (new_record or {}).get("restriction", ""),
        "except_old": (old_record or {}).get("except", ""),
        "except_new": (new_record or {}).get("except", ""),
        "from_ways": source.get("from_ways", ""),
        "via": source.get("via", ""),
        "to_ways": source.get("to_ways", ""),
        "geometry": Point(lon, lat) if lat is not None and lon is not None else None,
    }


def parse_way_tags(payload: dict, tags: list[str]) -> dict[int, dict]:
    """Vytáhne sledované tagy way (turn:lanes atd.) z odpovědi Overpassu."""
    result: dict[int, dict] = {}
    for element in payload.get("elements", []):
        if element.get("type") != "way":
            continue
        element_tags = element.get("tags", {}) or {}
        center = element.get("center") or {}
        record = {tag: element_tags.get(tag, "") for tag in tags}
        record["name"] = element_tags.get("name", "")
        record["highway"] = element_tags.get("highway", "")
        record["lat"] = center.get("lat")
        record["lon"] = center.get("lon")
        result[int(element["id"])] = record
    return result


def diff_way_tags(old: dict[int, dict], new: dict[int, dict], tags: list[str]) -> pd.DataFrame:
    rows: list[dict] = []
    for way_id in sorted(set(old) | set(new)):
        old_record = old.get(way_id, {})
        new_record = new.get(way_id, {})
        for tag in tags:
            old_value = (old_record.get(tag) or "").strip()
            new_value = (new_record.get(tag) or "").strip()
            if old_value == new_value:
                continue
            if not old_value:
                change_type = "turn_tag_added"
            elif not new_value:
                change_type = "turn_tag_removed"
            else:
                change_type = "turn_tag_changed"
            source = new_record or old_record
            rows.append(
                {
                    "way_id": way_id,
                    "change_type": change_type,
                    "category": "turn_lanes",
                    "tag": tag,
                    "value_old": old_value,
                    "value_new": new_value,
                    "name": source.get("name", ""),
                    "highway": source.get("highway", ""),
                    "lat": source.get("lat"),
                    "lon": source.get("lon"),
                }
            )
    return pd.DataFrame(rows)


def run_overpass_diff(args, bbox, baseline_date: str, way_geoms: pd.Series):
    """Stáhne a porovná zákazy odbočení a pruhové tagy (starý stav = attic data)."""
    cache_dir = Path(args.cache_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = bbox_hash(bbox)

    restrictions_df = pd.DataFrame()
    turn_tags_df = pd.DataFrame()

    log("Overpass: stahuji zákazy odbočení (aktuální stav) …")
    current_payload = overpass_request(
        args.overpass_url,
        build_restriction_query(bbox, None),
        cache_dir / f"restrictions_{key}_{stamp}.json",
        args.refresh,
    )
    current = parse_restrictions(current_payload)
    log(f"  aktuálně {len(current)} relací type=restriction")

    old = None
    try:
        log(f"Overpass: stahuji zákazy odbočení k datu {baseline_date} (historická data) …")
        old_payload = overpass_request(
            args.overpass_url,
            build_restriction_query(bbox, baseline_date),
            cache_dir / f"restrictions_{key}_{baseline_date[:10]}.json",
            args.refresh,
        )
        old = parse_restrictions(old_payload)
        log(f"  k {baseline_date[:10]} bylo {len(old)} relací type=restriction")
    except Exception as exc:  # noqa: BLE001
        log(f"  ! historická data se nepodařilo získat ({exc})")
        log("    -> porovnání zákazů odbočení se přeskakuje, uloží se jen aktuální stav")

    if old is not None:
        restrictions_df = diff_restrictions(old, current)
    else:
        restrictions_df = pd.DataFrame(
            [
                {**record, "change_type": "restriction_current_state", "category": "turn_restriction",
                 "geometry": Point(record["lon"], record["lat"]) if record["lat"] is not None else None}
                for record in current.values()
            ]
        )

    if args.turn_tags:
        try:
            log("Overpass: stahuji pruhové tagy (turn:lanes …) …")
            current_tags = parse_way_tags(
                overpass_request(
                    args.overpass_url,
                    build_turn_tag_query(bbox, args.turn_tags, None),
                    cache_dir / f"turntags_{key}_{stamp}.json",
                    args.refresh,
                ),
                args.turn_tags,
            )
            old_tags = parse_way_tags(
                overpass_request(
                    args.overpass_url,
                    build_turn_tag_query(bbox, args.turn_tags, baseline_date),
                    cache_dir / f"turntags_{key}_{baseline_date[:10]}.json",
                    args.refresh,
                ),
                args.turn_tags,
            )
            log(f"  aktuálně {len(current_tags)} way, k {baseline_date[:10]} {len(old_tags)} way")
            turn_tags_df = diff_way_tags(old_tags, current_tags, args.turn_tags)
        except Exception as exc:  # noqa: BLE001
            log(f"  ! pruhové tagy se nepodařilo porovnat ({exc})")

    # geometrie: přednostně skutečná geometrie way, jinak střed z Overpassu
    if len(turn_tags_df):
        geoms = []
        for _, row in turn_tags_df.iterrows():
            geom = way_geoms.get(row["way_id"]) if way_geoms is not None else None
            if geom is None and row.get("lat") is not None:
                geom = Point(row["lon"], row["lat"])
            geoms.append(geom)
        turn_tags_df["geometry"] = geoms
        turn_tags_df = turn_tags_df.drop(columns=["lat", "lon"])

    return restrictions_df, turn_tags_df


# ---------------------------------------------------------------------------
# Výstupy
# ---------------------------------------------------------------------------


def write_layer(frame: pd.DataFrame, gpkg_path: Path, layer: str, outdir: Path, csv_sep: str) -> None:
    """Zapíše tabulku do GeoPackage (má-li geometrii) a vždy do CSV."""
    if frame is None or not len(frame):
        log(f"  vrstva '{layer}': žádné záznamy")
        return

    csv_path = outdir / f"{layer}.csv"
    frame.drop(columns=[c for c in ("geometry",) if c in frame.columns]).to_csv(
        csv_path, index=False, sep=csv_sep, encoding="utf-8-sig"
    )

    if "geometry" not in frame.columns:
        log(f"  vrstva '{layer}': {len(frame)} záznamů -> {csv_path.name}")
        return

    geo = frame[frame["geometry"].notna()].copy()
    if not len(geo):
        log(f"  vrstva '{layer}': {len(frame)} záznamů (bez geometrie) -> {csv_path.name}")
        return

    gdf = gpd.GeoDataFrame(geo, geometry="geometry", crs="EPSG:4326")
    for column in gdf.columns:
        if column == "geometry":
            continue
        if gdf[column].dtype == object:
            gdf[column] = gdf[column].astype(str).replace({"None": "", "nan": ""})
    gdf.to_file(gpkg_path, layer=layer, driver="GPKG")
    log(f"  vrstva '{layer}': {len(gdf)} záznamů -> {gpkg_path.name} + {csv_path.name}")


def build_summary(
    way_changes: pd.DataFrame,
    node_changes: pd.DataFrame,
    restrictions: pd.DataFrame,
    turn_tags: pd.DataFrame,
    context: dict,
) -> str:
    lines = ["# Souhrn změn v síti OSM", ""]
    lines.append(f"- referenční stav: `{context['baseline']}`")
    lines.append(f"- aktuální stav: `{context['current']}`")
    lines.append(f"- rozsah (W,S,E,N): {context['bbox']}")
    lines.append(f"- datum referenčního stavu (pro Overpass): {context['baseline_date'] or 'neurčeno'}")
    lines.append(f"- porovnáno OSM way: {context['n_old_ways']} (staré) / {context['n_new_ways']} (nové)")
    lines.append(f"- vygenerováno: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    lines.append("## Změny komunikací podle kategorie")
    lines.append("")
    if len(way_changes):
        table = (
            way_changes.groupby(["category", "change_type", "confidence"]).size().reset_index(name="pocet")
        )
        lines.append("| kategorie | typ změny | spolehlivost | počet |")
        lines.append("|---|---|---|---:|")
        for _, row in table.iterrows():
            lines.append(
                f"| {row['category']} | {row['change_type']} | {row['confidence']} | {row['pocet']} |"
            )
    else:
        lines.append("_žádné změny_")
    lines.append("")

    speed = way_changes[way_changes["category"] == "speed"] if len(way_changes) else pd.DataFrame()
    if len(speed):
        confident = speed[speed["confidence"] == "high"]
        lines.append("## Změny rychlostí – největší rozdíly")
        lines.append("")
        lines.append("| way_id | ulice | z | na | Δ km/h | délka (m) |")
        lines.append("|---|---|---|---|---:|---:|")
        top = confident.copy()
        if "speed_delta" in top.columns:
            top = top.reindex(top["speed_delta"].abs().sort_values(ascending=False).index)
        for _, row in top.head(25).iterrows():
            delta = row.get("speed_delta")
            lines.append(
                f"| {row['way_id']} | {row['name'] or '-'} | {row['value_old'] or '-'} | "
                f"{row['value_new'] or '-'} | {'' if pd.isna(delta) else delta} | {row['length_m']:.0f} |"
            )
        lines.append("")

    oneway = way_changes[way_changes["category"] == "oneway"] if len(way_changes) else pd.DataFrame()
    if len(oneway):
        lines.append("## Změny jednosměrek")
        lines.append("")
        lines.append("| way_id | ulice | z | na | délka (m) |")
        lines.append("|---|---|---|---|---:|")
        for _, row in oneway[oneway["confidence"] == "high"].head(50).iterrows():
            lines.append(
                f"| {row['way_id']} | {row['name'] or '-'} | {row['value_old'] or '-'} | "
                f"{row['value_new'] or '-'} | {row['length_m']:.0f} |"
            )
        lines.append("")

    lines.append("## Křižovatky (uzly)")
    lines.append("")
    if len(node_changes):
        for change_type, count in node_changes["change_type"].value_counts().items():
            lines.append(f"- {change_type}: {count}")
    else:
        lines.append("_žádné změny_")
    lines.append("")

    lines.append("## Zákazy odbočení (OSM relace type=restriction)")
    lines.append("")
    if len(restrictions):
        for change_type, count in restrictions["change_type"].value_counts().items():
            lines.append(f"- {change_type}: {count}")
    else:
        lines.append("_neporovnáno nebo žádné změny_")
    lines.append("")

    lines.append("## Pruhové tagy (turn:lanes apod.)")
    lines.append("")
    if len(turn_tags):
        for change_type, count in turn_tags["change_type"].value_counts().items():
            lines.append(f"- {change_type}: {count}")
    else:
        lines.append("_neporovnáno nebo žádné změny_")
    lines.append("")

    lines.append("## Poznámky k interpretaci")
    lines.append("")
    lines.append(
        "- Záznamy s `confidence=low` vznikly ze sloučených hran (jedna hrana grafu "
        "obsahuje více OSM way) nebo leží u okraje zájmového území; je vhodné je ověřit ručně."
    )
    lines.append(
        "- Porovnává se OSM way ID. Pokud editor way rozdělil nebo sloučil, projeví se to "
        "jako `way_added` + `way_removed`, i když se v terénu nic nezměnilo."
    )
    lines.append(
        "- Zákazy odbočení a pruhové tagy se nečtou z GeoPackage (OSMnx je neexportuje), "
        "ale z historických dat Overpass API k datu `--baseline-date`."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def infer_baseline_date(path: Path) -> str | None:
    """Odhadne datum snapshotu z názvu souboru (např. ``..._11_2025.gpkg``)."""
    stem = path.stem

    match = re.search(r"(20\d\d)[-_](\d{1,2})[-_](\d{1,2})", stem)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}T00:00:00Z"

    match = re.search(r"(?<!\d)(\d{1,2})[-_](20\d\d)(?!\d)", stem)
    if match:
        month, year = match.groups()
        if 1 <= int(month) <= 12:
            return f"{year}-{int(month):02d}-01T00:00:00Z"

    match = re.search(r"(20\d\d)[-_](\d{1,2})(?!\d)", stem)
    if match:
        year, month = match.groups()
        if 1 <= int(month) <= 12:
            return f"{year}-{int(month):02d}-01T00:00:00Z"
    return None


def normalize_date(value: str) -> str:
    """Doplní čas a Z, pokud uživatel zadal jen datum."""
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return f"{text}T00:00:00Z"
    if not text.endswith("Z"):
        return f"{text}Z"
    return text


def inner_bbox_polygon(bbox, buffer_m: float):
    """Zmenší bbox o daný počet metrů (pro detekci okraje území)."""
    if buffer_m <= 0:
        return None
    west, south, east, north = bbox
    lat_mid = (south + north) / 2.0
    d_lat = buffer_m / 111_320.0
    d_lon = buffer_m / (111_320.0 * max(math.cos(math.radians(lat_mid)), 0.1))
    if east - west <= 2 * d_lon or north - south <= 2 * d_lat:
        return None
    return box(west + d_lon, south + d_lat, east - d_lon, north - d_lat)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Porovnání změn silniční sítě OSM (rychlosti, jednosměrky, křižovatkové pohyby).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--baseline", required=True, type=Path, help="GeoPackage se starým stavem (z OSMnx)")
    parser.add_argument("--current", type=Path, help="GeoPackage s aktuálním stavem; bez něj se stáhne z OSM")
    parser.add_argument("--outdir", type=Path, default=Path("vystup"), help="adresář pro výstupy")
    parser.add_argument("--cache-dir", type=Path, default=None, help="adresář pro cache stažených dat")
    parser.add_argument("--refresh", action="store_true", help="ignorovat cache a stáhnout data znovu")
    parser.add_argument("--network-type", default="drive", help="typ sítě pro OSMnx (drive, drive_service, all …)")
    parser.add_argument("--simplify", dest="simplify", action="store_true", default=None,
                        help="vynutit zjednodušení grafu (jinak se převezme z referenčního souboru)")
    parser.add_argument("--no-simplify", dest="simplify", action="store_false", default=None,
                        help="vynutit nezjednodušený graf")
    parser.add_argument("--retain-all", dest="retain_all", action="store_true", default=None,
                        help="ponechat i nesouvislé části sítě (jinak se převezme z referenčního souboru)")
    parser.add_argument("--no-retain-all", dest="retain_all", action="store_false", default=None,
                        help="ponechat jen největší souvislou část sítě")
    parser.add_argument("--truncate-by-edge", action="store_true",
                        help="ponechat celé hrany přesahující rozsah (jako OSMnx truncate_by_edge)")
    parser.add_argument("--bbox", help="vlastní rozsah W,S,E,N (jinak se bere z --baseline)")
    parser.add_argument("--attrs", help="čárkou oddělený seznam porovnávaných atributů")
    parser.add_argument(
        "--baseline-date",
        help="datum starého stavu pro historická data Overpassu (YYYY-MM-DD); jinak se odhadne z názvu souboru",
    )
    parser.add_argument("--no-overpass", dest="overpass", action="store_false",
                        help="vynechat zákazy odbočení a pruhové tagy z Overpassu")
    parser.add_argument("--overpass-url", default=OVERPASS_DEFAULT_URL, help="endpoint Overpass API")
    parser.add_argument("--no-turn-tags", dest="use_turn_tags", action="store_false",
                        help="vynechat porovnání turn:lanes a spol.")
    parser.add_argument("--boundary-buffer", type=float, default=150.0,
                        help="pás u okraje území v metrech, kde se nové/zaniklé way označí jako nejisté")
    parser.add_argument("--edges-layer", help="název vrstvy hran v GeoPackage")
    parser.add_argument("--nodes-layer", help="název vrstvy uzlů v GeoPackage")
    parser.add_argument("--csv-sep", default=";", help="oddělovač v CSV")

    args = parser.parse_args(argv)
    args.turn_tags = list(DEFAULT_TURN_TAGS) if args.use_turn_tags else []
    if args.cache_dir is None:
        args.cache_dir = args.outdir / "cache"
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    if not args.baseline.exists():
        raise SystemExit(f"Soubor {args.baseline} neexistuje.")

    args.outdir.mkdir(parents=True, exist_ok=True)
    attrs = [a.strip() for a in args.attrs.split(",")] if args.attrs else list(DEFAULT_ATTRS)

    # --- 1. starý stav ------------------------------------------------------
    log(f"Načítám referenční stav: {args.baseline}")
    old_edges, old_nodes = read_snapshot(args.baseline, args.edges_layer, args.nodes_layer)
    log(f"  hran: {len(old_edges)}, uzlů: {len(old_nodes)}")

    detected = detect_graph_options(old_edges)
    log(f"  referenční graf: souvislých komponent {detected['components']}, "
        f"zjednodušený: {'ano' if detected['simplify'] else 'ne'}")
    simplify = detected["simplify"] if args.simplify is None else args.simplify
    retain_all = detected["retain_all"] if args.retain_all is None else args.retain_all

    if args.bbox:
        bbox = tuple(float(x) for x in args.bbox.split(","))
        if len(bbox) != 4:
            raise SystemExit("--bbox musí mít tvar W,S,E,N")
    else:
        bbox = bbox_from_gpkg(args.baseline, old_edges)
    log("  rozsah (W,S,E,N): %.6f, %.6f, %.6f, %.6f" % bbox)

    # --- 2. aktuální stav ---------------------------------------------------
    if args.current:
        current_path = args.current
        if not current_path.exists():
            raise SystemExit(f"Soubor {current_path} neexistuje.")
        log(f"Načítám aktuální stav ze souboru: {current_path}")
    else:
        stamp = datetime.now().strftime("%Y%m%d")
        current_path = Path(args.cache_dir) / f"current_{args.network_type}_{bbox_hash(bbox)}_{stamp}.gpkg"
        if current_path.exists() and not args.refresh:
            log(f"Aktuální stav beru z cache: {current_path}")
        else:
            download_current_snapshot(
                bbox, args.network_type, simplify, retain_all, args.truncate_by_edge, current_path
            )

    new_edges, new_nodes = read_snapshot(current_path, args.edges_layer, args.nodes_layer)
    log(f"  hran: {len(new_edges)}, uzlů: {len(new_nodes)}")

    # --- 3. porovnání way ---------------------------------------------------
    old_ways = build_way_table(old_edges, attrs, "referenční stav")
    new_ways = build_way_table(new_edges, attrs, "aktuální stav")

    inner = inner_bbox_polygon(bbox, args.boundary_buffer)
    way_changes = diff_ways(old_ways, new_ways, attrs, inner)
    log(f"  nalezeno {len(way_changes)} změn na komunikacích")

    # --- 4. porovnání uzlů --------------------------------------------------
    node_changes = diff_nodes(old_nodes, new_nodes)
    log(f"  nalezeno {len(node_changes)} změn v uzlech")

    # --- 5. Overpass --------------------------------------------------------
    restrictions = pd.DataFrame()
    turn_tags = pd.DataFrame()
    baseline_date = None
    if args.overpass:
        baseline_date = normalize_date(args.baseline_date) if args.baseline_date else infer_baseline_date(args.baseline)
        if baseline_date:
            if not args.baseline_date:
                log(f"Datum referenčního stavu odhadnuto z názvu souboru: {baseline_date}"
                    " (uprav přes --baseline-date, pokud nesedí)")
            try:
                restrictions, turn_tags = run_overpass_diff(args, bbox, baseline_date, new_ways["geometry"])
            except Exception as exc:  # noqa: BLE001
                log(f"! Overpass část selhala: {exc}")
        else:
            log("! Datum referenčního stavu se nepodařilo odhadnout – zadej --baseline-date YYYY-MM-DD.")
            log("  Zákazy odbočení a pruhové tagy se přeskakují.")

    # --- 6. výstupy ---------------------------------------------------------
    gpkg_path = args.outdir / "zmeny.gpkg"
    if gpkg_path.exists():
        gpkg_path.unlink()

    log(f"Zapisuji výstupy do {args.outdir}")
    if len(way_changes):
        write_layer(way_changes[way_changes["change_type"] == "attr_changed"].reset_index(drop=True),
                    gpkg_path, "ways_changed", args.outdir, args.csv_sep)
        write_layer(way_changes[way_changes["change_type"] == "way_added"].reset_index(drop=True),
                    gpkg_path, "ways_added", args.outdir, args.csv_sep)
        write_layer(way_changes[way_changes["change_type"] == "way_removed"].reset_index(drop=True),
                    gpkg_path, "ways_removed", args.outdir, args.csv_sep)
    write_layer(node_changes, gpkg_path, "nodes_changed", args.outdir, args.csv_sep)
    write_layer(restrictions, gpkg_path, "turn_restrictions", args.outdir, args.csv_sep)
    write_layer(turn_tags, gpkg_path, "turn_lanes", args.outdir, args.csv_sep)

    summary = build_summary(
        way_changes,
        node_changes,
        restrictions,
        turn_tags,
        {
            "baseline": args.baseline,
            "current": current_path,
            "bbox": "%.6f, %.6f, %.6f, %.6f" % bbox,
            "baseline_date": baseline_date,
            "n_old_ways": len(old_ways),
            "n_new_ways": len(new_ways),
        },
    )
    summary_path = args.outdir / "souhrn.md"
    summary_path.write_text(summary, encoding="utf-8")
    log(f"Souhrn: {summary_path}")

    print()
    print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())

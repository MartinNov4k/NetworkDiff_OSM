#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Testy k osm_network_diff.py – běží offline, bez sítě.

    python test_osm_network_diff.py
"""

import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point, box

import osm_network_diff as nd


class TestNormalizace(unittest.TestCase):
    def test_seznam_hodnot_je_nezavisly_na_poradi(self):
        self.assertEqual(nd.normalize_attr("maxspeed", "['50', '30']"), "30|50")
        self.assertEqual(nd.normalize_attr("maxspeed", "['30', '50']"), "30|50")
        self.assertEqual(nd.normalize_attr("maxspeed", ["50", "30"]), "30|50")

    def test_oneway_ruzne_zapisy(self):
        for value in (True, 1, "1", "yes", "True", "-1"):
            self.assertEqual(nd.normalize_attr("oneway", value), "yes", msg=repr(value))
        for value in (False, 0, "0", "no", "False"):
            self.assertEqual(nd.normalize_attr("oneway", value), "no", msg=repr(value))

    def test_prazdne_hodnoty(self):
        for value in (None, float("nan"), "", "  ", []):
            self.assertEqual(nd.normalize_attr("maxspeed", value), "")

    def test_cisla_bez_desetinne_nuly(self):
        self.assertEqual(nd.normalize_attr("lanes", 2.0), "2")

    def test_nazev_si_zachova_velka_pismena(self):
        self.assertEqual(nd.normalize_attr("name", "Vinohradská"), "Vinohradská")


class TestRychlosti(unittest.TestCase):
    def test_ciselne_a_slovni_hodnoty(self):
        self.assertEqual(nd.parse_speed_kph("30"), 30.0)
        self.assertEqual(nd.parse_speed_kph("CZ:urban"), 50.0)
        self.assertEqual(nd.parse_speed_kph("CZ:motorway"), 130.0)
        self.assertEqual(nd.parse_speed_kph("living_street"), 20.0)
        self.assertEqual(nd.parse_speed_kph("DE:zone30"), 30.0)
        self.assertAlmostEqual(nd.parse_speed_kph("30 mph"), 48.3, places=1)
        self.assertIsNone(nd.parse_speed_kph("signals"))

    def test_rozsah_z_vice_hodnot(self):
        self.assertEqual(nd.speed_bounds("30|50"), (30.0, 50.0))
        self.assertEqual(nd.speed_bounds(""), (None, None))


class TestOdhadDataSnapshotu(unittest.TestCase):
    def test_mesic_rok_v_nazvu(self):
        self.assertEqual(
            nd.infer_baseline_date(Path("osm_snapshot_11_2025.gpkg")), "2025-11-01T00:00:00Z"
        )

    def test_plne_datum(self):
        self.assertEqual(
            nd.infer_baseline_date(Path("praha_2025-11-15.gpkg")), "2025-11-15T00:00:00Z"
        )

    def test_neznamy_nazev(self):
        self.assertIsNone(nd.infer_baseline_date(Path("praha.gpkg")))

    def test_normalizace_zadaneho_data(self):
        self.assertEqual(nd.normalize_date("2025-11-01"), "2025-11-01T00:00:00Z")
        self.assertEqual(nd.normalize_date("2025-11-01T12:00:00Z"), "2025-11-01T12:00:00Z")


def make_edges(rows):
    """Postaví minimální vrstvu hran ve tvaru, jaký dělá OSMnx."""
    frame = pd.DataFrame(rows)
    return gpd.GeoDataFrame(frame, geometry="geometry", crs="EPSG:4326")


class TestPorovnaniWay(unittest.TestCase):
    def setUp(self):
        line = LineString([(14.40, 50.08), (14.41, 50.08)])
        line2 = LineString([(14.41, 50.08), (14.42, 50.08)])

        self.old = make_edges(
            [
                dict(u=1, v=2, key=0, osmid="10", highway="residential", maxspeed="50",
                     oneway=False, name="Krátká", length=100.0, geometry=line),
                # opačná hrana téže obousměrné ulice – nesmí zdvojit délku
                dict(u=2, v=1, key=0, osmid="10", highway="residential", maxspeed="50",
                     oneway=False, name="Krátká", length=100.0, geometry=line),
                dict(u=2, v=3, key=0, osmid="20", highway="tertiary", maxspeed="50",
                     oneway=True, name="Dlouhá", length=200.0, geometry=line2),
            ]
        )
        self.new = make_edges(
            [
                dict(u=1, v=2, key=0, osmid="10", highway="residential", maxspeed="30",
                     oneway=True, name="Krátká", length=100.0, geometry=line),
                dict(u=2, v=3, key=0, osmid="30", highway="tertiary", maxspeed="50",
                     oneway=True, name="Nová", length=200.0, geometry=line2),
            ]
        )
        self.attrs = ["maxspeed", "oneway", "highway", "name"]

    def test_delka_se_nezdvojuje(self):
        table = nd.build_way_table(self.old, self.attrs, "test")
        self.assertEqual(table.loc[10, "length_m"], 100.0)

    def test_zmena_rychlosti_a_jednosmerky(self):
        old_table = nd.build_way_table(self.old, self.attrs, "old")
        new_table = nd.build_way_table(self.new, self.attrs, "new")
        changes = nd.diff_ways(old_table, new_table, self.attrs, None)

        speed = changes[(changes.way_id == 10) & (changes.attribute == "maxspeed")].iloc[0]
        self.assertEqual((speed.value_old, speed.value_new), ("50", "30"))
        self.assertEqual(speed.category, "speed")
        self.assertEqual(speed.speed_delta, -20.0)
        self.assertEqual(speed.confidence, "high")

        oneway = changes[(changes.way_id == 10) & (changes.attribute == "oneway")].iloc[0]
        self.assertEqual((oneway.value_old, oneway.value_new), ("no", "yes"))

    def test_nova_a_zanikla_way(self):
        old_table = nd.build_way_table(self.old, self.attrs, "old")
        new_table = nd.build_way_table(self.new, self.attrs, "new")
        changes = nd.diff_ways(old_table, new_table, self.attrs, None)

        self.assertEqual(set(changes[changes.change_type == "way_added"].way_id), {30})
        self.assertEqual(set(changes[changes.change_type == "way_removed"].way_id), {20})

    def test_slouceni_hrany_snizuje_spolehlivost(self):
        line = LineString([(14.40, 50.08), (14.41, 50.08)])
        merged_old = make_edges(
            [dict(u=1, v=2, key=0, osmid="[10, 11]", highway="residential", maxspeed="50",
                  oneway=False, name="Krátká", length=100.0, geometry=line)]
        )
        merged_new = make_edges(
            [dict(u=1, v=2, key=0, osmid="[10, 11]", highway="residential", maxspeed="30",
                  oneway=False, name="Krátká", length=100.0, geometry=line)]
        )
        changes = nd.diff_ways(
            nd.build_way_table(merged_old, self.attrs, "old"),
            nd.build_way_table(merged_new, self.attrs, "new"),
            self.attrs,
            None,
        )
        self.assertEqual(len(changes), 2)  # obě way ze sloučené hrany
        self.assertTrue((changes.confidence == "low").all())

    def test_okraj_uzemi_snizuje_spolehlivost(self):
        old_table = nd.build_way_table(self.old, self.attrs, "old")
        new_table = nd.build_way_table(self.new, self.attrs, "new")
        # vnitřní obálka, do které nová way (14.41–14.42) nespadá
        inner = box(14.395, 50.075, 14.415, 50.085)
        changes = nd.diff_ways(old_table, new_table, self.attrs, inner)
        added = changes[changes.change_type == "way_added"].iloc[0]
        self.assertEqual(added.confidence, "low")
        self.assertIn("okraje", added.note)


class TestPorovnaniUzlu(unittest.TestCase):
    def test_semafor_a_pocet_ramen(self):
        old = gpd.GeoDataFrame(
            {
                "osmid": [1, 2, 3],
                "highway": [None, "traffic_signals", None],
                "street_count": [4, 4, 3],
                "geometry": [Point(14.4, 50.08), Point(14.41, 50.08), Point(14.42, 50.08)],
            },
            crs="EPSG:4326",
        )
        new = gpd.GeoDataFrame(
            {
                "osmid": [1, 2, 3],
                "highway": ["traffic_signals", None, None],
                "street_count": [4, 4, 5],
                "geometry": [Point(14.4, 50.08), Point(14.41, 50.08), Point(14.42, 50.08)],
            },
            crs="EPSG:4326",
        )
        changes = nd.diff_nodes(old, new)
        by_node = {int(r.node_id): r.change_type for _, r in changes.iterrows()}
        self.assertEqual(by_node[1], "node_control_changed")   # přibyl semafor
        self.assertEqual(by_node[2], "node_control_changed")   # semafor zmizel
        self.assertEqual(by_node[3], "node_arms_changed")      # přibylo rameno


class TestZakazyOdboceni(unittest.TestCase):
    def payload(self, relations):
        return {"elements": relations}

    def relation(self, rel_id, restriction, from_way, via, to_way, extra=None):
        tags = {"type": "restriction", "restriction": restriction}
        tags.update(extra or {})
        return {
            "type": "relation",
            "id": rel_id,
            "tags": tags,
            "members": [
                {"type": "way", "ref": from_way, "role": "from"},
                {"type": "node", "ref": via, "role": "via"},
                {"type": "way", "ref": to_way, "role": "to"},
            ],
            "center": {"lat": 50.08, "lon": 14.42},
        }

    def test_parsovani(self):
        parsed = nd.parse_restrictions(self.payload([self.relation(1, "no_left_turn", 10, 100, 20)]))
        self.assertEqual(parsed[1]["restriction"], "restriction=no_left_turn")
        self.assertEqual(parsed[1]["from_ways"], "10")
        self.assertEqual(parsed[1]["via"], "100")
        self.assertEqual(parsed[1]["to_ways"], "20")

    def test_pridany_zruseny_zmeneny(self):
        old = nd.parse_restrictions(
            self.payload([
                self.relation(1, "no_left_turn", 10, 100, 20),
                self.relation(2, "no_u_turn", 30, 300, 40),
            ])
        )
        new = nd.parse_restrictions(
            self.payload([
                self.relation(1, "no_entry", 10, 100, 20),   # změna typu zákazu
                self.relation(3, "no_right_turn", 50, 500, 60),  # nový
            ])
        )
        changes = nd.diff_restrictions(old, new)
        by_id = {int(r.rel_id): r.change_type for _, r in changes.iterrows()}
        self.assertEqual(by_id[1], "restriction_changed")
        self.assertEqual(by_id[2], "restriction_removed")
        self.assertEqual(by_id[3], "restriction_added")

        changed = changes[changes.rel_id == 1].iloc[0]
        self.assertEqual(changed.restriction_old, "restriction=no_left_turn")
        self.assertEqual(changed.restriction_new, "restriction=no_entry")
        self.assertIsInstance(changed.geometry, Point)

    def test_precislovana_relace_neni_zmena(self):
        """Smazání a znovuzaložení stejného zákazu pod jiným ID se nehlásí."""
        old = nd.parse_restrictions(self.payload([self.relation(1, "no_left_turn", 10, 100, 20)]))
        new = nd.parse_restrictions(self.payload([self.relation(9, "no_left_turn", 10, 100, 20)]))
        self.assertEqual(len(nd.diff_restrictions(old, new)), 0)

    def test_dotaz_obsahuje_datum(self):
        bbox = (14.2, 49.9, 14.7, 50.2)
        query = nd.build_restriction_query(bbox, "2025-11-01T00:00:00Z")
        self.assertIn('[date:"2025-11-01T00:00:00Z"]', query)
        self.assertIn("49.9,14.2,50.2,14.7", query)          # pořadí S,W,N,E
        self.assertNotIn("[date:", nd.build_restriction_query(bbox, None))


class TestPruhoveTagy(unittest.TestCase):
    def test_diff_turn_lanes(self):
        tags = ["turn:lanes"]
        payload_old = {
            "elements": [
                {"type": "way", "id": 1, "tags": {"highway": "primary", "name": "A", "turn:lanes": "left|through"},
                 "center": {"lat": 50.08, "lon": 14.42}},
                {"type": "way", "id": 2, "tags": {"highway": "primary", "name": "B", "turn:lanes": "through"},
                 "center": {"lat": 50.08, "lon": 14.43}},
            ]
        }
        payload_new = {
            "elements": [
                {"type": "way", "id": 1, "tags": {"highway": "primary", "name": "A", "turn:lanes": "left|through|right"},
                 "center": {"lat": 50.08, "lon": 14.42}},
                {"type": "way", "id": 3, "tags": {"highway": "primary", "name": "C", "turn:lanes": "right"},
                 "center": {"lat": 50.08, "lon": 14.44}},
            ]
        }
        old = nd.parse_way_tags(payload_old, tags)
        new = nd.parse_way_tags(payload_new, tags)
        changes = nd.diff_way_tags(old, new, tags)
        by_way = {int(r.way_id): r.change_type for _, r in changes.iterrows()}
        self.assertEqual(by_way[1], "turn_tag_changed")
        self.assertEqual(by_way[2], "turn_tag_removed")
        self.assertEqual(by_way[3], "turn_tag_added")

    def test_dotaz_na_pruhove_tagy(self):
        query = nd.build_turn_tag_query((14.2, 49.9, 14.7, 50.2), ["turn:lanes", "turn"], "2025-11-01T00:00:00Z")
        self.assertIn('way["highway"]["turn:lanes"]', query)
        self.assertIn('way["highway"]["turn"]', query)
        self.assertIn('[date:"2025-11-01T00:00:00Z"]', query)


class TestPomocne(unittest.TestCase):
    def test_vnitrni_obalka(self):
        inner = nd.inner_bbox_polygon((14.2, 49.9, 14.7, 50.2), 150.0)
        west, south, east, north = inner.bounds
        self.assertGreater(west, 14.2)
        self.assertLess(east, 14.7)
        self.assertIsNone(nd.inner_bbox_polygon((14.2, 49.9, 14.7, 50.2), 0))

    def test_prilis_velky_buffer_vrati_none(self):
        self.assertIsNone(nd.inner_bbox_polygon((14.40, 50.08, 14.401, 50.081), 150.0))


class TestDetekceParametruGrafu(unittest.TestCase):
    def frame(self, rows):
        return gpd.GeoDataFrame(
            pd.DataFrame(rows), geometry="geometry", crs="EPSG:4326"
        )

    def test_souvisly_zjednoduseny_graf(self):
        line = LineString([(14.40, 50.08), (14.41, 50.08)])
        edges = self.frame(
            [
                dict(u=1, v=2, osmid="[10, 11]", geometry=line),
                dict(u=2, v=3, osmid="12", geometry=line),
            ]
        )
        options = nd.detect_graph_options(edges)
        self.assertEqual(options["components"], 1)
        self.assertFalse(options["retain_all"])
        self.assertTrue(options["simplify"])

    def test_nesouvisly_nezjednoduseny_graf(self):
        line = LineString([(14.40, 50.08), (14.41, 50.08)])
        edges = self.frame(
            [
                dict(u=1, v=2, osmid="10", geometry=line),
                dict(u=8, v=9, osmid="11", geometry=line),
            ]
        )
        options = nd.detect_graph_options(edges)
        self.assertEqual(options["components"], 2)
        self.assertTrue(options["retain_all"])
        self.assertFalse(options["simplify"])


class TestPrepinace(unittest.TestCase):
    def test_trojstavove_prepinace(self):
        self.assertIsNone(nd.parse_args(["--baseline", "x.gpkg"]).simplify)
        self.assertIsNone(nd.parse_args(["--baseline", "x.gpkg"]).retain_all)
        self.assertFalse(nd.parse_args(["--baseline", "x.gpkg", "--no-simplify"]).simplify)
        self.assertTrue(nd.parse_args(["--baseline", "x.gpkg", "--retain-all"]).retain_all)

    def test_vypnuti_overpassu(self):
        args = nd.parse_args(["--baseline", "x.gpkg", "--no-overpass", "--no-turn-tags"])
        self.assertFalse(args.overpass)
        self.assertEqual(args.turn_tags, [])


class FakeOverpassHandler(BaseHTTPRequestHandler):
    """Minimální náhrada Overpass API pro test bez internetu."""

    RESTRICTIONS_NOW = [
        {"type": "relation", "id": 1, "tags": {"type": "restriction", "restriction": "no_left_turn"},
         "members": [{"type": "way", "ref": 10, "role": "from"},
                     {"type": "node", "ref": 100, "role": "via"},
                     {"type": "way", "ref": 20, "role": "to"}],
         "center": {"lat": 50.08, "lon": 14.42}},
        {"type": "relation", "id": 3, "tags": {"type": "restriction", "restriction": "no_right_turn"},
         "members": [{"type": "way", "ref": 50, "role": "from"},
                     {"type": "node", "ref": 500, "role": "via"},
                     {"type": "way", "ref": 60, "role": "to"}],
         "center": {"lat": 50.09, "lon": 14.43}},
    ]
    RESTRICTIONS_OLD = [RESTRICTIONS_NOW[0]]

    TURN_NOW = [{"type": "way", "id": 10, "tags": {"highway": "primary", "name": "A",
                                                   "turn:lanes": "left|through|right"},
                 "center": {"lat": 50.08, "lon": 14.42}}]
    TURN_OLD = [{"type": "way", "id": 10, "tags": {"highway": "primary", "name": "A",
                                                   "turn:lanes": "left|through"},
                 "center": {"lat": 50.08, "lon": 14.42}}]

    requests_seen: list = []

    def do_POST(self):  # noqa: N802 - vyžadováno BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", 0))
        query = urllib.parse.parse_qs(self.rfile.read(length).decode())["data"][0]
        FakeOverpassHandler.requests_seen.append(query)

        historical = "[date:" in query
        if "type\"~\"^restriction" in query:
            elements = self.RESTRICTIONS_OLD if historical else self.RESTRICTIONS_NOW
        else:
            elements = self.TURN_OLD if historical else self.TURN_NOW

        body = json.dumps({"elements": elements}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # ticho v testech
        pass


class TestOverpassProtiFalesnemuServeru(unittest.TestCase):
    """Ověří celý řetězec dotaz -> cache -> parsování -> tabulka změn."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeOverpassHandler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/api/interpreter"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeOverpassHandler.requests_seen.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_args(self):
        return nd.parse_args(
            [
                "--baseline", "x.gpkg",
                "--outdir", self.tmp.name,
                "--cache-dir", str(Path(self.tmp.name) / "cache"),
                "--overpass-url", self.url,
            ]
        )

    def test_diff_zakazu_a_pruhovych_tagu(self):
        args = self.make_args()
        bbox = (14.40, 50.07, 14.45, 50.10)
        restrictions, turn_tags = nd.run_overpass_diff(
            args, bbox, "2025-11-01T00:00:00Z", pd.Series(dtype=object)
        )

        by_id = {int(r.rel_id): r.change_type for _, r in restrictions.iterrows()}
        self.assertEqual(by_id, {3: "restriction_added"})

        self.assertEqual(len(turn_tags), 1)
        row = turn_tags.iloc[0]
        self.assertEqual(row.change_type, "turn_tag_changed")
        self.assertEqual(row.value_old, "left|through")
        self.assertEqual(row.value_new, "left|through|right")

        # dva dotazy na zákazy (dnes + historie) a dva na pruhové tagy
        self.assertEqual(len(FakeOverpassHandler.requests_seen), 4)
        self.assertEqual(sum("[date:" in q for q in FakeOverpassHandler.requests_seen), 2)

    def test_cache_setri_dotazy(self):
        args = self.make_args()
        bbox = (14.40, 50.07, 14.45, 50.10)
        nd.run_overpass_diff(args, bbox, "2025-11-01T00:00:00Z", pd.Series(dtype=object))
        self.assertEqual(len(FakeOverpassHandler.requests_seen), 4)

        nd.run_overpass_diff(args, bbox, "2025-11-01T00:00:00Z", pd.Series(dtype=object))
        self.assertEqual(len(FakeOverpassHandler.requests_seen), 4)  # nic navíc, čte se cache

        args.refresh = True
        nd.run_overpass_diff(args, bbox, "2025-11-01T00:00:00Z", pd.Series(dtype=object))
        self.assertEqual(len(FakeOverpassHandler.requests_seen), 8)  # --refresh cache obchází

    def test_zapis_vrstev(self):
        args = self.make_args()
        bbox = (14.40, 50.07, 14.45, 50.10)
        restrictions, turn_tags = nd.run_overpass_diff(
            args, bbox, "2025-11-01T00:00:00Z", pd.Series(dtype=object)
        )
        outdir = Path(self.tmp.name)
        gpkg = outdir / "zmeny.gpkg"
        nd.write_layer(restrictions, gpkg, "turn_restrictions", outdir, ";")
        nd.write_layer(turn_tags, gpkg, "turn_lanes", outdir, ";")

        self.assertTrue((outdir / "turn_restrictions.csv").exists())
        written = gpd.read_file(gpkg, layer="turn_restrictions")
        self.assertEqual(len(written), 1)
        self.assertEqual(written.crs.to_epsg(), 4326)


class TestChybyOverpassu(unittest.TestCase):
    """Chyba 4xx se neopakuje, chyba 5xx ano."""

    class Handler(BaseHTTPRequestHandler):
        status = 400
        hits = 0

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            type(self).hits += 1
            body = b"line 1: parse error"
            self.send_response(type(self).status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), self.Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/interpreter"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.Handler.hits = 0

    def test_chyba_dotazu_se_neopakuje(self):
        self.Handler.status = 400
        with self.assertRaises(nd.OverpassClientError):
            nd.overpass_request(self.url, "dotaz", None, refresh=True, retries=3)
        self.assertEqual(self.Handler.hits, 1)

    def test_pretizeny_server_se_zkusi_znovu(self):
        self.Handler.status = 504
        with self.assertRaises(RuntimeError):
            nd.overpass_request(self.url, "dotaz", None, refresh=True, retries=2)
        self.assertEqual(self.Handler.hits, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

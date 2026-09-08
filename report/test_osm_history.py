#!/usr/bin/env python3
"""Testy vyhodnocování historie OSM. Běží offline – síť se nepoužívá."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from osm_history import (baseline_index, classify, find_tag_edit,  # noqa: E402
                         geometry_changed, parse_ts)

SINCE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def v(version: int, ts: str, tags: dict, changeset: int = 0,
      nodes: list[int] | None = None, visible: bool = True) -> dict:
    return {"version": version, "timestamp": ts, "tags": tags, "changeset": changeset or version,
            "user": f"user{version}", "visible": visible, "nodes": nodes or [1, 2]}


class BaselineTest(unittest.TestCase):
    def test_posledni_verze_pred_datem(self):
        versions = [v(1, "2020-05-01T00:00:00Z", {}), v(2, "2023-12-31T23:00:00Z", {}),
                    v(3, "2024-06-01T00:00:00Z", {})]
        self.assertEqual(baseline_index(versions, SINCE), 1)

    def test_objekt_vznikl_az_po_datu(self):
        self.assertIsNone(baseline_index([v(1, "2025-01-01T00:00:00Z", {})], SINCE))


class TagEditTest(unittest.TestCase):
    def test_najde_changeset_ktery_tag_zmenil(self):
        versions = [
            v(1, "2020-01-01T00:00:00Z", {"oneway": "no"}),
            v(2, "2024-03-01T00:00:00Z", {"oneway": "no", "surface": "asphalt"}, changeset=11),
            v(3, "2024-07-01T00:00:00Z", {"oneway": "yes", "surface": "asphalt"}, changeset=22),
        ]
        edit = find_tag_edit(versions, SINCE, "oneway")
        self.assertEqual(edit["changeset"], 22)

    def test_vraci_posledni_zmenu_kdyz_jich_je_vic(self):
        versions = [
            v(1, "2020-01-01T00:00:00Z", {"maxspeed": "50"}),
            v(2, "2024-02-01T00:00:00Z", {"maxspeed": "30"}, changeset=11),
            v(3, "2024-09-01T00:00:00Z", {"maxspeed": "20"}, changeset=22),
        ]
        self.assertEqual(find_tag_edit(versions, SINCE, "maxspeed")["changeset"], 22)

    def test_tag_se_nemenil(self):
        """Diff hlásí změnu, ale v historii objektu žádná není – artefakt grafu."""
        versions = [
            v(1, "2020-01-01T00:00:00Z", {"lanes": "2"}),
            v(2, "2024-05-01T00:00:00Z", {"lanes": "2", "name": "Nová"}, changeset=11),
        ]
        self.assertIsNone(find_tag_edit(versions, SINCE, "lanes"))

    def test_smazany_objekt(self):
        versions = [
            v(1, "2020-01-01T00:00:00Z", {"restriction": "no_left_turn"}),
            v(2, "2024-04-01T00:00:00Z", {}, changeset=33, visible=False),
        ]
        edit = find_tag_edit(versions, SINCE, "restriction")
        self.assertEqual(edit["changeset"], 33)
        self.assertFalse(edit["visible"])

    def test_objekt_vznikl_v_obdobi(self):
        versions = [v(1, "2024-08-01T00:00:00Z", {"restriction": "no_u_turn"}, changeset=44)]
        self.assertEqual(find_tag_edit(versions, SINCE, "restriction")["changeset"], 44)

    def test_zmena_pred_referencnim_datem_se_nepocita(self):
        versions = [
            v(1, "2020-01-01T00:00:00Z", {"oneway": "no"}),
            v(2, "2023-06-01T00:00:00Z", {"oneway": "yes"}, changeset=11),
        ]
        self.assertIsNone(find_tag_edit(versions, SINCE, "oneway"))


class GeometryTest(unittest.TestCase):
    def test_zmena_geometrie(self):
        versions = [v(1, "2020-01-01T00:00:00Z", {}, nodes=[1, 2]),
                    v(2, "2024-05-01T00:00:00Z", {}, nodes=[1, 5, 2])]
        self.assertTrue(geometry_changed(versions, SINCE))

    def test_beze_zmeny_geometrie(self):
        versions = [v(1, "2020-01-01T00:00:00Z", {}, nodes=[1, 2]),
                    v(2, "2024-05-01T00:00:00Z", {"name": "X"}, nodes=[1, 2])]
        self.assertFalse(geometry_changed(versions, SINCE))


class ClassifyTest(unittest.TestCase):
    def test_kampan_ma_prednost(self):
        cs = {"changes": 5, "created_by": "JOSM/1.5"}
        self.assertEqual(classify(cs, object_hits=40), "campaign")

    def test_hromadna_editace(self):
        self.assertEqual(classify({"changes": 500, "created_by": "JOSM/1.5"}, 1), "bulk")

    def test_mikrotagovani(self):
        self.assertEqual(classify({"changes": 1, "created_by": "StreetComplete 58.2"}, 1), "micro")

    def test_lokalni_editace(self):
        self.assertEqual(classify({"changes": 6, "created_by": "iD 2.27"}, 1), "local")

    def test_bez_changesetu(self):
        self.assertEqual(classify(None, 0), "unknown")


class ChangesetBatchTest(unittest.TestCase):
    """Changesety se tahají po stovkách, ne po jednom."""

    def test_deli_na_davky_po_stu(self):
        from osm_history import Api

        api = Api.__new__(Api)
        calls: list[str] = []

        def fake_get(path):
            calls.append(path)
            ids = path.split("=", 1)[1].split(",")
            return {"changesets": [{"id": int(i), "user": "u", "changes_count": 3,
                                    "tags": {"comment": "c"}} for i in ids]}

        api.get = fake_get
        result = api.changesets(list(range(1, 251)) + [7, 7])
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(result), 250)
        self.assertEqual(result[7]["comment"], "c")


class CacheTest(unittest.TestCase):
    def test_zaklada_adresar_pro_cache(self):
        """Běh z adresáře bez report/ nesmí spadnout při ukládání cache."""
        import tempfile

        from osm_history import Api

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report" / "cache.json"
            api = Api("https://example.invalid", target, sleep=0)
            api.cache["way/1/history.json"] = {"elements": []}
            api.save()
            self.assertTrue(target.exists())


class TimestampTest(unittest.TestCase):
    def test_parsuje_zulu(self):
        self.assertEqual(parse_ts("2024-05-01T12:00:00Z").year, 2024)


if __name__ == "__main__":
    unittest.main(verbosity=2)

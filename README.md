# NetworkDiff_OSM

Porovnání dvou stavů silniční sítě OpenStreetMap – co se v zájmovém území
(např. v Praze) změnilo z hlediska **rychlostí**, **jednosměrek**,
**povolených křižovatkových pohybů** a **zatřídění komunikací**.

Jako referenční („starý") stav slouží GeoPackage vyexportovaný z OSMnx
(vrstvy `nodes` a `edges`). Aktuální stav si script stáhne sám ve **stejném
rozsahu** – bbox se čte přímo z metadat referenčního GeoPackage.

## Instalace

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Spuštění

```bash
python osm_network_diff.py --baseline osm_snapshot_11_2025.gpkg --outdir vystup
```

Typicky trvá pár minut – většinu času zabere stažení sítě z Overpassu.
Stažený aktuální stav se ukládá do `vystup/cache/`, takže další spuštění
téhož dne je okamžité (`--refresh` vynutí nové stažení).

Další užitečné varianty:

```bash
# porovnání dvou vlastních souborů, bez stahování
python osm_network_diff.py --baseline stary.gpkg --current novy.gpkg

# jen síť z OSMnx, bez dotazů na Overpass
python osm_network_diff.py --baseline osm_snapshot_11_2025.gpkg --no-overpass

# ruční zadání data referenčního stavu (jinak se odhaduje z názvu souboru)
python osm_network_diff.py --baseline snapshot.gpkg --baseline-date 2025-11-01

# jiný rozsah, jiný typ sítě
python osm_network_diff.py --baseline snapshot.gpkg \
    --bbox 14.30,50.02,14.60,50.13 --network-type drive_service
```

Kompletní přehled přepínačů: `python osm_network_diff.py --help`.

## Co se porovnává

| Oblast | Zdroj dat | Výstupní vrstva |
|---|---|---|
| rychlosti (`maxspeed`), jednosměrky (`oneway`), třída komunikace (`highway`), pruhy (`lanes`), `access`, `junction`, `name`, `bridge`, `tunnel`, `ref`, `width` | GeoPackage vs. stažený aktuální stav | `ways_changed` |
| nové a zaniklé komunikace | tamtéž | `ways_added`, `ways_removed` |
| semafory, kruhové objezdy, stopky, počet ramen křižovatky | vrstva uzlů | `nodes_changed` |
| zákazy odbočení (OSM relace `type=restriction`) | Overpass API (aktuální + historická data) | `turn_restrictions` |
| pruhové tagy `turn:lanes`, `turn`, `*:conditional` | Overpass API (aktuální + historická data) | `turn_lanes` |

Porovnává se na úrovni **OSM way ID**, ne na úrovni hran grafu – to je
odolnější vůči tomu, že OSMnx při zjednodušení slučuje více way do jedné
hrany a naopak jednu way dělí na více hran.

## Výstupy

V adresáři `--outdir` (výchozí `vystup/`):

- `zmeny.gpkg` – vrstvy pro QGIS (EPSG:4326), viz tabulka výše
- `ways_changed.csv`, `ways_added.csv`, … – stejná data jako tabulky
  (oddělovač `;`, UTF-8 s BOM, otevře se přímo v Excelu)
- `souhrn.md` – souhrnná statistika, žebříček největších změn rychlostí
  a seznam změněných jednosměrek

Sloupec **`confidence`** je důležitý:

- `high` – změna je jednoznačná,
- `low` – hodnotu nešlo spolehlivě přiřadit konkrétní way (pochází ze
  sloučené hrany), nebo prvek leží u okraje zájmového území, kde se může
  lišit ořez. Tyto záznamy je vhodné ověřit ručně.

U změn rychlosti navíc přibývají sloupce `speed_kph_old`, `speed_kph_new`
a `speed_delta` (číselně v km/h, včetně převodu hodnot typu `CZ:urban`
nebo `30 mph`) – hodí se pro symbolizaci v QGIS.

## Zákazy odbočení a historická data

OSMnx do GeoPackage neexportuje relace `type=restriction`, takže starý stav
zákazů odbočení v referenčním souboru **není**. Script ho proto načítá
z historických („attic") dat Overpass API k datu referenčního stavu.

Datum se odhaduje z názvu souboru (`osm_snapshot_11_2025.gpkg` →
`2025-11-01`); pokud odhad nesedí, zadej `--baseline-date YYYY-MM-DD`.
Historická data nemají všechny instance Overpassu – když je server neposkytne,
script to oznámí, porovnání zákazů přeskočí a uloží alespoň jejich aktuální
stav jako základ pro příští srovnání.

Relace, která byla smazána a znovu založena se stejným obsahem (stejné
`from`/`via`/`to` a stejný typ zákazu), se jako změna nehlásí.

## Na co si dát pozor

- **Dělení a slučování way.** Když editor rozdělí way na dvě, projeví se to
  jako `way_removed` + `way_added`, i když se v terénu nic nezměnilo.
- **Okraj území.** Parametry stahování (`simplify`, `retain_all`) se
  automaticky odvozují z referenčního souboru, aby si oba stavy odpovídaly.
  Přesto se u hranice mohou lišit ořezy – takové přírůstky a úbytky script
  označí jako `confidence=low` (šířku pásma řídí `--boundary-buffer`).
- **Chybějící `maxspeed`** neznamená, že se nesmí jezdit rychle – v ČR platí
  obecná úprava. Prázdná hodnota v `value_old`/`value_new` znamená, že tag
  v OSM nebyl vyplněn.

## HTML přehled změn pro model

Ze CSV výstupů se dá poskládat jednostránkový přehled zaměřený na to, co má
dopad na dopravní model:

```bash
python report/build_report.py vystup/ways_changed.csv vystup/nodes_changed.csv \
    vystup/turn_restrictions.csv -o report/network_diff_report.html
```

Výsledkem je jeden soubor HTML s vloženými daty (bez externích závislostí).
Šablona vzhledu je `report/template.html`.

**Co přehledem projde.** Jen změny hran s `confidence=high`, a z nich jen
atributy, které mění chování sítě v přiřazení: `oneway`, `access`, `maxspeed`
a `lanes`. U `maxspeed` a `lanes` navíc jen tam, kde byla hodnota uvedená před
i po – samotné doplnění dosud chybějícího tagu není změna v terénu. Dál projdou
změny řízení křižovatek, počtu ramen a zákazy odbočení. Odfiltrovaná je třída
komunikace, název, číslo silnice a šířka; vrstvy `ways_added` a `ways_removed`
přehled nezpracovává. Kolik toho filtr zahodil, je vidět na konci stránky.

## Kdo změnu udělal a proč

Diff porovnává dva *stavy* sítě, takže sám o sobě neodliší reálnou změnu
v terénu od přetagování. To doplní `report/osm_history.py`: ke každé položce
dohledá v historii objektu changeset, který sledovaný tag skutečně změnil,
a stáhne k němu autora, komentář, editor a velikost.

```bash
python report/osm_history.py vystup/ways_changed.csv vystup/nodes_changed.csv \
    vystup/turn_restrictions.csv --since 2024-01-01 -o report/history.json

python report/build_report.py vystup/ways_changed.csv vystup/nodes_changed.csv \
    vystup/turn_restrictions.csv --history report/history.json --since 2024-01-01
```

`--since` je datum referenčního stavu (stejné, jaké script vypíše jako
„datum referenčního stavu" v souhrnu). Odpovědi se cachují do
`report/.osm_history_cache.json`, takže opakovaný běh nestahuje znovu totéž;
mezi dotazy se čeká `--sleep` sekund (výchozí 0,4), aby to bylo k API slušné.

Každá položka pak dostane jedno z označení:

| Označení | Co to znamená |
| --- | --- |
| **lokální editace** | malý changeset na jednom místě – nejlepší kandidát na reálnou změnu |
| **mikrotagování** | StreetComplete / Every Door / MapRoulette – obvykle doplnění stavu, který platil už dřív (u `maxspeed` ale často jde o skutečně odečtenou značku) |
| **hromadná editace** | changeset s velkým počtem změn (práh `--bulk-threshold`) |
| **kampaň** | jeden changeset mění tentýž tag na mnoha objektech (práh `--campaign-threshold`) – úklid dat, ne terén |
| **beze změny tagu** | v historii objektu odpovídající editace není – řádek v diffu vznikl zpracováním grafu (rozdělení nebo sloučení way) |

Poslední řádek je nejužitečnější: odhalí položky, které nevznikly žádnou
editací v OSM, takže do modelu nepatří.

Testy vyhodnocování historie (offline, bez sítě):

```bash
python report/test_osm_history.py
```

## Testy

```bash
python test_osm_network_diff.py
```

Testy běží kompletně offline – Overpass API nahrazuje lokální falešný server.

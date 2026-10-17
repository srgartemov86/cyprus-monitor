"""
QSR competitors in Cyprus (Limassol, Nicosia, Paphos, Larnaca) from OpenStreetMap
(Overpass API). Writes `data/qsr_cyprus.json` for gen_map and cycle (Dodo points =
cannibalization penalty in scoring).
Run weekly — OSM data changes slowly, Overpass rate-limits requests.
Brands: McDonald's, KFC, Burger King, Pizza Hut, Domino's, Papa John's, Dodo Pizza.
Logos: public/brands/<slug>.{png|svg}; brands without a logo file use
letter icons (icon_type='letter').

17.10.2026: один запрос на весь остров зеркала не тянут (504), поэтому bbox по
городам и перебор зеркал; overpass-api.de в тот день отвечал 406/timeout,
работало только maps.mail.ru.
"""
import json, os, time
from collections import defaultdict
from curl_cffi import requests
OUTPUT = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'qsr_cyprus.json')
# bbox по городам (small = быстрее для зеркал)
BOXES = {
    'Limassol': "34.62,32.95,34.76,33.13",
    'Nicosia': "35.08,33.26,35.22,33.46",
    'Paphos': "34.70,32.36,34.82,32.50",
    'Larnaca': "34.86,33.55,34.98,33.68",
}
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# (lowercase keyword → brand_slug), matched against name|brand.
# «dodo» целиком не матчим: ловит кипрскую сеть Dodonis.
RULES = [
    ('mcdonald',     'mcdonalds'),
    ('kfc',          'kfc'),
    ('burger king',  'burgerking'),
    ('burgerking',   'burgerking'),
    ('domino',       'dominos'),
    ('pizza hut',    'pizzahut'),
    ('pizzahut',     'pizzahut'),
    ('papa john',    'papajohns'),
    ('dodo pizza',   'dodopizza'),
]

BRAND_META = {
    'mcdonalds':  {'name': "McDonald's",   'logo': 'brands/mcdonalds.svg'},
    'kfc':        {'name': 'KFC',          'logo': 'brands/kfc.png'},
    'burgerking': {'name': 'Burger King',  'logo': 'brands/burgerking.svg'},
    'dominos':    {'name': "Domino's",     'logo': 'brands/dominos.svg'},
    'pizzahut':   {'name': 'Pizza Hut',    'logo': 'brands/pizzahut.svg'},
    'papajohns':  {'name': "Papa John's",  'logo': 'brands/papajohns.png',
                   'icon_type': 'letter', 'letter': 'P'},
    'dodopizza':  {'name': 'Dodo Pizza',   'logo': 'dodo-logo.png'},
}

# Manual overrides for brands with poor OSM coverage.
# Dodo Pizza Cyprus: адреса из publicapi.dodois.io/cy (unitinfo/all, 17.10.2026):
# Limassol-1 «28 October Avenue, 327», Limassol-2 «Omonoias, 38» — координаты из OSM
# (Nominatim по адресу Omonoias уводит в Писсури); Nicosia-1 «Nikis, 42» — Nominatim
# точно (здание), Nicosia-2 «Ammochostou, 5» (Aglantzia) — центроид улицы.
MANUAL_OVERRIDES = {
    'dodopizza': [
        {'lat': 34.6847147, 'lon': 33.0603467, 'name': 'Dodo Pizza Limassol-1', 'address': '28 October Avenue 327'},
        {'lat': 34.6711888, 'lon': 33.0117607, 'name': 'Dodo Pizza Limassol-2', 'address': 'Omonoias 38'},
        {'lat': 35.15666, 'lon': 33.35761, 'name': 'Dodo Pizza Nicosia-1', 'address': 'Nikis 42'},
        {'lat': 35.15748, 'lon': 33.39724, 'name': 'Dodo Pizza Nicosia-2', 'address': 'Ammochostou 5, Aglantzia'},
    ],
}


def classify(name):
    n = (name or '').lower()
    for kw, slug in RULES:
        if kw in n:
            return slug
    return None


def overpass(query):
    """Первое живое зеркало; None если все легли."""
    for ep in MIRRORS:
        try:
            r = requests.post(ep, data={"data": query},
                              headers={"Accept": "application/json", "User-Agent": "DodoMonitor/1.0"},
                              timeout=120, impersonate="chrome120")
            if r.status_code == 200 and r.text.lstrip().startswith('{'):
                return r.json().get('elements', [])
            print(f"  overpass {ep}: http {r.status_code}")
        except Exception as e:
            print(f"  overpass {ep}: {type(e).__name__}")
    return None


def main():
    name_re = "|".join(sorted(set(kw for kw, _ in RULES)))
    els, failed = [], []
    for city, bbox in BOXES.items():
        query = f'''[out:json][timeout:50];
(
  nwr["name"~"{name_re}",i]({bbox});
  nwr["brand"~"{name_re}",i]({bbox});
);
out center tags;'''
        got = overpass(query)
        if got is None:
            failed.append(city)
            continue
        print(f"  {city}: {len(got)} elements")
        els += got
        time.sleep(2)
    if not els:
        raise SystemExit("overpass: nothing fetched, keeping previous file")

    by_brand = defaultdict(list)
    seen = set()
    for e in els:
        tags = e.get('tags', {})
        name = tags.get('name') or tags.get('brand') or ''
        lat = e.get('lat') or (e.get('center') or {}).get('lat')
        lon = e.get('lon') or (e.get('center') or {}).get('lon')
        if not (lat and lon): continue
        slug = classify(name)
        if not slug: continue
        key = (slug, round(lat, 4), round(lon, 4))  # dedup ~11m
        if key in seen: continue
        seen.add(key)
        addr_parts = [tags.get('addr:street', ''), tags.get('addr:housenumber', '')]
        addr = ' '.join(p for p in addr_parts if p).strip()
        by_brand[slug].append({'lat': lat, 'lon': lon, 'name': name, 'address': addr})

    # При частичном сборе (часть городов 504) сохраняем старые точки этих городов
    prev = {}
    if failed and os.path.exists(OUTPUT):
        try:
            prev = json.load(open(OUTPUT, encoding='utf-8')).get('brands', {})
        except Exception:
            prev = {}

    out = {
        'fetched_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
        'source': 'OpenStreetMap via Overpass (per-city bbox)',
        'failed_cities': failed,
        'brands': {},
    }
    for slug, meta in BRAND_META.items():
        if slug in MANUAL_OVERRIDES:
            points, src = MANUAL_OVERRIDES[slug], 'official site (manual)'
        else:
            points, src = list(by_brand.get(slug, [])), 'OSM/Overpass'
            if failed:
                for p in (prev.get(slug) or {}).get('points') or []:
                    for city in failed:
                        s, w, n, e_ = map(float, BOXES[city].split(','))
                        if s <= p['lat'] <= n and w <= p['lon'] <= e_:
                            points.append(p)
        brand_out = {
            'name': meta['name'], 'logo': meta['logo'], 'source': src,
            'count': len(points), 'points': points,
        }
        if 'icon_type' in meta: brand_out['icon_type'] = meta['icon_type']
        if 'letter' in meta:    brand_out['letter'] = meta['letter']
        out['brands'][slug] = brand_out

    json.dump(out, open(OUTPUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    total = sum(len(b['points']) for b in out['brands'].values())
    print(f"wrote {OUTPUT}")
    print(f"total points: {total}" + (f" (failed: {', '.join(failed)})" if failed else ''))
    for slug, b in out['brands'].items():
        print(f"  {b['name']:<16} {b['count']:>3}")


if __name__ == '__main__':
    main()

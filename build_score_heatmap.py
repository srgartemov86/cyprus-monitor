#!/usr/bin/env python3
"""Scoring heatmap для Кипра: квадратная сетка по bbox четырёх городов.

Всё из OSM напрямую: жители — из жилых зданий (площадь × этажность ÷ м²/чел,
та же классификация, что в scoring.py), POI и береговая линия — тайловая
докачка Overpass. Скор каждой ячейки — ТА ЖЕ формула scoring.compose_score
(cy-v1), включая бонус побережья и штраф за близость своих пиццерий
(точки Dodo из data/qsr_cyprus.json). Выход: data/public/score_heatmap_cyprus.geojson
+ страница naselje_score.html.

Запуск разовый/по просьбе (десятки Overpass-тайлов, 5–15 мин) — НЕ в часовом цикле.
Сырой кэш зданий/POI: data/osm_raw_cache_cyprus.json (TTL 72 ч), в git не идёт.
"""
import json, math, os, sys, time
from collections import defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scoring
from scoring import (RES_TYPES, NONRES_BUILDINGS, LEVELS_DEFAULT, M2_BY_TYPE,
                     M2_PER_PERSON, FLOOR_EFFICIENCY, FOOD_AMENITIES, EDU_AMENITIES)

DATA = os.environ.get('CY_DATA', os.path.join(HERE, 'data'))
OUT = os.path.join(DATA, 'public', 'score_heatmap_cyprus.geojson')
RAW_CACHE = os.path.join(DATA, 'osm_raw_cache_cyprus.json')
QSR_PATH = os.path.join(DATA, 'qsr_cyprus.json')
RAW_CACHE_TTL_H = 72
UA = 'dodo-cyprus-location-scout/1.0 (s.artemov@dodobrands.io)'
MIRRORS = ['https://overpass-api.de/api/interpreter',
           'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
           'https://overpass.private.coffee/api/interpreter']

# Bbox городских ядер (6 км от центра поиска + запас на кольцо 1 км)
CITY_BOXES = {
    'Limassol': (34.630, 32.960, 34.740, 33.120),
    'Nicosia':  (35.110, 33.290, 35.210, 33.430),
    'Paphos':   (34.735, 32.390, 34.800, 32.470),
    'Larnaca':  (34.880, 33.580, 34.950, 33.660),
}
CELL_M = 300.0
TILE = 0.022            # ~2.4 км тайлы Overpass
EARTH = 6371000.0


def hav(la1, lo1, la2, lo2):
    p = math.pi / 180
    a = (math.sin((la2 - la1) * p / 2) ** 2 +
         math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2)
    return 2 * EARTH * math.asin(math.sqrt(a))


def poly_area_m2(geom, lat0, lon0):
    if not geom or len(geom) < 3:
        return 0.0
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    pts = [((p['lon'] - lon0) * mlon, (p['lat'] - lat0) * mlat) for p in geom]
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def centroid(el):
    if 'lat' in el and 'lon' in el:
        return el['lat'], el['lon']
    if 'center' in el:
        return el['center']['lat'], el['center']['lon']
    g = el.get('geometry')
    if g:
        return (sum(p['lat'] for p in g) / len(g),
                sum(p['lon'] for p in g) / len(g))
    return None


def fetch_tile(S, W, N, E):
    q = (f'[out:json][timeout:120];('
         f'way["building"]({S},{W},{N},{E});relation["building"]({S},{W},{N},{E});'
         f'nwr["amenity"]({S},{W},{N},{E});nwr["shop"]({S},{W},{N},{E});'
         f'nwr["office"]({S},{W},{N},{E});'
         f'nwr["public_transport"="platform"]({S},{W},{N},{E});'
         f'node["highway"="bus_stop"]({S},{W},{N},{E});'
         f'way["natural"="coastline"]({S},{W},{N},{E});'
         f');out tags geom;')
    for _ in range(2):
        for m in MIRRORS:
            try:
                r = requests.post(m, data={'data': q},
                                  headers={'User-Agent': UA}, timeout=150)
                if r.status_code == 200 and r.text.lstrip().startswith('{'):
                    return r.json().get('elements', [])
            except Exception:
                continue
        time.sleep(4)
    return None


def load_raw_cache():
    if not os.path.exists(RAW_CACHE):
        return None
    try:
        c = json.load(open(RAW_CACHE))
        age_h = (time.time() - c.get('fetched_at', 0)) / 3600.0
        if age_h > RAW_CACHE_TTL_H:
            return None
        print(f'raw cache hit (age {age_h:.1f}h): people={len(c["people"])}, '
              f'poi={ {k: len(v) for k, v in c["pois"].items()} }', flush=True)
        return c
    except Exception:
        return None


def dodo_points():
    try:
        pts = json.load(open(QSR_PATH, encoding='utf-8'))['brands']['dodopizza']['points']
        return [(p['lat'], p['lon']) for p in pts]
    except Exception:
        return []


def main():
    cached = load_raw_cache()
    if cached:
        people = [tuple(p) for p in cached['people']]
        pois = {k: [tuple(c) for c in v] for k, v in cached['pois'].items()}
        return score_and_write(people, pois)

    tiles = []
    for city, (S, W, N, E) in CITY_BOXES.items():
        lat = S
        while lat < N:
            lon = W
            while lon < E:
                tiles.append((city, lat, lon, min(lat + TILE, N), min(lon + TILE, E)))
                lon += TILE
            lat += TILE
    print(f'tiles: {len(tiles)}', flush=True)

    people = []   # (lat, lon, ppl) на здание
    pois = {'transit': [], 'shop': [], 'office': [], 'food': [], 'edu': [], 'coast': []}
    seen = set()
    fails = 0
    for ti, (city, la, lo, la2, lo2) in enumerate(tiles):
        els = fetch_tile(la, lo, la2, lo2)
        if els is None:
            fails += 1
            print(f'  tile {ti+1}/{len(tiles)} {city} FAIL', flush=True)
            continue
        n = 0
        for e in els:
            key = (e.get('type'), e.get('id'))
            if key in seen:
                continue
            seen.add(key)
            t = e.get('tags', {}) or {}
            if t.get('natural') == 'coastline':
                for p in (e.get('geometry') or [])[::3]:  # каждая 3-я точка линии хватает
                    pois['coast'].append((p['lat'], p['lon']))
                continue
            c = centroid(e)
            if not c:
                continue
            b = t.get('building')
            if b and not (t.get('amenity') or t.get('shop') or t.get('office')):
                if b in NONRES_BUILDINGS:
                    continue
                if b in RES_TYPES or b == 'yes':
                    area = poly_area_m2(e.get('geometry'), c[0], c[1])
                    if area >= 25:
                        try:
                            lev = float(t.get('building:levels'))
                        except (TypeError, ValueError):
                            lev = LEVELS_DEFAULT.get(b, 3)
                        lev = max(1.0, min(lev, 35.0))
                        fac = 1.0 if b in RES_TYPES else 0.55
                        m2 = M2_BY_TYPE.get(b, M2_PER_PERSON)
                        ppl = area * lev * FLOOR_EFFICIENCY / m2 * fac
                        people.append((c[0], c[1], ppl))
                continue
            am = t.get('amenity')
            if am in FOOD_AMENITIES:
                pois['food'].append(c)
            if am in EDU_AMENITIES:
                pois['edu'].append(c)
            if t.get('shop'):
                pois['shop'].append(c)
            if t.get('office'):
                pois['office'].append(c)
            if t.get('public_transport') == 'platform' or t.get('highway') == 'bus_stop':
                pois['transit'].append(c)
            n += 1
        print(f'  tile {ti+1}/{len(tiles)} {city} ok ({n} poi, people-buildings {len(people)})', flush=True)
        time.sleep(0.8)
    print(f'people-buildings: {len(people)}, POI:',
          {k: len(v) for k, v in pois.items()}, 'fails', fails, flush=True)
    if fails > len(tiles) // 3:
        raise SystemExit('too many tile failures, not writing cache')
    try:
        json.dump({'fetched_at': time.time(), 'people': people, 'pois': pois}, open(RAW_CACHE, 'w'))
        print(f'raw cache written: {RAW_CACHE}', flush=True)
    except Exception as e:
        print(f'raw cache write failed: {e}', flush=True)
    return score_and_write(people, pois)


def score_and_write(people, pois):
    B = 0.006

    def bucket(points):
        g = defaultdict(list)
        for p in points:
            g[(round(p[0] / B), round(p[1] / B))].append(p)
        return g

    poi_buckets = {k: bucket(v) for k, v in pois.items()}
    ppl_bucket = bucket(people)

    def count_within(cat, lat, lon, R):
        g = poi_buckets.get(cat) or {}
        rb = int(R / 111320.0 / B) + 1
        bi, bj = round(lat / B), round(lon / B)
        cnt = 0
        for di in range(-rb, rb + 1):
            for dj in range(-rb, rb + 1):
                for la, lo in g.get((bi + di, bj + dj), ()):
                    if hav(lat, lon, la, lo) <= R:
                        cnt += 1
        return cnt

    def residents_within(lat, lon, R):
        rb = int(R / 111320.0 / B) + 1
        bi, bj = round(lat / B), round(lon / B)
        tot = 0.0
        for di in range(-rb, rb + 1):
            for dj in range(-rb, rb + 1):
                for la, lo, ppl in ppl_bucket.get((bi + di, bj + dj), ()):
                    if hav(lat, lon, la, lo) <= R:
                        tot += ppl
        return tot

    dodo = dodo_points()
    dlat = CELL_M / 111320.0
    print('scoring grid...', flush=True)
    feats, scored = [], 0
    for city, (S, W, N, E) in CITY_BOXES.items():
        lat = S
        while lat < N:
            dlon = CELL_M / (111320.0 * math.cos(math.radians(lat)))
            lon = W
            while lon < E:
                clat, clon = lat + dlat / 2, lon + dlon / 2
                res500 = residents_within(clat, clon, 500)
                transit = count_within('transit', clat, clon, 300)
                shops = count_within('shop', clat, clon, 300)
                if res500 < 200 and transit + shops < 3:
                    lon += dlon
                    continue  # пустырь/море/горы — не скорим
                res1000 = residents_within(clat, clon, 1000)
                food = count_within('food', clat, clon, 400)
                comp = count_within('food', clat, clon, 200)
                offices = count_within('office', clat, clon, 500)
                edu = count_within('edu', clat, clon, 500)
                sea = count_within('coast', clat, clon, scoring.SEA_RADIUS) > 0
                nd = min((hav(clat, clon, p[0], p[1]) for p in dodo), default=None)
                sc = scoring.compose_score(round(res500), round(res1000), transit, shops, food,
                                           offices, edu, comp, nd / 1000.0 if nd is not None else None, sea)
                ring = [[lon, lat], [lon + dlon, lat], [lon + dlon, lat + dlat], [lon, lat + dlat], [lon, lat]]
                feats.append({'type': 'Feature',
                              'geometry': {'type': 'Polygon', 'coordinates': [[[round(x, 5), round(y, 5)] for x, y in ring]]},
                              'properties': {'score': sc['score'], 'city': city, 'res500': round(res500),
                                             'transit': transit, 'shops': shops, 'food': food,
                                             'comp': comp, 'offices': offices, 'sea': int(sea)}})
                scored += 1
                lon += dlon
            lat += dlat

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({'type': 'FeatureCollection', 'formula': scoring.SCORE_FORMULA, 'features': feats},
              open(OUT, 'w'), separators=(',', ':'))
    ss = sorted(f['properties']['score'] for f in feats)
    print(f'DONE scored={scored}', flush=True)
    if ss:
        print(f'score: min {ss[0]} median {ss[len(ss)//2]} p90 {ss[int(len(ss)*0.9)]} max {ss[-1]}', flush=True)
    print(f'wrote {OUT} ({os.path.getsize(OUT)//1024} KB)', flush=True)


if __name__ == '__main__':
    main()

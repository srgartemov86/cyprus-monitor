#!/usr/bin/env python3
"""Location scoring for food-service (общепит) suitability — Belgrade.

One Overpass call per lot, then everything computed offline. No heavy GIS deps
(only requests + stdlib math) — важно на Python 3.14, где колёса rasterio/geopandas
/h3 ещё не собираются.

Сигналы (всё в пределах радиусов от точки лота):
  • residents_500 / residents_1000 — оценка ЖИТЕЛЕЙ по жилому фонду OSM
       residents ≈ Σ(площадь_застройки · этажность · 0.8) / 30 м²-на-человека
       (площадь — shoelace по геометрии здания; этажность — building:levels или
        дефолт по типу; 'building=yes' учитывается с понижающим коэф. 0.55)
  • transit_300   — остановки транспорта (footfall / доступность)
  • shops_300     — магазины (торговая активность улицы)
  • food_400      — действующий общепит (доказанный трафик; но и конкуренция)
  • offices_500 + edu — дневное население (офисы, ВУЗы/школы)
  • direct_competitors_200 — прямые конкуренты рядом (штраф)
  • nearest_dodo_km — каннибализация со своими точками (штраф если близко)

Итог: score 0..100 + разбивка + человекочитаемая строка.
Graceful: при недоступности Overpass возвращает score=None (цикл не падает).
Результат кэшируется вызывающим кодом в state по ключу лота.
"""
import math, time, requests

UA = "dodo-cluj-location-scout/1.0 (s.artemov@dodobrands.io)"
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
FETCH_RADIUS = 1100           # м: один запрос; ≥1000 чтобы полностью покрыть кольцо residents_1000
FLOOR_EFFICIENCY = 0.8        # доля полезной площади этажа

# Жители = Σ(footprint · этажность · FLOOR_EFFICIENCY / M2_на-человека-ПО-ТИПУ).
# ПОЧЕМУ ПО ТИПУ, а не единый M2 + глобальный множитель: единый множитель давит ВСЁ
# одинаково, включая хорошо измеренные башни с реальной этажностью (Нови-Београд тегирован
# на 95%) → занижал их. Разная плотность заселения по типам — физический рычаг: квартиры
# плотнее (меньше м²/чел), частные дома просторнее. Калибровка на census-тотал Kontur зашита
# именно в эти M2, а не в плоский CALIB → высотность сохраняется. Подобрано по ядру Белграда
# (129 607 зданий): тотал ~1.18M (Belgrade core ~1.1–1.3M реально; Kontur 946k занижает),
# Нови-Београд сравнялся с Kontur (был ×0.5), частный сектор корректно низкий.
M2_PER_PERSON = 48.0          # дефолт для нераспознанного жилого типа
M2_BY_TYPE = {                # м² жил. площади на человека по типу застройки
    "apartments": 40, "residential": 40, "dormitory": 40,          # многоквартирные — плотно
    "house": 55, "detached": 55, "semidetached_house": 55,         # частный сектор — просторно
    "bungalow": 55, "terrace": 55, "yes": 50,                      # yes — неопределённый, средне
}

RES_TYPES = {"residential", "apartments", "house", "detached", "terrace",
             "dormitory", "semidetached_house", "bungalow"}
NONRES_BUILDINGS = {"commercial", "retail", "office", "industrial", "warehouse",
                    "church", "cathedral", "mosque", "chapel", "school",
                    "university", "college", "hospital", "garage", "garages",
                    "kiosk", "hangar", "public", "civic", "sports_centre",
                    "supermarket", "hotel", "parking", "roof", "shed",
                    "construction", "service", "transportation", "train_station"}
LEVELS_DEFAULT = {"apartments": 4, "residential": 4, "dormitory": 4, "terrace": 3,
                  "house": 2, "detached": 2, "semidetached_house": 2,
                  "bungalow": 1, "yes": 3}
FOOD_AMENITIES = {"restaurant", "fast_food", "cafe", "bar", "pub", "food_court",
                  "ice_cream", "biergarten"}
EDU_AMENITIES = {"university", "college", "school", "language_school"}

EARTH = 6371000.0


def _hav(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2 +
         math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * EARTH * math.asin(math.sqrt(a))


def _circ_intersect_area(R, r, d):
    """Площадь пересечения двух кругов (радиусы R, r; расстояние между центрами d)."""
    if d >= R + r:
        return 0.0
    if d <= abs(R - r):
        return math.pi * min(R, r) ** 2
    R2, r2, d2 = R * R, r * r, d * d
    a1 = math.acos(max(-1.0, min(1.0, (d2 + r2 - R2) / (2 * d * r))))
    a2 = math.acos(max(-1.0, min(1.0, (d2 + R2 - r2) / (2 * d * R))))
    tri = 0.5 * math.sqrt(max(0.0, (-d + r + R) * (d + r - R) * (d - r + R) * (d + r + R)))
    return r2 * a1 + R2 * a2 - tri


# --- Kontur Population (H3 r8, census-grade) — census-якорь гибрида + фоллбэк ---
_KONTUR = None
_KONTUR_PATH = __import__("os").path.join(__import__("os").environ.get("CLUJ_DATA", "/Users/dodo/cluj-location-monitor"), "kontur_cluj.npz")


def _load_kontur():
    global _KONTUR
    if _KONTUR is None:
        try:
            import numpy as np
            d = np.load(_KONTUR_PATH)
            _KONTUR = (d['lat'], d['lon'], d['pop'], d['r_eq'])
        except Exception:
            _KONTUR = False
    return _KONTUR


def kontur_population_within(lat, lon, R):
    """Население в радиусе R (м) по Kontur, площадно-взвешенно (доля гексагона в круге).
    None — точка вне покрытия кэша (тогда вызывающий откатывается на OSM-оценку)."""
    k = _load_kontur()
    if not k:
        return None
    import numpy as np
    klat, klon, kpop, kreq = k
    dlat = (R + 800) / 111320.0
    dlon = dlat / max(0.2, math.cos(math.radians(lat)))
    m = (np.abs(klat - lat) < dlat) & (np.abs(klon - lon) < dlon)
    if not m.any():
        return None
    total = 0.0
    for la, lo, pp, rq in zip(klat[m], klon[m], kpop[m], kreq[m]):
        d = _hav(lat, lon, la, lo)
        if d >= R + rq:
            continue
        frac = _circ_intersect_area(R, rq, d) / (math.pi * rq * rq)
        total += pp * min(1.0, frac)
    return total


def _poly_area_m2(geom, lat0, lon0):
    """Площадь полигона (м²) планарной аппроксимацией вокруг центра лота."""
    if not geom or len(geom) < 3:
        return 0.0
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    pts = [((p["lon"] - lon0) * mlon, (p["lat"] - lat0) * mlat) for p in geom]
    s = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def _centroid(el):
    """(lat, lon) центра элемента или None."""
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    if "center" in el:
        return el["center"]["lat"], el["center"]["lon"]
    g = el.get("geometry")
    if g:
        return (sum(p["lat"] for p in g) / len(g),
                sum(p["lon"] for p in g) / len(g))
    return None


def fetch_osm(lat, lon, timeout=180):
    """Один Overpass-запрос: здания + POI + транспорт в радиусе FETCH_RADIUS.
    timeout 85→180 и 2 круга по зеркалам — паритет с belgrade-monitor 2026-07-16
    (в плотной застройке запрос идёт >85с, разовый затык зеркала ронял score=None)."""
    q = (f"[out:json][timeout:170];("
         f'way["building"](around:{FETCH_RADIUS},{lat},{lon});'
         f'relation["building"](around:{FETCH_RADIUS},{lat},{lon});'
         f'nwr["amenity"](around:{FETCH_RADIUS},{lat},{lon});'
         f'nwr["shop"](around:{FETCH_RADIUS},{lat},{lon});'
         f'nwr["office"](around:{FETCH_RADIUS},{lat},{lon});'
         f'nwr["public_transport"="platform"](around:{FETCH_RADIUS},{lat},{lon});'
         f'node["highway"="bus_stop"](around:{FETCH_RADIUS},{lat},{lon});'
         f'nwr["railway"~"^(station|tram_stop|subway_entrance|halt)$"](around:{FETCH_RADIUS},{lat},{lon});'
         f");out tags geom;")
    # ОБЩИЙ дедлайн на лот: 3 зеркала × 2 круга × 180 с = до 18 минут при зависших
    # зеркалах, а process-фаза здесь ограничена 900 с (в belgrade-monitor так упали
    # два прогона 16.09.2026). Не уложились — score=None.
    deadline = time.time() + 240
    for round_n in range(2):
        for m in MIRRORS:
            left = deadline - time.time()
            if left < 15:
                return None
            try:
                r = requests.post(m, data={"data": q},
                                  headers={"User-Agent": UA}, timeout=min(timeout, left))
                if r.status_code == 200:
                    return r.json().get("elements", [])
            except Exception:
                continue
        if round_n == 0:
            time.sleep(5)
    return None


def _sat(x):
    return max(0.0, min(1.0, x))


def compose_score(res_500, res_1000, transit_300, shops_300, food_400,
                  offices_500, edu_500, competitors_200, nearest_dodo_km):
    """Композиция итогового скора из сигналов. Вынесено отдельно, чтобы пересчёт
    из кэша (смена источника населения) использовал ту же формулу, что и score_location."""
    s_catchment = _sat(res_500 / 10000.0)
    s_secondary = _sat(res_1000 / 40000.0)
    s_transit   = _sat(transit_300 / 6.0)
    s_retail    = _sat(shops_300 / 40.0)
    s_food      = _sat(food_400 / 20.0)
    s_daytime   = _sat((offices_500 + 4 * edu_500) / 40.0)
    s_comp_pen  = _sat(competitors_200 / 8.0)
    s_cannib_pen = 0.0 if nearest_dodo_km is None else max(0.0, 1.0 - nearest_dodo_km / 0.6)
    score = 100.0 * (
        0.28 * s_catchment + 0.10 * s_secondary + 0.16 * s_transit +
        0.12 * s_retail + 0.12 * s_food + 0.14 * s_daytime -
        0.10 * s_comp_pen - 0.08 * s_cannib_pen
    )
    score = max(0, min(100, round(score)))
    return {
        "score": score,
        "residents_500": res_500, "residents_1000": res_1000,
        "transit_300": transit_300, "shops_300": shops_300, "food_400": food_400,
        "offices_500": offices_500, "edu_500": edu_500,
        "competitors_200": competitors_200,
        "nearest_dodo_km": round(nearest_dodo_km, 2) if nearest_dodo_km is not None else None,
        "breakdown": {
            "catchment": round(s_catchment, 2), "secondary": round(s_secondary, 2),
            "transit": round(s_transit, 2), "retail": round(s_retail, 2),
            "food": round(s_food, 2), "daytime": round(s_daytime, 2),
            "comp_penalty": round(s_comp_pen, 2), "cannib_penalty": round(s_cannib_pen, 2),
        },
    }


def score_location(lat, lon, dodo_points=None, elements=None):
    """Возвращает dict со score (0..100), жителями и разбивкой, либо {'score': None}
    если OSM недоступен. dodo_points: [(lat,lon),...] для каннибализации."""
    if elements is None:
        elements = fetch_osm(lat, lon)
    if elements is None:
        return {"score": None, "reason": "overpass_unavailable"}

    res_500_osm = res_1000_osm = 0.0
    transit_300 = shops_300 = food_400 = offices_500 = 0
    edu_500 = direct_comp_200 = 0

    for e in elements:
        t = e.get("tags", {}) or {}
        c = _centroid(e)
        if not c:
            continue
        d = _hav(lat, lon, c[0], c[1])

        b = t.get("building")
        if b and not (t.get("amenity") or t.get("shop") or t.get("office")):
            if b in NONRES_BUILDINGS:
                pass  # нежилое — в население не идёт
            elif b in RES_TYPES or b == "yes":
                area = _poly_area_m2(e.get("geometry"), lat, lon)
                if area >= 25:
                    try:
                        lev = float(t.get("building:levels"))
                    except (TypeError, ValueError):
                        lev = LEVELS_DEFAULT.get(b, 3)
                    lev = max(1.0, min(lev, 35.0))
                    fac = 1.0 if b in RES_TYPES else 0.55  # 'yes' — понижаем
                    m2 = M2_BY_TYPE.get(b, M2_PER_PERSON)  # плотность заселения по типу
                    ppl = area * lev * FLOOR_EFFICIENCY / m2 * fac
                    if d <= 500:
                        res_500_osm += ppl
                    if d <= 1000:
                        res_1000_osm += ppl
            # building присутствует — это не POI, дальше не классифицируем
            continue

        am = t.get("amenity")
        if am in FOOD_AMENITIES:
            if d <= 400:
                food_400 += 1
            if d <= 200:
                direct_comp_200 += 1
        if am in EDU_AMENITIES and d <= 500:
            edu_500 += 1
        if t.get("shop") and d <= 300:
            shops_300 += 1
        if t.get("office") and d <= 500:
            offices_500 += 1
        if d <= 300 and (t.get("public_transport") == "platform"
                         or t.get("highway") == "bus_stop"
                         or t.get("railway") in ("station", "tram_stop",
                                                 "subway_entrance", "halt")):
            transit_300 += 1

    # население: ГИБРИД — OSM-оценка по этажности с заселением ПО ТИПУ (различает высотки/
    # частный сектор; калибровка зашита в M2_BY_TYPE, без плоского множителя). Kontur считаем
    # параллельно: census-якорь, фоллбэк и флаг завышения над низкоэтажной застройкой.
    k500 = kontur_population_within(lat, lon, 500)
    k1000 = kontur_population_within(lat, lon, 1000)
    h500, h1000 = res_500_osm, res_1000_osm
    if res_500_osm > 0 or res_1000_osm > 0:
        res_500, res_1000, pop_source = round(h500), round(h1000), "hybrid"
    elif k500 is not None:                       # нет жилых зданий в OSM — откат на Kontur
        res_500, res_1000, pop_source = round(k500), round(k1000 or 0), "kontur"
    else:
        res_500, res_1000, pop_source = 0, 0, "none"

    # каннибализация
    nearest_dodo_km = None
    if dodo_points:
        nearest_dodo_km = min(_hav(lat, lon, p[0], p[1]) for p in dodo_points) / 1000.0

    out = compose_score(res_500, res_1000, transit_300, shops_300, food_400,
                        offices_500, edu_500, direct_comp_200, nearest_dodo_km)
    out["pop_source"] = pop_source
    out["residents_500_kontur"] = round(k500) if k500 is not None else None
    out["residents_500_osm_raw"] = round(res_500_osm)
    # Kontur заметно выше гибрида → низкоэтажная/частная застройка (Kontur её завышал)
    if k500 and h500 and k500 / max(h500, 1.0) >= 1.6:
        out["low_rise_flag"] = True
    return out


def score_emoji(score):
    if score is None:
        return "❔"
    if score >= 70:
        return "🟢"
    if score >= 50:
        return "🟡"
    if score >= 30:
        return "🟠"
    return "🔴"


def score_line(sc):
    """Человекочитаемая строка для карточки/Telegram."""
    if not sc or sc.get("score") is None:
        return ""
    parts = [f"{score_emoji(sc['score'])} Location score {sc['score']}/100"]
    parts.append(f"👥 ~{sc['residents_500']:,} residents in 500 m".replace(",", " "))
    extra = []
    if sc["transit_300"]:
        extra.append(f"🚏 {sc['transit_300']} transit stops in 300 m")
    if sc["offices_500"] or sc["edu_500"]:
        extra.append(f"🏢 {sc['offices_500']} offices/{sc['edu_500']} edu")
    if sc["competitors_200"]:
        extra.append(f"⚔️ {sc['competitors_200']} competitors in 200 m")
    if sc.get("nearest_dodo_km") is not None:
        extra.append(f"🍕 Dodo {sc['nearest_dodo_km']} km")
    line = " · ".join(parts)
    if extra:
        line += "\n   " + " · ".join(extra)
    return line


if __name__ == "__main__":
    import sys, json
    lat, lon = float(sys.argv[1]), float(sys.argv[2])
    t = time.time()
    sc = score_location(lat, lon)
    sc["_sec"] = round(time.time() - t, 1)
    print(json.dumps(sc, ensure_ascii=False, indent=2))
    print(score_line(sc))

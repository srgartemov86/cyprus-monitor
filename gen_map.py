"""
Генерирует self-contained HTML-карту со всеми актуальными лотами из state.json.

Источники:
- state.json — координаты, район, площадь, цена, URL, флаги (свежесть, uncertain).
- Google Sheets (CSV-экспорт через Profile 3 cookies) — описание из колонки F.
- Nominatim — догеокодирование лотов без координат (с записью обратно в state).

Вывод: /Users/dodo/pizzeria-location-monitor/lokali.html  (Leaflet + OSM tiles).
Маркер: цвет по свежести (зелёный <7д / оранжевый 7-30д / серый 30+д).
Popup: район · цена · метраж · описание · кнопка-ссылка.
"""
import json, datetime, csv, html as _html, time, sys, os, re, io, math
from curl_cffi import requests

STATE_PATH = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'state.json')
QSR_PATH   = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'qsr_cyprus.json')
OUT_HTML   = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'public', 'lokali.html')
SHEET_ID   = '1NZNlx2G24Ea-zGNurKx7fTAmHgLK4tOSjtB7ScYx-7c'
SHEET_GID  = '498788918'
CHROME_PROFILE_COOKIES = '/Users/dodo/Library/Application Support/Google/Chrome/Profile 3/Cookies'
NOMINATIM_UA = "DodoPizzeriaMonitor/1.0 (s.artemov@dodobrands.io)"

# Status в колонке K нормализуем lowercase → bucket
STATUS_NOT_SUITABLE = {'не подходит', 'отклонено', 'отказ', 'reject', 'nope', 'no',
                       'not suitable', 'rejected', 'pass'}
STATUS_REMOVED = {'снят с сайта', 'снят', 'removed', 'inactive', 'dead',
                  'removed from site'}
# Триггеры «избранное / на обсуждении» → красная звезда на карте.
# Нормализуем статус: lowercase + удалить точки/лишние пробелы, потом сверяем с set.
STATUS_IN_PROGRESS = {
    'в работе', 'в процессе', 'wip', 'in progress', 'working',
    'двигаем',
    'звонок назначен', 'смотрю', 'смотрим', 'переговоры',
    'избранное', 'favorite', 'starred',
}
# «ОК. Двигаем на обсуждение» — синяя звезда, приоритет выше «в работе».
STATUS_TO_REVIEW = {
    'ок двигаем на обсуждение', 'ok двигаем на обсуждение',
    'двигаем на обсуждение',
    'на обсуждение', 'на обсуждении', 'обсуждаем',
    'to review', 'review', 'discuss', 'for discussion',
}

TRG_REPUBLIKE = (34.6750, 33.0443)  # Limassol centre (legacy name)

# В Клуже пиццерий Dodo нет.
def _dodo_locations():
    """Пиццерии Dodo на Кипре из qsr_cyprus.json (бренд dodopizza) — слой на карте."""
    try:
        with open(QSR_PATH, encoding='utf-8') as f:
            pts = (json.load(f).get('brands') or {}).get('dodopizza', {}).get('points') or []
        return [{'lat': p['lat'], 'lon': p['lon'], 'name': p.get('name') or 'Dodo Pizza'} for p in pts]
    except Exception:
        return []


DODO_LOCATIONS = _dodo_locations()


def load_state():
    return json.load(open(STATE_PATH, encoding='utf-8'))


def save_state(state):
    state['version'] = state.get('version', 0) + 1
    json.dump(state, open(STATE_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


_SHORTLINK_CACHE = {}

def _expand_shortlink(url):
    """Resolve maps.app.goo.gl / goo.gl/maps via single HEAD-redirect. Cached per-run."""
    if url in _SHORTLINK_CACHE:
        return _SHORTLINK_CACHE[url]
    try:
        r = requests.get(url, allow_redirects=False, timeout=8,
                         headers={'User-Agent': 'Mozilla/5.0'})
        loc = r.headers.get('Location') or r.headers.get('location') or url
    except Exception:
        loc = url
    _SHORTLINK_CACHE[url] = loc
    return loc


def parse_manual_coord(s):
    """Парсит ручную координату из Sheets-колонки L. Поддерживает:
       - Google Maps full URLs (place/@lat,lon, ?q=lat,lon, ?ll=, api=1&query=, !3d!4d)
       - Короткие ссылки maps.app.goo.gl / goo.gl/maps (один redirect-hop)
       - Голую строку 'lat,lon' (или 'lat, lon')

    Возвращает (lat, lon) или None если распарсить не удалось / sanity-check провален.
    """
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    if 'maps.app.goo.gl' in s or 'goo.gl/maps' in s:
        s = _expand_shortlink(s)
    patterns = [
        r'@(-?\d+\.\d+),(-?\d+\.\d+)',           # /maps/place/.../@lat,lon,z
        r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)',       # !3dLAT!4dLON
        r'[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)',      # ?q=lat,lon
        r'[?&]query=(-?\d+\.\d+),(-?\d+\.\d+)',  # ?api=1&query=lat,lon
        r'[?&]ll=(-?\d+\.\d+),(-?\d+\.\d+)',     # ?ll=lat,lon
        r'^(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)$',    # bare 'lat,lon' (whole string)
    ]
    for p in patterns:
        m = re.search(p, s)
        if m:
            try:
                lat = float(m.group(1)); lon = float(m.group(2))
            except ValueError:
                continue
            if not (-90 < lat < 90 and -180 < lon < 180):
                continue
            # Sanity: точка должна быть на Кипре (≤120 км от центра острова)
            import math
            dlat = math.radians(lat - 35.0)
            dlon = math.radians(lon - 33.2)
            a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(35.0)) * math.sin(dlon/2)**2
            dist_km = 6371 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            if dist_km > 120:
                print(f'  manual_coord rejected (not in Cyprus, {dist_km:.1f}km): {s[:80]}', file=sys.stderr)
                return None
            return (lat, lon)
    return None


def fetch_sheet_rows():
    """url -> {row, address, district, area, price, description, date_posted, status, removal_date, manual_lat, manual_lon}

    row — 1-indexed номер строки в Sheets (для прямых ссылок и update_cells).
    status — содержимое колонки K (пользовательский фильтр / "Снят с сайта").
    removal_date — колонка I (timestamp когда check_status пометил «снят»).
    manual_lat/manual_lon — координаты из колонки L (если заполнена и парсится).

    Если URL встречается несколько раз — оставляем самую верхнюю (insert_at_top → она же новейшая).
    """
    try:
        if os.environ.get('GOOGLE_TOKEN_PATH') or not os.path.exists(CHROME_PROFILE_COOKIES):
            # Сервер (GitHub Actions): куки Chrome недоступны — читаем через Sheets API
            # тем же OAuth-токеном, что и sheets_append. values.get отдаёт готовые rows
            # (многострочные ячейки не проблема — нет CSV-парсинга вообще).
            from sheets_append import _sheets_service
            resp = _sheets_service().spreadsheets().values().get(
                spreadsheetId=SHEET_ID, range='Locations!A:L').execute()
            rows = resp.get('values', [])
        else:
            import browser_cookie3
            cj = browser_cookie3.chrome(domain_name='.google.com', cookie_file=CHROME_PROFILE_COOKIES)
            r = requests.get(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}",
                             cookies=cj, timeout=20)
            # csv.reader должен получить файл целиком, а не пред-разбитый splitlines() —
            # иначе многострочные поля (длинные opis-ы с \n внутри кавычек) ломают индексацию строк
            # и URL → row mapping съезжает (баг 2026-05-15: Omladinskih на real row 32 резолвился как 20).
            rows = list(csv.reader(io.StringIO(r.text)))
    except Exception as e:
        print(f"  sheet fetch failed: {e}", file=sys.stderr)
        return {}
    def to_int(s):
        try: return int(re.sub(r'[^0-9]', '', s or ''))
        except: return None
    by_url = {}
    # Реальная схема таблицы (подтверждено 2026-05-14):
    # A Адрес | B Район | C Площадь | D Цена | E Ссылка | F Текст | G Дата размещения
    # H Дата добавления (bot) | I Дата снятия с сайта (bot) | J Комментарий (user) | K Статус (user)
    # rows[0] = header, rows[1] = first data row = sheet row 2
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) < 6: continue
        url_raw = row[4].strip()
        if not url_raw: continue
        m = re.search(r'https?://[^\s|]+', url_raw)
        url = m.group(0) if m else url_raw
        if url in by_url: continue  # keep topmost (newest)
        manual_raw = row[11].strip() if len(row) > 11 else ''   # L — пользовательская точка
        manual_lat = manual_lon = None
        if manual_raw:
            parsed = parse_manual_coord(manual_raw)
            if parsed:
                manual_lat, manual_lon = parsed
            else:
                print(f"  row {idx}: manual_coord не распарсен: {manual_raw[:80]!r}", file=sys.stderr)
        by_url[url] = {
            'row': idx,
            'address':      row[0].strip(),
            'district':     row[1].strip(),
            'area':         to_int(row[2]),
            'price':        to_int(row[3]),
            'description':  row[5].strip(),
            'date_posted':  row[6].strip() if len(row) > 6 else '',
            'removal_date': row[8].strip() if len(row) > 8 else '',   # I
            'comment':      row[9].strip() if len(row) > 9 else '',   # J
            'status':       row[10].strip() if len(row) > 10 else '', # K
            'manual_raw':   manual_raw,
            'manual_lat':   manual_lat,
            'manual_lon':   manual_lon,
        }
    return by_url


def sheet_row_url(row):
    """Прямая ссылка на конкретную строку в Sheets."""
    return f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit?gid={SHEET_GID}&range=A{row}"


def geocode(query):
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": query, "format": "json", "limit": 1, "countrycodes": "ro"},
                         headers={"User-Agent": NOMINATIM_UA}, timeout=15)
        d = r.json()
        if d:
            return float(d[0]['lat']), float(d[0]['lon'])
    except Exception:
        pass
    return None, None


def geocode_multi(query, limit=10):
    """Возвращает все Nominatim-кандидаты для запроса (для disambiguation)."""
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": query, "format": "json", "limit": limit,
                                 "countrycodes": "ro", "addressdetails": 1},
                         headers={"User-Agent": NOMINATIM_UA}, timeout=15)
        return r.json() or []
    except Exception:
        return []


# Опорные центроиды районов для disambiguation Nominatim-результатов
DISTRICT_CENTROIDS = {  # «район» = город задачи (district в state: «Limassol (Zakaki)»)
    'Limassol': (34.6750, 33.0443), 'Nicosia': (35.1725, 33.3620),
    'Paphos': (34.7720, 32.4297), 'Larnaca': (34.9160, 33.6310),
}


def district_centroid(district):
    if not district: return None
    d = district.split('(')[0].strip()
    for name, c in DISTRICT_CENTROIDS.items():
        if name.lower() in d.lower():
            return c
    return None


def _haversine(la1, lo1, la2, lo2):
    import math
    R = 6371000
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1); dl = math.radians(lo2 - lo1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def regeo_nominatim(state, max_shift_threshold_m=200, district_radius_m=6000):
    """Перегеокодит лоты с geo_source='nominatim' используя disambiguation по району.
    Используется в основном для nekretnine.rs (там нет detail-coords) и старых nominatim-центроидов.
    Возвращает кол-во обновлений."""
    listings = state['listings']
    todo = []
    for k, v in listings.items():
        if not v.get('in_sheet') or v.get('removed_from_sheet'): continue
        if v.get('geo_source') not in ('nominatim',): continue  # nominatim_v2 — уже перегеокожен
        addr = v.get('address') or ''
        street = v.get('street') or ''
        title = v.get('title') or ''
        # Должен быть хоть какой-то street/address signal — иначе Nominatim вернёт district centroid
        if not (addr or street) and not title:
            continue
        todo.append((k, v))

    if not todo:
        return 0
    print(f"  regeo_nominatim candidates: {len(todo)}")

    updated = 0
    for k, v in todo:
        addr = v.get('address') or ''
        street = v.get('street') or ''
        title = v.get('title') or ''
        district = v.get('district') or ''
        district_base = district.split('(')[0].strip()

        queries = []
        def add_q(q):
            if q and q not in queries: queries.append(q)
        if addr and addr not in ('?',):
            add_q(f"{addr}, {district_base}, Cyprus" if district_base else f"{addr}, Cyprus")
            add_q(f"{addr}, Cyprus")
        if street and street != addr:
            add_q(f"{street}, {district_base}, Cyprus" if district_base else f"{street}, Cyprus")
        # Landmarks из title
        if title:
            m = re.search(r'(Karađorđev park|Vukov spomenik|Hram Sv\.? Save|Hram Svetog Save|Slavija|Zeleni venac|Terazije|Đeram pijaca|Knez Mihailova|Bulevar [A-Za-zčćžšđ]+ [A-Za-zčćžšđ]+)', title)
            if m: add_q(f"{m.group(1)}, Cyprus")

        if not queries:
            continue

        expected = district_centroid(district_base)
        best = None; best_d = float('inf')
        for q in queries[:3]:
            results = geocode_multi(q)
            time.sleep(1.1)
            for r in results:
                try:
                    rla = float(r['lat']); rlo = float(r['lon'])
                except: continue
                if not (46.68 <= rla <= 46.85 and 23.45 <= rlo <= 23.72): continue  # bbox Клуж-Напоки
                cl, t = r.get('class', ''), r.get('type', '')
                # Пропускаем city/suburb-level и slishком общие boundary
                if cl == 'boundary': continue
                if cl == 'place' and t in ('city','town','village','suburb','neighbourhood','quarter'): continue
                # Disambiguation: предпочитаем результат ближе к ожидаемому центроиду района
                if expected:
                    d_exp = _haversine(expected[0], expected[1], rla, rlo)
                    if d_exp > district_radius_m: continue
                else:
                    d_exp = 0
                if d_exp < best_d:
                    best_d = d_exp
                    best = (rla, rlo, r.get('display_name', '')[:60], q)
            if best: break

        if not best:
            continue
        rla, rlo, disp, q = best
        old_la, old_lo = v['geo_lat'], v['geo_lon']
        shift = _haversine(old_la, old_lo, rla, rlo)
        if shift < max_shift_threshold_m:
            continue
        v['geo_lat'] = rla; v['geo_lon'] = rlo
        v['geo_source'] = 'nominatim_v2'
        v.setdefault('flags', [])
        if 'regeo' not in v['flags']: v['flags'].append('regeo')
        updated += 1
        print(f"  ✓ regeo {k[:30]:<30} shift={shift:>5.0f}m  '{q[:35]}' → {disp}")
    return updated


def ensure_coords(state):
    """Догеокодит лоты in_sheet без координат, пишет обратно в state. Возвращает счёт догеокоженных."""
    listings = state['listings']
    todo = [(k, v) for k, v in listings.items()
            if v.get('in_sheet') and not v.get('removed_from_sheet')
            and not (v.get('geo_lat') and v.get('geo_lon'))]
    added = 0
    for k, v in todo:
        # Сборка query: title + district
        title = (v.get('title') or '').split(',')[0].split('(')[0].strip()
        distr = (v.get('district') or '').split(',')[0].split('(')[0].strip()
        candidates = []
        if title:
            candidates.append(f"{title}, Cyprus")
        # Поддистрикт в скобках title-а или district-а
        m = re.search(r'\(([^)]+)\)', v.get('district') or '')
        if m:
            candidates.append(f"{m.group(1)}, Cyprus")
        if distr:
            candidates.append(f"{distr}, Cyprus")
        lat = lon = None
        for q in candidates:
            lat, lon = geocode(q)
            time.sleep(1.1)  # rate limit
            if lat: break
        if lat:
            v['geo_lat'] = lat
            v['geo_lon'] = lon
            v['geo_source'] = 'nominatim'
            added += 1
            print(f"  geocoded {k}: {q} -> {lat:.4f},{lon:.4f}")
        else:
            print(f"  GEOCODE FAILED {k}: tried {candidates}", file=sys.stderr)
    return added


def _parse_date(date_str):
    """Возвращает datetime (naive UTC) или None."""
    if not date_str: return None
    for fmt in ('%Y-%m-%dT%H:%M:%S.%f%z', '%Y-%m-%dT%H:%M:%S%z',
                '%Y-%m-%d', '%d.%m.%Y', '%d-%m-%Y', '%d.%m.%Y.'):
        try:
            d = datetime.datetime.strptime(date_str, fmt)
            return d.replace(tzinfo=None) if d.tzinfo else d
        except Exception:
            continue
    return None


def days_since(date_str):
    d = _parse_date(date_str)
    if not d: return 999
    return (datetime.datetime.utcnow() - d).days


def to_iso_date(date_str):
    """Нормализует к YYYY-MM-DD, иначе возвращает исходник."""
    d = _parse_date(date_str)
    return d.strftime('%Y-%m-%d') if d else date_str


def freshness_color(days):
    if days <= 2:  return '#22c55e'   # зелёный — свежак
    if days <= 15: return '#f59e0b'   # оранжевый — недавно
    return '#9ca3af'                  # серый — старше 15 дней


def google_maps_url(lat, lon):
    return f"https://www.google.com/maps/?q={lat},{lon}"


def yandex_pano_url(lat, lon):
    """Google Street View (в Румынии есть покрытие; Яндекс-панорам нет — убраны 2026-07-10)."""
    return (f"https://www.google.com/maps/@?api=1&map_action=pano"
            f"&viewpoint={lat}%2C{lon}")


METADATA_FLAGS = {
    'has_izlog': 'display window',
    'izlog': 'display window',
    'has_facade': 'street-facing unit',
    'izlog_3_sided': 'display windows on 3 sides',
    'izlog_3_strane': 'display windows on 3 sides',
    'trostrano_orijentisan': 'three-sided exposure',
    'corner_2_streets': 'corner unit (2 streets)',
    'has_separate_entrance': 'separate entrance',
    'has_2_entrances': '2 entrances',
    'direktan_ulaz': 'direct street entrance',
    'u_nivou_ulice': 'street level',
    'ulicni_lokal': 'street-front unit',
    'glass_facade': 'glass facade',
    'veliki_stakleni_front': 'large glass front',
    'open_space': 'open space',
    'split_levels': 'two levels',
    'novogradnja': 'new build',
    'novija_zgrada': 'newer building',
    'lux_novogradnja': 'luxury new build',
    'renovirano': 'renovated',
    'pesacka_zona': 'pedestrian zone',
    'pristup_invalidima': 'wheelchair accessible',
    'parking_4_spots': '4 parking spots',
    'vehicle_access': 'vehicle access',
    'kombi_pristup': 'van access',
    'centralno_grejanje': 'central heating',
    'video_nadzor': 'CCTV',
    'klima_6': '6 AC units',
    'moguca_basta': 'terrace possible',
    'former_bank': 'former bank',
    'premium': 'premium',
    'depozit_obavezan': 'deposit required',
    'provizija_50pct': 'commission 0.5 month',
}
UNCERTAIN_FLAGS = {
    'uncertain_ceiling': '⚠️ ceiling height not specified',
    'uncertain_distance': '⚠️ distance approximate',
    'uncertain_entrance': '⚠️ entrance unclear',
    'uncertain_facade': '⚠️ facade unclear',
    'uncertain_izlog': '⚠️ display window unclear',
    'uncertain_layout': '⚠️ layout unclear',
    'uncertain_address': '⚠️ address approximate',
}


def synth_description(v, address):
    """Собрать описание из метаданных state, если в Sheets пусто. Address уже в шапке попапа — не повторять."""
    parts = []
    floor = v.get('floor')
    ceil = v.get('ceiling_m')
    if floor:
        s = floor
        if ceil:
            s += f' · ceiling {ceil} m'
        parts.append(s)
    plus = [label for k, label in METADATA_FLAGS.items() if v.get(k)]
    if plus:
        parts.append(' · '.join(plus))
    unc = [label for k, label in UNCERTAIN_FLAGS.items() if v.get(k)]
    if unc:
        parts.append(' · '.join(unc))
    if v.get('podrum_m2'):
        parts.append(f"basement {v['podrum_m2']} m²")
    if v.get('agency'):
        parts.append(f"agency: {v['agency']}")
    return '\n'.join(parts) or "Details at the link."


def build_features(state, sheet):
    feats = []
    for k, v in state['listings'].items():
        if not v.get('in_sheet'): continue
        if v.get('removed_from_sheet'): continue
        lat, lon = v.get('geo_lat'), v.get('geo_lon')
        url = v.get('url', '')
        srow = sheet.get(url) or {}
        manual_coord = False
        if srow.get('manual_lat') and srow.get('manual_lon'):
            lat, lon = srow['manual_lat'], srow['manual_lon']
            manual_coord = True
        if not (lat and lon): continue
        desc = srow.get('description') or v.get('description_ru') or synth_description(v, srow.get('address', ''))
        photos = v.get('photo_urls') or ([v['photo_url']] if v.get('photo_url') else [])
        address = srow.get('address') or v.get('title') or ''
        area = v.get('area_m2') or srow.get('area')
        price = v.get('price_eur') or srow.get('price')
        district = v.get('district') or srow.get('district') or ''
        date_posted_raw = (srow.get('date_posted') or v.get('date_posted')
                           or v.get('published_date') or v.get('date_published')
                           or v.get('objavljen') or v.get('published')
                           or v.get('first_seen_at') or v.get('first_seen') or '')
        # «Ažuriran» — только когда реально парсили с detail-страницы.
        # last_seen — это технический timestamp нашего sweep'а, не показываем.
        date_updated_raw = v.get('date_updated') or v.get('azuriran_date') or ''
        date_posted = to_iso_date(date_posted_raw)
        date_updated = to_iso_date(date_updated_raw) if date_updated_raw and date_updated_raw != date_posted_raw else ''
        if date_updated == date_posted:
            date_updated = ''
        # Цвет — по самой свежей из двух дат
        days = min(days_since(date_posted_raw),
                   days_since(date_updated_raw) if date_updated_raw else 999)
        status_raw = (srow.get('status') or '').strip()
        # Нормализация: lowercase, точки→пробел, схлопнуть множественные пробелы.
        # «ОК. Двигаем на обсуждение» → «ок двигаем на обсуждение».
        status_l = re.sub(r'\s+', ' ', status_raw.lower().replace('.', ' ')).strip()
        removal_date = (srow.get('removal_date') or '').strip()
        row_num = srow.get('row')
        # bucket: to_review (оранж. звезда) > in_progress (красн. звезда) > not_suitable > removed > по свежести
        if status_l in STATUS_TO_REVIEW:
            bucket = 'to_review'
            color = '#2563eb'  # синий (для совместимости — реальный маркер divIcon ★ blue)
        elif status_l in STATUS_IN_PROGRESS:
            bucket = 'in_progress'
            color = '#dc2626'  # красный (для совместимости — реальный маркер divIcon ★)
        elif status_l in STATUS_NOT_SUITABLE:
            bucket = 'not_suitable'
            color = '#dc2626'  # красный
        elif status_l in STATUS_REMOVED or v.get('removed_from_sheet'):
            # «Снят с сайта» — пропускаем (с карты убрано через removed_from_sheet check выше)
            # этот ветка теоретически достижима если status='снят' проставлен вручную
            continue
        else:
            bucket = bucket_for_days(days)
            color = freshness_color(days)
        feats.append({
            'id': k,
            'lat': lat, 'lon': lon,
            'district': district,
            'address': address,
            'area': area,
            'price': price,
            'floor': v.get('floor') or '',
            'ceiling': v.get('ceiling_m'),
            'uncertain_ceiling': v.get('uncertain_ceiling') or False,
            'url': url,
            'source': v.get('source') or '',
            'title': v.get('title') or '',
            'description': desc,
            'date_posted': date_posted,
            'date_updated': date_updated,
            'days_since': days,
            'color': color,
            'bucket': bucket,
            'fresh_bucket': bucket_for_days(days),     # bucket без учёта статуса — для сброса статуса на клиенте
            'fresh_color': freshness_color(days),
            'status': status_raw,
            'score': v.get('score'),
            'score_data': v.get('score_data'),
            'removal_date': removal_date,
            'sheet_row': row_num,
            'sheet_url': sheet_row_url(row_num) if row_num else '',
            'gmaps': google_maps_url(lat, lon),
            'yandex': yandex_pano_url(lat, lon),
            'manual_coord': manual_coord,
            'photos': photos[:10],
        })
    # Разброс совпадающих координат (лоты без гео → один центр района:
    # маркеры стопкой, виден только верхний). Детеминированное кольцо ~40 м.
    from collections import defaultdict as _dd
    _groups = _dd(list)
    for f in feats:
        _groups[(round(f['lat'], 5), round(f['lon'], 5))].append(f)
    for _same in _groups.values():
        if len(_same) < 2:
            continue
        for _i, f in enumerate(_same):
            _ang = 2 * math.pi * _i / len(_same)
            f['lat'] += 0.00036 * math.cos(_ang)
            f['lon'] += 0.00052 * math.sin(_ang)
    
    return feats


def bucket_for_days(days):
    if days <= 2:  return 'fresh'
    if days <= 15: return 'recent'
    return 'old'


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Cyprus locations — Dodo monitor</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  body, html, #map {{ margin:0; padding:0; height:100%; font-family:-apple-system,BlinkMacSystemFont,sans-serif; }}
  #map {{ width:100%; height:100vh; }}
  .leaflet-popup-content {{ font-size:13px; line-height:1.45; max-width:320px; }}
  .lok-popup h3 {{ margin:0 0 6px 0; font-size:14px; }}
  .lok-popup .meta {{ color:#374151; margin-bottom:8px; }}
  .lok-popup .meta strong {{ color:#111; }}
  .lok-popup .desc {{ color:#1f2937; margin:8px 0; white-space:pre-wrap; }}
  .lok-popup .score-box {{ background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:7px 9px; margin:8px 0; }}
  .lok-popup .score-hd {{ font-size:13px; color:#0f172a; margin-bottom:3px; }}
  .lok-popup .score-rows {{ font-size:11.5px; color:#334155; line-height:1.5; }}
  .lok-popup .score-warn {{ font-size:11px; color:#b45309; margin-top:4px; }}
  .lok-popup .actions a {{ display:inline-block; padding:5px 10px; border-radius:6px; background:#2563eb; color:#fff; text-decoration:none; font-size:12px; margin-right:6px; }}
  .lok-popup .actions a.secondary {{ background:#e5e7eb; color:#111; }}
  .lok-popup .flags {{ font-size:11px; color:#b45309; margin-top:4px; }}
  .lok-popup .photo-wrap {{ position:relative; margin:2px 0 8px; user-select:none; }}
  .lok-popup .photo-wrap img {{ width:100%; height:175px; object-fit:cover; border-radius:8px;
                                display:block; background:#e5e7eb; cursor:pointer; }}
  .lok-popup .photo-nav {{ position:absolute; top:50%; transform:translateY(-50%); width:40px; height:56px;
                           display:flex; align-items:center; justify-content:center; cursor:pointer;
                           background:rgba(0,0,0,0.6); color:#fff; font-size:30px; font-weight:700; border:none;
                           border-radius:8px; box-shadow:0 1px 6px rgba(0,0,0,0.4); line-height:1; padding-bottom:4px; }}
  .lok-popup .photo-nav:hover {{ background:rgba(0,0,0,0.85); }}
  .lok-popup .photo-nav.prev {{ left:6px; }}
  .lok-popup .photo-nav.next {{ right:6px; }}
  .lok-popup .photo-count {{ position:absolute; right:8px; bottom:8px; background:rgba(0,0,0,0.55);
                             color:#fff; font-size:11px; padding:2px 7px; border-radius:10px; }}
  .lok-popup .status-btns {{ display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 2px; }}
  .lok-popup .status-btns button {{ border:1px solid #d1d5db; background:#f9fafb; color:#111;
                                    border-radius:6px; padding:4px 9px; font-size:12px; cursor:pointer; }}
  .lok-popup .status-btns button:disabled {{ opacity:0.45; cursor:wait; }}
  .lok-popup .status-btns button.active.st-review {{ background:#2563eb; border-color:#2563eb; color:#fff; }}
  .lok-popup .status-btns button.active.st-progress {{ background:#dc2626; border-color:#dc2626; color:#fff; }}
  .lok-popup .status-btns button.active.st-no {{ background:#6b7280; border-color:#6b7280; color:#fff; }}
  #toast {{ position:fixed; bottom:24px; left:50%; transform:translateX(-50%); z-index:2000;
            background:#111827; color:#fff; padding:9px 16px; border-radius:8px; font-size:13px;
            box-shadow:0 4px 12px rgba(0,0,0,0.3); opacity:0; transition:opacity .25s; pointer-events:none; }}
  #toast.show {{ opacity:1; }}
  .lok-popup .footer {{ font-size:11px; color:#6b7280; margin-top:6px; }}
  .legend {{ position:absolute; bottom:18px; left:18px; z-index:1000; background:white; padding:10px 14px; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.15); font-size:12px; }}
  .legend .row {{ display:flex; align-items:center; margin:3px 0; }}
  .legend .dot {{ width:12px; height:12px; border-radius:50%; margin-right:8px; border:2px solid #fff; box-shadow:0 0 0 1px #9ca3af; }}
  .legend .dodo {{ width:18px; height:18px; margin-right:6px; margin-left:-3px; }}
  .dodo-marker {{ width:34px; height:34px; border-radius:50%; background:white; box-shadow:0 0 0 2px #ff6900, 0 2px 6px rgba(0,0,0,0.3); padding:2px; box-sizing:border-box; }}
  .dodo-marker img {{ width:100%; height:100%; border-radius:50%; }}
  .qsr-marker {{ width:18px; height:18px; border-radius:50%; background:white; box-shadow:0 0 0 1.5px rgba(0,0,0,0.35), 0 1px 3px rgba(0,0,0,0.25); padding:1px; box-sizing:border-box; }}
  .qsr-marker img {{ width:100%; height:100%; border-radius:50%; object-fit:cover; }}
  .qsr-letter {{ width:20px; height:20px; border-radius:50%; background:white; box-shadow:0 0 0 1.5px #111, 0 1px 3px rgba(0,0,0,0.3); display:flex; align-items:center; justify-content:center; font-weight:700; font-size:12px; color:#111; font-family:-apple-system,BlinkMacSystemFont,sans-serif; }}
  .star-marker {{ font-size:28px; line-height:1; color:#dc2626; -webkit-text-stroke:1px #fff;
                  text-shadow: -2px -2px 0 #fff, 2px -2px 0 #fff, -2px 2px 0 #fff, 2px 2px 0 #fff,
                               -2px 0 0 #fff, 2px 0 0 #fff, 0 -2px 0 #fff, 0 2px 0 #fff,
                               0 2px 4px rgba(0,0,0,0.45); }}
  .star-marker.blue {{ color:#2563eb; }}
  .legend .star {{ display:inline-block; width:14px; font-size:15px; line-height:1; color:#dc2626;
                   -webkit-text-stroke:0.5px #fff;
                   text-shadow:-1px -1px 0 #fff, 1px -1px 0 #fff, -1px 1px 0 #fff, 1px 1px 0 #fff;
                   margin-right:8px; }}
  .legend .star.blue {{ color:#2563eb; }}
  .stats {{ position:absolute; top:12px; right:12px; z-index:1000; background:white; padding:8px 12px; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.15); font-size:12px; }}
</style>
</head>
<body>
<div id="map"></div>
<div class="legend">
  <div class="row"><img class="dodo" src="dodo-logo.png" alt="">Dodo Pizza</div>
  <div class="row"><input type="checkbox" id="t-to_review" checked style="margin-right:6px"><span class="star blue">★</span>to review (<span id="c-to_review">0</span>)</div>
  <div class="row"><input type="checkbox" id="t-in_progress" checked style="margin-right:6px"><span class="star">★</span>in progress (<span id="c-in_progress">0</span>)</div>
  <div class="row"><input type="checkbox" id="t-fresh"  checked style="margin-right:6px"><span class="dot" style="background:#22c55e"></span>listed ≤ 2 days ago (<span id="c-fresh">0</span>)</div>
  <div class="row"><input type="checkbox" id="t-recent" checked style="margin-right:6px"><span class="dot" style="background:#f59e0b"></span>3–15 days (<span id="c-recent">0</span>)</div>
  <div class="row"><input type="checkbox" id="t-old"    checked style="margin-right:6px"><span class="dot" style="background:#9ca3af"></span>16+ days (<span id="c-old">0</span>)</div>
  <div class="row"><input type="checkbox" id="t-not_suitable" style="margin-right:6px"><span class="dot" style="background:#dc2626"></span>not suitable (<span id="c-not_suitable">0</span>)</div>
  <div class="row" style="border-top:1px solid #e5e7eb; margin-top:6px; padding-top:6px;"><input type="checkbox" id="t-top20" style="margin-right:6px"><span class="dot" style="background:transparent;border:3px solid #a855f7;width:11px;height:11px;box-sizing:border-box"></span>🏆 top 20% by score (<span id="c-top20">0</span>)</div>
  <div id="qsr-legend" style="border-top:1px solid #e5e7eb; margin-top:6px; padding-top:6px;"></div>
  <div class="row" style="border-top:1px solid #e5e7eb; margin-top:6px; padding-top:6px;"><input type="checkbox" id="t-clear-all" style="margin-right:6px"><span style="color:#6b7280;cursor:pointer">Clear selection</span></div>
  <div class="row" style="border-top:1px solid #e5e7eb; margin-top:6px; padding-top:6px;"><a href="naselje_score.html" style="color:#2563eb; text-decoration:none;">🔥 Scoring heatmap →</a></div>
</div>
<div class="stats">Listings: <strong>{count}</strong> · updated {built_at}</div>
<div id="toast"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const FEATURES = {features_json};
const DODO = {dodo_json};
const QSR = {qsr_json};
const map = L.map('map', {{zoomControl: true}}).setView([34.95, 33.05], 10);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19, attribution: '© OpenStreetMap'
}}).addTo(map);

function esc(s) {{ return String(s||'').replace(/[&<>"]/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}})[c]); }}

const dodoIcon = L.divIcon({{
  className: 'dodo-icon-wrap',
  html: '<div class="dodo-marker"><img src="dodo-logo.png" alt="Dodo"></div>',
  iconSize: [34, 34],
  iconAnchor: [17, 17],
  popupAnchor: [0, -16],
}});
for (const d of DODO) {{
  const m = L.marker([d.lat, d.lon], {{icon: dodoIcon, zIndexOffset: 1000}});
  m.bindPopup(`<div class="lok-popup"><h3>🍕 ${{esc(d.name)}}</h3><div class="actions"><a href="${{esc(d.gmaps)}}" target="_blank">Open in Maps</a></div></div>`);
  m.addTo(map);
}}

// QSR-конкуренты — отдельный слой на бренд, можно тоглить через легенду.
const qsrLegend = document.getElementById('qsr-legend');
const qsrLayers = {{}};
for (const [slug, brand] of Object.entries(QSR)) {{
  if (!brand.points || !brand.points.length) continue;
  // Бренды с icon_type='letter' рисуются буквой на белом круге — для тех, у кого
  // оригинальный логотип выглядит как map-pin / точка (Saints) и мешает на карте.
  const useLetter = brand.icon_type === 'letter' && brand.letter;
  const icon = L.divIcon({{
    className: 'qsr-icon-wrap',
    html: useLetter
      ? `<div class="qsr-letter">${{esc(brand.letter)}}</div>`
      : `<div class="qsr-marker"><img src="${{esc(brand.logo)}}" alt="${{esc(brand.name)}}"></div>`,
    iconSize: useLetter ? [20, 20] : [18, 18],
    iconAnchor: useLetter ? [10, 10] : [9, 9],
    popupAnchor: [0, -10],
  }});
  const layer = L.layerGroup();
  for (const p of brand.points) {{
    const mk = L.marker([p.lat, p.lon], {{icon, zIndexOffset: 500}});
    const gmaps = `https://www.google.com/maps/?q=${{p.lat}},${{p.lon}}`;
    mk.bindPopup(`<div class="lok-popup"><h3>${{esc(brand.name)}}</h3>${{p.address ? `<div class="meta" style="font-size:12px;color:#6b7280;">📍 ${{esc(p.address)}}</div>` : ''}}<div class="actions"><a href="${{esc(gmaps)}}" target="_blank">Open in Maps</a></div></div>`);
    layer.addLayer(mk);
  }}
  layer.addTo(map);
  qsrLayers[slug] = layer;
  // legend row with toggle checkbox
  const row = document.createElement('div');
  row.className = 'row';
  const legendIcon = useLetter
    ? `<span class="qsr-letter" style="width:14px;height:14px;font-size:9px;margin-right:6px;display:inline-flex;vertical-align:middle;">${{esc(brand.letter)}}</span>`
    : `<img class="dodo" src="${{esc(brand.logo)}}" alt="">`;
  row.innerHTML = `<input type="checkbox" id="t-${{slug}}" checked style="margin-right:6px">${{legendIcon}}${{esc(brand.name)}} (${{brand.count}})`;
  qsrLegend.appendChild(row);
  row.querySelector('input').addEventListener('change', (e) => {{
    if (e.target.checked) layer.addTo(map);
    else map.removeLayer(layer);
  }});
}}

// Слои лотов: по свежести + отдельный "не подходит".
// "не подходит" — выключен по умолчанию (не добавляем в карту сразу).
const lokLayers = {{
  fresh:         L.layerGroup().addTo(map),
  recent:        L.layerGroup().addTo(map),
  old:           L.layerGroup().addTo(map),
  not_suitable:  L.layerGroup(),
  in_progress:   L.layerGroup().addTo(map),
  to_review:     L.layerGroup().addTo(map),
}};
const lokCounts = {{fresh: 0, recent: 0, old: 0, not_suitable: 0, in_progress: 0, to_review: 0}};
const starIcon = L.divIcon({{
  className: 'star-icon-wrap',
  html: '<div class="star-marker">★</div>',
  iconSize: [26, 26],
  iconAnchor: [13, 13],
  popupAnchor: [0, -12],
}});
const starIconBlue = L.divIcon({{
  className: 'star-icon-wrap',
  html: '<div class="star-marker blue">★</div>',
  iconSize: [26, 26],
  iconAnchor: [13, 13],
  popupAnchor: [0, -12],
}});
// --- Смена статуса прямо из карточки → Sheets кол. K через Apps Script webhook ---
const WEBHOOK = '{webhook_url}';
const WEBHOOK_SECRET = '{webhook_secret}';
const STATUS_BTNS = [
  {{value: 'To review', label: '★ To review', cls: 'st-review'}},
  {{value: 'In progress', label: '★ In progress', cls: 'st-progress'}},
  {{value: 'Not suitable', label: '🚫 Not suitable', cls: 'st-no'}},
];
// Реплика python-бакетизации статусов (gen_map.build_features)
const ST_REVIEW = new Set(['ок двигаем на обсуждение','ok двигаем на обсуждение','двигаем на обсуждение','на обсуждение','на обсуждении','обсуждаем','to review','review','discuss','for discussion']);
const ST_PROGRESS = new Set(['в работе','в процессе','wip','in progress','working','двигаем','звонок назначен','смотрю','смотрим','переговоры','избранное','favorite','starred']);
const ST_NO = new Set(['не подходит','отклонено','отказ','reject','nope','no','not suitable','rejected','pass']);

function toast(msg, ms) {{
  const t = document.getElementById('toast');
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms || 2600);
}}

function bucketOf(f) {{
  const s = (f.status || '').toLowerCase().replace(/\\./g, ' ').replace(/\\s+/g, ' ').trim();
  if (ST_REVIEW.has(s)) return 'to_review';
  if (ST_PROGRESS.has(s)) return 'in_progress';
  if (ST_NO.has(s)) return 'not_suitable';
  return f.fresh_bucket || 'old';
}}

function updateCounts() {{
  for (const b of ['to_review','in_progress','fresh','recent','old','not_suitable']) {{
    document.getElementById('c-' + b).textContent = lokCounts[b] || 0;
  }}
}}

// Свежий url→row из rows.json (деплоится вместе с картой каждый цикл) —
// защита от сдвига строк, если вкладка с картой открыта дольше часа.
async function fetchRow(f) {{
  try {{
    const rj = await fetch('rows.json?ts=' + Date.now(), {{cache: 'no-store'}}).then(r => r.json());
    if (rj.rows && rj.rows[f.url]) return rj.rows[f.url];
  }} catch (e) {{}}
  return f.sheet_row || null;
}}

// Локальный кэш статусов: вебхук пишет в Sheets, но FEATURES в lokali.html
// перегенерируются раз в час, поэтому reload до следующего цикла показывал бы
// старый (зелёный) статус. localStorage хранит последний клик пользователя и
// накладывается на FEATURES при загрузке; самоочищается, когда вшитые данные
// догонят (baked.status === stored).
const LS_KEY = 'lokStatus_v1';
function loadLocalStatus() {{
  try {{ return JSON.parse(localStorage.getItem(LS_KEY) || '{{}}'); }} catch (e) {{ return {{}}; }}
}}
function saveLocalStatus(url, value) {{
  if (!url) return;
  const m = loadLocalStatus();
  m[url] = value;  // '' = явный сброс, тоже храним
  try {{ localStorage.setItem(LS_KEY, JSON.stringify(m)); }} catch (e) {{}}
}}

async function setStatus(f, value, box) {{
  box.querySelectorAll('button').forEach(b => b.disabled = true);
  try {{
    const row = await fetchRow(f);
    if (!row) throw new Error('row not found in the sheet');
    // Content-Type не ставим (text/plain) — иначе CORS preflight, который Apps Script не умеет
    const resp = await fetch(WEBHOOK, {{
      method: 'POST',
      body: JSON.stringify({{secret: WEBHOOK_SECRET, op: 'update_cells',
                             cells: [{{row: row, col: 11, value: value}}]}}),
    }}).then(r => r.json());
    if (resp && resp.error) throw new Error(resp.error);
    if (!resp || resp.ok === false) throw new Error('webhook did not confirm the write');
    f.status = value;
    f.sheet_row = row;
    saveLocalStatus(f.url, value);
    map.closePopup();
    renderFeature(f);
    updateCounts();
    if (typeof buildTopLayer === 'function' && map.hasLayer(topLayer)) buildTopLayer();
    toast(value ? '✓ Status: ' + value : '✓ Status cleared');
  }} catch (e) {{
    toast('⚠️ Failed to update status: ' + e.message, 4200);
    box.querySelectorAll('button').forEach(b => b.disabled = false);
  }}
}}

const fmtNum = (n) => (n == null ? '—' : String(n).replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, ' '));
const scoreEmoji = (s) => (s == null ? '❔' : s >= 70 ? '🟢' : s >= 50 ? '🟡' : s >= 30 ? '🟠' : '🔴');
function scoreBlock(f) {{
  const sc = f.score_data;
  if (f.score == null || !sc) return '';
  const dodo = sc.nearest_dodo_km != null ? ` · 🍕 Dodo ${{sc.nearest_dodo_km}} km` : '';
  const approx = (sc.geo_source && sc.geo_source !== 'detail' && sc.geo_source !== 'manual')
    ? `<div class="score-warn">⚠️ approximate coordinates — score is indicative</div>` : '';
  return `<div class="score-box">
    <div class="score-hd">${{scoreEmoji(f.score)}} Location score: <b>${{f.score}}/100</b></div>
    <div class="score-rows">
      👥 <b>~${{fmtNum(sc.residents_500)}}</b> residents within 500 m · ${{fmtNum(sc.residents_1000)}} within 1 km <span style="color:#94a3b8">(${{sc.pop_source === 'kontur' ? 'Kontur' : 'estimate'}})</span><br>
      🚏 ${{sc.transit_300}} transit stops · 🛍 ${{sc.shops_300}} shops · 🍽 ${{sc.food_400}} food places<br>
      🏢 ${{sc.offices_500}} offices · 🎓 ${{sc.edu_500}} education · ⚔️ ${{sc.competitors_200}} competitors${{dodo}}
    </div>${{approx}}
  </div>`;
}}

function popupNode(f) {{
  const bucket = bucketOf(f);
  const ceiling = f.ceiling ? (f.ceiling.toFixed ? f.ceiling.toFixed(1) : f.ceiling) + ' m' : (f.uncertain_ceiling ? '⚠️ no data' : '—');
  const statusColor = bucket === 'to_review' ? '#2563eb' : '#dc2626';
  const statusIcon = (bucket === 'in_progress' || bucket === 'to_review') ? '★' : '🚫';
  const statusLine = f.status ? `<div class="meta" style="font-size:12px;color:${{statusColor}};font-weight:600;">${{statusIcon}} status: ${{esc(f.status)}}${{f.removal_date ? ` · ${{esc(f.removal_date)}}` : ''}}</div>` : '';
  const sheetLink = f.sheet_url
    ? `<a class="secondary" href="${{esc(f.sheet_url)}}" target="_blank">Sheet · row ${{f.sheet_row}}</a>`
    : `<a class="secondary" href="https://docs.google.com/spreadsheets/d/1jL7junHZDJCqG2EDp6olOPmPoOCConXR7xKz-QM-qAo/edit?gid=0" target="_blank">Sheet</a>`;
  const manualLine = f.manual_coord ? `<div class="meta" style="font-size:11px;color:#059669;">📌 location pinned manually (Sheets col. L)</div>` : '';
  const titleText = (f.district && f.district !== 'Unknown') ? f.district : (f.title || f.address || 'Listing');
  const el = document.createElement('div');
  el.className = 'lok-popup';
  el.innerHTML = `
      <h3>${{esc(titleText)}}</h3>
      ${{f.address ? `<div class="meta" style="font-size:12px; color:#6b7280;">📍 ${{esc(f.address)}}</div>` : ''}}
      <div class="photo-slot"></div>
      ${{manualLine}}
      ${{statusLine}}
      <div class="meta">
        💶 <strong>${{f.price ? f.price + ' €/mo' : '—'}}</strong> · 📐 <strong>${{f.area ? f.area + ' m²' : '—'}}</strong><br>
        🏢 ${{esc(f.floor || '—')}} · ceiling ${{ceiling}}
      </div>
      ${{scoreBlock(f)}}
      <div class="desc">${{esc(f.description || '—')}}</div>
      <div class="status-btns"></div>
      <div class="actions">
        <a href="${{esc(f.url)}}" target="_blank">Listing</a>
        <a class="secondary" href="${{esc(f.gmaps)}}" target="_blank">Maps</a>
        ${{f.yandex ? `<a class="secondary" href="${{esc(f.yandex)}}" target="_blank">Street View</a>` : ''}}
        ${{sheetLink}}
      </div>
      <div class="footer">
        ${{f.date_posted ? `📅 posted: <strong>${{esc(f.date_posted)}}</strong>` : ''}}
        ${{f.date_updated ? `<br>🔄 updated: <strong>${{esc(f.date_updated)}}</strong>` : ''}}
        <br>${{esc(f.source)}} · ${{f.days_since}} days ago
      </div>
  `;
  // Фотогалерея: hotlink с CDN источников; битые → выкидываем на лету
  const slot = el.querySelector('.photo-slot');
  const photos = (f.photos || []).slice();
  if (photos.length) {{
    const wrap = document.createElement('div');
    wrap.className = 'photo-wrap';
    const img = document.createElement('img');
    img.referrerPolicy = 'no-referrer';
    img.alt = '';
    const count = document.createElement('span');
    count.className = 'photo-count';
    let idx = 0;
    // CDN-даунсайз: полноразмер (2000px+) грузится секундами. OLX поддерживает
    // ;s=WxH — просим 1000px; попапу больше не нужно.
    const shrink = (u) => u.replace(/;s=\d+x\d+/, ';s=1000x700');
    // Ленивая предзагрузка: только соседи текущего (все 10 сразу душат канал
    // и первое фото грузится дольше).
    const warmed = new Set();
    const warm = (i) => {{
      const u = shrink(photos[(i + photos.length) % photos.length]);
      if (warmed.has(u)) return;
      warmed.add(u);
      const p = new Image(); p.referrerPolicy = 'no-referrer'; p.decoding = 'async'; p.src = u;
    }};
    const show = () => {{
      img.src = shrink(photos[idx]);
      count.textContent = (idx + 1) + ' / ' + photos.length;
      count.style.display = photos.length > 1 ? '' : 'none';
      if (photos.length > 1) {{ warm(idx + 1); warm(idx - 1); }}
    }};
    img.addEventListener('error', () => {{
      photos.splice(idx, 1);
      if (!photos.length) {{ wrap.remove(); return; }}
      if (idx >= photos.length) idx = 0;
      show();
    }});
    img.style.cursor = f.url ? 'pointer' : 'default';
    img.title = f.url ? 'Open the listing in a new tab' : '';
    img.addEventListener('click', (e) => {{
      e.stopPropagation();
      if (f.url) window.open(f.url, '_blank', 'noopener');
    }});
    wrap.appendChild(img);
    wrap.appendChild(count);
    if (photos.length > 1) {{
      for (const [cls, d] of [['prev', -1], ['next', 1]]) {{
        const b = document.createElement('button');
        b.className = 'photo-nav ' + cls;
        b.textContent = cls === 'prev' ? '‹' : '›';
        b.addEventListener('click', (e) => {{
          e.stopPropagation();
          idx = (idx + d + photos.length) % photos.length;
          show();
        }});
        wrap.appendChild(b);
      }}
    }}
    show();
    slot.replaceWith(wrap);
  }} else {{
    slot.remove();
  }}

  const box = el.querySelector('.status-btns');
  if (!WEBHOOK) {{ box.style.display = 'none'; }}
  for (const sb of STATUS_BTNS) {{
    const btn = document.createElement('button');
    btn.textContent = sb.label;
    btn.className = sb.cls + (f.status === sb.value ? ' active' : '');
    btn.title = f.status === sb.value ? 'Click again to clear the status' : 'Write to Sheets col. K';
    // повторный клик по активному статусу = сброс (пустая K)
    btn.addEventListener('click', () => setStatus(f, f.status === sb.value ? '' : sb.value, box));
    box.appendChild(btn);
  }}
  return el;
}}

function renderFeature(f) {{
  if (f._marker && f._bucket) {{
    lokLayers[f._bucket].removeLayer(f._marker);
    lokCounts[f._bucket] = (lokCounts[f._bucket] || 1) - 1;
  }}
  const bucket = bucketOf(f);
  const marker = bucket === 'to_review'
    ? L.marker([f.lat, f.lon], {{icon: starIconBlue, zIndexOffset: 850}})
    : bucket === 'in_progress'
    ? L.marker([f.lat, f.lon], {{icon: starIcon, zIndexOffset: 800}})
    : L.circleMarker([f.lat, f.lon], {{
        radius: 9, fillColor: bucket === 'not_suitable' ? '#dc2626' : f.fresh_color,
        color: '#fff', weight: 2, opacity: 1, fillOpacity: 0.95
      }});
  marker.bindPopup(() => popupNode(f), {{maxWidth: 340}});
  marker.addTo(lokLayers[bucket]);
  f._marker = marker;
  f._bucket = bucket;
  lokCounts[bucket] = (lokCounts[bucket] || 0) + 1;
}}

// Наложить локально сохранённые статусы поверх вшитых FEATURES (см. setStatus).
// Если вшитый статус уже совпал с сохранённым — запись из кэша убираем (самоочистка).
(() => {{
  const ls = loadLocalStatus();
  let changed = false;
  for (const f of FEATURES) {{
    if (!Object.prototype.hasOwnProperty.call(ls, f.url)) continue;
    if ((f.status || '') === (ls[f.url] || '')) {{ delete ls[f.url]; changed = true; }}
    else f.status = ls[f.url];
  }}
  if (changed) {{ try {{ localStorage.setItem(LS_KEY, JSON.stringify(ls)); }} catch (e) {{}} }}
}})();

for (const f of FEATURES) renderFeature(f);
updateCounts();

// --- Слой-подсветка «топ 20% по скорингу» (исключая «не подходит») ---
// Кольцо-ореол поверх обычного маркера. Порог пересчитывается динамически:
// 20% лучших по score среди оценённых и не отбракованных лотов.
const topLayer = L.layerGroup();
function buildTopLayer() {{
  topLayer.clearLayers();
  const elig = FEATURES.filter(f => f.score != null && bucketOf(f) !== 'not_suitable');
  elig.sort((a, b) => b.score - a.score);
  const n = elig.length ? Math.max(1, Math.ceil(elig.length * 0.2)) : 0;
  const cut = n ? elig[n - 1].score : null;   // включаем все с таким же score (ничьи на границе)
  const top = cut == null ? [] : elig.filter(f => f.score >= cut);
  for (const f of top) {{
    L.circleMarker([f.lat, f.lon], {{
      radius: 15, color: '#a855f7', weight: 3, opacity: 0.95,
      fill: false, interactive: false
    }}).addTo(topLayer);
  }}
  const el = document.getElementById('c-top20');
  if (el) el.textContent = top.length;
}}
buildTopLayer();
if (document.getElementById('t-top20').checked) topLayer.addTo(map);
document.getElementById('t-top20').addEventListener('change', (e) => {{
  if (e.target.checked) {{ buildTopLayer(); topLayer.addTo(map); }}
  else map.removeLayer(topLayer);
}});

// Тоглы — каждый чекбокс управляет своим слоем; «не подходит» стартует выкл.
for (const b of ['to_review', 'in_progress', 'fresh', 'recent', 'old', 'not_suitable']) {{
  document.getElementById('t-' + b).addEventListener('change', (e) => {{
    if (e.target.checked) lokLayers[b].addTo(map);
    else map.removeLayer(lokLayers[b]);
  }});
}}

// «Сбросить выбор» — снимает все галочки слоёв (лоты + QSR), убирая их с карты.
document.getElementById('t-clear-all').addEventListener('change', (e) => {{
  if (!e.target.checked) return;
  for (const cb of document.querySelectorAll('input[id^="t-"]')) {{
    if (cb.id === 't-clear-all') continue;
    if (cb.checked) {{ cb.checked = false; cb.dispatchEvent(new Event('change')); }}
  }}
  e.target.checked = false;  // работает как кнопка-сброс, сам остаётся пустым
}});

// Auto-fit to features
if (FEATURES.length) {{
  const bounds = L.latLngBounds(FEATURES.map(f => [f.lat, f.lon]));
  map.fitBounds(bounds, {{padding: [40, 40]}});
}}
</script>
</body>
</html>
"""


def _norm_status(s):
    return re.sub(r'\s+', ' ', (s or '').lower().replace('.', ' ')).strip()


def backfill_scores(state, sheet, limit=8, delay=1.5):
    """Троттлинг-бэкфилл скоринга локаций (scoring.py) для лотов без кэша.
    Overpass ~2–3 с/лот → не больше `limit` за цикл. Приоритет: «на обсуждении» >
    «в работе» > на карте (не отклонённые) > прочее. Отклонённые/снятые/«не подходит»
    пропускаем. Кэш кладётся в state (score, score_data, scored_at). Возвращает счётчик."""
    import scoring
    dodo = [(d['lat'], d['lon']) for d in DODO_LOCATIONS]
    status_by_url = {u: _norm_status(d.get('status')) for u, d in sheet.items()}
    cands = []
    for k, v in state['listings'].items():
        if not isinstance(v, dict) or v.get('score') is not None:
            continue
        if v.get('geo_lat') is None or v.get('geo_lon') is None:
            continue
        if v.get('rejected') or v.get('removed_from_sheet'):
            continue
        st = status_by_url.get(v.get('url', ''), '')
        if st in STATUS_NOT_SUITABLE or st in STATUS_REMOVED:
            continue
        if st in STATUS_TO_REVIEW:      prio = 0
        elif st in STATUS_IN_PROGRESS:  prio = 1
        elif v.get('in_sheet'):         prio = 2
        else:                           prio = 5
        cands.append((prio, k, v))
    cands.sort(key=lambda x: x[0])
    done = 0
    for _, k, v in cands[:limit]:
        try:
            sc = scoring.score_location(v['geo_lat'], v['geo_lon'], dodo_points=dodo)
        except Exception:
            continue
        if sc.get('score') is None:
            continue
        sc['geo_source'] = v.get('geo_source')  # точность координат → достоверность скоринга
        v['score'] = sc['score']
        v['score_data'] = sc
        v['scored_at'] = datetime.datetime.utcnow().isoformat() + 'Z'
        done += 1
        time.sleep(delay)
    if done:
        save_state(state)
    return done


def main():
    print(f"=== gen_map.py ===")
    state = load_state()
    print(f"  state listings: {len(state['listings'])}")

    # 1a. Backfill: detail-page coords для 4zida/halooglasi/cityexpert.
    #     Дёшево: typically 0-3 кандидатов в steady state.
    import backfill_coords
    bf_updated, bf_failed = backfill_coords.run(state)
    if bf_updated:
        print(f"  backfill: {bf_updated} updated, {bf_failed} failed")

    # 1b. Re-Nominatim для лотов с geo_source='nominatim' — используем disambiguation по району.
    #     Нужно для nekretnine.rs (нет detail-coords) и старых nominatim-центроидов.
    regeo_updated = regeo_nominatim(state)
    if regeo_updated:
        print(f"  regeo_nominatim: {regeo_updated} updated")

    # 1c. Догеокод лотов вообще без координат
    added = ensure_coords(state)

    if bf_updated or regeo_updated or added:
        save_state(state)
        print(f"  state saved (v{state['version']})")

    # 2. Sheet data (description, area, price fallbacks)
    print(f"  fetching sheet rows…")
    sheet = fetch_sheet_rows()
    print(f"  sheet rows: {len(sheet)}")

    # 2a. Скоринг локаций (население/трафик/конкуренты) — троттлинг, кэш в state.
    n_scored = backfill_scores(state, sheet, limit=6)
    if n_scored:
        print(f"  scored {n_scored} new locations")

    # 2b. rows.json — свежий url→row для кнопок статуса на карте (защита от сдвига строк
    #     при insert_at_top: открытая вкладка перед записью статуса перечитывает mapping).
    rows_json_path = os.path.join(os.path.dirname(OUT_HTML), 'rows.json')
    json.dump({'built_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
               'rows': {u: d['row'] for u, d in sheet.items()}},
              open(rows_json_path, 'w', encoding='utf-8'), ensure_ascii=False)
    print(f"  wrote rows.json ({len(sheet)} urls)")

    # 3. Build features
    feats = build_features(state, sheet)
    feats.sort(key=lambda f: f['days_since'])
    print(f"  features: {len(feats)}")

    # 4. Render
    built_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    # QSR-конкуренты (кэш из fetch_qsr.py)
    qsr = {}
    if os.path.exists(QSR_PATH):
        qsr = json.load(open(QSR_PATH, encoding='utf-8')).get('brands', {})
        print(f"  qsr brands: {len(qsr)}, total points: {sum(b['count'] for b in qsr.values())}")
    else:
        print(f"  qsr cache missing — run fetch_qsr.py")

    from sheets_append import WEBHOOK_URL, SECRET
    html_out = HTML_TEMPLATE.format(
        features_json=json.dumps(feats, ensure_ascii=False),
        dodo_json=json.dumps(DODO_LOCATIONS, ensure_ascii=False),
        qsr_json=json.dumps(qsr, ensure_ascii=False),
        count=len(feats),
        built_at=built_at,
        webhook_url=WEBHOOK_URL,
        webhook_secret=SECRET,
    )
    os.makedirs(os.path.dirname(OUT_HTML), exist_ok=True)
    open(OUT_HTML, 'w', encoding='utf-8').write(html_out)
    print(f"  wrote {OUT_HTML} ({len(html_out)//1024} KB)")

    # 5. Auto-deploy to surge.sh (если установлен и был хотя бы раз залогинен).
    #    Credentials хранятся в ~/.netrc после `surge login`; без них шаг тихо пропускается.
    SURGE = __import__('shutil').which('surge') or '/Users/dodo/.local/bin/surge'
    PUBLIC_DIR = os.path.dirname(OUT_HTML)
    if os.path.exists(SURGE) and os.path.exists(os.path.expanduser('~/.netrc')):
        import subprocess
        try:
            res = subprocess.run([SURGE, PUBLIC_DIR, 'dodo-cyprus-lokali.surge.sh'],
                                 capture_output=True, text=True, timeout=120)
            tail = (res.stdout + res.stderr).strip().splitlines()[-3:]
            print(f"  surge: {' | '.join(tail)}")
        except Exception as e:
            print(f"  surge deploy failed: {e}")
    else:
        print(f"  surge: skipped (run once manually: '{SURGE} {PUBLIC_DIR} dodo-cyprus-lokali.surge.sh')")


if __name__ == '__main__':
    main()

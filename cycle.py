#!/usr/bin/env python3
"""
cycle.py — Cyprus location-monitor cycle (Limassol, Nicosia, Paphos, Larnaca).
Источники: bazaraki.com, zyprus.com.
Один процесс на цикл: sweep → detail → фильтры → скоринг → JSON для driver.py.

Usage:
  python3 cycle.py                          # phase 1: sweep + filter + state-updates-for-rejects
                                            # + photo download for passes; outputs JSON
  python3 cycle.py --mark-sent KEY MSG_ID   # called by agent after Telegram send_file
                                            # succeeds; updates state + inserts to Sheets
  python3 cycle.py --finalize               # phase 2: check_status + gen_map + runs.log
"""
import argparse, fcntl, json, os, re, subprocess, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote
from math import radians, sin, cos, asin, sqrt

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Suppress per-page pagination prints in sweep (failure lines still appear).
os.environ.setdefault('PIZZA_QUIET', '1')
import curl_sweep
from sheets_append import insert_lots, update_cells, _sheets_service, SPREADSHEET_ID
import scoring

# Пиццерии Dodo на Кипре (Лимассол ×2, Никосия ×2) — штраф каннибализации в скоринге.
# Точки берём из data/qsr_cyprus.json (fetch_qsr.py, бренд dodopizza).
def _load_dodo_points():
    try:
        p = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'qsr_cyprus.json')
        with open(p, encoding='utf-8') as f:
            pts = (json.load(f).get('brands') or {}).get('dodopizza', {}).get('points') or []
        return [(float(x['lat']), float(x['lon'])) for x in pts if x.get('lat') and x.get('lon')]
    except Exception:
        return []


DODO_POINTS = _load_dodo_points()


def score_and_cache(rec):
    """Считает скоринг локации для прошедшего лота и кладёт в rec (кэш в state).
    Возвращает dict скоринга или None (нет координат / Overpass недоступен)."""
    lat, lon = rec.get('geo_lat'), rec.get('geo_lon')
    if lat is None or lon is None:
        return None
    try:
        sc = scoring.score_location(lat, lon, dodo_points=DODO_POINTS)
    except Exception:
        return None
    if sc.get('score') is None:
        return None
    sc['geo_source'] = rec.get('geo_source')  # точность координат → достоверность скоринга
    rec['score'] = sc['score']
    rec['score_data'] = sc
    rec['scored_at'] = now_iso()
    return sc

STATE_PATH = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'state.json')
RUNS_LOG = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'runs.log')
PHOTO_DIR = Path(os.getcwd()) / '.cy-photos'
CHAT_ID = int(os.environ.get('CY_CHAT_ID', '0') or 0)  # супергруппа Cyprus-lokal-monitor; задаёт driver/workflow

# Параметры те же, что в Клуже (решение Сергея 17.10.2026: «параметры такие же»)
AREA_MIN, AREA_MAX = 89, 220
PRICE_MIN, PRICE_MAX = 1200, 7000
PRICE_PER_M2_MIN = 10.0  # €/м²: Лимассол 15–30, Никосия 10–20; дешевле — окраина/склад
CEILING_MIN = 3.0
# Четыре города задачи: центр и радиус поиска (6 км, как в Клуже).
# Центры: Лимассол — Anexartisias/Molos, Никосия — Eleftheria Sq., Пафос — Kennedy Sq.,
# Ларнака — Finikoudes.
CITIES = {
    'Limassol': (34.6750, 33.0443),
    'Nicosia': (35.1725, 33.3620),
    'Paphos': (34.7720, 32.4297),
    'Larnaca': (34.9160, 33.6310),
}
TRG_LAT, TRG_LON = CITIES['Limassol']  # legacy-ссылки (карта); расстояние считаем до ближайшего города
RADIUS_KM = 6.0
# ≤12 лотов за цикл: при ≥15 проходных driver шлёт одну сводку вместо карточек,
# а скоринг каждого лота — отдельный запрос в Overpass (dry-run 17.10 с 30 лотами
# шёл 25 мин). Очередь первого запуска рассасывается за 3–4 часовых цикла.
MAX_DETAILS_PER_CYCLE = 12
MAX_DETAILS_PER_SOURCE = 12


def nearest_city(lat, lon):
    """(город, км до его центра) для точки; ('', None) без координат."""
    if lat is None or lon is None:
        return '', None
    best, best_d = '', None
    for name, (clat, clon) in CITIES.items():
        d = haversine_km(lat, lon, clat, clon)
        if best_d is None or d < best_d:
            best, best_d = name, d
    return best, best_d
# Сколько циклов пытаемся дожать лот, застрявший без терминального статуса
# (упала отправка в Telegram и т.п.). known_ids() считает его известным,
# из выдачи он больше не придёт — очередь восстанавливаем сами.
STUCK_RETRIES = 6
# Фото — самая долгая часть цикла (часть источников идёт через прокси).
# 6 кадров хватает для решения по локации; качаем их параллельно.
MAX_PHOTOS_PER_LOT = 6
PHOTO_WORKERS = 5
# Курортные/дальние места вне четырёх городов (bazaraki относит их к районам
# Лимассола/Пафоса/Ларнаки) — тихий реджект по слагу URL или метке локации.
# Ayia Napa здесь НЕТ: курорт лежит в district Famagusta (режется по городу), а
# «Agia Napa» в Лимассоле — центральный район у церкви (ложный suburb 17.10).
RESORT_SLUGS = ('protaras', 'paralimni', 'pissouri', 'platres', 'polis', 'troodos',
               'lefkara', 'kakopetria')

# Districts вне задачи (Famagusta) и те же курорты — для метки location.
FAR_MUNI_SLUGS = ('famagusta', 'protaras', 'paralimni', 'deryneia', 'sotira', 'pissouri',
                  'platres', 'polis', 'troodos', 'lefkara', 'kakopetria', 'athienou', 'pyla')
# «Near-miss» коридор цены для листа реджектов: рядом с целевым 1200–5000.
PRICE_NEAR_LOW, PRICE_NEAR_HIGH = 900, 8000

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

# Description-level reject patterns (английский: объявления Кипра на английском).
OFFICE_PATS = [
    r'office\s+(?:building|block|space|premises|unit|floor|for\s+rent)', r'\boffices\b',
    r'open[\s-]?plan\s+office', r'business\s+cent(?:er|re)', r'serviced\s+office',
    r'co[\s-]?working',
]
MALL_PATS = [
    r'\bmall\b', r'shopping\s+(?:cent(?:er|re)|complex)', r'\bMy\s*Mall\b', r'Kings\s+Avenue',
    r'Metropolis\s+Mall', r'Nicosia\s+Mall', r'Larnaca\s+Mall', r'inside\s+(?:the\s+)?(?:mall|arcade)',
]
COURT_PATS = [r'inner\s+courtyard', r'back\s+of\s+the\s+building', r'no\s+(?:street\s+)?frontage',
              r'rear\s+(?:entrance|access)\s+only', r'through\s+the\s+courtyard']
DARK_PATS = [r'\bbasement\b', r'\bunderground\b',
             r'no\s+(?:natural\s+light|windows)', r'without\s+windows']

# Apartment detection: жильё протекает в commercial-выдачу (хозяева сдают
# «под офис»). Реджект если жилые маркеры есть И нет коммерческого контр-маркера.
RESIDENTIAL_PATS = [
    r'\bapartment\b', r'\bbedroom', r'\bvilla\b', r'\bmaisonette\b', r'\bpenthouse\b',
    r'\bstudio\s+flat', r'living\s+room', r'ideal\s+for\s+living', r'\bresidential\s+use\b',
]
# Коммерческие контр-маркеры: если есть — лот настоящий commercial, оставляем
# даже при жилом слове во flavour-тексте.
COMMERCIAL_KEEP = [
    r'\bshop\b', r'\bshops\b', r'\bshowroom', r'\bretail\b', r'\bstore\b', r'restaurant',
    r'pizz', r'fast\s*food', r'caf[eé]', r'coffee', r'\bbar\b', r'bakery', r'take[\s-]?away',
    r'street\s+front', r'frontage', r'shop\s*front', r'display\s+window', r'main\s+(?:road|street|avenue)',
    r'commercial', r'kiosk', r'\bsalon\b', r'pharmacy', r'\bf\s*&\s*b\b', r'food\s+and\s+beverage',
    r'\bcatering\b', r'\bbistro\b', r'\btavern', r'\bdeli\b',
]

# «Районы» = города задачи: канон. название + центр (для district по координатам
# и для карты). Районы внутри города bazaraki пишет сам в location («Limassol — Zakaki»).
CARTIERE = {
    'limassol': ('Limassol', 34.6750, 33.0443),
    'lemesos': ('Limassol', 34.6750, 33.0443),
    'nicosia': ('Nicosia', 35.1725, 33.3620),
    'lefkosia': ('Nicosia', 35.1725, 33.3620),
    'paphos': ('Paphos', 34.7720, 32.4297),
    'pafos': ('Paphos', 34.7720, 32.4297),
    'larnaca': ('Larnaca', 34.9160, 33.6310),
    'larnaka': ('Larnaca', 34.9160, 33.6310),
}

# Топоним (пригород/квартал) → город — для district из текста.
SUBDISTRICT_TO_MUNI = {
    'zakaki': 'Limassol', 'germasogeia': 'Limassol', 'agios athanasios': 'Limassol',
    'mesa geitonia': 'Limassol', 'polemidia': 'Limassol', 'ypsonas': 'Limassol',
    'agios tychonas': 'Limassol', 'mouttagiaka': 'Limassol', 'molos': 'Limassol',
    'makarios avenue': 'Limassol', 'anexartisias': 'Limassol', 'neapolis': 'Limassol',
    'strovolos': 'Nicosia', 'engomi': 'Nicosia', 'aglantzia': 'Nicosia', 'lakatamia': 'Nicosia',
    'latsia': 'Nicosia', 'agios dometios': 'Nicosia', 'ledra': 'Nicosia', 'makariou': 'Nicosia',
    'kato paphos': 'Paphos', 'chloraka': 'Paphos', 'geroskipou': 'Paphos', 'konia': 'Paphos',
    'tala': 'Paphos', 'peyia': 'Paphos', 'universal': 'Paphos',
    'aradippou': 'Larnaca', 'livadia': 'Larnaca', 'oroklini': 'Larnaca', 'dromolaxia': 'Larnaca',
    'finikoudes': 'Larnaca', 'mackenzie': 'Larnaca', 'kiti': 'Larnaca',
}


def _norm_sub(s):
    """Lowercase, strip diacritics для матча в SUBDISTRICT_TO_MUNI.
    Đ/đ нужно мапить вручную — NFKD их теряет."""
    import unicodedata
    s = s.replace('Đ', 'D').replace('đ', 'd')
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'[()]', '', s)
    return re.sub(r'\s+', ' ', s).strip()


class StateLock:
    """Advisory file lock — second concurrent cycle exits cleanly with empty result."""
    def __init__(self, path=STATE_PATH + '.lock'):
        self.path = path
        self.fd = None

    def __enter__(self):
        self.fd = open(self.path, 'w')
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.fd.close()
            print(json.dumps({
                'concurrent_cycle_running': True,
                'passes': [], 'rejects': [], 'summary': {},
            }))
            sys.exit(0)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fd.fileno(), fcntl.LOCK_UN)
        finally:
            self.fd.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return 2 * R * asin(sqrt(a))


def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


DRY_RUN = False  # --dry-run: считаем всё, НИЧЕГО не пишем (state/лист)


def save_state(s):
    if DRY_RUN:
        print('  [dry-run] state.json НЕ сохранён', file=sys.stderr)
        return
    tmp = STATE_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def decode_escapes(s):
    """Decode JSON-style \\uXXXX escapes while preserving raw UTF-8 multibyte chars."""
    try:
        return json.loads('"' + s + '"')
    except Exception:
        pass
    try:
        return bytes(s, 'utf-8').decode('unicode_escape', errors='ignore')
    except Exception:
        return s


def _bz_id(url):
    m = re.search(r'/adv/(\d+)_', url or '')
    return m.group(1) if m else None


def fetch_html(url, timeout=20):
    """Returns (html, http_status). http_status=0 on error."""
    if 'bazaraki.com' in url:
        # свип уже забрал detail-JSON (BZ_EMBED) — второй раз за страницей не ходим
        emb = curl_sweep.bz_embedded_html(_bz_id(url))
        if emb:
            return emb, 200
        r = curl_sweep.bz_get(url, timeout=max(timeout, 35))
        return (r.text, r.status_code) if r is not None else ('', 0)
    try:
        r = subprocess.run(
            ['curl', '-sL', '-A', UA, '--max-time', str(timeout), '--compressed',
             '-w', '\n__HTTP_CODE__%{http_code}', url],
            capture_output=True, timeout=timeout + 5,
        )
        body = r.stdout.decode('utf-8', errors='replace')
        idx = body.rfind('\n__HTTP_CODE__')
        if idx >= 0:
            code = body[idx + len('\n__HTTP_CODE__'):].strip()
            body = body[:idx]
            try:
                return body, int(code)
            except ValueError:
                return body, 0
        return body, 200
    except Exception:
        return '', 0


_ORD_FLOOR = {'first': 1, '1st': 1, 'second': 2, '2nd': 2, 'third': 3, '3rd': 3,
              'fourth': 4, '4th': 4, 'fifth': 5, '5th': 5}


def _floor_from_text(text):
    """Этаж из английского описания. '' если не найден. Канон: 'ground floor',
    'basement', 'mezzanine', 'floor N' (как parter/etaj N в Клуже)."""
    t = text.lower()
    # «ground floor + mezzanine/basement storage» — это всё равно уличный этаж;
    # приоритет у ground floor, если он назван явно
    has_ground = re.search(r'\bground\s*[-]?\s*floor\b|\bgroundfloor\b|\bg/f\b|\bstreet\s+level\b|'
                           r'\bground\s+level\b|\bισόγειο\b|\bισογείου\b', t)
    if has_ground:
        return 'ground floor'
    if re.search(r'\bmezzanine\b', t) and re.search(r'\bon\s+(?:the\s+)?mezzanine\b|mezzanine\s+(?:floor|level)\s+(?:shop|office|unit)', t):
        return 'mezzanine'
    if re.search(r'\b(?:in|on)\s+(?:the\s+)?basement\b|basement\s+(?:shop|unit|premises|level)\b', t):
        return 'basement'
    fm = re.search(r'\b(?:on\s+(?:the\s+)?)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th)\s+floor\b', t)
    if fm:
        return f'floor {_ORD_FLOOR[fm.group(1)]}'
    fm = re.search(r'\b(\d)(?:st|nd|rd|th)\s+floor\b|\bfloor\s+(\d)\b', t)
    if fm:
        return f'floor {fm.group(1) or fm.group(2)}'
    if re.search(r'\bupper\s+floor\b|\btop\s+floor\b', t):
        return 'upper floor'
    return ''


def _ceiling_from_text(text):
    t = text.lower()
    cm = (re.search(r'[îi]n[ăa]l[țt]ime[a]?[^0-9]{0,30}(\d[\.,]?\d*)\s*m', t)
          or re.search(r'\bh\s*[=:]\s*(\d[\.,]?\d*)\s*m', t)
          or re.search(r'tavan[e]?[^0-9]{0,25}(\d[\.,]?\d*)\s*m', t))
    if cm:
        try:
            v = float(cm.group(1).replace(',', '.'))
            if 2.0 <= v <= 8.0:  # защита от «înălțime 2026» и площадей
                return v
        except Exception:
            pass
    return None


def _bz_published_iso(rel):
    """'5 days ago' / '2 hours ago' / 'today' → ISO-дата (приблизительно)."""
    rel = (rel or '').lower()
    m = re.search(r'(\d+)\s+(minute|hour|day|week|month)', rel)
    if not m:
        return now_iso() if rel in ('today', 'just now') else ''
    n, unit = int(m.group(1)), m.group(2)
    secs = {'minute': 60, 'hour': 3600, 'day': 86400, 'week': 7 * 86400, 'month': 30 * 86400}[unit]
    return (datetime.now(timezone.utc) - timedelta(seconds=n * secs)).isoformat()


def parse_bazaraki(html, cand):
    """bazaraki detail: объект объявления из dehydrated JSON (или нашего мини-HTML).
    Координаты с флагом точности, описание, галерея, features (площадь, тип,
    индекс), published. Этаж — только из текста (отдельного поля у коммерции нет)."""
    out = {'title': cand.get('title') or '', 'description': '', 'street': '', 'subdistrict': ''}
    ad = curl_sweep.bz_ad_from_html(html, cand.get('id'))
    if not ad:
        return out
    out['title'] = ad.get('name') or out['title']
    desc = re.sub(r'<[^>]+>', ' ', ad.get('description') or '')
    out['description'] = re.sub(r'\s+', ' ', desc).strip()
    feats = {(f.get('name') or ''): (f.get('value') or '') for f in (ad.get('features') or [])}
    out['type'] = feats.get('Type') or ''
    out['subdistrict'] = (ad.get('location') or {}).get('name') or ''
    if feats.get('Postal code'):
        out['postal_code'] = feats['Postal code']
    co = (ad.get('location') or {}).get('coordinates') or {}
    if isinstance(co.get('lat'), (int, float)) and isinstance(co.get('lng'), (int, float)):
        out['lat'], out['lon'] = float(co['lat']), float(co['lng'])
        out['geo_approx'] = bool(co.get('is_approximate'))
    pub = (ad.get('counters') or {}).get('published') or cand.get('date')
    if pub:
        out['published_at'] = _bz_published_iso(pub)
    gal = ad.get('gallery') or {}
    imgs = gal.get('preview') or gal.get('full') or gal.get('small') or []
    if imgs:
        out['photo_url'] = imgs[0]
    out['is_closed'] = bool(ad.get('is_closed'))
    haystack = out['description'] + ' ' + out['title']
    f = _floor_from_text(haystack)
    if f:
        out['floor'] = f
    c = _ceiling_from_text(haystack)
    if c:
        out['ceiling'] = c
    return out




def _district_by_coords(lat, lon):
    """Ближайший город по центру (≤12 км, иначе '')."""
    name, d = nearest_city(lat, lon)
    return name if (d is not None and d <= 12) else ''


def _district_from_text(text):
    """Картье из текста (название района или топоним-улица)."""
    norm = _norm_sub(text)
    # только целым словом: иначе 'central' матчится внутри 'semicentral'
    def _word(t):
        return re.search(r'(?<![a-z])' + re.escape(t) + r'(?![a-z])', norm)
    for slug, (name, _la, _lo) in CARTIERE.items():
        if _word(slug.replace('-', ' ')):
            return name
    for topo, name in SUBDISTRICT_TO_MUNI.items():
        if _word(topo):
            return name
    return ''


def extract_district(cand, detail):
    # bazaraki: location «Limassol — Limassol Marina» → «Limassol (Limassol Marina)»;
    # формат «Город (район)» нужен дедупу и фидбеку (они режут по ' (').
    label = (detail.get('subdistrict') or cand.get('district_label') or '').strip()
    if label:
        parts = [p.strip() for p in re.split(r'\s[—–-]\s', label, maxsplit=1)]
        city = _district_from_text(parts[0]) or curl_sweep.city_of(label) or parts[0]
        area = parts[1] if len(parts) > 1 else ''
        # «Limassol — Limassol - Zakaki»: повтор города в районе убираем
        area = re.sub(r'^' + re.escape(city) + r'\s*[—–-]\s*', '', area).strip()
        if area and area.lower() != city.lower():
            return f'{city} ({area})'
        return city

    # фолбэки: координаты → ближайший город; затем текст
    lat = detail.get('lat', cand.get('lat'))
    lon = detail.get('lon', cand.get('lon'))
    d = _district_by_coords(lat, lon)
    if d:
        return d
    d = _district_from_text((cand.get('title') or '') + ' ' +
                            (detail.get('description') or '')[:500])
    if d:
        return d
    return 'Unknown'


def extract_photo_urls(html, source, max_n=10):
    """Все фото объявления (hotlink-able URLs) для галереи на карте."""
    urls = []
    try:
        if source == 'bazaraki.com':
            ad = curl_sweep.bz_ad_from_html(html) or {}
            gal = ad.get('gallery') or {}
            # preview (~900px webp) хватает и альбому, и попапу карты; full тяжелее
            urls = list(gal.get('preview') or gal.get('full') or gal.get('small') or [])
        elif source == 'zyprus.com':
            urls = list(curl_sweep.zy_parse_detail(html).get('images') or [])
    except Exception:
        pass
    # Canonical dedup: OLX отдаёт один файл в нескольких размерах
    # (…/image, …/image;s=1271x962, …;s=389x272) — по полному URL они разные,
    # но это одно фото. Дедуп по базе, обрезав размерный/качественный суффикс.
    seen, out = set(), []
    for u in urls:
        base = re.sub(r';s=\d+x\d+(?:;q=\d+)?$', '', u)
        if base in seen:
            continue
        seen.add(base)
        out.append(u)
    return out[:max_n]


def apply_filters(cand, detail, skip_price=False):
    """Returns (passed_bool, flags_list, reject_reason).

    skip_price=True пропускает area/price/€m² гейты и проверяет только структуру
    (этаж/курорт/назначение/офис/молл/двор/квартира). Нужно price-only пути, чтобы
    перед публикацией price-реджекта в дайджест отсеять структурный брак
    (верхний этаж, квартира, офис) — иначе в дайджест попадают квартиры на верхних этажах."""
    flags = []
    a = cand.get('area')
    p = cand.get('price')
    if not skip_price:
        if a is None or not (AREA_MIN <= a <= AREA_MAX):
            return False, flags, f'area={a}'
        if p is None or not (PRICE_MIN <= p <= PRICE_MAX):
            return False, flags, f'price={p}'
        if a and p / a < PRICE_PER_M2_MIN:
            return False, flags, f'price_per_m2={p/a:.1f}'

    floor = ((detail.get('floor') or cand.get('floor') or '')).lower().strip().rstrip('.')
    if not floor:
        flags.append('uncertain_floor')
    elif floor == 'ground floor':
        pass
    elif (re.search(r'floor\s*\d+', floor) or re.search(r'^[1-9]$', floor)
          or floor in ('basement', 'mezzanine', 'upper floor', 'penthouse')):
        return False, flags, f'floor={floor}'

    url = (cand.get('url') or '').lower()
    if any(s in url for s in RESORT_SLUGS):
        return False, flags, 'suburb'
    muni_norm = _norm_sub(cand.get('district_label') or cand.get('municipality') or '').replace(' ', '-')
    if muni_norm and any(s in muni_norm for s in RESORT_SLUGS + FAR_MUNI_SLUGS):
        return False, flags, 'suburb'

    type_ = (detail.get('type') or cand.get('type') or '').lower()
    if any(w in type_ for w in ('office', 'storage', 'warehouse', 'industrial', 'residential')):
        return False, flags, f'type={type_[:30]}'

    haystack = ((detail.get('description', '') or '') + ' ' +
                (detail.get('title', '') or '')).lower()

    # Офис-паттерн в контексте СОСЕДСТВА («next to office buildings») — это
    # описание окружения, не назначения помещения.
    _PROX = re.compile(
        r'(?:next\s+to|near|close\s+to|opposite|surrounded\s+by|area\s+with|among|'
        r'walking\s+distance\s+(?:to|from)|minutes?\s+(?:from|to)|plenty\s+of|lots\s+of|many)'
        r'[a-z,\s]{0,30}$')
    # Тип bazaraki «Shops, showrooms» / «Restaurants, bars» явный: «shop/office
    # space» в тексте такого лота — не офис, а универсальное помещение.
    _explicit_retail = any(w in type_ for w in ('shop', 'showroom', 'restaurant', 'bar'))
    for pat in ([] if _explicit_retail else OFFICE_PATS):
        hard_hit = False
        for m_ in re.finditer(pat, haystack):
            ctx = haystack[max(0, m_.start() - 45):m_.start()]
            if _PROX.search(ctx):
                continue  # соседство — не считаем
            hard_hit = True
            break
        if hard_hit:
            return False, flags, f'office:{pat[:25]}'
    for pat in MALL_PATS:
        if re.search(pat, haystack):
            return False, flags, 'mall'
    for pat in COURT_PATS:
        if re.search(pat, haystack):
            return False, flags, 'courtyard'
    for pat in DARK_PATS:
        if re.search(pat, haystack):
            return False, flags, f'dark:{pat[:25]}'

    if (any(re.search(p, haystack) for p in RESIDENTIAL_PATS)
            and not any(re.search(m, haystack) for m in COMMERCIAL_KEEP)):
        return False, flags, 'apartment'

    # фильтр по потолку убран 2026-07-11 по решению Сергея;
    # высота, если указана, по-прежнему показывается в caption (✅ ceiling X m)

    if 'lat' in detail and 'lon' in detail:
        city, d = nearest_city(detail['lat'], detail['lon'])
        if d > RADIUS_KM:
            return False, flags, f'distance={d:.1f}km'
        if detail.get('geo_approx'):
            flags.append('approx_pin')
    else:
        flags.append('uncertain_distance')

    return True, flags, ''


def download_photo(url, listing_key):
    PHOTO_DIR.mkdir(exist_ok=True)
    ext_m = re.search(r'\.(jpe?g|webp|avif|png)(?:[?#]|$)', url, re.IGNORECASE)
    ext = (ext_m.group(1).lower() if ext_m else 'jpg').replace('jpeg', 'jpg')
    raw_path = PHOTO_DIR / f"{listing_key}.{ext}"
    # 2 попытки: единичный сбой curl (таймаут/сброс) иначе роняет фото из альбома,
    # а лот с одним фото уходит текстом без альбома.
    ok = False
    for _attempt in range(2):
        try:
            subprocess.run(['curl', '-sL', '-A', UA, '--max-time', '30', url,
                            '-o', str(raw_path)],
                           capture_output=True, timeout=35)
        except Exception:
            continue
        if raw_path.exists() and raw_path.stat().st_size >= 1000:
            ok = True
            break
    if not ok:
        return None
    if ext in ('webp', 'avif', 'png'):
        jpg = PHOTO_DIR / f"{listing_key}.jpg"
        try:
            if sys.platform == 'darwin':
                subprocess.run(['sips', '-s', 'format', 'jpeg', str(raw_path),
                                '--out', str(jpg)],
                               capture_output=True, timeout=10)
            else:  # Linux (GitHub Actions): sips нет — Pillow
                from PIL import Image
                Image.open(raw_path).convert('RGB').save(jpg, 'JPEG', quality=88)
            if jpg.exists() and jpg.stat().st_size > 1000:
                return jpg
        except Exception:
            pass
    return raw_path


def yandex_pano_url(lat, lon):
    """Google Street View в точке (в Румынии покрытие хорошее; Яндекс-панорам нет).
    Имя функции историческое, оставлено ради существующих вызовов."""
    return (f"https://www.google.com/maps/@?api=1&map_action=pano"
            f"&viewpoint={lat}%2C{lon}")


def yandex_search_url(addr):
    return f"https://www.google.com/maps/search/?api=1&query={quote(f'{addr}, Cyprus')}"


def reason_ru(reason):
    """Reject reason code → (English label, badge color). Имя функции оставлено
    ради унаследованных вызовов; тексты — английские (команда партнёра)."""
    r = reason or ''
    if r.startswith('floor='):
        return (f'not ground floor ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('distance='):
        return (f'too far from center ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('ceiling='):
        return (f'low ceiling ({r.split("=", 1)[1]})', '#d97706')
    if r.startswith('price_per_m2='):
        return (f'too cheap per m² ({r.split("=", 1)[1]})', '#d97706')
    if r.startswith('area='):
        return (f'area outside {AREA_MIN}–{AREA_MAX} ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('price='):
        return (f'price outside {PRICE_MIN}–{PRICE_MAX} ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('office:'):
        return ('office / business center', '#7c3aed')
    if r == 'mall':
        return ('inside shopping mall', '#7c3aed')
    if r == 'courtyard':
        return ('courtyard entrance', '#7c3aed')
    if r.startswith('dark:'):
        return ('no windows / basement', '#374151')
    if r == 'apartment':
        return ('residential, not commercial', '#7c3aed')
    if r == 'suburb':
        return ('suburb (out of zone)', '#dc2626')
    if r.startswith('type='):
        return ('office / warehouse', '#7c3aed')
    if r.startswith('fetch_fail'):
        return ('page failed to load', '#9ca3af')
    if r == 'district_blacklist':
        return ('blacklisted district', '#dc2626')
    return (r or 'rejected', '#6b7280')


def _esc_html(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _reject_map_links(r):
    """(google_url, yandex_url) для реджекта по координатам или адресу."""
    lat, lon = r.get('lat'), r.get('lon')
    if lat and lon:
        return (f"https://www.google.com/maps/?q={lat},{lon}", yandex_pano_url(lat, lon))
    addr = r.get('address') or ''
    q = quote(f"{addr}, Cyprus")
    return (f"https://www.google.com/maps/search/?api=1&query={q}", yandex_search_url(addr))


def build_reject_digest(rejects):
    """HTML-сообщение по новым лотам, не прошедшим фильтр (одной строкой причина).
    Реджекты в карту/таблицу НЕ попадают — это только обзор «текущей картины»."""
    items = list(rejects or [])
    if not items:
        return None

    # Сортировка по €/м² от высокой к низкой (дорогие за метр — выше).
    def _ppm(r):
        a, p = r.get('area'), r.get('price')
        return (p / a) if (a and p) else -1
    items.sort(key=_ppm, reverse=True)

    n = len(items)

    def word(k):
        return 'лот' if k % 10 == 1 and k % 100 != 11 else \
               ('лота' if 2 <= k % 10 <= 4 and not 12 <= k % 100 <= 14 else 'лотов')

    lines = [f"🔍 <b>Не прошли фильтр: {n} {word(n)}</b> · не в таблице/карте"]
    shown = 0
    for i, r in enumerate(items, 1):
        label, _ = reason_ru(r['reason'])
        a, p = r.get('area'), r.get('price')
        ppm = f"{p / a:.0f} €/м²" if (a and p) else '—'
        district = (r.get('district') or '').strip()
        address = (r.get('address') or '').strip()
        if district.lower() in ('unknown', 'unknown district', '—'):
            district = ''
        title = address or district or '—'
        suffix = ''
        if district and address and district.lower() not in address.lower():
            suffix = f" <i>({_esc_html(district)})</i>"
        url = _esc_html(r.get("url") or '')
        head = f'<a href="{url}"><b>{_esc_html(title)}</b></a>' if url else f"<b>{_esc_html(title)}</b>"
        sc = r.get('score')
        score_str = f" · {scoring.score_emoji(sc)} {sc}/100" if sc is not None else " · ❔ скоринг —"
        block = (f"{i}. {head}{suffix} · "
                 f"{a or '—'}м² · {p or '—'}€ · {ppm}{score_str} ❌ {_esc_html(label)}")
        # safety: держим сообщение под лимитом Telegram (4096)
        if sum(len(x) + 1 for x in lines) + len(block) > 3900 and shown:
            lines.append(f"…ещё {n - shown}, см. следующий прогон")
            break
        lines.append(block)
        shown += 1

    return {'message': '\n'.join(lines), 'count': n}


def write_rejects_to_sheet(rejects):
    """Записать не прошедшие фильтр лоты в лист «не прошли фильтр» (gid 1460013302)
    через Sheets API. Раньше они уходили reject-дайджестом в Telegram — теперь копятся
    в таблице для анализа. Append-only: дедуп держит state (rejected-лоты не пересылаются).
    Колонки A..H: Дата и время | Адрес | Район | Площадь | Цена | Скоринг | Ссылка | Причина."""
    if not rejects:
        return {'ok': True, 'inserted': 0}
    try:
        from zoneinfo import ZoneInfo
        now_bg = datetime.now(ZoneInfo('Asia/Nicosia')).strftime('%Y-%m-%d %H:%M')
    except Exception:
        now_bg = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    rows = []
    for r in rejects:
        label, _ = reason_ru(r.get('reason'))
        district = (r.get('district') or '').strip()
        if district.lower() in ('unknown', 'unknown district', '—'):
            district = ''
        a, p, sc = r.get('area'), r.get('price'), r.get('score')
        rows.append([
            now_bg,
            (r.get('address') or '').strip(),
            district,
            a if a is not None else '',
            p if p is not None else '',
            sc if sc is not None else '',
            r.get('url') or '',
            label,
        ])
    if DRY_RUN:
        print(f'  [dry-run] реджект-лист: {len(rows)} строк НЕ записаны', file=sys.stderr)
        return {'ok': True, 'inserted': 0, 'dry_run': True}
    try:
        import sheets_append
        return sheets_append.append_reject_rows(rows)
    except Exception as e:
        print(f'  reject-sheet write failed: {e}', file=sys.stderr)
        return {'ok': False, 'error': str(e), 'inserted': 0}


def build_caption(cand, detail, district_str, flags):
    src = cand.get('source', '?').replace('.com', '')
    area = cand.get('area')
    price = cand.get('price')
    floor = detail.get('floor') or 'floor n/a'
    addr = (detail.get('street') or cand.get('street') or '').strip()
    if not addr:
        # уточняем, ЧЕГО именно нет: текст-адреса при точном пине — мелочь,
        # а вот «area only» значит, что и точка на карте ориентировочная
        if 'lat' in detail:
            addr = ('no street address (area only)' if detail.get('geo_approx')
                    else 'no street address (map pin is exact)')
        else:
            addr = 'no address or map pin in the listing'

    pano = None
    approx = detail.get('geo_approx')
    if 'lat' in detail and 'lon' in detail:
        maps = f"https://www.google.com/maps/?q={detail['lat']},{detail['lon']}"
        if approx:
            # пин = центр круга/района, не помещение: Street View оттуда бессмыслен
            maps += ' (approximate area)'
        else:
            pano = yandex_pano_url(detail['lat'], detail['lon'])
    else:
        q_text = addr if addr != 'no exact address' else district_str.split(' (')[0]
        q = quote(f"{q_text}, Cyprus")
        maps = f"https://www.google.com/maps/search/?api=1&query={q}"

    plus = []
    if detail.get('ceiling'):
        plus.append(f'ceiling {detail["ceiling"]}m')

    cap = f"🍕 Location · {district_str}\n"
    cap += f"📍 {addr}\n"
    cap += f"🗺 {maps}\n"
    if pano:
        cap += f"🌐 Street View: {pano}\n"
    cap += f"📐 {area} m² · 💶 {price} €/mo · 🏢 {floor}\n"
    if plus:
        cap += f"✅ {' · '.join(plus)}\n"
    cap += f"🔗 {src}: {cand['url']}\n"
    if flags:
        cap += ' '.join(f'⚠️ {f}' for f in flags) + '\n'

    desc = (detail.get('description') or '').strip()
    if desc:
        snippet = desc[:300].rstrip()
        cap += f"\n📝 Summary: {snippet}\n"

    return cap[:1024]


def parse_detail(html, cand):
    src = cand.get('source')
    if src == 'bazaraki.com': return parse_bazaraki(html, cand)
    if src == 'zyprus.com': return parse_zyprus(html, cand)
    return {}


def parse_zyprus(html, cand):
    """zyprus detail → формат detail (см. curl_sweep.zy_parse_detail)."""
    z = curl_sweep.zy_parse_detail(html)
    out = {'title': z.get('title') or cand.get('title') or '', 'description': z.get('description') or '',
           'street': cand.get('street') or '', 'subdistrict': cand.get('district_label') or '',
           'type': cand.get('type') or ''}
    if z.get('lat') is not None:
        out['lat'], out['lon'] = z['lat'], z['lon']
    if z.get('published_at'):
        out['published_at'] = z['published_at']
    if z.get('images'):
        out['photo_url'] = z['images'][0]
    # площадь/цена из detail точнее карточки — освежаем кандидата
    if z.get('area'):
        cand['area'] = z['area']
    if z.get('price'):
        cand['price'] = z['price']
    haystack = out['description'] + ' ' + out['title']
    f = _floor_from_text(haystack)
    if f:
        out['floor'] = f
    c = _ceiling_from_text(haystack)
    if c:
        out['ceiling'] = c
    return out


def _price_similar(p1, p2):
    """Цены «про одно и то же»: разница ≤ max(100€, 3%)."""
    return abs(p1 - p2) <= max(100, 0.03 * max(p1, p2))


def dedupe_passes_same_cycle(s, passes, duplicates):
    """Кросс-посты, пришедшие ОДНИМ прогоном, обходят find_active_duplicate
    (он сверяет только с in_sheet-лотами, а внутрицикловые ещё не отправлены) —
    иначе кросс-пост одного помещения на двух сайтах даёт 2 карточки в чате.
    Матчинг пар внутри passes: площадь ±3 м² + цена похожа + (гео <250 м ИЛИ
    одинаковое начало описания — кросс-посты копируют текст дословно).
    Оставляем лот с адресом, второй клеим как alt_url."""
    def desc_prefix(k):
        d = (s['listings'].get(k, {}).get('description') or '').lower()
        return re.sub(r'\s+', '', d)[:120]

    kept = []
    for p_ in passes:
        rec = s['listings'].get(p_['listing_key'], {})
        dup_of = None
        for q in kept:
            qrec = s['listings'].get(q['listing_key'], {})
            a1, a2 = rec.get('area_m2'), qrec.get('area_m2')
            p1, p2 = rec.get('price_eur'), qrec.get('price_eur')
            if not (a1 and a2 and p1 and p2): continue
            if abs(a1 - a2) > 3 or not _price_similar(p1, p2): continue
            geo_close = False
            if all(rec.get(x) is not None for x in ('geo_lat', 'geo_lon')) and \
               all(qrec.get(x) is not None for x in ('geo_lat', 'geo_lon')):
                geo_close = haversine_km(rec['geo_lat'], rec['geo_lon'],
                                         qrec['geo_lat'], qrec['geo_lon']) < 0.25
            same_text = (desc_prefix(p_['listing_key']) and
                         desc_prefix(p_['listing_key']) == desc_prefix(q['listing_key']))
            # то же правило, что в find_active_duplicate: точный метраж + точная
            # цена + тот же район = кросс-пост, даже когда координат ещё нет
            exact = (p_.get('area') == q.get('area') and p_.get('price') == q.get('price')
                     and (p_.get('district') or '').split('(')[0].strip().lower()
                     == (q.get('district') or '').split('(')[0].strip().lower()
                     and bool(p_.get('district')))
            if geo_close or same_text or exact:
                dup_of = q
                break
        if dup_of is None:
            kept.append(p_)
            continue
        # если у нового есть адрес, а у оставленного нет — меняем местами
        if rec.get('address') and not s['listings'].get(dup_of['listing_key'], {}).get('address'):
            kept[kept.index(dup_of)] = p_
            p_, dup_of = dup_of, p_
            rec = s['listings'].get(p_['listing_key'], {})
        canon = s['listings'].get(dup_of['listing_key'], {})
        canon.setdefault('alt_urls', [])
        if rec.get('url') and rec['url'] not in canon['alt_urls']:
            canon['alt_urls'].append(rec['url'])
        rec['rejected'] = True
        rec['duplicate_of'] = dup_of['listing_key']
        duplicates.append({'key': p_['listing_key'],
                           'duplicate_of': dup_of['listing_key'],
                           'url': rec.get('url'), 'canonical_url': canon.get('url'),
                           'district': p_.get('district'), 'area': rec.get('area_m2'),
                           'price': rec.get('price_eur'), 'same_cycle': True})
    return kept


def find_active_duplicate(s, rec, self_key):
    """Ищет активный in_sheet лот с тем же физическим помещением: гео <200 м +
    площадь ±3 м² + цена ±max(100€,3%). Агентства перезаливают объявления с новыми
    ID и кросс-постят на другие сайты — по ключу source_id это «новые» лоты.
    Fallback без гео: точное совпадение непустого адреса. Возвращает (key, rec)
    канонического лота или (None, None)."""
    a, p = rec.get('area_m2'), rec.get('price_eur')
    if not a or not p:
        return None, None
    lat, lon = rec.get('geo_lat'), rec.get('geo_lon')
    addr = (rec.get('address') or '').strip().lower()
    for k, v in s['listings'].items():
        if k == self_key or not v.get('in_sheet') or v.get('removed_from_sheet'):
            continue
        va, vp = v.get('area_m2'), v.get('price_eur')
        if not va or not vp or abs(va - a) > 3 or not _price_similar(p, vp):
            continue
        # Точное совпадение метража И цены при том же районе — кросс-пост одного
        # помещения. Гео тут не помогает: у части источников координат нет на
        # момент проверки, а адресный геокодинг двух записей одного адреса легко
        # расходится на километры.
        _d1 = (rec.get('district') or '').split('(')[0].strip().lower()
        _d2 = (v.get('district') or '').split('(')[0].strip().lower()
        if va == a and vp == p and _d1 and _d1 == _d2:
            return k, v

        vlat, vlon = v.get('geo_lat'), v.get('geo_lon')
        if lat is not None and vlat is not None:
            if haversine_km(lat, lon, vlat, vlon) <= 0.2:
                return k, v
            continue  # оба с гео, но далеко — соседняя похожая площадь, не дубль
        vaddr = (v.get('address') or '').strip().lower()
        if addr and vaddr and addr == vaddr:
            return k, v
    return None, None


def detect_price_changes(s, all_l):
    """Сравнивает цены из sweep с активными in_sheet лотами. Изменение
    ≥ max(50€, 5%) → обновляет state (price_eur + price_history) и кол. D в
    Sheets, возвращает список изменений — агент шлёт по ним сообщения
    «💶 Цена изменилась». Порог 5% отсекает шум конверсии RSD→EUR."""
    changes, seen = [], set()
    for l in all_l:
        cid, src = l.get('id'), l.get('source', '?')
        newp = l.get('price')
        if not cid or not newp or not (200 <= newp <= 20000):
            continue
        key = f"{src.split('.')[0]}_{cid}"
        if key in seen:
            continue
        seen.add(key)
        rec = s['listings'].get(key)
        if not rec or not rec.get('in_sheet') or rec.get('removed_from_sheet'):
            continue
        oldp = rec.get('price_eur')
        if not oldp or abs(newp - oldp) < max(50, 0.05 * oldp):
            continue
        rec.setdefault('price_history', []).append(
            {'at': now_iso(), 'old': oldp, 'new': newp})
        rec['price_eur'] = newp
        changes.append({'key': key, 'old': oldp, 'new': newp,
                        'district': rec.get('district') or '',
                        'address': rec.get('address') or '',
                        'url': rec.get('url') or '', 'source': rec.get('source') or '',
                        'reply_to_message_id': rec.get('telegram_message_id'),
                        'sheet_updated': False})
    if changes:
        try:
            svc = _sheets_service()
            urls = svc.spreadsheets().values().get(
                spreadsheetId=SPREADSHEET_ID, range='E2:E2000'
            ).execute().get('values', [])
            row_by_url = {r[0].strip(): i for i, r in enumerate(urls, start=2) if r and r[0]}
            cells, targets = [], []
            for c in changes:
                row = row_by_url.get(c['url'].strip())
                if row:
                    cells.append({'row': row, 'col': 4, 'value': c['new']})
                    targets.append(c)
            if cells:
                update_cells(cells)  # при исключении sheet_updated останется False
                for c in targets:
                    c['sheet_updated'] = True
        except Exception as e:
            print(f'  price-change sheet update failed: {e}', file=sys.stderr)
    return changes


# Статусы кол. K основного листа → категория фидбека (startswith, lower).
# Команда партнёра англоязычная — принимаем оба языка.
FEEDBACK_LIKE = ('в работе', 'ок', 'in progress', 'ok', 'wip', 'working')
FEEDBACK_DISLIKE = ('не подходит', 'отказ', 'not suitable', 'rejected', 'no', 'pass')


def collect_sheet_feedback():
    """Фидбек-луп через Google Sheets: читает кол. B (район) и K (статус,
    Сергей ставит из попапа карты) и агрегирует лайки/дизлайки по районам.
    Ключи: полная строка «Општина (Подрайон)» и отдельно општина."""
    svc = _sheets_service()
    vals = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range='A2:K2000').execute().get('values', [])
    by_district = {}
    for r in vals:
        district = r[1].strip() if len(r) > 1 and r[1] else ''
        status = r[10].strip().lower() if len(r) > 10 and r[10] else ''
        if not district or not status:
            continue
        if status.startswith(FEEDBACK_LIKE):
            cat = 'like'
        elif status.startswith(FEEDBACK_DISLIKE):
            cat = 'dislike'
        else:
            continue
        for dkey in {district, district.split(' (')[0].strip()}:
            agg = by_district.setdefault(dkey, {'like': 0, 'dislike': 0})
            agg[cat] += 1
    return {'by_district': by_district, 'source': 'sheet_col_K',
            'updated_at': now_iso()}


def run_process():
    with StateLock():
        s = load_state()
        s.setdefault('listings', {})

        # Clean old photos
        try:
            subprocess.run(['find', str(PHOTO_DIR), '-type', 'f', '-mtime', '+7', '-delete'],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        PHOTO_DIR.mkdir(exist_ok=True)

        t0 = time.time()
        all_l, sources_down, sweep_errors = [], [], []

        # Кулдаун источника: сайт под долгой блокировкой (Cloudflare) не должен
        # каждый цикл жечь минуты на заведомо мёртвые ретраи.
        # 3 провала подряд → пауза 6 ч, затем одна разведочная попытка.
        _cool = s.setdefault('source_cooldown', {})
        _now_ts = time.time()

        for name, fn, pages in [
            # 4 типа × ≤6 стр. по всему Кипру (замер 17.10: 165 лотов на 5 стр.)
            ('bazaraki', curl_sweep.sweep_bazaraki, 6),
            ('zyprus', curl_sweep.sweep_zyprus, 3),  # ~30 лотов на 2 стр.
        ]:
            if (_cool.get(name) or {}).get('until', 0) > _now_ts:
                sources_down.append(name)
                sweep_errors.append(f'{name}:cooldown')
                continue
            try:
                all_l.extend(fn(pages=pages))
                _cool.pop(name, None)  # источник ожил — снимаем кулдаун
            except Exception as e:
                sources_down.append(name)
                sweep_errors.append(f'{name}:{type(e).__name__}')
                _c = _cool.setdefault(name, {'fails': 0, 'until': 0})
                _c['fails'] = _c.get('fails', 0) + 1
                if _c['fails'] >= 3:
                    _c['until'] = _now_ts + 6 * 3600
                    _c['fails'] = 0

        # Здоровье парсинга по источникам — для канарейки (судит по факту цикла,
        # НЕ делает своих запросов: лишний фетч ловил 403 после прокси-очереди цикла)
        _sw = {}
        for l in all_l:
            _src_key = (l.get('source') or '?').split('.')[0]
            d = _sw.setdefault(_src_key, {'cards': 0, 'parsed': 0})
            d['cards'] += 1
            if l.get('price') and l.get('area'):
                d['parsed'] += 1
        s['last_sweep_health'] = {'at': now_iso(), 'by_source': _sw,
                                  'down': list(sources_down)}

        # Prefilter — same logic as curl_sweep.main()
        filtered = []
        for l in all_l:
            if not l.get('area') or not l.get('price'): continue
            if l['area'] < AREA_MIN or l['area'] > AREA_MAX: continue
            if l['price'] < PRICE_MIN or l['price'] > PRICE_MAX: continue
            if l['price'] / l['area'] < PRICE_PER_M2_MIN: continue
            url = (l.get('url') or '').lower()
            if any(slug in url for slug in RESORT_SLUGS): continue
            type_ = (l.get('type', '') or '').lower()
            if any(b in type_ for b in ['office', 'storage', 'warehouse', 'industrial']): continue
            # Курорты и населённые пункты вне четырёх городов режем по
            # municipality (city.name) и по url-слагу.
            mun = _norm_sub(l.get('municipality') or '')
            mun_slug = mun.replace(' ', '-')
            if any(x == mun_slug or x in url for x in FAR_MUNI_SLUGS):
                continue
            filtered.append(l)

        # Изменения цены на активных лотах видны только в sweep-выдаче (по ключу
        # они «известные» и в detail-цикл не попадают) — ловим их здесь.
        price_changes = detect_price_changes(s, all_l)

        known = curl_sweep.known_ids()
        new_lots = [l for l in filtered if l.get('id') and l['id'] not in known]

        pending = list(s.get('pending_candidates', []))

        # Лоты, зависшие без терминального статуса: прошли фильтр, но не уехали
        # в чат/таблицу (сбой сети или Telegram). В выдаче они уже «известные»,
        # поэтому вернуть их в работу может только этот проход.
        stuck = []
        for _k, _v in (s.get('listings') or {}).items():
            if not isinstance(_v, dict):
                continue
            if _v.get('in_sheet') or _v.get('rejected') or _v.get('removed_from_sheet'):
                continue
            if not (_v.get('url') and _v.get('id')):
                continue
            _n = int(_v.get('stuck_retries') or 0)
            if _n >= STUCK_RETRIES:
                continue
            _v['stuck_retries'] = _n + 1
            stuck.append({
                'source': _v.get('source'), 'id': _v.get('id'), 'url': _v['url'],
                'title': _v.get('title') or '', 'area': _v.get('area_m2'),
                'price': _v.get('price_eur'), 'street': _v.get('address') or '',
                'municipality': _v.get('district') or '', 'type': None,
                'floor': _v.get('floor'), 'lat': _v.get('geo_lat'), 'lon': _v.get('geo_lon'),
                'date': _v.get('refreshed_at') or '',
            })
        if stuck:
            print(f'  requeued stuck (no terminal status): {len(stuck)}', file=sys.stderr)

        # Цена/метраж в очереди устаревают: кандидат помнит значения на момент
        # постановки. Освежаем из текущего свипа, иначе лот, вышедший за рамки
        # (правка цены владельцем), проезжает фильтр по старому значению.
        _fresh = {}
        for _l in all_l:
            if _l.get('id'):
                _fresh[f"{(_l.get('source') or '?').split('.')[0]}_{_l['id']}"] = _l

        def _refresh(c):
            f = _fresh.get(f"{(c.get('source') or '?').split('.')[0]}_{c.get('id')}")
            if f:
                if f.get('price'):
                    c['price'] = f['price']
                if f.get('area'):
                    c['area'] = f['area']
            return c

        pending = [_refresh(c) for c in pending]
        stuck = [_refresh(c) for c in stuck]

        # Зависшие идут ПЕРВЫМИ: им осталась только отправка, а очередь pending
        # может быть на сотни лотов и отодвинет их на сутки.
        candidates = stuck + new_lots + pending

        # Cap per source
        per_src, capped, leftover = {}, [], []
        for c in candidates:
            src = c.get('source', '?')
            if (per_src.get(src, 0) >= MAX_DETAILS_PER_SOURCE
                    or len(capped) >= MAX_DETAILS_PER_CYCLE):
                leftover.append(c)
                continue
            per_src[src] = per_src.get(src, 0) + 1
            capped.append(c)
        # Очередь дедуплицируем: один лот попадает в candidates и из pending, и
        # из stuck (known_ids() исключает его только из new_lots), поэтому без
        # этого хвост очереди копит копии и съедает слоты следующих циклов.
        _seen_q, _uniq = set(), []
        for _c in leftover:
            _qk = f"{(_c.get('source') or '?').split('.')[0]}_{_c.get('id')}"
            if _qk in _seen_q:
                continue
            _seen_q.add(_qk)
            _uniq.append(_c)
        s['pending_candidates'] = _uniq

        passes, rejects, duplicates, deferred = [], [], [], []
        done_this_cycle = set()  # лот мог быть и в new, и в pending → не обрабатывать дважды

        for cand in capped:
            src = cand.get('source', '?')
            cid = cand.get('id', '')
            src_prefix = src.split('.')[0]
            key = f"{src_prefix}_{cid}"
            if key in done_this_cycle:
                continue
            done_this_cycle.add(key)

            existing = s['listings'].get(key)
            if existing and (existing.get('in_sheet') or existing.get('rejected')
                             or existing.get('removed_from_sheet')):
                existing['last_seen_at'] = now_iso()
                continue
            # else: new OR zombie (no terminal status) — fall through to detail-fetch

            html, status = fetch_html(cand['url'])
            if not html and int(cand.get('_fetch_fails') or 0) < 6:
                # bazaraki за Cloudflare: сбой загрузки через прокси не значит, что лот
                # плохой. Возвращаем в очередь без строки в Rejected (иначе дубль
                # каждый час); после 6 неудач подряд лот уходит в обычный fetch_fail.
                cand['_fetch_fails'] = int(cand.get('_fetch_fails') or 0) + 1
                s['pending_candidates'].append(cand)
                continue
            if not html:
                rejects.append({
                    'key': key, 'reason': f'fetch_fail:http={status}',
                    'district': '', 'address': (cand.get('street') or '').strip(),
                    'area': cand.get('area'), 'price': cand.get('price'),
                    'url': cand['url'], 'source': src, 'lat': None, 'lon': None,
                })
                continue

            detail = parse_detail(html, cand)
            passed, flags, reason = apply_filters(cand, detail)
            district_str = extract_district(cand, detail)

            rec = {
                'source': src, 'id': cid, 'url': cand['url'],
                'title': (detail.get('title') or cand.get('title') or '')[:200],
                'area_m2': cand.get('area'), 'price_eur': cand.get('price'),
                'floor': detail.get('floor') or cand.get('floor'),
                'address': (detail.get('street') or cand.get('street') or '').strip(),
                'district': district_str,
                'subdistrict': None,
                'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                'in_sheet': False, 'alerted': False,
                'flags': flags,
                'description': (detail.get('description') or '')[:2000],
            }
            if 'lat' in detail:
                rec['geo_lat'] = detail['lat']
                rec['geo_lon'] = detail['lon']
                rec['geo_source'] = 'detail_approx' if detail.get('geo_approx') else 'detail'
            if detail.get('refreshed_at'): rec['refreshed_at'] = detail['refreshed_at']
            if detail.get('published_at'): rec['published_at'] = detail['published_at']
            if detail.get('photo_url'): rec['photo_url'] = detail['photo_url']
            purls = extract_photo_urls(html, src)
            if purls: rec['photo_urls'] = purls

            if not passed:
                rec['rejected'] = True
                rec['flags'] = [reason] + flags
                s['listings'][key] = rec
                rejects.append({
                    'key': key, 'reason': reason,
                    'district': district_str, 'address': rec['address'],
                    'area': cand.get('area'), 'price': cand.get('price'),
                    'url': cand['url'], 'source': src,
                    'lat': detail.get('lat'), 'lon': detail.get('lon'),
                })
                continue

            rec['rejected'] = False
            s['listings'][key] = rec

            # Ручной blacklist районов (state.district_blacklist, substring-match).
            bl = s.get('district_blacklist') or []
            if district_str and any(b.lower() in district_str.lower() for b in bl):
                rec['rejected'] = True
                rec['flags'] = ['district_blacklist'] + flags
                rejects.append({
                    'key': key, 'reason': 'district_blacklist',
                    'district': district_str, 'address': rec['address'],
                    'area': cand.get('area'), 'price': cand.get('price'),
                    'url': cand['url'], 'source': src,
                    'lat': detail.get('lat'), 'lon': detail.get('lon'),
                })
                continue

            # Дубль-детект: то же помещение уже в таблице под другим ID/источником.
            dup_key, dup_rec = find_active_duplicate(s, rec, self_key=key)
            if dup_key:
                rec['rejected'] = True
                rec['duplicate_of'] = dup_key
                rec['flags'] = [f'duplicate_of:{dup_key}'] + flags
                alt = dup_rec.setdefault('alt_urls', [])
                if rec['url'] and rec['url'] != dup_rec.get('url') and rec['url'] not in alt:
                    alt.append(rec['url'])
                duplicates.append({
                    'key': key, 'duplicate_of': dup_key,
                    'url': rec['url'], 'canonical_url': dup_rec.get('url'),
                    'district': district_str,
                    'area': cand.get('area'), 'price': cand.get('price'),
                })
                continue

            sc = score_and_cache(rec)

            # Все фото лота (до 10) — Telegram-альбом одним сообщением.
            gallery = purls or ([detail['photo_url']] if detail.get('photo_url') else [])
            sel = list(enumerate(gallery[:MAX_PHOTOS_PER_LOT]))
            photo_paths = []
            if sel:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=PHOTO_WORKERS) as _ex:
                    got = list(_ex.map(lambda t: download_photo(t[1], f'{key}_{t[0]}'), sel))
                photo_paths = [str(x) for x in got if x]  # ex.map сохраняет порядок
            photo_path = photo_paths[0] if photo_paths else None

            # --- Инварианты перед отправкой: запрет тихой деградации ---
            # Пасс без фото/координат/скоринга при ПЕРВОЙ попытке откладываем на
            # следующий цикл (сеть/Overpass часто оживают). Со второй — шлём с пометкой.
            missing = []
            if gallery and not photo_paths:
                missing.append('photos')
            if rec.get('geo_lat') is None:
                missing.append('coordinates')
            elif sc is None:
                missing.append('location score')
            # Счётчик отсрочек храним в state по ключу лота, а не только в кандидате:
            # «зависшие» пересобираются из базы без _send_retry и обрабатываются первыми,
            # из-за чего лот без координат откладывался бесконечно (кейс 10.09.2026).
            _defers = s.setdefault('send_defers', {})
            retry_n = max(int(cand.get('_send_retry') or 0), int(_defers.get(key) or 0))
            if missing and retry_n == 0:
                cand['_send_retry'] = 1
                _defers[key] = 1
                s['pending_candidates'].append(cand)
                deferred.append({'key': key, 'missing': missing, 'url': cand['url']})
                continue

            caption = build_caption(cand, detail, district_str, flags)
            if missing:
                caption += f"⚠️ unavailable: {', '.join(missing)}\n"
            if sc:
                caption = caption + "\n\n" + scoring.score_line(sc)

            passes.append({
                'listing_key': key,
                'photo_path': photo_path,
                'photo_paths': photo_paths,
                'caption': caption,
                'chat_id': CHAT_ID,
                'district': district_str,
                'address': rec['address'],
                'area': cand.get('area'),
                'price': cand.get('price'),
                'url': cand['url'],
                'flags': flags,
                'score': (sc or {}).get('score'),
                'had_photos': bool(gallery),
            })

        # --- Реджекты по цене / цене за м² ---
        # Метраж в норме (100–220), но цена вне 1300–6000 или €/м² ниже минимума.
        # В дайджест идут только настоящие price-near-miss: валидный локал на первом этаже,
        # не прошедший ТОЛЬКО по цене. Поэтому перед публикацией фетчим detail и гоняем
        # структурные фильтры (skip_price=True): этаж, назначение, офис/молл/двор/квартира.
        # Структурный брак (квартира на верхнем этаже, офис) → молча в state как
        # rejected, в дайджест НЕ публикуем — это не «почти подошло», это просто не то.
        # Дедуп через state. Кэп публикаций = ≤8 строк суммарно с detail-реджектами;
        # отдельный бюджет фетчей, чтобы просканить мимо квартир и найти реальные near-miss.
        MAX_REJECTS_SHOWN = 20
        price_cap = max(0, MAX_REJECTS_SHOWN - len(rejects))
        struct_fetch_budget = 25
        pr_seen = set()
        for l in all_l:
            if price_cap <= 0 or struct_fetch_budget <= 0:
                break
            a, p = l.get('area'), l.get('price')
            if not a or not p:
                continue
            if a < AREA_MIN or a > AREA_MAX:
                continue
            p_ok = PRICE_MIN <= p <= PRICE_MAX
            ppm_ok = (p / a) >= PRICE_PER_M2_MIN
            if p_ok and ppm_ok:
                continue
            cid = l.get('id')
            if not cid:
                continue
            src = l.get('source', '?')
            key = f"{src.split('.')[0]}_{cid}"
            if key in pr_seen:
                continue
            pr_seen.add(key)
            url = (l.get('url') or '').lower()
            if any(slug in url for slug in RESORT_SLUGS):
                continue
            if any(s in _norm_sub(l.get('municipality') or '').replace(' ', '-')
                   for s in RESORT_SLUGS):
                continue
            type_ = (l.get('type', '') or '').lower()
            if any(b in type_ for b in ['office', 'storage', 'warehouse', 'industrial']):
                continue
            existing = s['listings'].get(key)
            if existing and (existing.get('in_sheet') or existing.get('rejected')
                             or existing.get('removed_from_sheet')):
                existing['last_seen_at'] = now_iso()
                continue
            price_reason = f'price={p}' if not p_ok else f'price_per_m2={p / a:.1f}'
            addr = (l.get('street') or l.get('name') or '').strip()
            district = (l.get('municipality') or '').strip()

            def _silent_reject(reason_flag):
                """Записать молчаливый реджект в state (в дайджест НЕ идёт)."""
                s['listings'][key] = {
                    'source': src, 'id': cid, 'url': l.get('url'),
                    'area_m2': a, 'price_eur': p,
                    'address': addr, 'district': district,
                    'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                    'in_sheet': False, 'alerted': False,
                    'rejected': True, 'flags': [reason_flag, price_reason],
                }

            # Дальний пригород (Обреновац/Лазаревац/…) — вне зоны, не показываем.
            muni_l = (l.get('municipality') or '').strip().lower()
            if any(slug in url for slug in FAR_MUNI_SLUGS) or \
               any(muni_l == m or m in muni_l for m in FAR_MUNI_SLUGS):
                _silent_reject('far_muni')
                continue
            # Далёкий промах по цене (8000€ / 900€) — это шум, не «почти подошло».
            if not p_ok and not (PRICE_NEAR_LOW <= p <= PRICE_NEAR_HIGH):
                _silent_reject('price_far')
                continue

            # Фетч detail + структурная проверка перед публикацией.
            cand_l = {
                'source': src, 'id': cid, 'url': l.get('url'),
                'area': a, 'price': p, 'type': l.get('type'),
                'floor': l.get('floor'), 'street': l.get('street'),
            }
            struct_html, _st = fetch_html(l.get('url'))
            struct_fetch_budget -= 1
            struct_ok, _f, struct_reason = (True, [], None)
            detail_l = {}
            if struct_html:
                detail_l = parse_detail(struct_html, cand_l)
                struct_ok, _f, struct_reason = apply_filters(cand_l, detail_l, skip_price=True)
                d2 = extract_district(cand_l, detail_l)
                if d2:
                    district = d2
                if detail_l.get('street'):
                    addr = detail_l['street'].strip()
                # Пригород мог проявиться только в районе из detail.
                dl = _norm_sub(district).replace(' ', '-')
                if any(m in dl for m in FAR_MUNI_SLUGS) or \
                   any(m in dl for m in RESORT_SLUGS):
                    _silent_reject('far_muni')
                    continue

            if not struct_ok:
                # структурный брак — молча реджектим, в дайджест НЕ кладём
                s['listings'][key] = {
                    'source': src, 'id': cid, 'url': l.get('url'),
                    'area_m2': a, 'price_eur': p,
                    'floor': detail_l.get('floor') or l.get('floor'),
                    'address': addr, 'district': district,
                    'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                    'in_sheet': False, 'alerted': False,
                    'rejected': True, 'flags': [struct_reason, price_reason],
                }
                continue

            # настоящий price-near-miss — публикуем
            s['listings'][key] = {
                'source': src, 'id': cid, 'url': l.get('url'),
                'area_m2': a, 'price_eur': p,
                'floor': detail_l.get('floor') or l.get('floor'),
                'address': addr, 'district': district,
                'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                'in_sheet': False, 'alerted': False,
                'rejected': True, 'flags': [price_reason],
            }
            rejects.append({
                'key': key, 'reason': price_reason,
                'district': district, 'address': addr,
                'area': a, 'price': p,
                'url': l.get('url'), 'source': src,
                'lat': detail_l.get('lat'), 'lon': detail_l.get('lon'),
            })
            price_cap -= 1

        # --- Скоринг локаций для реджектов (по координатам) ---
        # Каждый score = 1 запрос в Overpass, поэтому бюджет, чтобы не словить
        # троттлинг и не растянуть часовой цикл. Скорим в порядке €/м² убыв.
        # (как в дайджесте) — при нехватке бюджета приоритет дорогим за метр.
        SCORE_BUDGET = 20
        _scored = 0
        for r in sorted(rejects,
                        key=lambda x: ((x.get('price') or 0) / (x.get('area') or 1)),
                        reverse=True):
            if _scored >= SCORE_BUDGET:
                break
            lat, lon = r.get('lat'), r.get('lon')
            if lat is None or lon is None:
                continue
            try:
                sc = scoring.score_location(lat, lon, dodo_points=DODO_POINTS)
            except Exception:
                continue
            if not sc or sc.get('score') is None:
                continue
            r['score'] = sc['score']
            lst = s['listings'].get(r['key'])
            if lst is not None:
                lst['score'] = sc['score']
                lst['score_data'] = sc
                lst['scored_at'] = now_iso()
            _scored += 1

        # Фидбек из Sheets (кол. K, собирается в finalize): районы, где есть
        # лоты «В работе»/«ОК», помечаем звездой и поднимаем в начало выдачи.
        fb = (s.get('feedback_aggregates') or {}).get('by_district', {})
        for p in passes:
            d = p.get('district') or ''
            agg = fb.get(d) or fb.get(d.split(' (')[0].strip()) or {}
            if agg.get('like', 0) > 0:
                p['feedback_like'] = True
                p['caption'] = ('⭐ District with locations already in progress\n'
                                + p['caption'])[:1024]
        passes.sort(key=lambda p: 0 if p.get('feedback_like') else 1)

        passes = dedupe_passes_same_cycle(s, passes, duplicates)

        s['last_cycle_at'] = now_iso()

        # Дневные счётчики для вечерней сводки-воронки в чат (driver шлёт после 21:00).
        # Сбрасываются при смене даты (Клуж).
        import zoneinfo
        from datetime import datetime as _dt
        _today = _dt.now(zoneinfo.ZoneInfo('Asia/Nicosia')).strftime('%Y-%m-%d')
        ds = s.get('daily_stats') or {}
        if ds.get('date') != _today:
            ds = {'date': _today, 'runs': 0, 'sweep_last': 0, 'new': 0,
                  'checked': 0, 'rejects': 0, 'passes': 0, 'duplicates': 0}
        ds['runs'] += 1
        ds['sweep_last'] = len(all_l)
        ds['new'] += len(new_lots)
        ds['checked'] += len(capped)
        ds['rejects'] += len(rejects)
        ds['passes'] += len(passes)
        ds['duplicates'] += len(duplicates)
        s['daily_stats'] = ds
        # Статистика цикла для штампа Last scan в Rejected!J1 (finalize её читает).
        s['last_cycle_stats'] = {
            'at': now_iso(), 'sweep_raw': len(all_l), 'new': len(new_lots),
            'passes': len(passes), 'rejects': len(rejects),
            'sources_down': sources_down,
        }
        save_state(s)

        elapsed = time.time() - t0
        summary = {
            'sweep_raw': len(all_l),
            'filtered': len(filtered),
            'new': len(new_lots),
            'pending_in': len(pending),
            'pending_out': len(leftover),
            'processed': len(capped),
            'passes': len(passes),
            'rejects': len(rejects),
            'duplicates': len(duplicates),
            'deferred': len(deferred),
            'price_changes': len(price_changes),
            'sources_down': sources_down,
            'errors': sweep_errors[:5],
            'time_sec': round(elapsed, 1),
        }

        # Compact stderr summary for the agent
        print(f'\n=== cycle.py SUMMARY ({elapsed:.1f}s) ===', file=sys.stderr)
        print(f'sweep={summary["sweep_raw"]} filtered={summary["filtered"]} '
              f'new={summary["new"]} pending={len(pending)}→{len(leftover)}', file=sys.stderr)
        print(f'processed={summary["processed"]} passes={summary["passes"]} '
              f'rejects={summary["rejects"]}', file=sys.stderr)
        for r in rejects[:8]:
            print(f"  REJECT {r['key'][:40]}: {r['reason']}", file=sys.stderr)
        for p in passes[:5]:
            print(f"  PASS {p['listing_key'][:40]}: {p['district']} "
                  f"{p['area']}m² {p['price']}€", file=sys.stderr)
        for d in duplicates[:5]:
            print(f"  DUP {d['key'][:40]} == {d['duplicate_of'][:40]}", file=sys.stderr)
        for c in price_changes[:5]:
            print(f"  PRICE {c['key'][:40]}: {c['old']}€ → {c['new']}€", file=sys.stderr)
        if sources_down:
            print(f'  sources_down={sources_down} errors={sweep_errors[:5]}', file=sys.stderr)

        # Реджекты теперь не в Telegram, а в лист «не прошли фильтр».
        reject_sheet = write_rejects_to_sheet(rejects)
        if reject_sheet.get('inserted'):
            print(f'  reject-sheet: +{reject_sheet["inserted"]} строк', file=sys.stderr)

        print(json.dumps({
            'passes': passes,
            'rejects': rejects,
            'duplicates': duplicates,
            'deferred': deferred,
            'price_changes': price_changes,
            'reject_digest': None,
            'reject_sheet': reject_sheet,
            'summary': summary,
        }, ensure_ascii=False))


def cmd_mark_sent(listing_key, message_id, desc_ru=None):
    """Called by agent after Telegram send_file succeeds.
    desc_ru — английская выжимка от driver (Gemini); идёт в Sheets кол. F (карта показывает её).
    Без неё падаем на сырое описание объявления."""
    with StateLock():
        s = load_state()
        rec = s.get('listings', {}).get(listing_key)
        if not rec:
            print(json.dumps({'ok': False, 'error': f'listing_key {listing_key} not in state'}))
            return 1

        rec['alerted'] = True
        if desc_ru:
            rec['description_ru'] = desc_ru[:1000]  # gen_map fallback когда Sheets F пуст
        try:
            rec['telegram_message_id'] = int(message_id)
        except (ValueError, TypeError):
            pass
        rec['sent_at'] = now_iso()
        save_state(s)  # save Telegram metadata first

        try:
            sheet_resp = insert_lots([{
                'address': rec.get('address') or '',
                'district': rec.get('district') or '',
                'area': rec.get('area_m2'),
                'price': rec.get('price_eur'),
                'url': rec.get('url'),
                'description_ru': (desc_ru or rec.get('description') or '')[:1000],
                'date_posted': (rec.get('refreshed_at')
                                or rec.get('published_at')
                                or rec.get('first_seen_at') or '')[:10],
            }])
        except Exception as e:
            print(json.dumps({'ok': False, 'sheets_error': str(e),
                              'state_updated': True, 'in_sheet': False}))
            return 1

        rec['in_sheet'] = True

        # Находим строку лота по URL один раз (insert_at_top кладёт наверх):
        # нужна и для скоринга в кол. M, и для named range (ссылка из Telegram).
        sheet_link = None
        lot_row = None
        if rec.get('url'):
            try:
                svc = _sheets_service()
                urls = svc.spreadsheets().values().get(
                    spreadsheetId=SPREADSHEET_ID, range='E2:E1000'
                ).execute().get('values', [])
                target = rec['url'].strip()
                for i, row in enumerate(urls, start=2):
                    if row and row[0].strip() == target:
                        lot_row = i
                        break
            except Exception:
                pass
        sc = rec.get('score')
        if sc is not None and lot_row:
            try:
                update_cells([{'row': lot_row, 'col': 13, 'value': sc}])
            except Exception:
                pass  # скоринг в M не критичен, не валим mark-sent
        if lot_row:
            try:
                from sheets_append import create_lot_named_range
                sheet_link = create_lot_named_range(lot_row, listing_key)
                rec['sheet_link'] = sheet_link
            except Exception:
                pass  # ссылка — nice-to-have

        s.setdefault('sent_messages', {})[str(message_id)] = {
            'listing_key': listing_key, 'sent_at': rec['sent_at'],
        }
        save_state(s)
        print(json.dumps({'ok': True, 'sheets': sheet_resp,
                          'listing_key': listing_key,
                          'sheet_link': sheet_link,
                          'in_sheet': True}))
    return 0


def run_canary(s):
    """Канарейка: раз в сутки по 1 живому лоту с каждого источника — парсер вернул
    цену/фото/описание? Сломался парсер или бан — видно в тот же день (health-алерт)."""
    res = {}
    # sweep-каналы: по результату sweep'а ПОСЛЕДНЕГО цикла из state — свой
    # контрольный фетч дублировал трафик и ловил 403 после прокси-очереди
    # цикла (ложные тревоги 18.07 и 27.07)
    sh = s.get('last_sweep_health') or {}
    by_src = sh.get('by_source') or {}
    down = set(sh.get('down') or [])
    for name, srck in (('bazaraki_sweep', 'bazaraki'), ('zyprus_sweep', 'zyprus')):
        if srck in down:
            res[name] = 'FAIL: source down in last sweep'
            continue
        d = by_src.get(srck) or {}
        if not d.get('cards'):
            res[name] = 'FAIL: 0 cards in last sweep'
        elif not d.get('parsed'):
            res[name] = f"FAIL: {d['cards']} cards, 0 parsed (markup changed?)"
        else:
            res[name] = 'ok'
    # detail-каналы: свежие живые in_sheet лоты каждого источника.
    # Пробуем до 3: свежеудалённый лот может отдавать 200-заглушку/редирект —
    # один мёртвый подопытный не должен давать ложную тревогу. FAIL — если все.
    for prefix, src_name, label in (('bazaraki_', 'bazaraki.com', 'bazaraki_detail'),
                                    ('zyprus_', 'zyprus.com', 'zyprus_detail')):
        recs = []
        for k, v in sorted(s.get('listings', {}).items(),
                           key=lambda kv: ((kv[1] or {}).get('last_seen_at') or ''),
                           reverse=True):
            if (k.startswith(prefix) and isinstance(v, dict) and v.get('in_sheet')
                    and not v.get('removed_from_sheet') and v.get('url')):
                recs.append(v)
                if len(recs) >= 3:
                    break
        if not recs:
            res[label] = 'skip: no live listing'
            continue
        last_reason = 'no data'
        for rec in recs:
            try:
                html, code = fetch_html(rec['url'])
                if not html or code != 200:
                    last_reason = f'http={code}'
                    continue
                d = parse_detail(html, {'source': src_name, 'url': rec['url']})
                if d.get('photo_url') or d.get('description') or d.get('lat'):
                    res[label] = 'ok'
                    break
                last_reason = 'detail empty (listing gone/redesign?)'
            except Exception as e:
                last_reason = str(e)
        else:
            res[label] = f'FAIL x{len(recs)}: {last_reason}'
    return res


def cmd_finalize():
    """Run check_status + gen_map + write runs.log line. Single tool-call replacement
    for the previous 3-step agent flow."""
    t0 = time.time()
    out = {'ok': True}

    # check_status manages its own state writes; prints summary on stdout
    # «Last scan» в Rejected!J1 — обновляется КАЖДЫМ прогоном, даже пустым,
    # и несёт статистику цикла: одна ячейка отвечает «жив и что видел».
    try:
        if DRY_RUN:
            raise RuntimeError('dry-run: штамп не пишем')
        from datetime import datetime as _dt
        import zoneinfo
        _now = _dt.now(zoneinfo.ZoneInfo('Asia/Nicosia')).strftime('%Y-%m-%d %H:%M')
        _st = load_state().get('last_cycle_stats') or {}
        _down = _st.get('sources_down') or []
        _extra = ''
        if _st:
            _extra = (f" · sweep {_st.get('sweep_raw', '?')} · new {_st.get('new', '?')}"
                      f" · src {2 - len(_down)}/2"
                      + (f" · DOWN: {','.join(_down)}" if _down else ''))
        _sheets_service().spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range='Rejected!J1',
            valueInputOption='RAW',
            body={'values': [[f'Last scan: {_now} (Cyprus){_extra}']]}).execute()
        out['last_scan_stamp'] = True
    except Exception as e:
        out['last_scan_stamp'] = f'fail: {e}'

    # Проверка живости объявлений — вспомогательный шаг: её таймаут НЕ должен
    # ронять цикл (массовые 403 растягивают check_status, а карта и surge
    # должны обновляться в любом случае).
    try:
        cr = subprocess.run(['python3', str(SCRIPT_DIR / 'check_status.py')],
                            capture_output=True, text=True, timeout=600)
        cout = (cr.stdout or '') + '\n' + (cr.stderr or '')
    except subprocess.TimeoutExpired:
        out['check_timeout'] = True
        cr = subprocess.CompletedProcess([], 1, '', 'timeout')
        cout = ''
    m = re.search(r'(\d+)\s+killed[^a-z]*?(\d+)\s+alive', cout)
    killed = int(m.group(1)) if m else 0
    alive = int(m.group(2)) if m else 0
    out['check_killed'] = killed
    out['check_alive'] = alive
    out['check_rc'] = cr.returncode
    if cr.returncode != 0:
        out['check_stderr_tail'] = (cr.stderr or '').strip().split('\n')[-3:]

    # gen_map deploys to surge.sh automatically; prints to stdout
    mr = subprocess.run(['python3', str(SCRIPT_DIR / 'gen_map.py')],
                        capture_output=True, text=True, timeout=240)
    mout = (mr.stdout or '') + '\n' + (mr.stderr or '')
    out['map_ok'] = bool(re.search(r'wrote\s+/.*lokali\.html', mout))
    out['map_surge_ok'] = 'Success!' in mout
    feat_m = re.search(r'features:\s*(\d+)', mout)
    out['map_features'] = int(feat_m.group(1)) if feat_m else None
    out['map_rc'] = mr.returncode
    if mr.returncode != 0:
        out['map_stderr_tail'] = (mr.stderr or '').strip().split('\n')[-3:]

    # Sweep zombies: listings >24h old without any terminal status → mark dead.
    # Also flag legacy keys (not matching <src>_<id> with known prefix) — they can't
    # be re-attached by future sweeps.
    KNOWN_PREFIXES = ('bazaraki_', 'zyprus_', 'dot_')
    zombie_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    zombies_marked, legacy_marked = 0, 0

    # Фидбек из кол. K основного листа (Сергей ставит статусы из попапа карты).
    # Применяется в следующем process-цикле: буст районов с лотами «В работе».
    try:
        feedback = collect_sheet_feedback()
    except Exception as e:
        feedback = None
        out['feedback_error'] = str(e)[:200]

    # Канарейка парсеров: раз в сутки, сетевые запросы ДО StateLock (не держим лок).
    canary = None
    _canary_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:
        _s0 = load_state()
        if (_s0.get('canary') or {}).get('date') != _canary_date:
            canary = run_canary(_s0)
    except Exception as e:
        canary = {'canary_error': f'FAIL: {e}'}

    with StateLock():
        s = load_state()
        listings = s.get('listings', {})

        # Смена формулы скоринга (scoring.SCORE_FORMULA): старые скоры пересчитываем
        # из кэша сырых сигналов без Overpass, иначе карта и колонка M таблицы
        # продолжают показывать числа по прошлой формуле.
        rescored, _m_cells = 0, []
        for k, r in listings.items():
            sc = r.get('score_data') if isinstance(r, dict) else None
            if scoring.score_is_stale(sc):
                new = scoring.rescore_from_cache(sc)
                r['score_data'], r['score'] = new, new['score']
                rescored += 1
        if rescored:
            try:
                urls = _sheets_service().spreadsheets().values().get(
                    spreadsheetId=SPREADSHEET_ID, range='E2:E1000').execute().get('values', [])
                row_by_url = {row[0].strip(): i for i, row in enumerate(urls, start=2) if row and row[0]}
                for k, r in listings.items():
                    if r.get('in_sheet') and r.get('score') is not None and r.get('url') in row_by_url:
                        _m_cells.append({'row': row_by_url[r['url']], 'col': 13, 'value': r['score']})
                if _m_cells and not DRY_RUN:
                    update_cells(_m_cells)
            except Exception as e:
                out['rescore_sheet_error'] = str(e)[:160]
        out['rescored'] = rescored
        out['rescored_sheet_rows'] = len(_m_cells)

        if feedback:
            s['feedback_aggregates'] = feedback
            out['feedback_districts'] = len(feedback['by_district'])
        if canary is not None:
            s['canary'] = {'date': _canary_date, 'results': canary}
            out['canary'] = s['canary']

        # Компакция: терминальным записям старше 45 дней тяжёлые поля не нужны
        # (описания и фото-списки — основной вес state.json).
        # 45 дней держали тяжёлые поля (описание, фото, скоринг) у уже отклонённых
        # лотов, а state целиком коммитится в git КАЖДЫМ прогоном. У Белграда
        # это дало файл на 3.6 МБ; порог в 3 дня сжимает 1700 записей и режет
        # его на четверть. Дольше эти поля не нужны: реджект в дайджест уходит
        # в том же цикле, а дедуп сверяется только с лотами из таблицы.
        compact_cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        compacted = 0
        for k, r in listings.items():
            if not (r.get('rejected') or r.get('removed_from_sheet')):
                continue
            if r.get('compacted'):
                continue
            last = (r.get('last_seen_at') or r.get('last_seen')
                    or r.get('first_seen_at') or r.get('first_seen') or '')
            try:
                lt = datetime.fromisoformat(last.replace('Z', '+00:00'))
            except Exception:
                lt = None
            if lt is None or lt > compact_cutoff:
                continue
            for f in ('description', 'description_ru', 'photo_urls', 'score_data'):
                r.pop(f, None)
            r['compacted'] = True
            compacted += 1
        out['compacted'] = compacted
        for k, r in listings.items():
            if r.get('in_sheet') or r.get('rejected') or r.get('removed_from_sheet'):
                continue
            first_seen = r.get('first_seen_at') or ''
            try:
                dt = datetime.fromisoformat(first_seen.replace('Z', '+00:00')) if first_seen else None
            except Exception:
                dt = None
            is_legacy = not any(k.startswith(p) for p in KNOWN_PREFIXES)
            if is_legacy:
                r['removed_from_sheet'] = True
                r['stale_marked_at'] = now_iso()
                r['dead_reason'] = 'legacy_key_unreachable'
                r['removed_human'] = True
                legacy_marked += 1
            elif int(r.get('stuck_retries') or 0) < STUCK_RETRIES and dt is not None:
                # Лот ещё в бюджете повторных попыток отправки — хоронить рано.
                # Жнец режет по 24 часам, а при реальном ритме прогонов (~3/сутки)
                # шесть попыток растягиваются на двое суток, и он успевал убить
                # лот, который система как раз пыталась дожать.
                continue
            elif dt is None or dt < zombie_cutoff:
                # No timestamps OR older than 24h with no terminal status → dead
                r['removed_from_sheet'] = True
                r['stale_marked_at'] = now_iso()
                r['dead_reason'] = 'stale_no_status'
                r['removed_human'] = True
                zombies_marked += 1
        save_state(s)
        total = len(s.get('listings', {}))
    out['zombies_marked'] = zombies_marked
    out['legacy_marked'] = legacy_marked

    # Append runs.log
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    line = (f'{ts} · finalize · total={total} · '
            f'check={alive}alive/{killed}killed · '
            f'map={"ok" if out["map_ok"] else "ERR"} '
            f'features={out["map_features"]} · '
            f'surge={"ok" if out["map_surge_ok"] else "ERR"}')
    with open(RUNS_LOG, 'a') as f:
        f.write(line + '\n')

    out['time_sec'] = round(time.time() - t0, 1)
    out['log_line'] = line
    print(json.dumps(out, ensure_ascii=False))
    return 0


GEOCODE_MAX_KM = 9.0  # результат Nominatim дальше от ближайшего города задачи — мимо
_STREET_TYPE_RE = re.compile(
    r'^(?:strada|str|bulevardul|bd|b-dul|bulevard|calea|aleea|piata|piața|splaiul)\.?\s+',
    re.IGNORECASE)


def _nominatim(q):
    """(lat, lon, addresstype) первого результата или None. stdlib: на macOS-python
    без certifi падает проверка сертификата, поэтому тот же fallback, что в tg_bot."""
    import ssl, urllib.request, urllib.parse
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(
        {'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'ro'})
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'cyprus-location-monitor/1.0'})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            d = json.loads(r.read().decode())
    except Exception:
        return None
    if not d:
        return None
    return float(d[0]['lat']), float(d[0]['lon']), d[0].get('addresstype') or ''


def geocode_hint(hint, city=''):
    """Адресная зацепка из текста объявления ('Str. Muncii nr. 209') → (lat, lon).
    Запросы от точного к грубому: как есть → без типа улицы (в OSM это может быть
    Bulevardul, а агент пишет Str.) → без номера дома. Принимаем только точки в
    пределах Клужа и не центроиды города/района: их у лота и так есть."""
    q0 = re.sub(r'^[~\s]+|\s*\((?:from listing text|approx[^)]*)\)\s*$', '', hint or '').strip()
    q0 = re.sub(r'\b(?:nr|no)\.?\s*(\d+)', r'\1', q0, flags=re.IGNORECASE)
    q0 = re.sub(r',?\s*(?:Cyprus|Limassol|Nicosia|Paphos|Larnaca)\s*$', '', q0, flags=re.IGNORECASE)
    if not q0:
        return None, None
    seg = q0.split(',')[0].strip()  # 'zona OMV Marasti, Toni Auto' → первый ориентир
    variants = []
    for base in (q0, seg):
        no_type = _STREET_TYPE_RE.sub('', base)
        no_num = re.sub(r'\s+\d+[a-z]?\b', '', no_type).strip()
        variants += [base, no_type, no_num]
    seen, queries = set(), []
    for v in variants:
        v = v.strip(' ,')
        if v and v.lower() not in seen:
            seen.add(v.lower())
            queries.append(v)
    for i, q in enumerate(queries[:4]):
        if i:
            time.sleep(1.1)  # лимит Nominatim 1 rps
        res = _nominatim(f'{q}, {city or "Cyprus"}')
        if not res:
            continue
        lat, lon, atype = res
        if atype in ('city', 'town', 'municipality', 'suburb', 'county', 'state', 'country'):
            continue
        _c, _d = nearest_city(lat, lon)
        if _d is not None and _d <= GEOCODE_MAX_KM and (not city or _c == city):
            return lat, lon
    return None, None


def cmd_geocode_hint(listing_key, hint):
    """driver.py: у лота нет ни адреса, ни пина, но Gemini вытащил из текста улицу.
    Геокодим её, пишем координаты (приблизительные) и район в state, считаем
    скоринг, чтобы карточка получила пин, расстояние и score. stdout — JSON."""
    # город лота известен из state — геокодим в его пределах
    try:
        _rec0 = load_state().get('listings', {}).get(listing_key) or {}
        _city0 = (_rec0.get('district') or '').split(' (')[0].strip()
    except Exception:
        _city0 = ''
    lat, lon = geocode_hint(hint, city=_city0 if _city0 in CITIES else '')
    if lat is None:
        print(json.dumps({'ok': False}))
        return 0
    _cn, _cd = nearest_city(lat, lon)
    out = {'ok': True, 'lat': lat, 'lon': lon, 'dist_km': round(_cd, 1),
           'district': _cn, 'score_line': ''}
    with StateLock():
        s = load_state()
        rec = s.get('listings', {}).get(listing_key)
        if rec and rec.get('geo_source') not in ('detail', 'manual'):
            rec['geo_lat'], rec['geo_lon'] = lat, lon
            rec['geo_source'] = 'hint_nominatim'  # карта и скоринг покажут как approx
            rec['address_hint'] = hint
            if not rec.get('address'):
                rec['address'] = f'~{hint} (from listing text)'
            if out['district']:
                rec['district'] = out['district']
            try:
                sc = score_and_cache(rec)
                out['score_line'] = scoring.score_line(sc) if sc else ''
            except Exception:
                pass
            save_state(s)
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mark-sent', nargs=2, metavar=('KEY', 'MSG_ID'),
                    help='Mark a pass as sent + insert to Sheets')
    ap.add_argument('--geocode-hint', nargs=2, metavar=('KEY', 'HINT'),
                    help='Геокодировать адресную зацепку из текста, записать geo+score в state')
    ap.add_argument('--desc-ru', default=None,
                    help='Английская выжимка для Sheets кол. F (вместо сырого описания)')
    ap.add_argument('--finalize', action='store_true',
                    help='Run check_status + gen_map + runs.log')
    ap.add_argument('--dry-run', action='store_true',
                    help='Полный прогон БЕЗ записи: state не сохраняется, лист не трогается. '
                         'Ручные прогоны — ТОЛЬКО так: живой монитор в облаке, иначе дубли.')
    args = ap.parse_args()

    if args.dry_run:
        global DRY_RUN
        DRY_RUN = True
        print('=== DRY-RUN: state и Google Sheet НЕ будут изменены ===', file=sys.stderr)

    if args.dry_run and (args.finalize or args.mark_sent or args.geocode_hint):
        # finalize/mark-sent пишут через подпроцессы (check_status → лист,
        # gen_map → surge) — dry-run их не гейтит. Запрещаем совсем.
        print('dry-run поддерживает только process-фазу (без --finalize/--mark-sent)',
              file=sys.stderr)
        return 2

    if args.mark_sent:
        return cmd_mark_sent(args.mark_sent[0], args.mark_sent[1], desc_ru=args.desc_ru)
    if args.geocode_hint:
        return cmd_geocode_hint(args.geocode_hint[0], args.geocode_hint[1])
    if args.finalize:
        return cmd_finalize()
    return run_process()


if __name__ == '__main__':
    sys.exit(main() or 0)

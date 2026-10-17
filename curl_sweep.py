#!/usr/bin/env python3
"""curl_sweep.py — Cyprus commercial-rent sweep (Limassol, Nicosia, Paphos, Larnaca).

Источники:
  • bazaraki.com — главный классифайд Кипра (проверено 17.10.2026).
    Cloudflare режет DC-IP и голый curl (403 «Just a moment»); через резидентный
    кипрский прокси DataImpulse (HALO_PROXY, таргетинг __cr.cy) с браузерным
    TLS-отпечатком curl_cffi отвечает 200.
    Фильтр по типу помещения работает ТОЛЬКО на уровне страны
    (/commercial-property/type---N/); в сочетании с городом в пути сайт молча
    отдаёт общий поиск (квартиры, машины). Поэтому свипаем нужные типы по всему
    Кипру с фильтром цены в URL (price_min/price_max работают) и режем по городу
    из поля location карточки («Limassol — Zakaki»). Фильтр площади в URL не
    работает (area_min/area_max игнорируются).
    Площади и координат в выдаче нет: для ещё не известных лотов тянем detail
    (dehydrated JSON react-query внутри self.__next_f.push) и складываем в BZ_EMBED,
    чтобы cycle.fetch_html не ходил за страницей второй раз.

Интерфейс для cycle.py: sweep_bazaraki(pages), known_ids(), bz_get(url),
bz_ad_from_html(html), BZ_EMBED, BZ_TYPES.
"""
import json, os, re, sys, time

VERBOSE = os.environ.get('PIZZA_QUIET') != '1'

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36')

STATE_PATH = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'state.json')
_KEY_PREFIXES = ('bazaraki_', 'zyprus_', 'dot_')

# Резидентный прокси DataImpulse: секрет HALO_PROXY общий с другими мониторами,
# таргетинг страны подменяем на Кипр.
_RES_PROXY = re.sub(r'__cr\.[a-z]{2}', '__cr.cy', os.environ.get('HALO_PROXY', ''))
_bz_sid = [1]  # sticky-sid: инкремент при неудаче = новый exit-IP
_IMPS = ('chrome124', 'chrome131', 'chrome120', 'safari17_0')

BZ_BASE = 'https://www.bazaraki.com'
BZ_LIST = BZ_BASE + '/real-estate-to-rent/commercial-property/'
# Типы bazaraki (type---N): 1 Offices, 2 Storage/warehouses, 3 Restaurants/bars,
# 4 Shops/showrooms, 5 Other, 6 Building, 7 Mixed use, 8 Residential buildings.
BZ_TYPES = {3: 'Restaurants, bars', 4: 'Shops, showrooms', 6: 'Building', 7: 'Mixed use'}
# Фильтр цены в URL: те же рамки, что в cycle (PRICE_NEAR_*), чтобы near-miss
# по цене тоже попадал в выдачу для листа Rejected.
BZ_PRICE_MIN, BZ_PRICE_MAX = 900, 8000
BZ_DETAIL_CAP = 80  # detail-фетчей за свип: первый прогон не должен идти 10 минут

# Города задачи: метка города из location карточки → канон.
CITY_LABELS = {'limassol': 'Limassol', 'nicosia': 'Nicosia', 'paphos': 'Paphos',
               'pafos': 'Paphos', 'larnaca': 'Larnaca', 'larnaka': 'Larnaca'}

BZ_EMBED = {}  # id -> dict объявления (detail JSON), наполняет sweep в этом же процессе


def _v(msg):
    if VERBOSE:
        print(msg, file=sys.stderr)


class _Resp:
    """Минимальный ответ в форме curl_cffi.Response."""
    def __init__(self, text, status_code):
        self.text, self.status_code = text, status_code


def bz_get(url, timeout=35, attempts=4):
    """GET bazaraki: curl_cffi + резидентный CY-прокси, ротация sid при неудаче.
    Без прокси (локальный запуск без HALO_PROXY) одна прямая попытка."""
    from curl_cffi import requests as cffi
    last = None
    n = attempts if _RES_PROXY else 1
    for i in range(n):
        proxies = None
        if _RES_PROXY:
            pr = re.sub(r'^(https?://[^:]+)', rf'\1__sid.cy{_bz_sid[0]}', _RES_PROXY, count=1)
            proxies = {'http': pr, 'https': pr}
        try:
            r = cffi.get(url, impersonate=_IMPS[i % len(_IMPS)], timeout=timeout,
                         proxies=proxies, allow_redirects=True,
                         headers={'Accept-Language': 'en-GB,en;q=0.9'})
            if r.status_code == 200 and 'Just a moment' not in r.text[:3000]:
                return r
            last = r
        except Exception as e:
            _v(f'  bz_get {type(e).__name__}: {url[:80]}')
        _bz_sid[0] += 1
        time.sleep(1.5)
    return last


def _rsc_text(html):
    """Склеенный и раскодированный поток React Server Components
    (self.__next_f.push([1,"..."])) — там лежат JSON-данные страницы."""
    out = []
    for c in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S):
        try:
            out.append(json.loads('"' + c + '"'))
        except Exception:
            pass
    return '\n'.join(out)


def _balanced_json(text, start):
    """JSON-объект, начинающийся в text[start] ('{'), с учётом строк и экранирования."""
    depth, instr, esc = 0, False, False
    for j in range(start, len(text)):
        ch = text[j]
        if instr:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                instr = False
            continue
        if ch == '"':
            instr = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return json.loads(text[start:j + 1])
    return None


def bz_ad_from_html(html, aid=None):
    """Объект объявления из detail-страницы bazaraki (или из нашего мини-HTML
    с <script id="bz-ad">). None, если страницы нет / объявление снято."""
    m = re.search(r'<script id="bz-ad" type="application/json">(.*?)</script>', html or '', re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None
    T = _rsc_text(html or '')
    if not T:
        return None
    pat = r'\{"id":%s,"slug":' % (aid if aid else r'\d+')
    mm = re.search(pat, T)
    if not mm:
        return None
    try:
        return _balanced_json(T, mm.start())
    except Exception:
        return None


def _price_int(s):
    """'€1.500' / '8000' → 1500 / 8000; None если не число."""
    if s is None:
        return None
    d = re.sub(r'[^\d]', '', str(s))
    return int(d) if d else None


def _area_from_text(s):
    m = re.search(r'(\d{1,3}(?:,\d{3})+|\d{2,4}(?:[.,]\d)?)\s*(?:m²|m2|sqm|sq\.?\s*m|square\s*met)', s or '', re.I)
    if not m:
        return None
    v = m.group(1)
    v = v.replace(',', '') if re.fullmatch(r'\d{1,3}(?:,\d{3})+', v) else v.replace(',', '.')
    try:
        return float(v)
    except ValueError:
        return None


def bz_cand_from_ad(ad, base=None):
    """Кандидат для cycle из detail-объекта bazaraki (обогащает base из листинга)."""
    c = dict(base or {})
    feats = {(f.get('name') or ''): (f.get('value') or '') for f in (ad.get('features') or [])}
    loc = ad.get('location') or {}
    c.update({
        'source': 'bazaraki.com', 'id': str(ad.get('id')),
        'url': c.get('url') or f"{BZ_BASE}/adv/{ad.get('id')}_{ad.get('slug')}/",
        'title': ad.get('name') or c.get('title') or '',
        'price': _price_int((ad.get('price') or {}).get('price_without_currency')) or c.get('price'),
        'area': _area_from_text(feats.get('Property area')) or _area_from_text(ad.get('name')),
        'type': feats.get('Type') or c.get('type'),
        'postal_code': feats.get('Postal code') or '',
        'district_label': loc.get('name') or c.get('district_label') or '',
        'date': (ad.get('counters') or {}).get('published') or c.get('date') or '',
        'is_closed': bool(ad.get('is_closed')),
        'street': '',
    })
    c['municipality'] = city_of(c['district_label'])
    co = loc.get('coordinates') or {}
    if isinstance(co.get('lat'), (int, float)) and isinstance(co.get('lng'), (int, float)):
        c['lat'], c['lon'] = float(co['lat']), float(co['lng'])
        c['geo_approx'] = bool(co.get('is_approximate'))
    return c


def city_of(location_label):
    """'Limassol — Limassol Marina' → 'Limassol'; '' если город не из задачи."""
    head = (location_label or '').split('—')[0].split('-')[0].strip().lower()
    return CITY_LABELS.get(head, '')


def _list_ads(html):
    """Карточки листинга из RSC-потока: id, title, price, url, location, thumb."""
    T = _rsc_text(html)
    out = []
    for m in re.finditer(r'\{"id":(\d+),"title":"((?:[^"\\]|\\.)*)","description":"(?:[^"\\]|\\.)*",'
                         r'"price":"([^"]*)".*?"first_thumb":"([^"]*)".*?"published":"([^"]*)",'
                         r'"url":"([^"]+)".*?"location":"([^"]*)"', T, re.S):
        aid, title, price, thumb, published, url, loc = m.groups()
        out.append({'id': aid, 'title': json.loads('"' + title + '"'), 'price': _price_int(price),
                    'thumb': thumb, 'date': published, 'url': BZ_BASE + url,
                    'district_label': loc, 'municipality': city_of(loc)})
    return out


def sweep_bazaraki(pages=6):
    """Все типы BZ_TYPES по Кипру с фильтром цены; detail для не известных лотов
    (площадь, координаты, тип) — в BZ_EMBED и в кандидата."""
    known = known_ids()
    seen, cands = set(), []
    for t, tname in BZ_TYPES.items():
        for p in range(1, pages + 1):
            url = f'{BZ_LIST}type---{t}/?price_min={BZ_PRICE_MIN}&price_max={BZ_PRICE_MAX}&page={p}'
            r = bz_get(url)
            if r is None or r.status_code != 200:
                raise RuntimeError(f'bazaraki type {t} page {p}: '
                                   f'http={getattr(r, "status_code", None)}')
            ads = _list_ads(r.text)
            new = [a for a in ads if a['id'] not in seen]
            _v(f'  bazaraki type {t} p{p}: {len(ads)} cards, {len(new)} new')
            if not new:
                break
            for a in new:
                seen.add(a['id'])
                a['type'] = tname
                a['source'] = 'bazaraki.com'
                cands.append(a)
            if len(ads) < 50:
                break
            time.sleep(0.7)
    # города задачи: Famagusta/Ayia Napa и прочие — вон
    cands = [c for c in cands if c['municipality']]
    # площадь/координаты только в detail: тянем для не известных лотов
    fetched = 0
    for c in cands:
        if c['id'] in known or fetched >= BZ_DETAIL_CAP:
            continue
        r = bz_get(c['url'])
        fetched += 1
        ad = bz_ad_from_html(r.text, c['id']) if (r is not None and r.status_code == 200) else None
        if ad:
            BZ_EMBED[c['id']] = ad
            c.update(bz_cand_from_ad(ad, c))
        else:
            _v(f'  bazaraki detail miss: {c["url"][:80]}')
        time.sleep(0.5)
    # известным лотам площадь из state (нужна detect_price_changes и prefilter)
    if known:
        try:
            with open(STATE_PATH) as f:
                ls = json.load(f).get('listings', {})
            for c in cands:
                if c.get('area') is None:
                    rec = ls.get(f"bazaraki_{c['id']}") or {}
                    if rec.get('area_m2'):
                        c['area'] = rec['area_m2']
        except Exception:
            pass
    _v(f'  bazaraki: {len(cands)} lots in target cities, {fetched} detail fetched')
    return cands


def bz_embedded_html(aid):
    """Мини-HTML для cycle.fetch_html/parse: объявление из кеша свипа."""
    ad = BZ_EMBED.get(str(aid))
    if not ad:
        return None
    return ('<html><head><title>' + (ad.get('name') or '') + '</title></head><body>'
            '<script id="bz-ad" type="application/json">' + json.dumps(ad, ensure_ascii=False)
            + '</script></body></html>')


def known_ids():
    """ID лотов, уже учтённых в state (любой терминальный или промежуточный статус)."""
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
    except Exception:
        return set()
    ids = set()
    for k in (s.get('listings') or {}):
        for p in _KEY_PREFIXES:
            if k.startswith(p):
                ids.add(k[len(p):])
    return ids


if __name__ == '__main__':
    os.environ.setdefault('PIZZA_QUIET', '0')
    lots = sweep_bazaraki(pages=int(sys.argv[1]) if len(sys.argv) > 1 else 3)
    for l in lots[:15]:
        print(l.get('municipality'), '|', l.get('district_label'), '|', l.get('area'), 'm²',
              l.get('price'), '€ |', l.get('type'), '|', (l.get('title') or '')[:50])
    print(len(lots), 'lots')


# --------------------------------------------------------------------------
# zyprus.com — агентский портал (Drupal), ~30 коммерческих лотов в аренду по Кипру.
# Открыт без прокси. Поиск: /search/rent/grid?type[5]=5&type[17]=17&type[355]=355
# (Shop, Commercial Building, Mixed-use Building; Office=7 намеренно не берём),
# страницы 0-индексные (?page=N), 24 карточки + 1 промо-дубль. Карточка несёт
# title («Shop 750 SQM For Rent»), цену и адрес («Kolonakiou Avenue, Agios
# Athanasios, Limassol»); detail — координаты (drupalSettings leaflet features),
# «Covered Area», описание, фото в JSON-LD Product. Проверено 17.10.2026.
# --------------------------------------------------------------------------
ZY_BASE = 'https://www.zyprus.com'
ZY_LIST = ZY_BASE + '/search/rent/grid?type%5B5%5D=5&type%5B17%5D=17&type%5B355%5D=355'


def zy_get(url, timeout=35):
    """Сначала напрямую (с Mac работает), при неудаче через CY-прокси: с раннера
    GitHub первый боевой прогон 17.10 отдал zyprus как источник down."""
    from curl_cffi import requests as cffi
    routes = [None]
    if _RES_PROXY:
        routes.append({'http': _RES_PROXY, 'https': _RES_PROXY})
    for i, proxies in enumerate(routes):
        try:
            r = cffi.get(url, impersonate=_IMPS[i % len(_IMPS)], timeout=timeout,
                         allow_redirects=True, proxies=proxies,
                         headers={'Accept-Language': 'en-GB,en;q=0.9'})
            if r.status_code in (200, 404) and 'Just a moment' not in r.text[:3000]:
                return r
            _v(f'  zy_get http={r.status_code} via {"proxy" if proxies else "direct"}: {url[:80]}')
        except Exception as e:
            _v(f'  zy_get {type(e).__name__} via {"proxy" if proxies else "direct"}: {url[:80]}')
        time.sleep(1.0)
    return None


def _strip(html):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html or '')).strip()


def zy_split_location(loc):
    """'Kolonakiou Avenue, Agios Athanasios, Limassol' → (city, area, street)."""
    parts = [p.strip() for p in (loc or '').split(',') if p.strip()]
    if not parts:
        return '', '', ''
    city = CITY_LABELS.get(parts[-1].lower(), '')
    area = parts[-2] if len(parts) >= 2 else ''
    street = parts[0] if len(parts) >= 3 else ''
    if area.lower() == (parts[-1] or '').lower():
        area = ''
    return city, area, street


def _zy_cards(html):
    out = []
    for m in re.finditer(r'<article[^>]*about="(/property/(\d+)/[^"]+)"[^>]*>.*?</article>', html, re.S):
        card, path, pid = m.group(0), m.group(1), m.group(2)

        def fld(cls):
            mm = re.search(r'class="property-details__%s"[^>]*>(.*?)</div>' % cls, card, re.S)
            return _strip(mm.group(1)) if mm else ''
        title = fld('title') or path.rsplit('/', 1)[-1].replace('-', ' ')
        price = _price_int(re.sub(r'(?<=\d)[.,](?=\d{3}\b)', '', fld('price').split('€')[-1]))
        loc = fld('location')
        city, area, street = zy_split_location(loc)
        # «Commercial Building 1,052 SQM»: запятая — разделитель тысяч (баг 17.10: читалось 52)
        tm = re.match(r'\s*([A-Za-z\- ]+?)\s+(\d{1,3}(?:,\d{3})+|\d{2,5})\s*(?:sqm|m²|m2)', title, re.I)
        out.append({'id': pid, 'url': ZY_BASE + path, 'title': title, 'price': price,
                    'area': float(tm.group(2).replace(',', '')) if tm else _area_from_text(title),
                    'type': tm.group(1).strip() if tm else '', 'street': street,
                    'district_label': f'{city} — {area}' if area else city,
                    'municipality': city, 'source': 'zyprus.com', 'date': ''})
    return out


def sweep_zyprus(pages=3):
    """Shop + Commercial Building + Mixed-use Building, аренда, весь Кипр."""
    seen, cands = set(), []
    for p in range(pages):
        r = zy_get(f'{ZY_LIST}&page={p}')
        if r is None or r.status_code != 200:
            raise RuntimeError(f'zyprus page {p}: http={getattr(r, "status_code", None)}')
        cards = _zy_cards(r.text)
        new = [c for c in cards if c['id'] not in seen]
        _v(f'  zyprus p{p}: {len(cards)} cards, {len(new)} new')
        if not new:
            break
        for c in new:
            seen.add(c['id'])
            cands.append(c)
        if len(cards) < 20:
            break
        time.sleep(0.7)
    cands = [c for c in cands if c['municipality']]
    _v(f'  zyprus: {len(cands)} lots in target cities')
    return cands


def zy_parse_detail(html):
    """Поля detail-страницы zyprus: area, price, lat/lon, description, images, published."""
    out = {}
    m = re.search(r'(\d[\d.,]*)\s*m²\s*<span class="area">Covered Area', html)
    if m:
        try:
            out['area'] = float(m.group(1).replace(',', ''))
        except ValueError:
            pass
    m = re.search(r'class="property-details__price"[^>]*>(.*?)</div>', html, re.S)
    if m:
        out['price'] = _price_int(re.sub(r'(?<=\d)[.,](?=\d{3}\b)', '', _strip(m.group(1)).split('€')[-1]))
    m = re.search(r'"features":\[\{"type":"point","lat":(-?[\d.]+),"lon":(-?[\d.]+)', html)
    if m:
        out['lat'], out['lon'] = float(m.group(1)), float(m.group(2))
    m = re.search(r'class="field field--name-body[^"]*"[^>]*>(.*?)(?:</section>|<div class="field field--name-field)', html, re.S)
    if m:
        out['description'] = _strip(m.group(1))[:3000]
    m = re.search(r'article:published_time" content="([^"]+)"', html)
    if m:
        out['published_at'] = m.group(1)
    m = re.search(r'<title>([^<|:]*)', html)
    if m:
        out['title'] = m.group(1).strip()
    imgs = []
    ld = re.search(r'"@type":\s*"Product".*?"image":\s*\{[^}]*?"url":\s*\[(.*?)\]', html, re.S)
    if ld:
        imgs = re.findall(r'"(https?://[^"]+)"', ld.group(1))
    if not imgs:
        og = re.search(r'property="og:image" content="([^"]+)"', html)
        imgs = [og.group(1)] if og else []
    out['images'] = imgs
    return out

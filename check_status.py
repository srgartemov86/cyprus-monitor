#!/usr/bin/env python3
"""
Live-status check для лотов, помеченных in_sheet=True (Кипр).

Запускается из cycle.py --finalize ПОСЛЕ sweep, ДО gen_map.
Помечает мёртвые объявления `removed_from_sheet=True` чтобы они исчезли с карты.

Mortality markers:
  • bazaraki.com: 404/410 при снятии; живая страница несёт dehydrated JSON
                  объявления с полем is_closed (true = снято, страница ещё отдаётся).
                  Страница за Cloudflare — только curl_sweep.bz_get (CY-прокси);
                  блок/таймаут = 'unknown', НЕ dead.
  • zyprus.com:   404 / title «Page not found» при снятии.

Anti-rate-limit: кэп MAX_PER_CYCLE_PER_SOURCE, пауза DELAY_SEC между запросами.
Stale-grace: не проверяем лоты моложе MIN_AGE_HOURS (first_seen_at).
"""

import json, re, subprocess, time, sys, os, datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STATE_PATH = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'state.json')

# Throttling per source (per cycle)
MAX_PER_CYCLE_PER_SOURCE = {'bazaraki.com': 25, 'zyprus.com': 20}
DELAY_SEC_PER_SOURCE = {'bazaraki.com': 1.0, 'zyprus.com': 0.5}

# Don't check lots seen < this many hours ago (avoid checking fresh sweep results)
MIN_AGE_HOURS = 6

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'


def is_dead(src, http_code, size, title, closed=None):
    """Returns one of: 'dead', 'alive', 'rate_limited', 'unknown'."""
    title_l = (title or '').lower()
    if '429' in (title or '') or 'exceeded the limits' in title_l or http_code == '429':
        return 'rate_limited'
    if http_code in ('404', '410'):
        return 'dead'
    if http_code != '200':
        return 'unknown'
    if src == 'bazaraki.com':
        if closed is True:
            return 'dead'
        if closed is False:
            return 'alive'
        return 'unknown'  # JSON объявления не нашли: редизайн или заглушка
    if src == 'zyprus.com':
        if 'page not found' in title_l or 'not found' in title_l:
            return 'dead'
        return 'alive' if title else 'unknown'
    return 'unknown'


def fetch(url, timeout=25):
    """Returns (http_code, size, title, closed) or ('ERR', 0, '<error>', None)."""
    if 'bazaraki.com' in url:
        try:
            import curl_sweep
            r = curl_sweep.bz_get(url, timeout=timeout, attempts=2)
            if r is None:
                return 'ERR', 0, 'bazaraki block after retries', None
            body = r.text
            m = re.search(r'<title>([^<]*)</title>', body)
            title = m.group(1).strip() if m else ''
            if r.status_code != 200:
                return str(r.status_code), len(body), title, None
            ad = curl_sweep.bz_ad_from_html(body)
            closed = bool(ad.get('is_closed')) if ad else None
            return '200', len(body), title, closed
        except Exception as e:
            return 'ERR', 0, str(e)[:80], None

    try:
        r = subprocess.run(
            ['curl', '-sL', '-A', UA, '--max-time', str(timeout),
             '-w', '\n__HTTP_CODE__%{http_code}', url],
            capture_output=True, timeout=timeout + 5
        )
        body = r.stdout.decode('utf-8', errors='replace')
        idx = body.rfind('\n__HTTP_CODE__')
        if idx >= 0:
            http_code = body[idx + len('\n__HTTP_CODE__'):].strip()
            body = body[:idx]
        else:
            http_code = '???'
        m = re.search(r'<title>([^<]*)</title>', body)
        title = m.group(1).strip() if m else ''
        return http_code, len(body), title, None
    except Exception as e:
        return 'ERR', 0, str(e)[:80], None


def check_one(key, src, url):
    http_code, size, title, closed = fetch(url)
    verdict = is_dead(src, http_code, size, title, closed)
    return key, src, url, http_code, size, title, verdict


def main():
    print('=== check_status ===')
    t0 = time.time()
    with open(STATE_PATH) as f:
        state = json.load(f)

    now = dt.datetime.now(dt.timezone.utc)
    min_age = dt.timedelta(hours=MIN_AGE_HOURS)

    by_source = {}
    for k, v in state['listings'].items():
        if not v.get('in_sheet'): continue
        if v.get('removed_from_sheet'): continue
        if v.get('rejected'): continue
        url = v.get('url', '')
        if not url: continue
        src = v.get('source', '')
        if src not in MAX_PER_CYCLE_PER_SOURCE: continue
        # Grace для свежих НОВЫХ лотов (first_seen_at), а не недавно увиденных:
        # last_seen обновляет каждый свип, и мёртвые лоты иначе живут вечно.
        first_seen = v.get('first_seen_at')
        if first_seen:
            try:
                fso = dt.datetime.fromisoformat(first_seen.replace('Z', '+00:00'))
                if now - fso < min_age: continue
            except Exception: pass
        by_source.setdefault(src, []).append((k, src, url))

    plan = []
    for src, items in by_source.items():
        items.sort(key=lambda x: str(state['listings'][x[0]].get('last_seen_at') or ''))
        cap = MAX_PER_CYCLE_PER_SOURCE.get(src, 10)
        plan.append((src, items[:cap], items[cap:]))
        print(f'  {src:15} candidates={len(items):>3} checking={min(cap,len(items)):>2} deferred={max(0,len(items)-cap):>3}')

    def run_source(src, items, delay):
        out = []
        for i, (k, s, u) in enumerate(items):
            if i > 0: time.sleep(delay)
            out.append(check_one(k, s, u))
        return out

    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(run_source, src, items, DELAY_SEC_PER_SOURCE.get(src, 1.0))
                   for src, items, _ in plan]
        for f in as_completed(futures):
            results.extend(f.result())

    killed, killed_urls, rate_limited = [], [], []
    alive_count = unknown_count = err_count = 0
    iso_now = now.isoformat()
    human_now = now.astimezone().strftime('%Y-%m-%d %H:%M')
    for k, src, url, code, size, title, verdict in results:
        v = state['listings'].get(k)
        if v is None: continue
        if verdict == 'dead':
            v['removed_from_sheet'] = True
            v['stale_marked_at'] = iso_now
            v['removed_human'] = human_now
            v['dead_reason'] = f'status_check title={title[:80]!r} size={size} http={code}'
            killed.append((k, src, title[:60]))
            killed_urls.append((url, human_now))
        elif verdict == 'rate_limited':
            v['rate_limited_at'] = iso_now
            rate_limited.append((k, src))
        elif verdict == 'alive':
            v['last_seen_at'] = iso_now
            v.pop('rate_limited_at', None)
            alive_count += 1
        elif verdict == 'unknown':
            unknown_count += 1
        else:
            err_count += 1

    with open(STATE_PATH, 'w') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    sheet_updated = 0
    if killed_urls:
        try:
            sheet_updated = mark_dead_in_sheet(killed_urls)
        except Exception as e:
            print(f'  sheet update failed: {e}', file=sys.stderr)

    print(f'\nResult: {len(killed)} killed · {alive_count} alive · {len(rate_limited)} rate-limited · '
          f'{unknown_count} unknown · {err_count} err · sheet_updated={sheet_updated} · {time.time()-t0:.1f}s')
    if killed:
        print('\nKILLED:')
        for k, src, t in killed[:30]:
            print(f'  {src:15} {k[:35]:<35} title={t!r}')
    return {'killed': len(killed), 'alive': alive_count, 'rate_limited': len(rate_limited),
            'unknown': unknown_count, 'err': err_count, 'sheet_updated': sheet_updated}


# Sheet column indices (1-indexed for update_cells).
#   I (9)  = Removed from site  (bot owns — timestamp когда detected)
#   J (10) = Comment            (user-only — НИКОГДА не трогаем)
#   K (11) = Status             (user-only, кроме "Removed from site" если пусто)
COL_REMOVAL_DATE = 9   # I
COL_COMMENT      = 10  # J — DO NOT WRITE
COL_STATUS       = 11  # K
COL_MANUAL_COORD = 12  # L — user override: Google Maps URL / "lat,lon"
HEADER_REMOVAL_DATE = 'Removed from site'
HEADER_STATUS = 'Status'
HEADER_MANUAL_COORD = 'Exact location'


def mark_dead_in_sheet(dead_urls):
    """Для каждого URL находит строку в Sheets и пишет I = timestamp (всегда),
    K = "Removed from site" (ТОЛЬКО если ячейка пустая). J не трогаем."""
    import gen_map
    from sheets_append import update_cells
    sheet = gen_map.fetch_sheet_rows()
    if not sheet:
        print('  sheet empty / unfetchable — skipping writeback', file=sys.stderr)
        return 0

    cells = [
        {'row': 1, 'col': COL_REMOVAL_DATE, 'value': HEADER_REMOVAL_DATE},
        {'row': 1, 'col': COL_STATUS,       'value': HEADER_STATUS},
        {'row': 1, 'col': COL_MANUAL_COORD, 'value': HEADER_MANUAL_COORD},
    ]
    touched_rows = 0
    for url, human_ts in dead_urls:
        srow = sheet.get(url)
        if not srow:
            print(f'  no sheet row for dead url: {url[:80]}', file=sys.stderr)
            continue
        row = srow['row']
        current_status = (srow.get('status') or '').strip()
        if not current_status or current_status.lower() in ('removed', 'removed from site'):
            cells.append({'row': row, 'col': COL_STATUS, 'value': 'Removed from site'})
        else:
            print(f'  row {row}: keeping user status {current_status!r}, only writing date')
        cells.append({'row': row, 'col': COL_REMOVAL_DATE, 'value': human_ts})
        touched_rows += 1

    if touched_rows == 0:
        return 0
    res = update_cells(cells)
    print(f'  sheet writeback: {res}')
    return touched_rows


if __name__ == '__main__':
    main()

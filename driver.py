#!/usr/bin/env python3
"""driver.py — серверный оркестратор часового цикла (замена CCD-агента).

Делает то, что на Mac делал Claude-агент по SKILL.md:
  1. cycle.py (process phase) → JSON
  2. passes → альбом фото + caption в Telegram → --mark-sent
     Вместо LLM-перевода румынского Summary — глоссарий-замена терминов
     (команда партнёра читает остальное через built-in Translate в Telegram).
  3. price_changes → текстовое сообщение (reply на исходный лот, если есть id)
  4. cycle.py --finalize (check_status + gen_map + surge deploy)

Запускается из GitHub Actions (см. .github/workflows/monitor.yml).
Env: CY_DATA, TG_SESSION, TG_API_ID, TG_API_HASH, GOOGLE_TOKEN_PATH,
     CY_CHAT_ID (default 3828339567).
"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).parent
CHAT_ID = os.environ.get('CY_CHAT_ID', '')  # супергруппа Cyprus-lokal-monitor (workflow)
# Служебные уведомления (health-алерты) — лично Сергею в личку с ботом
# @clujmonitorbot (135201285), не в рабочий чат лотов.
ALERT_CHAT_ID = os.environ.get('CY_ALERT_CHAT_ID', '135201285')
MANY_PASSES = 15  # SKILL: при ≥15 лотов — одна сводка вместо N сообщений

# Глоссарий ro→en (SKILL.md): термины недвижимости, по которым принимается решение.
# Остальной текст остаётся румынским — Telegram Translate в один тап.
GLOSSARY = [
    (r'\bspa[țt]iu comercial\b', 'commercial space'),
    (r'\bvad comercial\b', 'foot traffic'),
    (r'\bvad pietonal\b', 'foot traffic'),
    (r'\bparter\b', 'ground floor'),
    (r'\bvitrin[ăa]\b', 'display window'),
    (r'\bla strad[ăa]\b', 'street-facing'),
    (r'\bstradal[ăa]?\b', 'street-facing'),
    (r'\bchirie\b', 'rent'),
    (r'\bgaran[țt]ie\b', 'deposit'),
    (r'\b[îi]ntre[țt]inere\b', 'maintenance'),
    (r'\brenovat[ăa]?\b', 'renovated'),
    (r'\bfinisat[ăa]?\b', 'fitted out'),
    (r'\bautoriza[țt]ie de func[țt]ionare\b', 'operating permit'),
    (r'\balimenta[țt]ie public[ăa]\b', 'food service'),
    (r'\bteras[ăa]\b', 'terrace'),
    (r'\bcentral[ăa] termic[ăa]\b', 'own heating unit'),
    (r'\bgeamuri? termopan\b', 'double glazing'),
]


def glossary_en(text):
    for pat, repl in GLOSSARY:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return text


# Модели по убыванию качества; alias-имена (-latest) не протухают при ротации версий.
GEMINI_MODELS = ('gemini-flash-latest', 'gemini-flash-lite-latest')


def gemini_summary(desc_ro):
    """Румынское описание → английская выжимка 2–4 строки (Gemini, free tier).
    None при любой ошибке — вызывающий откатывается на глоссарий."""
    key = os.environ.get('GEMINI_API_KEY')
    if not key or not (desc_ro or '').strip():
        return None
    import requests
    prompt = ("You are helping a pizza chain scout rental locations in Cyprus. Compress this "
              "commercial-property listing (English, sometimes Greek or Russian: translate) to 2-4 short "
              "lines with only what matters for a restaurant tenant: space type/condition, floor, "
              "street frontage/windows, utilities (ventilation, power, HVAC, kitchen), terms "
              "(deposit, availability, VAT). "
              "Plain text, no intro, no bullets, no markdown. "
              "Then, on the LAST line, output exactly 'LOCATION: <street name or nearby landmarks "
              "mentioned in the text, max 8 words>' or 'LOCATION: none' if the text gives no hint.\n\n"
              + desc_ro[:2000])
    body = {'contents': [{'parts': [{'text': prompt}]}],
            # думающие flash-модели тратят токены на reasoning ДО ответа —
            # 500 обрезало перевод на полуслове, нужен запас
            'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 3000}}
    for model in GEMINI_MODELS:
        try:
            r = requests.post(
                f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}',
                json=body, timeout=40)
            data = r.json()
            text = data['candidates'][0]['content']['parts'][0]['text'].strip()
            if len(text) > 20:
                return text
        except Exception as e:
            print(f'  gemini {model} failed: {e}', file=sys.stderr)
    return None


def split_location_hint(text):
    """Отделяет 'LOCATION: ...' (последняя строка ответа Gemini) от summary."""
    if not text:
        return text, None
    m = re.search(r'\n?LOCATION:\s*(.+)\s*$', text, re.IGNORECASE)
    if not m:
        return text, None
    hint = m.group(1).strip().rstrip('.')
    if hint.lower() in ('none', 'n/a', '-'):
        hint = None
    return text[:m.start()].rstrip(), hint


def english_summary(pass_rec):
    """Английский Summary для caption/таблицы/карты: Gemini → глоссарий-fallback."""
    state_path = os.path.join(os.environ.get('CY_DATA', '.'), 'state.json')
    desc = ''
    try:
        with open(state_path, encoding='utf-8') as f:
            rec = json.load(f).get('listings', {}).get(pass_rec['listing_key'], {})
        # заголовок вперёд: ориентиры места агенты часто пишут ТОЛЬКО в нём
        # (кейс 'in zona OMV Marasti, Toni Auto, Posta' — в описании их нет).
        # У старых лотов title в state нет — восстанавливаем из URL-слага.
        title = rec.get('title')
        if not title and rec.get('url'):
            m = re.search(r'/(?:d/)?oferta/([a-z0-9\-]+?)(?:-ID\w+)?(?:\.html)?$',
                          rec['url'], re.IGNORECASE)
            if m:
                title = m.group(1).replace('-', ' ')
        desc = ' — '.join(x for x in (title, rec.get('description')) if x)
    except Exception:
        pass
    if not desc:  # в state нет — берём snippet из caption
        m = re.search(r'📝 Summary:\s*(.+)', pass_rec.get('caption') or '', re.DOTALL)
        desc = m.group(1).strip() if m else ''
    g = gemini_summary(desc)
    if g:
        return split_location_hint(g)
    return glossary_en(desc), None


def run_json(args, timeout):
    """Запуск python-скрипта, JSON — последняя непустая строка stdout."""
    r = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                       timeout=timeout, cwd=HERE)
    sys.stderr.write(r.stderr or '')
    lines = [l for l in (r.stdout or '').strip().splitlines() if l.strip()]
    for line in reversed(lines):
        if line.lstrip().startswith('{'):
            return json.loads(line)
    raise RuntimeError(f'{args[0]}: no JSON in output (rc={r.returncode})')


def send_album(caption, photos):
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                     encoding='utf-8') as f:
        f.write(caption)
        cap_path = f.name
    try:
        return run_json(['send_album.py', CHAT_ID, cap_path] + photos[:10], 120)
    finally:
        os.unlink(cap_path)


def send_text(text, reply_to=None, chat_id=None, html=False):
    args = ['send_text.py', chat_id or CHAT_ID, '-']
    if reply_to:
        args += ['--reply-to', str(reply_to)]
    if html:
        args += ['--html']
    r = subprocess.run([sys.executable] + args, input=text, capture_output=True,
                       text=True, timeout=60, cwd=HERE)
    sys.stderr.write(r.stderr or '')
    return r.returncode == 0


def main():
    out = run_json(['cycle.py'], 1500)  # bazaraki detail через прокси + Overpass: до ~15 мин
    if out.get('concurrent_cycle_running'):
        print('concurrent cycle running — exit')
        return 0

    passes = out.get('passes') or []
    sent = failed = nophoto_sent = 0

    if len(passes) >= MANY_PASSES:
        lines = [f'🍕 {len(passes)} new locations (bulk):']
        for p in passes:
            lines.append(f"· {p.get('district','?')} · {p.get('area')} m² · "
                         f"{p.get('price')} €/mo · {p.get('url','')}")
        if send_text('\n'.join(lines)[:4000]):
            # bulk: mark-sent без message_id привязки к лоту — ставим 0
            for p in passes:
                subprocess.run([sys.executable, 'cycle.py', '--mark-sent',
                                p['listing_key'], '0'],
                               capture_output=True, cwd=HERE, timeout=120)
            sent = len(passes)
    else:
        for p in passes:
            caption = p.get('caption') or ''
            summary, loc_hint = english_summary(p)
            # адресная зацепка из текста объявления (улица/ориентиры) — в строку 📍,
            # когда структурного адреса нет (кейс OLX 'zona OMV Marasti...')
            if loc_hint:
                for stub in ('📍 no street address (area only)',
                             '📍 no street address (map pin is exact)',
                             '📍 no address or map pin in the listing',
                             '📍 no exact address'):
                    if stub in caption:
                        caption = caption.replace(stub, f'📍 ~{loc_hint} (from listing text)')
                        break
            # Ни адреса, ни пина (🗺 = текстовый поиск по заглушке), но зацепка есть:
            # геокодим её, чтобы дать пин, расстояние, район и score
            # (кейс imobiliare 256317967 14.09: пин по 'Str. Muncii 209' вместо поиска
            # по строке 'no address or map pin in the listing').
            if loc_hint and 'google.com/maps/search/?api=1' in caption:
                try:
                    g = run_json(['cycle.py', '--geocode-hint', p['listing_key'], loc_hint], 240)
                except Exception as e:
                    g = {}
                    print(f'  geocode-hint failed: {e}', file=sys.stderr)
                if g.get('ok'):
                    caption = re.sub(
                        r'🗺 \S+',
                        f"🗺 https://www.google.com/maps/?q={g['lat']},{g['lon']} "
                        f"(approximate: geocoded from listing text)", caption, count=1)
                    caption = caption.replace('⚠️ uncertain_distance',
                                              f"⚠️ approx_distance_{g['dist_km']}km")
                    if g.get('district'):
                        caption = re.sub(r'^🍕 Location · [^\n]+',
                                         f"🍕 Location · {g['district']} (by street)",
                                         caption, count=1)
                    if g.get('score_line') and 'Location score' not in caption:
                        caption = caption.rstrip('\n') + '\n\n\n' + g['score_line']
            # Переклеиваем блок Summary на английский (cycle.py кладёт румынский snippet).
            # ВАЖНО: cycle приклеивает строку скоринга ПОСЛЕ Summary — сохранить её
            # (баг 2026-07-11: тупой срез до конца снёс "Location score" из карточек).
            if '📝 Summary:' in caption:
                idx = caption.index('📝 Summary:')
                head, tail = caption[:idx], caption[idx:]
                m = re.search(r'\n\n\S+ Location score.*$', tail, re.DOTALL)
                score_part = m.group(0) if m else ''
                # −140: резерв под "📋 Sheet: <link>", который допишется после mark-sent
                budget = 1024 - 140 - len(head) - len(score_part) - len('📝 Summary: \n')
                caption = head + f'📝 Summary: {summary[:max(budget, 100)].rstrip()}\n' + score_part
            elif summary:
                caption += f'\n📝 Summary: {summary[:600]}\n'
            caption = caption[:1024]
            photos = [ph for ph in (p.get('photo_paths') or []) if os.path.exists(ph)]
            try:
                if photos:
                    res = send_album(caption, photos)
                    mid = res.get('first_message_id')
                else:
                    mid = 0 if send_text(caption) else None
                    if p.get('had_photos'):
                        nophoto_sent += 1  # фото были в объявлении, но не скачались
                if mid is None:
                    raise RuntimeError('send failed')
                ms = subprocess.run([sys.executable, 'cycle.py', '--mark-sent',
                                     p['listing_key'], str(mid), '--desc-ru', summary[:900]],
                                    capture_output=True, text=True, cwd=HERE, timeout=120)
                # mark-sent вернул ссылку на строку таблицы → дописываем в caption
                try:
                    link = json.loads(ms.stdout.strip().splitlines()[-1]).get('sheet_link')
                except Exception:
                    link = None
                if link and mid and photos:
                    new_cap = f'{caption.rstrip()}\n📋 Sheet: {link}'[:1024]
                    er = subprocess.run([sys.executable, 'edit_caption.py', CHAT_ID,
                                         str(mid), '-'], input=new_cap, capture_output=True,
                                        text=True, timeout=60, cwd=HERE)
                    if '"ok": true' not in (er.stdout or ''):
                        print(f'  caption edit failed for {mid}', file=sys.stderr)
                sent += 1
            except Exception as e:
                # не mark-sent → лот останется unalerted, доедет следующим циклом
                print(f"  SEND FAIL {p.get('listing_key')}: {e}", file=sys.stderr)
                failed += 1

    for c in out.get('price_changes') or []:
        msg = (f"💶 Price changed · {c.get('district','?')} "
               f"{c.get('address','')} — was {c['old']} € → now {c['new']} €/mo\n"
               f"🔗 {c.get('url','')}")
        send_text(msg, reply_to=c.get('reply_to_message_id'))

    fin = run_json(['cycle.py', '--finalize'], 600)

    # --- Health-алерт: аномалии цикла → короткое ⚠️ в тот же чат (English) ---
    s_sum = out.get('summary') or {}
    alerts = []
    if s_sum.get('sources_down'):
        alerts.append('⛔ sources down: ' + ', '.join(s_sum['sources_down']))
    ff = [r for r in (out.get('rejects') or [])
          if str(r.get('reason', '')).startswith('fetch_fail')]
    if len(ff) >= 3:
        alerts.append(f'🌐 detail fetch failed for {len(ff)} listings (ban/network?)')
    if failed:
        alerts.append(f'✉️ send failed for {failed} listings')
    if nophoto_sent:
        alerts.append(f'📷 posted without photos (listing had them): {nophoto_sent}')
    noscore = [p for p in passes if p.get('score') is None]
    if noscore:
        alerts.append(f'📊 location score missing on {len(noscore)} posted')
    rs = out.get('reject_sheet') or {}
    if rs and not rs.get('ok'):
        alerts.append('📄 Rejected sheet write failed')
    if fin.get('map_ok') is False:
        alerts.append('🗺 map generation failed')
    if fin.get('map_surge_ok') is False:
        alerts.append('🌍 surge deploy failed')
    if isinstance(fin.get('last_scan_stamp'), str):
        alerts.append('🕐 Last scan stamp failed')
    canary_bad = [f'{k} — {v}' for k, v in
                  ((fin.get('canary') or {}).get('results') or {}).items()
                  if str(v).startswith('FAIL')]
    if canary_bad:
        alerts.append('🐤 parser canary:\n   ' + '\n   '.join(canary_bad))
    if alerts:
        send_text('⚠️ Cyprus monitor — cycle anomalies:\n'
                  + '\n'.join('· ' + a for a in alerts),
                  chat_id=ALERT_CHAT_ID)

    # Вечерняя сводка дня в чат (последний прогон дня: после 21:00 по Клужу).
    # Дедуп через state['daily_digest_sent'] — шлём один раз в сутки.
    try:
        from datetime import datetime
        import zoneinfo
        now_cluj = datetime.now(zoneinfo.ZoneInfo('Asia/Nicosia'))
        state_path = os.path.join(os.environ.get('CY_DATA', '.'), 'state.json')
        with open(state_path, encoding='utf-8') as f:
            st = json.load(f)
        ds = st.get('daily_stats') or {}
        today = now_cluj.strftime('%Y-%m-%d')
        if (now_cluj.hour >= 21 and ds.get('date') == today
                and st.get('daily_digest_sent') != today):
            _sheet = 'https://docs.google.com/spreadsheets/d/1PNXJg4aT5jeWYz9sqlQmE6yoz-Lvf6D8PgJFusPZCYE/edit'
            # прошедшие лоты дня — буллетами: район · м² · цена, ссылкой на объявление
            _passed_lines = ''
            _today_sent = [v for v in st.get('listings', {}).values()
                           if (v.get('sent_at') or '')[:10] == today
                           and v.get('telegram_message_id')]
            _today_sent.sort(key=lambda v: v.get('sent_at') or '')
            for v in _today_sent[:10]:
                _label = (f"{v.get('district') or '?'} · {v.get('area_m2')} m² · "
                          f"{v.get('price_eur')} €/mo")
                _passed_lines += (f"\n   • <a href=\"{v.get('url')}\">{_label}</a>"
                                  if v.get('url') else f"\n   • {_label}")
            msg = (f"📊 Daily summary · {now_cluj.strftime('%b %d')}\n"
                   f"· {ds['runs']} feed scans, ~{ds['sweep_last']} live listings watched\n"
                   f"· {ds['new']} new listings analyzed in detail\n"
                   f"· {ds['rejects']} rejected → <a href=\"{_sheet}#gid=133442332\">Rejected tab</a>\n"
                   f"· {ds['passes']} passed the filters and posted here → <a href=\"{_sheet}#gid=498788918\">Locations tab</a>"
                   f"{_passed_lines}\n"
                   f"🗺 Map: https://dodo-cyprus-lokali.surge.sh/lokali.html")
            if ds.get('duplicates'):
                msg += f"\n· {ds['duplicates']} cross-posted duplicates merged"
            if send_text(msg, html=True):
                st['daily_digest_sent'] = today
                with open(state_path, 'w', encoding='utf-8') as f:
                    json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'  daily digest failed: {e}', file=sys.stderr)

    s = out.get('summary') or {}
    print(json.dumps({
        'sweep': s.get('sweep_raw'), 'new': s.get('new'),
        'passes': len(passes), 'sent': sent, 'send_failed': failed,
        'rejects': s.get('rejects'), 'duplicates': s.get('duplicates'),
        'price_changes': len(out.get('price_changes') or []),
        'sources_down': s.get('sources_down'),
        'map_ok': fin.get('map_ok'), 'surge_ok': fin.get('map_surge_ok'),
    }, ensure_ascii=False))
    # Упавшая отправка не должна валить workflow: state цел, лот доедет позже
    return 0


if __name__ == '__main__':
    sys.exit(main())

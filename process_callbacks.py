#!/usr/bin/env python3
"""process_callbacks.py — нажатия inline-кнопок статуса под карточками.

Бот не имеет сервера, поэтому нажатия (callback_query) забираются getUpdates
раз в час из прогона driver.py. Для каждого: статус в state (rec['status']),
колонка K основного листа (по URL лота), текст статус-сообщения под карточкой.
Смещение updates хранится в state['tg_update_offset'].

Бот @clujmonitorbot общий для мониторов: getUpdates подтверждает ВСЕ апдейты,
так что кнопки должны обрабатываться только одним монитором (Кипр, 18.10.2026).
Callback_data: 'st|<listing_key>|<code>', коды в tg_bot.STATUS_BUTTONS.
"""
import json, os, sys, datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_bot

STATE_PATH = os.path.join(os.environ.get('CY_DATA', '/Users/dodo/cyprus-monitor-cloud/data'), 'state.json')
CHAT_ID = os.environ.get('CY_CHAT_ID', '')
COL_STATUS = 11  # K


def main():
    if not os.environ.get('TG_BOT_TOKEN'):
        print(json.dumps({'ok': False, 'error': 'no TG_BOT_TOKEN'}))
        return 0
    with open(STATE_PATH, encoding='utf-8') as f:
        state = json.load(f)
    offset = state.get('tg_update_offset')
    updates = tg_bot.get_updates(offset=offset)
    if not updates:
        print(json.dumps({'ok': True, 'applied': 0, 'updates': 0}))
        return 0

    listings = state.get('listings', {})
    applied, cells, edits = 0, [], []
    url_rows = None
    for u in updates:
        cq = u.get('callback_query') or {}
        data = cq.get('data') or ''
        if not data.startswith('st|'):
            continue
        _, key, code = (data.split('|') + ['', ''])[:3]
        rec = listings.get(key)
        label = tg_bot.STATUS_LABEL.get(code)
        if not rec or not label:
            continue
        who = (cq.get('from') or {})
        name = (who.get('first_name') or '') + (' ' + who['last_name'] if who.get('last_name') else '')
        when = dt.datetime.now(dt.timezone.utc).strftime('%d.%m %H:%M UTC')
        rec['status'] = label
        rec['status_by'] = name.strip() or who.get('username') or ''
        rec['status_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        applied += 1
        tg_bot.answer_callback(cq.get('id', ''), f'{label} saved')  # обычно уже протух, не страшно
        msg = (cq.get('message') or {})
        if msg.get('message_id'):
            edits.append((msg['message_id'], f'Status: {label} · {rec["status_by"]} · {when}', key,
                          rec.get('sheet_link')))
        if rec.get('url'):
            if url_rows is None:
                try:
                    from sheets_append import _sheets_service, SPREADSHEET_ID
                    vals = _sheets_service().spreadsheets().values().get(
                        spreadsheetId=SPREADSHEET_ID, range='E2:E2000').execute().get('values', [])
                    url_rows = {r[0].strip(): i for i, r in enumerate(vals, start=2) if r and r[0]}
                except Exception as e:
                    print(f'  sheet read failed: {e}', file=sys.stderr)
                    url_rows = {}
            row = url_rows.get(rec['url'].strip())
            if row:
                cells.append({'row': row, 'col': COL_STATUS, 'value': label})

    state['tg_update_offset'] = max(u['update_id'] for u in updates) + 1
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    sheet_ok = None
    if cells:
        try:
            from sheets_append import update_cells
            update_cells(cells)
            sheet_ok = len(cells)
        except Exception as e:
            sheet_ok = f'fail: {e}'
    for mid, text, key, link in edits:
        tg_bot.edit_status_message(CHAT_ID, mid, text, key, sheet_link=link)
    print(json.dumps({'ok': True, 'applied': applied, 'updates': len(updates), 'sheet': sheet_ok}))
    return 0


if __name__ == '__main__':
    sys.exit(main())

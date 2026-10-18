#!/usr/bin/env python3
"""Insert/manage rows in Cyprus Sheets — напрямую через Sheets API v4
(OAuth-токен drive), без Apps Script webhook.

Spreadsheet: 1NZNlx2G24Ea-zGNurKx7fTAmHgLK4tOSjtB7ScYx-7c
  Лист «Лоты» (первый, gid 498788918):
    A=Адрес | B=Район | C=Площадь | D=Цена | E=Ссылка | F=Текст объявления (RU)
    G=Дата размещения | H=Дата добавления | I=Дата снятия с сайта |
    J=Комментарий (user-only) | K=Статус | L=Точное место (user-only)
  Лист «не прошли фильтр» (gid 133442332): A..H как в Белграде.

Interface (cycle.py/check_status/gen_map импортируют эти имена): insert_lots, update_cells, append_reject_rows,
_sheets_service, SPREADSHEET_ID. WEBHOOK_URL/SECRET оставлены пустыми для
совместимости импортов (кнопок статуса на карте v1 нет).
"""
import json, os, sys, time


def _retry(fn, tries=3, base_delay=2.0):
    """Google API моргает (429/5xx/сеть) — 3 попытки с backoff вместо потери записи."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < tries - 1:
                time.sleep(base_delay * (i + 1))
    raise last
from datetime import datetime, timezone

SPREADSHEET_ID = '1PNXJg4aT5jeWYz9sqlQmE6yoz-Lvf6D8PgJFusPZCYE'  # таблица Cyprus locations (Dodo monitor), 17.10.2026
MAIN_SHEET_NAME = 'Locations'
MAIN_SHEET_GID = 498788918
REJECT_SHEET_NAME = 'Rejected'
GOOGLE_TOKEN = os.environ.get('GOOGLE_TOKEN_PATH', '/Users/dodo/.config/gcloud/dodo-drive-token.json')

# Apps Script вебхук — ТОЛЬКО для кнопок статусов на карте. Деплой общий с Клужем
# (проект cluj-lokali-webhook, версия 2 от 18.10.2026 выпущена через Apps Script API):
# секрет выбирает таблицу, пишет только в колонку K (ALLOWED_COLS).
# Все записи бота идут напрямую через Sheets API ниже — webhook им не нужен.
WEBHOOK_URL = 'https://script.google.com/macros/s/AKfycbyliXATQKxywDQ24IhbTukqf2yC0z2ccpXh_VK8y9FA2tq6JUrGpUuuvu3D3s2mTduq/exec'
SECRET = 'cyprus-lokali-4782e161ec403a61-2026'


def _sheets_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN)
    if not creds.valid:
        creds.refresh(Request())
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


def _sheet_id_by_title(svc, title):
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sh in meta.get('sheets', []):
        if sh['properties']['title'] == title:
            return sh['properties']['sheetId']
    return None


def _utc_now_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _normalize_date_posted(raw):
    if not raw: return ''
    if len(raw) >= 10 and raw[4] == '-' and raw[7] == '-':
        return raw[:10]
    if len(raw) >= 10 and raw[2] == '.' and raw[5] == '.':
        return f'{raw[6:10]}-{raw[3:5]}-{raw[0:2]}'
    return raw


def _insert_rows_at_top(sheet_name, fallback_gid, rows, ncols):
    """Вставить строки под заголовком (row 2), остальное сдвигается вниз."""
    svc = _sheets_service()
    sheet_id = _sheet_id_by_title(svc, sheet_name)
    if sheet_id is None:
        sheet_id = fallback_gid
    n = len(rows)
    # insertDimension не идемпотентна: ретрай после «успел вставить, ответ потерялся»
    # даст лишние пустые строки — терпимо, потеря данных хуже.
    _retry(lambda: svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={'requests': [{
            'insertDimension': {
                'range': {'sheetId': sheet_id, 'dimension': 'ROWS',
                          'startIndex': 1, 'endIndex': 1 + n},
                'inheritFromBefore': False,
            }
        }]},
    ).execute())
    end_col = chr(ord('A') + ncols - 1)
    _retry(lambda: svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{sheet_name}'!A2:{end_col}{1 + n}",
        valueInputOption='USER_ENTERED',
        body={'values': rows},
    ).execute())
    return {'ok': True, 'inserted': n, 'op': 'insert_at_top'}


def insert_lots(lots, timeout=30):
    """Insert lots at top of «Лоты». Bot пишет только A–H."""
    if not lots:
        return {'ok': True, 'inserted': 0}
    now_str = _utc_now_str()
    rows = []
    for l in lots:
        rows.append([
            l.get('address', ''),
            l.get('district', ''),
            l.get('area', ''),
            l.get('price', ''),
            l.get('url', ''),
            l.get('description_ru', ''),
            _normalize_date_posted(l.get('date_posted', '')),
            now_str,
        ])
    return _insert_rows_at_top(MAIN_SHEET_NAME, MAIN_SHEET_GID, rows, 8)


def update_cells(cells, timeout=30):
    """Generic cell updates on «Лоты». cells = [{row, col, value}, ...] (1-indexed)."""
    if not cells:
        return {'ok': True, 'updated': 0}
    svc = _sheets_service()
    data = []
    for c in cells:
        col_letter = chr(ord('A') + int(c['col']) - 1)
        data.append({
            'range': f"'{MAIN_SHEET_NAME}'!{col_letter}{c['row']}",
            'values': [[c['value']]],
        })
    svc.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={'valueInputOption': 'USER_ENTERED', 'data': data},
    ).execute()
    return {'ok': True, 'updated': len(data)}


def append_reject_rows(rows):
    """Реджекты — наверх листа «не прошли фильтр» (A..H)."""
    if not rows:
        return {'ok': True, 'inserted': 0}
    return _insert_rows_at_top(REJECT_SHEET_NAME, 133442332, rows, 8)


def delete_rows(row_numbers, timeout=30):
    """Delete rows on «Лоты» by 1-indexed row numbers."""
    if not row_numbers:
        return {'ok': True, 'deleted': 0}
    svc = _sheets_service()
    sheet_id = _sheet_id_by_title(svc, MAIN_SHEET_NAME) or MAIN_SHEET_GID
    reqs = [{'deleteDimension': {'range': {
        'sheetId': sheet_id, 'dimension': 'ROWS',
        'startIndex': r - 1, 'endIndex': r,
    }}} for r in sorted(row_numbers, reverse=True)]
    svc.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID,
                                   body={'requests': reqs}).execute()
    return {'ok': True, 'deleted': len(row_numbers)}


if __name__ == '__main__':
    lots = json.load(sys.stdin)
    print(json.dumps(insert_lots(lots)))


def create_lot_named_range(row, listing_key):
    """Named range на строку лота (A{row}:L{row}) листа Locations. Возвращает
    URL с #rangeid — он отслеживает строку при insert_at_top (обычный range=A{row}
    протухает с каждой вставкой сверху). Дубликат имени → пересоздаём."""
    name = 'lot_' + ''.join(c if c.isalnum() else '_' for c in listing_key)
    svc = _sheets_service()
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID,
                                  fields='namedRanges').execute()
    reqs = [{'deleteNamedRange': {'namedRangeId': nr['namedRangeId']}}
            for nr in meta.get('namedRanges', []) if nr.get('name') == name]
    reqs.append({'addNamedRange': {'namedRange': {
        'name': name,
        'range': {'sheetId': 498788918, 'startRowIndex': row - 1, 'endRowIndex': row,
                  'startColumnIndex': 0, 'endColumnIndex': 12}}}})
    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID, body={'requests': reqs}).execute()
    rid = resp['replies'][-1]['addNamedRange']['namedRange']['namedRangeId']
    return (f'https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}'
            f'/edit#gid=498788918&rangeid={rid}')

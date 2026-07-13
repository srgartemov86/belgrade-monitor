#!/usr/bin/env python3
"""Insert/manage rows in Belgrade-3 Sheets via Apps Script webhook.

Canonical column layout (подтверждено 2026-05-14):
  A=Адрес | B=Район | C=Площадь | D=Цена | E=Ссылка | F=Текст объявления (RU)
  G=Дата размещения (ISO yyyy-mm-dd) | H=Дата добавления (auto UTC)
  I=Дата снятия с сайта (bot, check_status) | J=Комментарий (user-only)
  K=Статус (user + bot: "Снят с сайта" only when K empty; user's "Не подходит" / "В работе" preserved)

Usage:
    from sheets_append import insert_lots, delete_rows, sort_by_date_posted
    insert_lots([
        {'address': 'Cara Nikolaja II', 'district': 'Vračar (Čubura)',
         'area': 220, 'price': 4000, 'url': 'https://4zida.rs/...',
         'description_ru': 'Полный смысловой перевод объявления...',
         'date_posted': '2026-04-29'},
    ])
    # → {'ok': True, 'inserted': 1}

Behavior:
- New lots inserted at top (row 2), rest shifts down. Bot writes 8 values per row (A–H).
- Column J (Комментарий) NEVER touched by bot.
- Column I (Дата снятия) and K (Статус) — bot writes from check_status.py when lot dies.
"""
import json, os, ssl, sys, urllib.request
from datetime import datetime, timezone

WEBHOOK_URL = 'https://script.google.com/macros/s/AKfycbxDxDV1Ha7RA1HbhR8935d0rTFpIzadBGkOac2YEGEOCjhVP-VJ57TDi6ud_Q8EUe7i/exec'
SECRET = 'pizz-belgrade-3-secret-2026'

def _post(payload, timeout=30):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(WEBHOOK_URL, data=body,
                                  headers={'Content-Type': 'application/json'},
                                  method='POST')
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return json.loads(r.read().decode())

def _utc_now_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

def _normalize_date_posted(raw):
    """Return ISO yyyy-mm-dd, accepting yyyy-mm-dd or dd.mm.yyyy or empty."""
    if not raw: return ''
    if len(raw) >= 10 and raw[4] == '-' and raw[7] == '-':
        return raw[:10]
    if len(raw) >= 10 and raw[2] == '.' and raw[5] == '.':
        return f'{raw[6:10]}-{raw[3:5]}-{raw[0:2]}'
    return raw

def insert_lots(lots, timeout=30):
    """Insert lots at top of sheet. Each lot is a dict with:
        address, district, area, price, url, description_ru (optional), date_posted (optional)
    Columns I (Комментарий) and J (Статус) are user-only — bot writes only A–H.
    Date добавления (H) auto-set to current UTC."""
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
            l.get('description_ru', ''),  # F — Russian translation of opis
            _normalize_date_posted(l.get('date_posted', '')),  # G
            now_str,  # H
        ])
    return _post({'secret': SECRET, 'op': 'insert_at_top', 'rows': rows}, timeout)

def delete_rows(row_numbers, timeout=30):
    """Delete rows by 1-indexed row numbers."""
    if not row_numbers:
        return {'ok': True, 'deleted': 0}
    return _post({'secret': SECRET, 'op': 'delete_rows', 'rowNumbers': list(row_numbers)}, timeout)

def sort_by_date_posted(timeout=30):
    """Re-sort sheet by column G (date posted) descending. Call after manual edits if needed."""
    return _post({'secret': SECRET, 'op': 'sort_by_column', 'column': 7, 'ascending': False, 'headerRows': 1}, timeout)

def update_cells(cells, timeout=30):
    """Generic cell updates. cells = [{row, col, value}, ...]"""
    return _post({'secret': SECRET, 'op': 'update_cells', 'cells': cells}, timeout)

# --- Лист «не прошли фильтр» (gid 1460013302) ---------------------------------
# Пишется НЕ через Apps Script webhook (он бьёт только в основной лист), а напрямую
# через Sheets API v4 под OAuth-токеном drive (см. reference_google_oauth).
# Колонки: A=Дата и время | B=Адрес | C=Район | D=Площадь | E=Цена |
#          F=Скоринг | G=Ссылка на объявление | H=Причина почему не прошло
SPREADSHEET_ID = '1jL7junHZDJCqG2EDp6olOPmPoOCConXR7xKz-QM-qAo'
REJECT_SHEET_NAME = 'не прошли фильтр'
GOOGLE_TOKEN = os.environ.get('GOOGLE_TOKEN_PATH', '/Users/dodo/.config/gcloud/dodo-drive-token.json')

def _sheets_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN)
    if not creds.valid:
        creds.refresh(Request())
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)

def _sheet_id_by_title(svc, title):
    """Вернуть числовой sheetId вкладки по её названию (или None)."""
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    for sh in meta.get('sheets', []):
        if sh['properties']['title'] == title:
            return sh['properties']['sheetId']
    return None


def append_reject_rows(rows):
    """Insert pre-built reject rows at TOP (row 2) of «не прошли фильтр» tab, rest shifts down.
    Each row is a list in column order A..H:
        [Дата и время, Адрес, Район, Площадь, Цена, Скоринг, Ссылка, Причина].
    Новые реджекты — наверху, по аналогии с основной таблицей (insert_at_top).
    Дедуп лежит на cycle.py (rejected-лоты в state не пересылаются)."""
    if not rows:
        return {'ok': True, 'inserted': 0}
    svc = _sheets_service()
    sheet_id = _sheet_id_by_title(svc, REJECT_SHEET_NAME)
    if sheet_id is None:
        # fallback: вкладка не найдена — допишем в конец, чтобы не потерять данные
        svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{REJECT_SHEET_NAME}'!A:H",
            valueInputOption='USER_ENTERED',
            insertDataOption='INSERT_ROWS',
            body={'values': rows},
        ).execute()
        return {'ok': True, 'inserted': len(rows), 'op': 'append_fallback'}
    n = len(rows)
    # 1) вставить n пустых строк сразу под заголовком (index 1, 0-based)
    svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={'requests': [{
            'insertDimension': {
                'range': {'sheetId': sheet_id, 'dimension': 'ROWS',
                          'startIndex': 1, 'endIndex': 1 + n},
                'inheritFromBefore': False,
            }
        }]},
    ).execute()
    # 2) записать значения в освободившиеся строки A2:H{1+n}
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{REJECT_SHEET_NAME}'!A2:H{1 + n}",
        valueInputOption='USER_ENTERED',
        body={'values': rows},
    ).execute()
    return {'ok': True, 'inserted': n, 'op': 'insert_at_top'}

# Backwards compat — old append_rows still works
def append_rows(rows, timeout=30):
    """DEPRECATED — use insert_lots() instead. Old-style append at bottom."""
    if not rows:
        return {'ok': True, 'inserted': 0}
    return _post({'secret': SECRET, 'op': 'append', 'rows': rows}, timeout)

if __name__ == '__main__':
    lots = json.load(sys.stdin)
    print(json.dumps(insert_lots(lots)))

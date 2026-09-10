#!/usr/bin/env python3
"""tg_bot.py — отправка через Telegram Bot API (токен в env TG_BOT_TOKEN).

С 10.09.2026 мониторы шлют от бота, а не от пользовательской сессии: общая
StringSession, запущенная параллельно с двух раннеров GitHub (разные IP),
была отозвана Telegram (AuthKeyDuplicatedError). Токен бота к IP не привязан.
CLI-аргументы и JSON-выход совпадают с telethon-скриптами, поэтому
driver.py и watchdog.py не меняются.
"""
import json, os, sys

import requests

API = 'https://api.telegram.org/bot{token}/{method}'


def _call(method, data=None, files=None, timeout=90):
    r = requests.post(API.format(token=os.environ['TG_BOT_TOKEN'], method=method),
                      data=data, files=files, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {'ok': False, 'description': f'HTTP {r.status_code}'}


def _with_chat(chat_id, fn):
    """Скрипты получают «голый» id как в telethon. Bot API ждёт -100<id> для
    супергрупп и id как есть для личных чатов — пробуем оба варианта."""
    s = str(chat_id).strip()
    last = None
    for cid in ([s] if s.startswith('-') else [f'-100{s}', s]):
        res = fn(cid)
        if res.get('ok'):
            return res
        last = res
        desc = (res.get('description') or '').lower()
        if 'chat not found' not in desc and 'peer_id_invalid' not in desc:
            break  # ошибка не про формат id: второй вариант не поможет
    return last or {'ok': False, 'description': 'no chat candidates'}


def send_text(chat_id, text, reply_to=None, html=False):
    data = {'text': (text or '')[:4096], 'disable_web_page_preview': 'true'}
    if html:
        data['parse_mode'] = 'HTML'
    if reply_to:
        data['reply_parameters'] = json.dumps(
            {'message_id': int(reply_to), 'allow_sending_without_reply': True})
    res = _with_chat(chat_id, lambda cid: _call('sendMessage', data={**data, 'chat_id': cid}))
    if not res.get('ok'):
        return {'ok': False, 'error': res.get('description')}
    return {'ok': True, 'message_id': res['result']['message_id']}


def send_album(chat_id, caption, photos):
    photos = [p for p in photos if os.path.exists(p)][:10]
    caption = (caption or '')[:1024]
    if not photos:
        return send_text(chat_id, caption)

    def go(cid):
        handles = []
        try:
            if len(photos) == 1:  # sendMediaGroup требует 2-10 элементов
                fh = open(photos[0], 'rb'); handles.append(fh)
                return _call('sendPhoto', data={'chat_id': cid, 'caption': caption},
                             files={'photo': fh}, timeout=180)
            media, files = [], {}
            for i, p in enumerate(photos):
                fh = open(p, 'rb'); handles.append(fh)
                files[f'photo{i}'] = fh
                item = {'type': 'photo', 'media': f'attach://photo{i}'}
                if i == 0 and caption:
                    item['caption'] = caption
                media.append(item)
            return _call('sendMediaGroup', data={'chat_id': cid, 'media': json.dumps(media)},
                         files=files, timeout=180)
        finally:
            for fh in handles:
                fh.close()

    res = _with_chat(chat_id, go)
    if not res.get('ok'):
        return {'ok': False, 'error': res.get('description')}
    msgs = res['result'] if isinstance(res['result'], list) else [res['result']]
    ids = [m['message_id'] for m in msgs]
    return {'ok': True, 'message_ids': ids, 'first_message_id': ids[0] if ids else None}


def edit_caption(chat_id, message_id, caption):
    res = _with_chat(chat_id, lambda cid: _call('editMessageCaption', data={
        'chat_id': cid, 'message_id': int(message_id), 'caption': (caption or '')[:1024]}))
    return {'ok': True} if res.get('ok') else {'ok': False, 'error': res.get('description')}


def _read(src):
    return sys.stdin.read() if src == '-' else open(src, encoding='utf-8').read()


def _emit(out):
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get('ok') else 1


def cli_send_album(argv):
    if len(argv) < 4:
        return _emit({'ok': False, 'error': 'usage: send_album.py CHAT_ID CAPTION_FILE PHOTO...'})
    return _emit(send_album(argv[1], _read(argv[2]), argv[3:13]))


def cli_send_text(argv):
    if len(argv) < 3:
        return _emit({'ok': False, 'error': 'usage: send_text.py CHAT_ID TEXT_FILE [--reply-to N] [--html]'})
    reply_to = argv[argv.index('--reply-to') + 1] if '--reply-to' in argv else None
    return _emit(send_text(argv[1], _read(argv[2]), reply_to=reply_to, html='--html' in argv))


def cli_edit_caption(argv):
    if len(argv) < 4:
        return _emit({'ok': False, 'error': 'usage: edit_caption.py CHAT_ID MSG_ID FILE'})
    return _emit(edit_caption(argv[1], argv[2], _read(argv[3])))

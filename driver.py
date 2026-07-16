#!/usr/bin/env python3
"""driver.py — серверный оркестратор цикла белградского монитора (замена CCD-агента).

Делает то, что на Mac делал Claude-агент по SKILL.md:
  1. cycle.py (process phase) → JSON
  2. passes → фото + caption в Telegram → --mark-sent --desc-ru
     Блок «📝 Кратко» переводится sr→ru: Gemini (free tier) → глоссарий-fallback.
  3. price_changes → «💶 Цена изменилась» (reply на исходный лот, если есть id)
  4. cycle.py --finalize (check_status + gen_map + surge deploy)

Запускается из GitHub Actions (см. .github/workflows/monitor.yml).
Env: BG_DATA, BG_PUBLIC, TG_SESSION, TG_API_ID, TG_API_HASH, GOOGLE_TOKEN_PATH,
     HALO_PROXY, GEMINI_API_KEY, BG_CHAT_ID (default 3951547035).
"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).parent
CHAT_ID = os.environ.get('BG_CHAT_ID', '3951547035')
# Служебные уведомления (health-алерты, weekly-отчёт) — лично Сергею в Daily wrap up,
# не в рабочий чат лотов (просьба 2026-07-16).
ALERT_CHAT_ID = os.environ.get('BG_ALERT_CHAT_ID', '5131688215')
MANY_PASSES = 15  # SKILL: при ≥15 лотов — одна сводка вместо N сообщений

# Глоссарий sr→ru (REFERENCE.md): термины, по которым принимается решение.
# Фолбэк на случай недоступности Gemini — остальной текст остаётся сербским.
GLOSSARY = [
    (r'\bprizemlje\b', 'первый этаж'),
    (r'\bvisoko prizemlje\b', 'высокий первый этаж'),
    (r'\bizlog\w*\b', 'витрина'),
    (r'\bba[šs]t\w*\b', 'терраса'),
    (r'\bnovogradnj\w*\b', 'новостройка'),
    (r'\bugostiteljstv\w*\b', 'общепит'),
    (r'\bugostiteljski objek\w*\b', 'помещение под общепит'),
    (r'\blokal\b', 'локал'),
    (r'\bposlovni prostor\b', 'коммерческое помещение'),
    (r'\bizdavanje\b', 'аренда'),
    (r'\bzakup\b', 'аренда'),
    (r'\bdepozit\b', 'депозит'),
    (r'\bmese[čc]no\b', 'в месяц'),
    (r'\bodli[čc]n\w* lokacij\w*\b', 'отличная локация'),
    (r'\bprometn\w+ ulic\w*\b', 'проходная улица'),
    (r'\bzaseban ulaz\b', 'отдельный вход'),
    (r'\bklim\w+\b', 'кондиционер'),
    (r'\bgrejanje\b', 'отопление'),
    (r'\brenoviran\w*\b', 'после ремонта'),
    (r'\bmokri [čc]vor\b', 'санузел'),
    (r'\btoalet\b', 'санузел'),
    (r'\bventilacij\w*\b', 'вентиляция'),
]


def glossary_ru(text):
    for pat, repl in GLOSSARY:
        text = re.sub(pat, repl, text, flags=re.IGNORECASE)
    return text


# Модели по убыванию качества; alias-имена (-latest) не протухают при ротации версий.
GEMINI_MODELS = ('gemini-flash-latest', 'gemini-flash-lite-latest')


def gemini_summary(desc_sr):
    """Сербское описание → русская выжимка 2–4 строки (Gemini, free tier).
    None при любой ошибке — вызывающий откатывается на глоссарий."""
    key = os.environ.get('GEMINI_API_KEY')
    if not key or not (desc_sr or '').strip():
        return None
    import requests
    prompt = ("Ты помогаешь пиццерийной сети искать помещения в аренду в Белграде. "
              "Переведи это сербское объявление о коммерческой недвижимости на русский и сожми "
              "до 2-4 коротких строк — только то, что важно арендатору-ресторатору: "
              "тип/состояние помещения, витрина/фасад/вход, коммуникации (вентиляция, мощность), "
              "условия (депозит, доступность). Смысловой перевод, не дословный: prizemlje=первый этаж, "
              "izlog=витрина, bašta=терраса. Только текст, без вступлений, списков и markdown.\n\n"
              + desc_sr[:2000])
    body = {'contents': [{'parts': [{'text': prompt}]}],
            # думающие flash-модели тратят токены на reasoning ДО ответа —
            # маленький лимит обрезает перевод на полуслове, нужен запас
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


def russian_summary(pass_rec):
    """Русское «Кратко» для caption/Sheets кол. F/карты: Gemini → глоссарий-fallback."""
    state_path = os.path.join(os.environ.get('BG_DATA', '.'), 'state.json')
    desc = ''
    try:
        with open(state_path, encoding='utf-8') as f:
            desc = (json.load(f).get('listings', {})
                    .get(pass_rec['listing_key'], {}).get('description') or '')
    except Exception:
        pass
    if not desc:  # в state нет — берём сербский snippet из caption
        m = re.search(r'📝 Кратко:\s*(.+)', pass_rec.get('caption') or '', re.DOTALL)
        desc = m.group(1).strip() if m else ''
    return gemini_summary(desc) or glossary_ru(desc)


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


def send_text(text, reply_to=None, chat_id=None):
    args = ['send_text.py', chat_id or CHAT_ID, '-']
    if reply_to:
        args += ['--reply-to', str(reply_to)]
    r = subprocess.run([sys.executable] + args, input=text, capture_output=True,
                       text=True, timeout=60, cwd=HERE)
    sys.stderr.write(r.stderr or '')
    return r.returncode == 0


def main():
    out = run_json(['cycle.py'], 1200)
    if out.get('concurrent_cycle_running'):
        print('concurrent cycle running — exit')
        return 0

    passes = out.get('passes') or []
    sent = failed = nophoto_sent = 0

    if len(passes) >= MANY_PASSES:
        lines = [f'🍕 {len(passes)} новых лотов (сводка):']
        for p in passes:
            lines.append(f"· {p.get('district','?')} · {p.get('area')} м² · "
                         f"{p.get('price')} €/мес · {p.get('url','')}")
        if send_text('\n'.join(lines)[:4000]):
            for p in passes:
                subprocess.run([sys.executable, 'cycle.py', '--mark-sent',
                                p['listing_key'], '0'],
                               capture_output=True, cwd=HERE, timeout=120)
            sent = len(passes)
    else:
        for p in passes:
            caption = p.get('caption') or ''
            summary = russian_summary(p)[:900]
            # переклеиваем блок «Кратко» на русский (cycle.py кладёт сербский snippet)
            if '📝 Кратко:' in caption:
                caption = caption[:caption.index('📝 Кратко:')] + f'📝 Кратко: {summary}\n'
            elif summary:
                caption += f'\n📝 Кратко: {summary}\n'
            caption = caption[:1024]
            photo = p.get('photo_path')
            photos = [photo] if photo and os.path.exists(photo) else []
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
                subprocess.run([sys.executable, 'cycle.py', '--mark-sent',
                                p['listing_key'], str(mid), '--desc-ru', summary],
                               capture_output=True, cwd=HERE, timeout=120)
                sent += 1
            except Exception as e:
                # не mark-sent → лот останется unalerted, доедет следующим циклом
                print(f"  SEND FAIL {p.get('listing_key')}: {e}", file=sys.stderr)
                failed += 1

    for c in out.get('price_changes') or []:
        msg = (f"💶 Цена изменилась · {c.get('district','?')}\n"
               f"📍 {c.get('address','')}\n"
               f"было {c['old']} € → стало {c['new']} €/мес\n"
               f"🔗 {c.get('url','')}")
        send_text(msg, reply_to=c.get('reply_to_message_id'))

    fin = run_json(['cycle.py', '--finalize'], 900)

    # --- Health-алерт: аномалии цикла → короткое ⚠️ в тот же чат ---
    # Раньше всё это молчало в JSON-логе Actions, который никто не читает.
    s = out.get('summary') or {}
    alerts = []
    if s.get('sources_down'):
        alerts.append('⛔ источники недоступны: ' + ', '.join(s['sources_down']))
    ff = [r for r in (out.get('rejects') or [])
          if str(r.get('reason', '')).startswith('fetch_fail')]
    if len(ff) >= 3:
        alerts.append(f'🌐 detail не скачался у {len(ff)} лотов (бан/сеть?)')
    if failed:
        alerts.append(f'✉️ не отправилось лотов: {failed}')
    if nophoto_sent:
        alerts.append(f'📷 постов без фото (фото в объявлении были): {nophoto_sent}')
    noscore = [p for p in passes if p.get('score') is None]
    if noscore:
        alerts.append(f'📊 скоринг не посчитался у {len(noscore)} отправленных')
    rs = out.get('reject_sheet') or {}
    if rs and not rs.get('ok'):
        alerts.append('📄 реджект-лист не записался')
    if fin.get('map_ok') is False:
        alerts.append('🗺 карта не сгенерировалась')
    if fin.get('map_surge_ok') is False:
        alerts.append('🌍 surge-деплой карты упал')
    if isinstance(fin.get('last_scan_stamp'), str):
        alerts.append('🕐 штамп Last scan не записался')
    canary_bad = [f'{k} — {v}' for k, v in
                  ((fin.get('canary') or {}).get('results') or {}).items()
                  if str(v).startswith('FAIL')]
    if canary_bad:
        alerts.append('🐤 канарейка парсеров:\n   ' + '\n   '.join(canary_bad))
    if alerts:
        send_text('⚠️ Монитор Белград — аномалии цикла:\n'
                  + '\n'.join('· ' + a for a in alerts),
                  chat_id=ALERT_CHAT_ID)

    # --- Weekly-самоотчёт (понедельник, раз в день; cycle.py сам решает) ---
    try:
        rep = run_json(['cycle.py', '--weekly-report'], 300)
        if rep.get('text'):
            send_text(rep['text'], chat_id=ALERT_CHAT_ID)
    except Exception as e:
        print(f'  weekly-report failed: {e}', file=sys.stderr)

    print(json.dumps({
        'sweep': s.get('sweep_raw'), 'new': s.get('new'),
        'passes': len(passes), 'sent': sent, 'send_failed': failed,
        'deferred': s.get('deferred'), 'nophoto_sent': nophoto_sent,
        'rejects': s.get('rejects'), 'duplicates': s.get('duplicates'),
        'price_changes': len(out.get('price_changes') or []),
        'sources_down': s.get('sources_down'),
        'map_ok': fin.get('map_ok'), 'surge_ok': fin.get('map_surge_ok'),
        'alerts_sent': len(alerts),
    }, ensure_ascii=False))
    # Упавшая отправка не должна валить workflow: state цел, лот доедет позже
    return 0


if __name__ == '__main__':
    sys.exit(main())

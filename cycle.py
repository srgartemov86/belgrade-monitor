#!/usr/bin/env python3
"""
cycle.py — single-script pizzeria-monitor cycle. Reduces ~30 agent tool-calls/cycle to 2.

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

# Точки Dodo для штрафа за каннибализацию (дублирует gen_map.DODO_LOCATIONS —
# намеренно, чтобы не тащить тяжёлый импорт gen_map в основной цикл).
DODO_POINTS = [(44.8456204, 20.4012657), (44.814985, 20.4564471)]


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

# BG_DATA (GitHub Actions: <workspace>/data) или дефолт Mac-пути
_DATA = os.environ.get('BG_DATA', '/Users/dodo/pizzeria-location-monitor')
STATE_PATH = os.path.join(_DATA, 'state.json')
RUNS_LOG = os.path.join(_DATA, 'runs.log')
PHOTO_DIR = Path(os.getcwd()) / '.pizzeria-photos'
CHAT_ID = 3951547035

AREA_MIN, AREA_MAX = 100, 220
PRICE_MIN, PRICE_MAX = 1300, 6000
PRICE_PER_M2_MIN = 20.0  # €/м²: дешевле обычно не подходит (плохая локация/качество)
CEILING_MIN = 3.0
TRG_LAT, TRG_LON = 44.8167, 20.4583
RADIUS_KM = 7.0
MAX_DETAILS_PER_CYCLE = 30
MAX_DETAILS_PER_SOURCE = 10
ZEMUN_SLUGS = ('zemun', 'altina', 'batajnica', 'galenika', 'kalvarija')

# Дальние пригороды (>7 км от центра) — в reject-дайджест не показываем, это не
# «почти подошло», а заведомо вне зоны. Сверяем по slug в url и по названию општины.
FAR_MUNI_SLUGS = ('obrenovac', 'lazarevac', 'mladenovac', 'sopot', 'barajevo',
                  'grocka', 'surcin', 'surčin')
# «Near-miss» коридор цены для дайджеста: лот по цене показываем, только если он
# рядом с целевым диапазоном 1300–6000. 8000€ или 900€ — это далёкий промах, шум.
PRICE_NEAR_LOW, PRICE_NEAR_HIGH = 1000, 7000

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0 Safari/537.36')

# Description-level reject patterns
OFFICE_PATS = [
    r'poslovno[- ]kancelarijski', r'\d+\s*kancelarij(?:a|e|ski)',
    r'direktorski\s+kabinet', r'\bsekreterijat',
    r'\d+\s*kabinet(?:a|e)', r'\bposlovna\s+zgrada\b',
    r'\bbusiness\s+center\b', r'\bbiznis\s+centar\b',
]
MALL_PATS = [
    r'tržni\s+centar', r'shopping\s+center', r'\bmall\b',
    r'\bU[šs]će\b', r'Stadion\s+TC', r'Delta\s+City', r'Airport\s+City',
]
COURT_PATS = [r'unutrašnje\s+dvorište', r'u\s+dvorištu\s+zgrade', r'bez\s+prolaza']
DARK_PATS = [r'\bpodrum\b', r'suteren\s+bez\s+prozora', r'bez\s+prirodnog\s+osvetljenja']

# Apartment (stan) detection. Many "salonac"/residential flats leak into the
# commercial sweep — owners list them hoping for office/agency tenants. They're
# useless for a pizzeria. Reject when residential-head-noun signals are present
# AND no genuine commercial/retail marker counters them. type/typology is almost
# always None for nekretnine/halooglasi, so this is description-driven.
RESIDENTIAL_PATS = [
    r'\bstan\b', r'\bstanu\b', r'\bstana\b', r'\bgarsonjer',
    r'\bspavać?[aeiou]', r'\bspavać?ih', r'\bmaster\s+spavać',
    r'\bdnevn[ai]\s+(?:soba|boravak)', r'\bidealan\s+dom\b', r'\bza\s+stanovanje\b',
    r'\b(?:jedno|dvo|tro|četvoro|cetvoro|jednoipo|dvoipo|troipo)soban\b',
]
# If any of these is present, treat the listing as genuinely commercial and keep
# it even if a residential word appears as flavour ("ovaj salonski stan ..." in a
# poslovni-prostor ad). Note: office words (kancelarija) are deliberately NOT here
# — pure offices are rejected by OFFICE_PATS anyway, and a "stan za kancelarije"
# is still a flat we don't want.
COMMERCIAL_KEEP = [
    r'poslovni\s+prostor', r'poslovni\s+objekat', r'\blokal[a-z]*\b',
    r'\bizlog', r'\bvitrin', r'ugostiteljsk', r'fast\s*food', r'\bpekar',
    r'restoran', r'\bkafan', r'\bkafić', r'prodavnic', r'\bradnj',
    r'\bapotek', r'kozmetičk', r'frizersk', r'\bsalon(?:a|e|i|u|om)?\b', r'delatnost',
    r'višenamensk', r'visenamensk', r'maloprodaj', r'veleprodaj',
    r'showroom', r'tržni\s+centar', r'\bteretan', r'igraonic', r'ordinacij',
]

MUNI_SLUGS = {
    'stari-grad': 'Stari Grad', 'vracar': 'Vračar', 'savski-venac': 'Savski Venac',
    'vozdovac': 'Voždovac', 'zvezdara': 'Zvezdara', 'palilula': 'Palilula',
    'novi-beograd': 'Novi Beograd', 'cukarica': 'Čukarica', 'rakovica': 'Rakovica',
    'zemun': 'Zemun', 'surcin': 'Surčin', 'mladenovac': 'Mladenovac',
    'sopot': 'Sopot', 'barajevo': 'Barajevo', 'obrenovac': 'Obrenovac',
    'lazarevac': 'Lazarevac', 'grocka': 'Grocka',
}

# Подрайон/топоним → општина. Используется парсерами без явной муниципальности (nekretnine).
# Ключи в нижнем регистре, latin без диакритики.
SUBDISTRICT_TO_MUNI = {
    'dorcol': 'Stari Grad', 'terazije': 'Stari Grad', 'zeleni venac': 'Stari Grad',
    'skadarlija': 'Stari Grad', 'kosancicev venac': 'Stari Grad',
    'hram': 'Vračar', 'vukov spomenik': 'Vračar', 'neimar': 'Vračar',
    'cubura': 'Vračar', 'krunski venac': 'Vračar', 'kalenic': 'Vračar',
    'prokop': 'Savski Venac', 'senjak': 'Savski Venac', 'dedinje': 'Savski Venac',
    'beograd na vodi': 'Savski Venac', 'mostarska petlja': 'Savski Venac',
    'banjica': 'Voždovac', 'sumice': 'Voždovac', 'veljko vlahovic': 'Voždovac',
    'stepa stepanovic': 'Voždovac', 'lekino brdo': 'Voždovac', 'autokomanda': 'Voždovac',
    'konjarnik': 'Zvezdara', 'medakovic': 'Zvezdara', 'mirijevo': 'Zvezdara',
    'crveni krst': 'Zvezdara', 'bogoslovija': 'Zvezdara',
    'deram': 'Zvezdara', 'deram pijaca': 'Zvezdara',
    'djeram': 'Zvezdara', 'djeram pijaca': 'Zvezdara',
    'cvetkova pijaca': 'Zvezdara',
    'karaburma': 'Palilula', 'krnjaca': 'Palilula', 'kotez': 'Palilula',
    'borca': 'Palilula', 'profesorska kolonija': 'Palilula', 'zira': 'Palilula',
    'bezanijska kosa': 'Novi Beograd', 'blok 70': 'Novi Beograd',
    'blok 45': 'Novi Beograd', 'stari merkator': 'Novi Beograd',
    'belville': 'Novi Beograd', 'ada medjica': 'Novi Beograd',
    'banovo brdo': 'Čukarica', 'zarkovo': 'Čukarica', 'cerak': 'Čukarica',
    'kanarevo brdo': 'Rakovica', 'petlovo brdo': 'Rakovica',
    'altina': 'Zemun', 'kalvarija': 'Zemun', 'galenika': 'Zemun', 'batajnica': 'Zemun',
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


DRY_RUN = False  # --dry-run: считаем всё, НИЧЕГО не пишем (state/лист)


def load_state():
    with open(STATE_PATH) as f:
        return json.load(f)


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


def fetch_html(url, timeout=20):
    """Returns (html, http_status). http_status=0 on error."""
    if 'halooglasi.com' in url:
        # halo_get: ретраи + прокси (HALO_PROXY) — с DC-IP GitHub напрямую 403
        r = curl_sweep.halo_get(url, timeout=timeout)
        return (r.text, r.status_code) if r is not None else ('', 0)
    if 'nekretnine.rs' in url:
        # DataDome режет обычный curl по TLS (403-заглушка без __NEXT_DATA__);
        # nek_get: curl_cffi напрямую → при неудаче residential-прокси (2026-07-16)
        r = curl_sweep.nek_get(url, timeout=timeout)
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


def parse_4zida(html):
    out = {}
    m = re.search(r'<title>([^<]+)</title>', html)
    out['title'] = m.group(1).strip() if m else ''

    lat = re.search(r'"latitude":\s*([\d.]+)', html)
    lon = re.search(r'"longitude":\s*([\d.]+)', html)
    if lat and lon:
        out['lat'] = float(lat.group(1))
        out['lon'] = float(lon.group(1))

    rf = re.search(r'"refreshedAt":"([^"]+)"', html)
    if rf: out['refreshed_at'] = rf.group(1)
    pub = re.search(r'"publishedAt":"([^"]+)"', html)
    if pub: out['published_at'] = pub.group(1)

    # Longest description (skip platform boilerplate and agency profile)
    longest = ''
    for d in re.findall(r'"description":"((?:[^"\\]|\\.)*)"', html):
        if 'platforma' in d.lower(): continue
        if '4zida.rs!' in d: continue
        s = decode_escapes(d)
        if len(s) > len(longest):
            longest = s
    out['description'] = longest

    # Street: last streetAddress that's not the agency profile (Matije Korvina / Subotica)
    addrs = re.findall(r'"streetAddress":"([^"]+)"', html)
    street = ''
    for a in addrs:
        if 'Matije Korvina' in a: continue
        if 'Subotica' in a: continue
        street = a  # last non-agency wins
    out['street'] = street.strip()

    og = (re.search(r'<meta property="og:image" content="([^"]+)"', html)
          or re.search(r'og:image"\s+content="([^"]+)"', html))
    out['photo_url'] = og.group(1) if og else None

    haystack = (longest + ' ' + out['title']).lower()
    if re.search(r'\b(na\s+prizemlj|prizemlju|prizemlje)\b', haystack):
        out['floor'] = 'prizemlje'
    else:
        m = re.search(r'na\s+(\d+)\.?\s*spratu', haystack)
        if m:
            out['floor'] = f"{m.group(1)}. sprat"

    cm = re.search(r'(?:visina(?:\s+plafona)?|plafon)[^a-zA-Z0-9]{0,20}(\d[\.,]?\d*)\s*m', haystack)
    if cm:
        try: out['ceiling'] = float(cm.group(1).replace(',', '.'))
        except Exception: pass

    _ldjson_enrich(html, out)
    return out


def _ldjson_enrich(html, out):
    """API-first дозаполнение из schema.org ld+json (4zida кладёт Place/Offer/
    CommercialProperty). Стабильнее регулярок по вёрстке: geo, цена, фото.
    Заполняет только отсутствующие поля — HTML-парсер остаётся приоритетным."""
    try:
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                            html, re.DOTALL)
        photos = []
        for b in blocks:
            try:
                d = json.loads(b)
            except Exception:
                continue
            items = d.get('@graph', [d]) if isinstance(d, dict) else []
            for it in items:
                if not isinstance(it, dict):
                    continue
                t = it.get('@type') or ''
                if t == 'Place' and 'lat' not in out:
                    geo = it.get('geo') or {}
                    la, lo = geo.get('latitude'), geo.get('longitude')
                    if isinstance(la, (int, float)) and isinstance(lo, (int, float)):
                        out['lat'], out['lon'] = float(la), float(lo)
                if t in ('CommercialProperty', 'Apartment', 'House', 'RealEstateListing'):
                    for im in (it.get('image') or []):
                        u = im.get('contentUrl') or im.get('url') if isinstance(im, dict) else im
                        if isinstance(u, str) and u.startswith('http') and u not in photos:
                            photos.append(u)
        if photos and not out.get('photo_url'):
            out['photo_url'] = photos[0]
        if photos:
            out['photos_ld'] = photos[:10]
    except Exception:
        pass  # обогащение best-effort, парсер не валим


def parse_nekretnine(html):
    """Detail parser for nekretnine.rs Next.js pages (migrated ~2026-05).
    All data lives in __NEXT_DATA__ → pageProps.detailData.realEstate.properties[0].
    Floor isn't a structured field (properties.floors = building height, not unit
    floor) — extracted from the Serbian description text."""
    out = {'title': '', 'description': '', 'street': '', 'subdistrict': ''}
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                  html, re.DOTALL)
    if not m:
        tm = re.search(r'<title>([^<]+)</title>', html)
        if tm:
            out['title'] = tm.group(1).strip()
        return out
    try:
        re0 = json.loads(m.group(1))['props']['pageProps']['detailData']['realEstate']
    except Exception:
        return out

    out['title'] = re0.get('title') or ''
    props = re0.get('properties') or [{}]
    p0 = props[0] if props else {}
    out['description'] = p0.get('description') or ''

    loc = p0.get('location') or {}
    lat, lon = loc.get('latitude'), loc.get('longitude')
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        out['lat'], out['lon'] = float(lat), float(lon)
    street = (loc.get('address') or '').strip()
    num = loc.get('streetNumber')
    if street and num:
        street = f'{street} {num}'
    out['street'] = street
    out['subdistrict'] = (loc.get('microzone') or loc.get('macrozone') or '').strip()

    photo = (p0.get('photo') or {}).get('urls') or {}
    if not photo:
        ph = (p0.get('multimedia') or {}).get('photos') or []
        photo = (ph[0].get('urls') or {}) if ph else {}
    out['photo_url'] = photo.get('large') or photo.get('medium') or photo.get('small')

    # Floor from description (Serbian). Prizemlje first (common lokal case); avoid
    # matching "N spratova" (genitive plural = total building floors).
    d = out['description'].lower()
    floor = ''
    if re.search(r'visoko\s+prizemlj', d):
        floor = 'visoko prizemlje'
    elif re.search(r'\bprizemlj', d):
        floor = 'prizemlje'
    else:
        fm = re.search(r'na\s+(\d+)\.?\s*sprat', d) or re.search(r'\b(\d+)\.\s*sprat\b', d)
        if fm:
            floor = f'{fm.group(1)}. sprat'
        elif re.search(r'\bsuteren\b', d):
            floor = 'suteren'
        elif re.search(r'\bpodrum\b', d):
            floor = 'podrum'
    if floor:
        out['floor'] = floor

    cm = re.search(r'visin[ae][^0-9]{0,30}(\d[\.,]?\d*)\s*m', d)
    if cm:
        try:
            out['ceiling'] = float(cm.group(1).replace(',', '.'))
        except Exception:
            pass

    return out


def parse_halooglasi(html):
    out = {}
    m = re.search(r'<title>([^<]+)</title>', html)
    out['title'] = m.group(1).strip() if m else ''

    geo_m = re.search(r'"GeoLocationRPT":"([^"]+)"', html)
    if geo_m:
        parts = geo_m.group(1).split(',')
        if len(parts) >= 2:
            try:
                out['lat'] = float(parts[0])
                out['lon'] = float(parts[1])
            except Exception: pass

    desc_m = re.search(r'"TextHtml":"((?:[^"\\]|\\.)+?)"', html)
    if desc_m:
        d = decode_escapes(desc_m.group(1))
        d = re.sub(r'<[^>]+>', ' ', d)
        out['description'] = re.sub(r'\s+', ' ', d).strip()
    else:
        out['description'] = ''

    haystack = out['description'].lower()
    if re.search(r'\bprizemlj', haystack):
        out['floor'] = 'prizemlje'

    cm = re.search(r'visina[^0-9]{0,30}(\d[\.,]?\d*)\s*m', haystack)
    if cm:
        try: out['ceiling'] = float(cm.group(1).replace(',', '.'))
        except Exception: pass

    # Картинки живут на img.halooglasi.com (www. отдаёт 404 — баг до 2026-06-11)
    img_m = re.search(r'"ImageURLs":\s*\["([^"]+)"', html)
    if img_m:
        u = img_m.group(1).replace('\\/', '/')
        if u.startswith('/'):
            u = 'https://img.halooglasi.com' + u
        out['photo_url'] = u
    else:
        out['photo_url'] = None

    out['street'] = ''  # sweep already has it
    return out


def parse_cityexpert(html, item):
    out = {}
    desc_m = re.search(r'"description":"((?:[^"\\]|\\.)+?)"', html)
    out['description'] = decode_escapes(desc_m.group(1)) if desc_m else ''

    haystack = out['description'].lower()
    cm = re.search(r'visina[^0-9]{0,30}(\d[\.,]?\d*)\s*m', haystack)
    if cm:
        try: out['ceiling'] = float(cm.group(1).replace(',', '.'))
        except Exception: pass

    loc = item.get('location', '')
    if loc and ',' in loc:
        try:
            lat, lon = [float(x.strip()) for x in loc.split(',')]
            out['lat'] = lat; out['lon'] = lon
        except Exception: pass

    cover_m = re.search(r'"coverPhoto":"([^"]+)"', html)
    bucket_m = re.search(r'"bucket":"([^"]+)"', html)
    if cover_m and bucket_m and item.get('id'):
        out['photo_url'] = (f"https://img.cityexpert.rs/properties/720x/"
                            f"{bucket_m.group(1)}/{item['id']}/slike/{cover_m.group(1)}")
    else:
        og = re.search(r'og:image"\s*content="([^"]+)"', html)
        out['photo_url'] = og.group(1) if og else None

    out['floor'] = item.get('floor') or ''
    out['street'] = item.get('name') or ''
    out['title'] = ''
    return out


def extract_district(cand, detail):
    src = cand.get('source')

    if src == '4zida.rs':
        m = re.search(r'/izdavanje-poslovnih-prostora/([^/]+)/', cand.get('url', ''))
        if m:
            slug = m.group(1)
            if slug.endswith('-beograd'):
                slug = slug[:-len('-beograd')]
            for muni_key in sorted(MUNI_SLUGS, key=len, reverse=True):
                if slug == muni_key:
                    return MUNI_SLUGS[muni_key]
                if slug.endswith('-' + muni_key):
                    sub_slug = slug[:-(len(muni_key) + 1)]
                    if sub_slug:
                        sub = ' '.join(w.capitalize() for w in sub_slug.split('-'))
                        return f"{MUNI_SLUGS[muni_key]} ({sub})"
                    return MUNI_SLUGS[muni_key]

    if src == 'halooglasi.com':
        muni = cand.get('municipality') or ''
        sub = cand.get('subdistrict') or ''
        if sub and muni:
            return f"{muni} ({sub})"
        return muni or 'Unknown'

    if src == 'cityexpert.rs':
        return cand.get('municipality') or 'Unknown'

    if src == 'nekretnine.rs':
        sub_raw = (detail.get('subdistrict') or '').strip()
        # Strip "( centar )" suffix и подобный шум для отображения
        sub = re.sub(r'\s*\([^)]*\)\s*$', '', sub_raw).strip()
        if sub:
            norm = _norm_sub(sub)
            # Если sub сам — općina (Zvezdara, Voždovac etc.), верни её
            for slug, muni_name in MUNI_SLUGS.items():
                if norm == slug.replace('-', ' ') or _norm_sub(muni_name) == norm:
                    return muni_name
            muni = SUBDISTRICT_TO_MUNI.get(norm)
            if not muni:
                first = norm.split(' ')[0]
                muni = SUBDISTRICT_TO_MUNI.get(first)
            if muni:
                return f"{muni} ({sub})"
            return sub
        return 'Unknown'

    return 'Unknown'


def extract_photo_urls(html, source, max_n=10):
    """Все фото объявления (hotlink-able URLs) для галереи на карте.
    Проверено 2026-06-11: все 4 CDN отдают картинки без Referer."""
    urls = []
    try:
        if source == '4zida.rs':
            # rs:fit:1920:1080 = полноразмерная галерея лота; 128/256 — логотипы/тизеры
            cand = re.findall(r'https://resizer\d*\.4zida\.rs/[A-Za-z0-9_\-]+/rs:fit:1920:1080:0/[A-Za-z0-9_\-/=:.]+', html)
            urls = list(dict.fromkeys(cand))
        elif source == 'nekretnine.rs':
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                          html, re.DOTALL)
            if m:
                p0 = (json.loads(m.group(1))['props']['pageProps']['detailData']
                      ['realEstate'].get('properties') or [{}])[0]
                for p in (p0.get('multimedia') or {}).get('photos') or []:
                    pu = p.get('urls') or {}
                    u = pu.get('large') or pu.get('medium') or pu.get('small')
                    if u: urls.append(u)
        elif source == 'halooglasi.com':
            m = re.search(r'"ImageURLs":\s*(\[[^\]]*\])', html)
            if m:
                for u in json.loads(m.group(1)):
                    u = u.replace('\\/', '/')
                    if u.startswith('/'):
                        u = 'https://img.halooglasi.com' + u
                    urls.append(u)
        elif source == 'cityexpert.rs':
            cand = re.findall(r'(?:https:)?//img\.cityexpert\.rs/properties/720x/[^"\\\s@]+?\.jpe?g', html)
            urls = ['https:' + u if u.startswith('//') else u for u in dict.fromkeys(cand)]
    except Exception:
        pass
    return urls[:max_n]


def apply_filters(cand, detail, skip_price=False):
    """Returns (passed_bool, flags_list, reject_reason).

    skip_price=True пропускает area/price/€m² гейты и проверяет только структуру
    (этаж/zemun/назначение/офис/молл/двор/квартира). Нужно price-only пути, чтобы
    перед публикацией price-реджекта в дайджест отсеять структурный брак
    (1.+ sprat, стан, офис) — иначе в дайджест попадают квартиры на верхних этажах."""
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
    elif floor in ('prizemlje', 'pr'):
        pass
    elif 'visoko' in floor or floor in ('vpr', 'vpr.'):
        return False, flags, f'floor={floor}'
    elif re.search(r'\d+\.?\s*sprat', floor) or re.search(r'^[1-9]$', floor) or 'penthaus' in floor:
        return False, flags, f'floor={floor}'

    url = (cand.get('url') or '').lower()
    if any(s in url for s in ZEMUN_SLUGS):
        return False, flags, 'zemun'

    type_ = (cand.get('type') or '').lower()
    if 'kancelarij' in type_ or 'magacin' in type_ or 'skladiste' in type_:
        return False, flags, f'type={type_[:30]}'

    haystack = ((detail.get('description', '') or '') + ' ' +
                (detail.get('title', '') or '')).lower()

    for pat in OFFICE_PATS:
        if re.search(pat, haystack):
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

    ceil = detail.get('ceiling')
    if ceil is not None and ceil < CEILING_MIN:
        return False, flags, f'ceiling={ceil}m'
    if ceil is None:
        flags.append('uncertain_ceiling')

    if 'lat' in detail and 'lon' in detail:
        d = haversine_km(detail['lat'], detail['lon'], TRG_LAT, TRG_LON)
        if d > RADIUS_KM:
            return False, flags, f'distance={d:.1f}km'
    else:
        flags.append('uncertain_distance')

    return True, flags, ''


def download_photo(url, listing_key):
    PHOTO_DIR.mkdir(exist_ok=True)
    ext_m = re.search(r'\.(jpe?g|webp|avif|png)(?:[?#]|$)', url, re.IGNORECASE)
    ext = (ext_m.group(1).lower() if ext_m else 'jpg').replace('jpeg', 'jpg')
    raw_path = PHOTO_DIR / f"{listing_key}.{ext}"
    # 2 попытки: единичный сбой curl (таймаут/сброс) раньше ронял пост в текст,
    # хотя картинка живая (баг «нет фото», 2026-07-14).
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
    """Прямая ссылка на панораму Яндекса в точке (lon,lat — Яндекс ждёт долготу первой).
    Если панорамы в точке нет — Яндекс открывает карту в этой точке."""
    return (f"https://yandex.ru/maps/?ll={lon}%2C{lat}&z=18"
            f"&panorama%5Bpoint%5D={lon}%2C{lat}"
            f"&panorama%5Bdirection%5D=0%2C0&panorama%5Bspan%5D=130%2C70")


def yandex_search_url(addr):
    return f"https://yandex.ru/maps/?text={quote(f'{addr}, Beograd, Serbia')}"


def reason_ru(reason):
    """Код причины реджекта → (русский текст, цвет бейджа)."""
    r = reason or ''
    if r.startswith('floor='):
        return (f'этаж не первый ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('distance='):
        return (f'далеко от центра ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('ceiling='):
        return (f'низкий потолок ({r.split("=", 1)[1]})', '#d97706')
    if r.startswith('price_per_m2='):
        return (f'дёшево за м² ({r.split("=", 1)[1]})', '#d97706')
    if r.startswith('area='):
        return (f'площадь вне 100–220 ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('price='):
        return (f'цена вне 1300–6000 ({r.split("=", 1)[1]})', '#dc2626')
    if r.startswith('office:'):
        return ('офис / бизнес-центр', '#7c3aed')
    if r == 'mall':
        return ('в торговом центре', '#7c3aed')
    if r == 'courtyard':
        return ('вход со двора', '#7c3aed')
    if r.startswith('dark:'):
        return ('без окон / подвал', '#374151')
    if r == 'apartment':
        return ('жильё, не локал', '#7c3aed')
    if r == 'zemun':
        return ('Земун (вне зоны)', '#dc2626')
    if r.startswith('type='):
        return ('склад / кабинет', '#7c3aed')
    if r.startswith('fetch_fail'):
        return ('страница не открылась', '#9ca3af')
    if r == 'district_blacklist':
        return ('район в чёрном списке', '#dc2626')
    return (r or 'не подошёл', '#6b7280')


def _esc_html(s):
    return (str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _reject_map_links(r):
    """(google_url, yandex_url) для реджекта по координатам или адресу."""
    lat, lon = r.get('lat'), r.get('lon')
    if lat and lon:
        return (f"https://www.google.com/maps/?q={lat},{lon}", yandex_pano_url(lat, lon))
    addr = r.get('address') or ''
    q = quote(f"{addr}, Beograd, Serbia")
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
        now_bg = datetime.now(ZoneInfo('Europe/Belgrade')).strftime('%Y-%m-%d %H:%M')
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
    src = cand.get('source', '?').replace('.rs', '').replace('.com', '')
    area = cand.get('area')
    price = cand.get('price')
    floor = detail.get('floor') or 'prizemlje'
    addr = (detail.get('street') or cand.get('street') or '').strip() or 'без точного адреса'

    yandex = None
    if 'lat' in detail and 'lon' in detail:
        maps = f"https://www.google.com/maps/?q={detail['lat']},{detail['lon']}"
        yandex = yandex_pano_url(detail['lat'], detail['lon'])
    else:
        q = quote(f"{addr}, Beograd, Serbia")
        maps = f"https://www.google.com/maps/search/?api=1&query={q}"
        if addr and addr != 'без точного адреса':
            yandex = yandex_search_url(addr)

    plus = []
    if detail.get('ceiling'):
        plus.append(f'потолок {detail["ceiling"]}м')

    cap = f"🍕 Локал · {district_str}\n"
    cap += f"📍 {addr}\n"
    cap += f"🗺 {maps}\n"
    if yandex:
        cap += f"🌐 Яндекс-панорама: {yandex}\n"
    cap += f"📐 {area} м² · 💶 {price} €/мес · 🏢 {floor}\n"
    if plus:
        cap += f"✅ {' · '.join(plus)}\n"
    cap += f"🔗 {src}: {cand['url']}\n"
    if flags:
        cap += ' '.join(f'⚠️ {f}' for f in flags) + '\n'

    desc = (detail.get('description') or '').strip()
    if desc:
        snippet = desc[:300].rstrip()
        cap += f"\n📝 Кратко: {snippet}\n"

    return cap[:1024]


def parse_detail(html, cand):
    src = cand.get('source')
    if src == '4zida.rs': return parse_4zida(html)
    if src == 'nekretnine.rs': return parse_nekretnine(html)
    if src == 'halooglasi.com': return parse_halooglasi(html)
    if src == 'cityexpert.rs': return parse_cityexpert(html, cand)
    return {}


def _price_similar(p1, p2):
    """Цены «про одно и то же»: разница ≤ max(100€, 3%)."""
    return abs(p1 - p2) <= max(100, 0.03 * max(p1, p2))


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
FEEDBACK_LIKE = ('в работе', 'ок')
FEEDBACK_DISLIKE = ('не подходит', 'отказ')


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

        for name, fn, pages in [
            ('4zida', curl_sweep.sweep_4zida, 20),
            ('nekretnine', curl_sweep.sweep_nekretnine, 6),
            ('cityexpert', curl_sweep.sweep_cityexpert, 3),
            ('halooglasi', curl_sweep.sweep_halooglasi, 16),
        ]:
            try:
                all_l.extend(fn(pages=pages))
            except Exception as e:
                sources_down.append(name)
                sweep_errors.append(f'{name}:{type(e).__name__}')

        # Prefilter — same logic as curl_sweep.main()
        filtered = []
        for l in all_l:
            if not l.get('area') or not l.get('price'): continue
            if l['area'] < AREA_MIN or l['area'] > AREA_MAX: continue
            if l['price'] < PRICE_MIN or l['price'] > PRICE_MAX: continue
            if l['price'] / l['area'] < PRICE_PER_M2_MIN: continue
            url = (l.get('url') or '').lower()
            if any(slug in url for slug in ZEMUN_SLUGS): continue
            type_ = (l.get('type', '') or '').lower()
            if any(b in type_ for b in ['kancelarij', 'magacin', 'skladiste',
                                        'poslovna-zgrada', 'hala', 'garaž']): continue
            if l.get('source') == 'cityexpert.rs':
                if (l.get('floor') or '').upper() != 'PR': continue
                if l.get('is_salonac'): continue
            if l.get('source') == 'halooglasi.com':
                ptype = (l.get('type') or '').lower()
                if ptype and ptype not in ('lokal', 'ugostiteljski objekat',
                                           'poslovni prostor', 'salon', 'restoran', 'kafana'):
                    continue
                floor = (l.get('floor') or '').upper().strip().rstrip('.')
                if floor and floor not in ('PR', 'PRIZEMLJE'): continue
                mun = (l.get('municipality') or '').lower()
                if any(x in mun for x in ('grocka', 'lazarevac', 'obrenovac',
                                          'mladenovac', 'sopot', 'barajevo',
                                          'surčin', 'surcin')):
                    continue
            filtered.append(l)

        # Изменения цены на активных лотах видны только в sweep-выдаче (по ключу
        # они «известные» и в detail-цикл не попадают) — ловим их здесь.
        price_changes = detect_price_changes(s, all_l)

        known = curl_sweep.known_ids()
        new_lots = [l for l in filtered if l.get('id') and l['id'] not in known]

        pending = list(s.get('pending_candidates', []))
        candidates = new_lots + pending

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
        s['pending_candidates'] = leftover

        passes, rejects, duplicates, deferred = [], [], [], []

        for cand in capped:
            src = cand.get('source', '?')
            cid = cand.get('id', '')
            src_prefix = src.split('.')[0]
            key = f"{src_prefix}_{cid}"

            existing = s['listings'].get(key)
            if existing and (existing.get('in_sheet') or existing.get('rejected')
                             or existing.get('removed_from_sheet')):
                existing['last_seen_at'] = now_iso()
                continue
            # else: new OR zombie (no terminal status) — fall through to detail-fetch

            html, status = fetch_html(cand['url'])
            if not html and src == 'nekretnine.rs' and cand.get('lat') is not None:
                # DataDome заглушил detail, но JSON-API свипа уже дал гео/фото/цену/
                # площадь — обрабатываем на них с флагами uncertain (этаж/потолок
                # неизвестны). Раньше такой лот умирал как fetch_fail, включая
                # ПРОХОДНЫЕ по цене (кейс 22.07: 4 лота по 20.7–25.4 €/м²).
                html = ''
                detail = {}
            elif not html:
                # 1-й провал — pending-ретрай следующим циклом (сеть/бан часто
                # временные). 2-й — реджект С ЗАПИСЬЮ в state: раньше fetch_fail
                # не записывался → нет дедупа → тот же лот дублировался в листе
                # каждым циклом (кейс 21-22.07: 4 лота × 2 пачки строк).
                if int(cand.get('_fetch_retry') or 0) == 0:
                    cand['_fetch_retry'] = 1
                    s['pending_candidates'].append(cand)
                    deferred.append({'key': key, 'missing': ['detail(fetch)'],
                                     'url': cand['url']})
                    continue
                s['listings'][key] = {
                    'source': src, 'id': cid, 'url': cand['url'],
                    'area_m2': cand.get('area'), 'price_eur': cand.get('price'),
                    'address': (cand.get('street') or '').strip(), 'district': '',
                    'first_seen_at': now_iso(), 'last_seen_at': now_iso(),
                    'in_sheet': False, 'alerted': False, 'rejected': True,
                    'flags': [f'fetch_fail:http={status}'],
                }
                rejects.append({
                    'key': key, 'reason': f'fetch_fail:http={status}',
                    'district': '', 'address': (cand.get('street') or '').strip(),
                    'area': cand.get('area'), 'price': cand.get('price'),
                    'url': cand['url'], 'source': src, 'lat': None, 'lon': None,
                })
                continue
            else:
                detail = parse_detail(html, cand)
            # nekretnine detail часто под DataDome (403 → пустой detail). Координаты,
            # улицу, район и фото доливаем из JSON-API свипа (cand) — там всё есть,
            # тогда фильтры/скоринг/карта работают как при живом detail (2026-07-16).
            if 'lat' not in detail and isinstance(cand.get('lat'), (int, float)) \
                    and isinstance(cand.get('lon'), (int, float)):
                detail['lat'] = cand['lat']
                detail['lon'] = cand['lon']
            if not detail.get('street') and cand.get('street'):
                detail['street'] = cand['street']
            if not detail.get('subdistrict') and cand.get('subdistrict'):
                detail['subdistrict'] = cand['subdistrict']
            if not detail.get('photo_url') and cand.get('photo_url'):
                detail['photo_url'] = cand['photo_url']
            passed, flags, reason = apply_filters(cand, detail)
            district_str = extract_district(cand, detail)
            if (not district_str or district_str == 'Unknown') and cand.get('macrozone'):
                district_str = cand['macrozone']

            rec = {
                'source': src, 'id': cid, 'url': cand['url'],
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
                rec['geo_source'] = 'detail'
            if detail.get('refreshed_at'): rec['refreshed_at'] = detail['refreshed_at']
            if detail.get('published_at'): rec['published_at'] = detail['published_at']
            if detail.get('photo_url'): rec['photo_url'] = detail['photo_url']
            purls = extract_photo_urls(html, src)
            if purls: rec['photo_urls'] = purls
            # nekretnine: фото из API свипа (detail-html под DataDome — extract даёт 0).
            if not rec.get('photo_urls') and cand.get('photo_urls'):
                rec['photo_urls'] = cand['photo_urls']
            if not rec.get('photo_url') and cand.get('photo_url'):
                rec['photo_url'] = cand['photo_url']
            # 4zida: фото из ld+json (стабильнее вёрстки).
            if not rec.get('photo_urls') and detail.get('photos_ld'):
                rec['photo_urls'] = detail['photos_ld']

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

            photo_path = None
            # Пробуем главное фото, при неудаче — следующие из объявления (до 6).
            # В лоте лежит 6-10 годных фото; раньше брали одно в одну попытку, и
            # любой сбой скачивания ронял пост в текст (баг «нет фото», 2026-07-14).
            photo_candidates = []
            if detail.get('photo_url'):
                photo_candidates.append(detail['photo_url'])
            for u in (rec.get('photo_urls') or []):
                if u and u not in photo_candidates:
                    photo_candidates.append(u)
            for u in photo_candidates[:6]:
                photo_path = download_photo(u, key)
                if photo_path:
                    break

            # --- Инварианты перед отправкой: запрет тихой деградации ---
            # Пасс без фото/координат/скоринга при ПЕРВОЙ попытке не уходит урезанным:
            # откладываем на следующий цикл (сеть/Overpass часто оживают за 2 часа).
            # Со второй попытки отправляем, но с явной пометкой в caption.
            missing = []
            if photo_candidates and not photo_path:
                missing.append('фото')
            if rec.get('geo_lat') is None:
                missing.append('координаты')
            elif sc is None:
                missing.append('скоринг')
            retry_n = int(cand.get('_send_retry') or 0)
            if missing and retry_n == 0:
                cand['_send_retry'] = 1
                s['pending_candidates'].append(cand)
                deferred.append({'key': key, 'missing': missing, 'url': cand['url']})
                continue
            invariant_note = f"⚠️ недоступно: {', '.join(missing)}\n" if missing else ''

            caption = build_caption(cand, detail, district_str, flags)
            if invariant_note:
                caption += invariant_note
            if sc:
                sl = scoring.score_line(sc)
                if sl:
                    # Скоринг ставим ПЕРЕД блоком «📝 Кратко», а не после. driver.py
                    # переклеивает «Кратко» на русский срезом caption[:index('📝 Кратко:')]
                    # — если скоринг после Кратко, он теряется (баг «аналогично Клужу»,
                    # 2026-07-14). Перед Кратко он остаётся в «голове», срез его сохраняет.
                    if '📝 Кратко:' in caption:
                        idx = caption.index('📝 Кратко:')
                        caption = caption[:idx] + sl + '\n\n' + caption[idx:]
                    else:
                        caption = caption + "\n\n" + sl
                    caption = caption[:1024]

            passes.append({
                'listing_key': key,
                'photo_path': str(photo_path) if photo_path else None,
                'caption': caption,
                'chat_id': CHAT_ID,
                'district': district_str,
                'address': rec['address'],
                'area': cand.get('area'),
                'price': cand.get('price'),
                'url': cand['url'],
                'flags': flags,
                'score': (sc or {}).get('score'),
                'had_photos': bool(photo_candidates),
            })

        # --- Реджекты по цене / цене за м² ---
        # Метраж в норме (100–220), но цена вне 1300–6000 или €/м² ниже минимума.
        # В дайджест идут только настоящие price-near-miss: валидный prizemlje-локал,
        # не прошедший ТОЛЬКО по цене. Поэтому перед публикацией фетчим detail и гоняем
        # структурные фильтры (skip_price=True): этаж, назначение, офис/молл/двор/квартира.
        # Структурный брак (квартира на 1.+ spratu, стан, офис) → молча в state как
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
            if any(slug in url for slug in ZEMUN_SLUGS):
                continue
            if (l.get('municipality') or '').strip().lower() == 'zemun':
                continue
            type_ = (l.get('type', '') or '').lower()
            if any(b in type_ for b in ['kancelarij', 'magacin', 'skladiste',
                                        'poslovna-zgrada', 'hala', 'garaž']):
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
                # Земун / дальний пригород мог проявиться только в районе из detail.
                dl = district.lower()
                if 'zemun' in dl or 'земун' in dl or \
                   any(m in dl for m in FAR_MUNI_SLUGS):
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
        # Overpass в центре Белграда может идти ~190 с/лот (после подъёма timeout).
        # Ограничиваем ещё и по стенным часам, чтобы реджект-скоринг не растянул цикл
        # (у GitHub Actions job-таймаут — не дать одному циклу его выесть).
        SCORE_WALL_BUDGET = 300  # сек
        _scored = 0
        _score_t0 = time.time()
        for r in sorted(rejects,
                        key=lambda x: ((x.get('price') or 0) / (x.get('area') or 1)),
                        reverse=True):
            if _scored >= SCORE_BUDGET or (time.time() - _score_t0) > SCORE_WALL_BUDGET:
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
                p['caption'] = ('⭐ Район, где уже есть лоты «В работе»\n'
                                + p['caption'])[:1024]
        passes.sort(key=lambda p: 0 if p.get('feedback_like') else 1)

        s['last_cycle_at'] = now_iso()
        # Статистика цикла для штампа Last scan в реджект-листе (finalize её читает).
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
            print(f'  sources_down={sources_down}', file=sys.stderr)

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
    desc_ru — русское «Кратко» от агента; идёт в Sheets кол. F (карта показывает её).
    Без него падаем на сырое сербское описание (хуже — карта будет на сербском)."""
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

        # Скоринг → кол. M (заполняется отдельно: webhook insert_at_top пишет только A–H).
        # Находим строку по URL (insert_at_top кладёт лот наверх), пишем M через Sheets API.
        sc = rec.get('score')
        if sc is not None and rec.get('url'):
            try:
                svc = _sheets_service()
                urls = svc.spreadsheets().values().get(
                    spreadsheetId=SPREADSHEET_ID, range='E2:E1000'
                ).execute().get('values', [])
                target = rec['url'].strip()
                for i, row in enumerate(urls, start=2):
                    if row and row[0].strip() == target:
                        update_cells([{'row': i, 'col': 13, 'value': sc}])
                        break
            except Exception:
                pass  # скоринг в M не критичен, не валим mark-sent

        s.setdefault('sent_messages', {})[str(message_id)] = {
            'listing_key': listing_key, 'sent_at': rec['sent_at'],
        }
        save_state(s)
        print(json.dumps({'ok': True, 'sheets': sheet_resp,
                          'listing_key': listing_key,
                          'in_sheet': True}))
    return 0


def run_canary(s):
    """Канарейка: раз в сутки по 1 живому лоту с каждого источника — парсер вернул
    цену/координаты/фото? Сломался парсер или пришёл бан — видно в тот же день через
    health-алерт, а не «через месяц заметили Unknown»."""
    res = {}
    # API-каналы: nekretnine и cityexpert отдают всё нужное прямо в свипе.
    try:
        nk = curl_sweep.sweep_nekretnine(1)
        c = nk[0] if nk else {}
        ok = bool(c.get('price') and c.get('area') and c.get('lat') and c.get('photo_url'))
        res['nekretnine_api'] = 'ok' if ok else (
            f"FAIL: price={bool(c.get('price'))} lat={bool(c.get('lat'))} "
            f"photo={bool(c.get('photo_url'))} (пустой ответ)" if not nk else
            f"FAIL: price={bool(c.get('price'))} lat={bool(c.get('lat'))} photo={bool(c.get('photo_url'))}")
    except Exception as e:
        res['nekretnine_api'] = f'FAIL: {e}'
    try:
        ce = curl_sweep.sweep_cityexpert(1)
        ok = bool(ce and ce[0].get('price') and ce[0].get('area'))
        res['cityexpert_api'] = 'ok' if ok else 'FAIL: пустой ответ / без цены'
    except Exception as e:
        res['cityexpert_api'] = f'FAIL: {e}'
    # Detail-каналы: свежие живые in_sheet лоты 4zida и halooglasi.
    # Пробуем до 3 лотов: свежеудалённый лот отдаёт 200 с редиректом на категорию
    # (парсер честно возвращает пусто) — один мёртвый подопытный не должен давать
    # ложную тревогу (кейс halo_detail 2026-07-17). FAIL — только если пустые ВСЕ.
    for prefix, src_name, label in (('4zida_', '4zida.rs', '4zida_detail'),
                                    ('halooglasi_', 'halooglasi.com', 'halo_detail')):
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
            res[label] = 'skip: нет живого лота'
            continue
        last_reason = 'нет данных'
        for rec in recs:
            try:
                html, code = fetch_html(rec['url'])
                if not html or code != 200:
                    last_reason = f'http={code}'
                    continue
                d = parse_detail(html, {'source': src_name, 'url': rec['url']})
                if d.get('photo_url') and (d.get('lat') or d.get('street')
                                           or d.get('description')):
                    res[label] = 'ok'
                    break
                last_reason = 'detail пустой (лот удалён/редизайн?)'
            except Exception as e:
                last_reason = str(e)
        else:
            res[label] = f'FAIL x{len(recs)}: {last_reason}'
    return res


def build_weekly_report(s, market_line=None):
    """Текст weekly-самоотчёта: воронка за 7 дней из state. None если данных нет."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    seen = rejected = insheet = alerted = nophoto = noscore = 0
    reasons = {}
    for v in s.get('listings', {}).values():
        if not isinstance(v, dict):
            continue
        try:
            fs = datetime.fromisoformat((v.get('first_seen_at') or '').replace('Z', '+00:00'))
        except Exception:
            continue
        if fs < cutoff:
            continue
        seen += 1
        if v.get('in_sheet'):
            insheet += 1
            if v.get('alerted'):
                alerted += 1
            if v.get('score') is None and v.get('geo_lat') is not None:
                noscore += 1
            if not v.get('photo_urls') and not v.get('photo_url'):
                nophoto += 1
        elif v.get('rejected'):
            rejected += 1
            r = str((v.get('flags') or ['?'])[0])
            if 'per_m2' in r: b = '€/м² < 20'
            elif 'price_far' in r: b = 'цена далеко вне диапазона'
            elif r.startswith('price'): b = 'цена вне 1300–6000'
            elif 'office' in r or 'kancelarij' in r or 'poslovn' in r: b = 'офис/БЦ'
            elif 'floor' in r or 'sprat' in r: b = 'этаж'
            elif 'far_muni' in r or 'zemun' in r: b = 'вне зоны'
            elif 'duplicate' in r: b = 'дубли'
            elif 'fetch_fail' in r: b = 'detail не скачался'
            else: b = 'прочее'
            reasons[b] = reasons.get(b, 0) + 1
    if seen == 0:
        return None
    lines = [f'📊 Монитор Белград — неделя:',
             f'· просмотрено новых: {seen}',
             f'· в таблицу/Telegram: {insheet} ({100*insheet//max(seen,1)}%)',
             f'· отсеяно: {rejected}']
    for b, n in sorted(reasons.items(), key=lambda x: -x[1])[:6]:
        lines.append(f'   – {b}: {n}')
    if noscore or nophoto:
        lines.append(f'· аномалии у отправленных: без скоринга {noscore}, без фото {nophoto}')
    can = (s.get('canary') or {}).get('results') or {}
    bad = [f'{k}' for k, v in can.items() if str(v).startswith('FAIL')]
    lines.append('· канарейка парсеров: ' + ('⚠️ ' + ', '.join(bad) if bad else 'все ok'))
    if market_line:
        lines.append(market_line)
    return '\n'.join(lines)


def market_selfcheck(s, days=7):
    """Самосверка с рынком: у 4zida дата создания зашита в ObjectId объявления —
    считаем, сколько объявлений реально появилось за N дней и сколько из
    подходящих (lokal/poslovni-prostor 100–220 м², не Земун/пригород) монитор
    записал в state. Подходящий лот МИМО state = красный флаг (пропуск sweep'а):
    даже далёкий промах по цене обязан лежать в state как silent-reject."""
    try:
        lst = curl_sweep.sweep_4zida(20)
    except Exception as e:
        return f'· рынок (4zida, {days}д): sweep failed — {str(e)[:80]}'
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

    def oid_ts(i):
        try:
            return int(str(i)[:8], 16)
        except Exception:
            return None

    fresh = [l for l in lst if (oid_ts(l.get('id')) or 0) >= cut]
    known = {k.split('_', 1)[-1] for k in s.get('listings', {})}
    recorded = sum(1 for l in fresh if str(l.get('id')) in known)

    def is_fitting(l):
        if not (l.get('area') and 100 <= l['area'] <= 220):
            return False
        if (l.get('type') or '') not in ('lokal', 'poslovni-prostor'):
            return False
        u = (l.get('url') or '').lower()
        return not any(sl in u for sl in ZEMUN_SLUGS + FAR_MUNI_SLUGS)

    fitting = [l for l in fresh if is_fitting(l)]
    missed = [l for l in fitting if str(l.get('id')) not in known]
    line = (f'· рынок (4zida, {days}д): новых объявлений {len(fresh)}, '
            f'из них локалы 100–220 м²: {len(fitting)}, записано монитором: {recorded}')
    if missed:
        line += ('\n   ⚠️ ПРОПУЩЕНЫ подходящие: '
                 + '; '.join(f"{l.get('area')}м²/{l.get('price')}€ {l.get('url')}"
                             for l in missed[:3]))
    return line


def cmd_weekly_report():
    """--weekly-report: если сегодня понедельник (Belgrade) и ещё не слали — вернуть
    текст отчёта и пометить дату в state. Отправляет driver."""
    import zoneinfo
    now_b = datetime.now(zoneinfo.ZoneInfo('Europe/Belgrade'))
    today = now_b.strftime('%Y-%m-%d')
    if now_b.weekday() != 0 or load_state().get('weekly_report_sent') == today:
        print(json.dumps({}))
        return 0
    # Сетевая самосверка с рынком — ДО StateLock (сеть под локом не держим).
    s0 = load_state()
    market = market_selfcheck(s0)
    with StateLock():
        s = load_state()
        if s.get('weekly_report_sent') == today:  # гонка с параллельным циклом
            print(json.dumps({}))
            return 0
        text = build_weekly_report(s, market_line=market)
        if text:
            s['weekly_report_sent'] = today
            save_state(s)
    print(json.dumps({'text': text} if text else {}, ensure_ascii=False))
    return 0


def cmd_finalize():
    """Run check_status + gen_map + write runs.log line. Single tool-call replacement
    for the previous 3-step agent flow."""
    t0 = time.time()
    out = {'ok': True}

    # «Last scan» в реджект-листе (J1) — обновляется КАЖДЫМ прогоном, даже пустым,
    # и несёт статистику цикла: одна ячейка отвечает «жив и что видел».
    # Свежий штамп + нет строк = тихий рынок; старый штамп = монитор умер.
    try:
        if DRY_RUN:
            raise RuntimeError('dry-run: штамп не пишем')
        from datetime import datetime as _dt
        import zoneinfo
        _now = _dt.now(zoneinfo.ZoneInfo('Europe/Belgrade')).strftime('%Y-%m-%d %H:%M')
        _st = load_state().get('last_cycle_stats') or {}
        _down = _st.get('sources_down') or []
        _n_src = 4 - len(_down)
        _extra = ''
        if _st:
            _extra = (f" · sweep {_st.get('sweep_raw', '?')} · new {_st.get('new', '?')}"
                      f" · src {_n_src}/4" + (f" · DOWN: {','.join(_down)}" if _down else ''))
        _sheets_service().spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID, range="'не прошли фильтр'!J1",
            valueInputOption='RAW',
            body={'values': [[f'Last scan: {_now} (Belgrade){_extra}']]}).execute()
        out['last_scan_stamp'] = True
    except Exception as e:
        out['last_scan_stamp'] = f'fail: {e}'

    # check_status manages its own state writes; prints summary on stdout
    cr = subprocess.run(['python3', str(SCRIPT_DIR / 'check_status.py')],
                        capture_output=True, text=True, timeout=300)
    cout = (cr.stdout or '') + '\n' + (cr.stderr or '')
    m = re.search(r'(\d+)\s+killed[^a-z]*?(\d+)\s+alive', cout)
    killed = int(m.group(1)) if m else 0
    alive = int(m.group(2)) if m else 0
    out['check_killed'] = killed
    out['check_alive'] = alive
    out['check_rc'] = cr.returncode
    if cr.returncode != 0:
        out['check_stderr_tail'] = (cr.stderr or '').strip().split('\n')[-3:]

    # Карта плотности населения: обновляем наложение лотов (без деплоя — его сделает
    # gen_map, он шипит весь public/). Best-effort, не валим цикл при ошибке.
    try:
        subprocess.run(['python3', str(SCRIPT_DIR / 'gen_density_map.py'), '--no-deploy'],
                       capture_output=True, text=True, timeout=120)
    except Exception:
        pass

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
    KNOWN_PREFIXES = ('4zida_', 'nekretnine_', 'halooglasi_', 'cityexpert_')
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
        if feedback:
            s['feedback_aggregates'] = feedback
            out['feedback_districts'] = len(feedback['by_district'])
        if canary is not None:
            s['canary'] = {'date': _canary_date, 'results': canary}
            out['canary'] = s['canary']

        # Компакция: терминальным записям старше 45 дней тяжёлые поля не нужны
        # (описания и фото-списки — основной вес state.json).
        compact_cutoff = datetime.now(timezone.utc) - timedelta(days=45)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mark-sent', nargs=2, metavar=('KEY', 'MSG_ID'),
                    help='Mark a pass as sent + insert to Sheets')
    ap.add_argument('--desc-ru', default=None,
                    help='Русское «Кратко» для Sheets кол. F (вместо сырого сербского описания)')
    ap.add_argument('--finalize', action='store_true',
                    help='Run check_status + gen_map + runs.log')
    ap.add_argument('--weekly-report', action='store_true',
                    help='Вернуть текст weekly-отчёта (понедельник, раз в день), отправляет driver')
    ap.add_argument('--dry-run', action='store_true',
                    help='Полный прогон БЕЗ записи: state не сохраняется, лист не трогается. '
                         'Для ручных прогонов — иначе дубли с облачным монитором.')
    args = ap.parse_args()

    if args.dry_run:
        global DRY_RUN
        DRY_RUN = True
        print('=== DRY-RUN: state и Google Sheet НЕ будут изменены ===', file=sys.stderr)

    if args.dry_run and (args.finalize or args.mark_sent):
        # finalize/mark-sent пишут через подпроцессы (check_status → лист,
        # gen_map → surge) и webhook — dry-run их не гейтит. Запрещаем совсем.
        print('dry-run поддерживает только process-фазу (без --finalize/--mark-sent)',
              file=sys.stderr)
        return 2

    if args.mark_sent:
        return cmd_mark_sent(args.mark_sent[0], args.mark_sent[1], desc_ru=args.desc_ru)
    if args.finalize:
        return cmd_finalize()
    if args.weekly_report:
        return cmd_weekly_report()
    return run_process()


if __name__ == '__main__':
    sys.exit(main() or 0)

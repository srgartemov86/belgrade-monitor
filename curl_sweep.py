#!/usr/bin/env python3
"""Curl-fast-path dry-run for pizzeria-location-monitor.
Sweeps 4zida.rs, nekretnine.rs, cityexpert.rs, halooglasi.com via curl, no Chrome MCP.
halooglasi requires curl_cffi (Chrome TLS-fingerprint impersonation) to bypass Cloudflare.
Reports candidates without sending Telegram or updating state.
"""
import json, os, re, subprocess, time, sys, html as html_lib
from urllib.parse import quote, urlencode

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

# Verbose per-page stderr prints. Set PIZZA_QUIET=1 (or curl_sweep.VERBOSE=False)
# to suppress them — error lines are always printed regardless.
VERBOSE = os.environ.get('PIZZA_QUIET') != '1'

def _v(msg):
    if VERBOSE:
        print(msg, file=sys.stderr)

UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'

# --- halooglasi через резидентный прокси (GitHub Actions: DC-IP получает 403) ---
# env HALO_PROXY = 'http://user__cr.rs:pass@gw.dataimpulse.com:823'. Без него — напрямую (Mac).
# Ротация exit-IP на каждый запрос через шлюз; часть IP у Cloudflare засвечена (~33%),
# поэтому ретраи. sticky-sid (__sid.N в логине) закрепляет удачный IP на следующие страницы.
HALO_PROXY = os.environ.get('HALO_PROXY', '')
_halo_sid = [1]  # счётчик sticky-сессий; при 403 инкремент = новый exit-IP
_HALO_IMPS = ('chrome120', 'chrome124', 'chrome131', 'safari17_0')


def _halo_proxy_url():
    """user → user__sid.haloN: DataImpulse держит один exit-IP на sid."""
    if not HALO_PROXY:
        return None
    return re.sub(r'^(https?://[^:]+)', rf'\1__sid.halo{_halo_sid[0]}', HALO_PROXY, count=1)


def halo_get(url, timeout=15, attempts=4):
    """GET halooglasi.com: ретраи с ротацией impersonate-профиля и exit-IP.
    Возвращает Response с status_code==200 и телом ≥5KB, либо None."""
    if not HAS_CFFI:
        return None
    for i in range(attempts):
        imp = _HALO_IMPS[i % len(_HALO_IMPS)]
        pr = _halo_proxy_url()
        proxies = {'http': pr, 'https': pr} if pr else None
        try:
            r = cffi_requests.get(url, impersonate=imp, timeout=timeout,
                                  proxies=proxies, allow_redirects=True)
            if r.status_code == 200 and len(r.content) >= 5000:
                return r
        except Exception:
            pass
        _halo_sid[0] += 1  # неудача → новый sticky-sid → новый exit-IP
    return None

BAD_TYPES = re.compile(r'kancelarij|magacin|skladiste|skladište|hala\b|poslovna zgrada|garaž|garaz|polusuteren|suteren|tržni|trzni|stan\b', re.I)

def fetch(url, timeout=10):
    r = subprocess.run(
        ['curl', '-sS', '-L', '--max-time', str(timeout), '-A', UA,
         '-H', 'Accept-Encoding: gzip', '--compressed', url],
        capture_output=True, timeout=timeout+5
    )
    return r.stdout.decode('utf-8', errors='replace'), r.returncode

def sweep_4zida(pages=3):
    """Returns list of {id, url, name, price, area, type, source}."""
    listings = []
    for p in range(1, pages+1):
        url = f'https://www.4zida.rs/izdavanje-poslovnih-prostora/beograd?strana={p}'
        t0 = time.time()
        html, rc = fetch(url)
        dt = time.time() - t0
        if rc != 0 or len(html) < 5000:
            print(f'  4zida p{p}: FAIL rc={rc} len={len(html)}', file=sys.stderr)
            continue
        # JSON-LD: all blocks, find ItemList
        ld_blocks = re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
        items = []
        for b in ld_blocks:
            try:
                d = json.loads(b)
                if isinstance(d, dict) and d.get('@type') == 'ItemList':
                    items = d.get('itemListElement', [])
                    break
            except Exception:
                pass
        if not items:
            print(f'  4zida p{p}: no ItemList JSON-LD', file=sys.stderr)
            continue
        # Build URL→area map
        url_to_area = {}
        for m in re.finditer(r'"floorSize":\{[^}]*?"value":(\d+)', html):
            nearby = html[max(0, m.start()-1000):m.start()+200]
            urls = re.findall(r'(https://www\.4zida\.rs/[^"]+/[a-f0-9]{24})', nearby)
            if urls:
                url_to_area[urls[-1]] = int(m.group(1))
        page_count = 0
        for it in items:
            o = it.get('item', {})
            url2 = o.get('url', '')
            offers = o.get('offers') or {}
            listings.append({
                'source': '4zida.rs',
                'id': url2.rstrip('/').split('/')[-1] if url2 else None,
                'url': url2,
                'name': o.get('name'),
                'price': offers.get('price'),
                'area': url_to_area.get(url2),
                'type': url2.split('/')[-2] if url2 else None,
            })
            page_count += 1
        _v(f'  4zida p{p}: {page_count} listings ({dt:.2f}s, {len(html)//1024}KB)')
    return listings

def sweep_nekretnine(pages=3):
    """JSON-API fast-path for nekretnine.rs (site migrated to Next.js ~2026-05;
    old HTML scraper returned 0 — silently broke the only date-sorted source).
    Endpoint: GET /api-next/search-list/listings/?...&criterio=data&ordine=desc&pag=N
    idContratto=2 = izdavanje (rent), idCategoria=26 = Prodavnice/Poslovni prostori,
    idComune=324 = Beograd. criterio=data&ordine=desc = newest first.
    Returns 25/page. Data lives in results[].realEstate (lat/lon included)."""
    base = 'https://www.nekretnine.rs/api-next/search-list/listings/'
    params = {
        'fkRegione': 'RS_1', 'idProvincia': 'RS_3', 'idComune': '324',
        'idNazione': 'RS', 'idContratto': '2', 'idCategoria': '26',
        '__lang': 'sr', 'path': '/izdavanje-lokala/beograd/',
        'criterio': 'data', 'ordine': 'desc',
    }
    listings = []
    for p in range(1, pages+1):
        url = base + '?' + urlencode({**params, 'pag': p})
        t0 = time.time()
        body, rc = fetch(url)
        dt = time.time() - t0
        if rc != 0 or len(body) < 100:
            print(f'  nek p{p}: FAIL rc={rc} len={len(body)}', file=sys.stderr)
            continue
        try:
            d = json.loads(body)
        except Exception as e:
            print(f'  nek p{p}: JSON-ERR {e} head={body[:80]!r}', file=sys.stderr)
            continue
        results = d.get('results') or []
        if not results:
            _v(f'  nek p{p}: 0 results (end of list)')
            break
        page_count = 0
        for r in results:
            re0 = r.get('realEstate') or {}
            if re0.get('contract') != 'rent':
                continue
            props = re0.get('properties') or [{}]
            p0 = props[0] if props else {}
            price = (re0.get('price') or {}).get('value')
            surf = p0.get('surface') or ''
            am = re.search(r'\d[\d.\s]*', surf)
            area = int(re.sub(r'\D', '', am.group(0))) if am else None
            url_l = (r.get('seo') or {}).get('url') or f"https://www.nekretnine.rs/oglasi/{re0.get('id')}/"
            typ = (p0.get('typology') or {}).get('name') or (re0.get('typology') or {}).get('name')
            loc = p0.get('location') or {}
            listings.append({
                'source': 'nekretnine.rs',
                'id': str(re0.get('id')),
                'url': url_l,
                'name': re0.get('title'),
                'price': int(price) if price else None,
                'area': area,
                'type': typ,
                'date': None,
                'street': loc.get('address'),
            })
            page_count += 1
        _v(f'  nek p{p}: {page_count} listings ({dt:.2f}s, {len(body)//1024}KB)')
    return listings

_CITYEXPERT_STRUCTURE_SLUG = {
    'OTHER': 'ostalo', 'STUDIO': 'garsonjera', 'ONE_ROOM': 'jednosoban',
    'ONE_AND_HALF': 'jednoiposoban', 'TWO_ROOM': 'dvosoban',
    'TWO_AND_HALF': 'dvoiposoban', 'THREE_ROOM': 'trosoban',
    'THREE_AND_HALF': 'troiposoban', 'FOUR_ROOM': 'cetvorosoban',
}

def _ce_slug(s):
    if not s: return ''
    repl = {'č':'c','ć':'c','š':'s','ž':'z','đ':'dj','Č':'c','Ć':'c','Š':'s','Ž':'z','Đ':'dj'}
    out = ''.join(repl.get(c, c) for c in s)
    out = out.lower()
    out = re.sub(r'[^a-z0-9]+', '-', out)
    return out.strip('-')

def sweep_cityexpert(pages=3):
    """JSON-API fast-path for cityexpert.rs (recovered 2026-05-04).
    Endpoint: GET /api/Search?req={"ptId":[4],"cityId":1,"rentOrSale":"r",
                                   "searchSource":"regular","sort":"datedsc",
                                   "currentPage":N}
    ptId=4 = poslovni prostor / lokal. cityId=1 = Beograd.
    Returns 30/page, ~3 pages = ~65 lots total citywide.
    """
    listings = []
    for p in range(1, pages+1):
        req = {
            'ptId': [4], 'cityId': 1, 'rentOrSale': 'r',
            'searchSource': 'regular', 'sort': 'datedsc', 'currentPage': p,
        }
        url = 'https://cityexpert.rs/api/Search?req=' + quote(json.dumps(req, separators=(',', ':')))
        t0 = time.time()
        body, rc = fetch(url)
        dt = time.time() - t0
        if rc != 0 or len(body) < 100:
            print(f'  cityexpert p{p}: FAIL rc={rc} len={len(body)}', file=sys.stderr)
            continue
        try:
            d = json.loads(body)
        except Exception as e:
            print(f'  cityexpert p{p}: JSON parse error {e}', file=sys.stderr)
            continue
        result = d.get('result', [])
        info = d.get('info', {})
        for r in result:
            prop_id = r.get('propId')
            if prop_id is None: continue
            structure = r.get('structure') or ''
            slug = '-'.join(filter(None, [
                _CITYEXPERT_STRUCTURE_SLUG.get(structure, _ce_slug(structure)),
                'lokal',
                _ce_slug(r.get('street')),
                _ce_slug(r.get('municipality')),
            ]))
            detail_url = f'https://cityexpert.rs/izdavanje-nekretnina/beograd/{prop_id}/{slug}'
            try:
                price_int = int(round(float(r.get('price') or 0)))
            except Exception:
                price_int = None
            try:
                area_int = int(round(float(r.get('size') or 0)))
            except Exception:
                area_int = None
            listings.append({
                'source': 'cityexpert.rs',
                'id': str(prop_id),
                'url': detail_url,
                'name': f"{r.get('street','')}, {r.get('municipality','')}".strip(', '),
                'price': price_int,
                'area': area_int,
                'type': 'lokal' if r.get('ptId') == 4 else None,
                'floor': r.get('floor'),
                'municipality': r.get('municipality'),
                'is_salonac': r.get('isSalonac'),
                'first_published': r.get('firstPublished'),
                'available_from': r.get('availableFrom'),
                'location': r.get('location'),
            })
        _v(f'  cityexpert p{p}: {len(result)} listings ({dt:.2f}s, {len(body)//1024}KB) info={info.get("pageNumber")}/{info.get("pageCount")}')
        if info.get('isLastPage'):
            break
    return listings

def _balanced_brace(s, start):
    i = s.find('{', start)
    if i < 0: return None
    depth = 0; in_str = False; esc = False; j = i
    while j < len(s):
        c = s[j]
        if esc: esc = False
        elif c == '\\': esc = True
        elif c == '"' and not esc: in_str = not in_str
        elif not in_str:
            if c == '{': depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0: return s[i:j+1]
        j += 1
    return None

_HAL_PROP_TYPES_RX = r'(Lokal|Hala|Magacin|Kancelarija|Kafana|Restoran|Ugostiteljski objekat|Salon|Poslovni prostor|Plac)'

def _parse_halooglasi_listhtml(list_html):
    """Parse a single Ad's ListHTML blob into structured fields."""
    if not list_html: return {}
    t = re.sub(r'<[^>]+>', '|', list_html)
    t = html_lib.unescape(t)
    t = t.replace('\xa0', ' ').replace(' ', ' ')
    t = re.sub(r'\|+', '|', t)
    t = re.sub(r'\s+', ' ', t).strip()

    out = {}
    pm = re.search(r'data-value="([\d.,]+)"', list_html)
    if pm:
        try: out['price'] = int(re.sub(r'[^\d]', '', pm.group(1)))
        except: pass
    am = re.search(r'(\d[\d.,]*)\s*m\s*\|\s*2\s*\|\s*Kvadratura', t)
    if am:
        try: out['area'] = int(float(am.group(1).replace('.', '').replace(',', '.')))
        except: pass
    sm = re.search(r'\|\s*([A-ZČĆŠŽĐa-zčćšžđ0-9.\-/]+)\s*\|\s*Spratnost', t)
    if sm: out['sprat'] = sm.group(1).strip()
    tm = re.search(r'\|\s*' + _HAL_PROP_TYPES_RX + r'\s*\|\s*Tip nekretnine', t)
    if tm: out['prop_type'] = tm.group(1)
    loc_m = re.search(r'\|\s*Beograd\s*\|(.*?)\|\s*' + _HAL_PROP_TYPES_RX + r'\s*\|\s*Tip nekretnine', t)
    if not loc_m:
        loc_m = re.search(r'\|\s*Beograd\s*\|(.*?)\|\s*\d[\d.,]*\s*m\s*\|\s*2\s*\|\s*Kvadratura', t)
    if loc_m:
        parts = [p.strip() for p in loc_m.group(1).split('|') if p.strip()]
        for p in parts:
            if p.startswith('Opština '): out['opstina'] = p[8:].strip()
        if len(parts) >= 2: out['subdistrict'] = parts[1]
        if len(parts) >= 3: out['street'] = parts[2]
        out['loc_chain'] = ' / '.join(parts)
    dm = re.search(r'(\d{2}\.\d{2}\.\d{4})', t)
    if dm: out['date_posted'] = dm.group(1)
    om = re.search(r'\|\s*(Vlasnik|Agencija|Investitor)\s*\|', t)
    if om: out['owner'] = om.group(1)
    return out

def sweep_halooglasi(pages=8):
    """Returns list of {id, url, title, price, area, sprat, type, opstina, ...}.
    Requires curl_cffi to bypass Cloudflare via Chrome TLS fingerprint."""
    if not HAS_CFFI:
        print('  halooglasi: SKIP (curl_cffi not installed)', file=sys.stderr)
        return []
    listings = []
    for p in range(1, pages+1):
        url = f'https://www.halooglasi.com/nekretnine/izdavanje-poslovnog-prostora/beograd?page={p}'
        t0 = time.time()
        # halo_get: ротация impersonate-профилей против CF-челленджей;
        # через HALO_PROXY (GitHub Actions) ещё и ротация exit-IP с ретраями.
        r = halo_get(url, timeout=15)
        if r is None:
            print(f'  halooglasi p{p}: FAIL after retries', file=sys.stderr)
            continue
        dt = time.time() - t0
        html = r.text
        m = re.search(r'QuidditaEnvironment\.serverListData\s*=', html)
        if not m:
            print(f'  halooglasi p{p}: no serverListData', file=sys.stderr)
            continue
        blob = _balanced_brace(html, m.start())
        if not blob: continue
        try:
            data = json.loads(blob)
        except Exception as e:
            print(f'  halooglasi p{p}: JSON err {e}', file=sys.stderr)
            continue
        ads = data.get('Ads', [])
        page_n = 0
        for ad in ads:
            fields = _parse_halooglasi_listhtml(html_lib.unescape(ad.get('ListHTML', '') or ''))
            rel = ad.get('RelativeUrl') or ''
            listings.append({
                'source': 'halooglasi.com',
                'id': str(ad.get('Id') or ''),
                'url': 'https://www.halooglasi.com' + rel,
                'name': ad.get('Title') or '',
                'price': fields.get('price'),
                'area': fields.get('area'),
                'floor': fields.get('sprat'),
                'type': fields.get('prop_type'),
                'municipality': fields.get('opstina'),
                'subdistrict': fields.get('subdistrict'),
                'street': fields.get('street'),
                'loc_chain': fields.get('loc_chain'),
                'date_posted': fields.get('date_posted'),
                'owner': fields.get('owner'),
            })
            page_n += 1
        _v(f'  halooglasi p{p}: {page_n} listings ({dt:.2f}s, {len(r.content)//1024}KB)')
    return listings

_KEY_PREFIXES = [
    'nekretnine.rs::', '4zida.rs::', 'halooglasi.com::', 'cityexpert.rs::',
    'nekretnine_', '4zida_', 'halooglasi_', 'cityexpert_',
    'nekretnine.rs:', '4zida.rs:', 'halooglasi.com:', 'cityexpert.rs:',
    'halooglasi:',
]

def known_ids():
    s = json.load(open(os.path.join(os.environ.get('BG_DATA', '/Users/dodo/pizzeria-location-monitor'), 'state.json')))
    out = set()
    for section in ('listings', 'rejected_in_filter_v6', 'rejected_after_detail'):
        for k in s.get(section, {}):
            out.add(k)
            for pref in _KEY_PREFIXES:
                if k.startswith(pref):
                    out.add(k[len(pref):])
                    break
    for c in s.get('pending_candidates', []):
        url = c.get('url', '')
        if 'Nk' in url:
            m = re.search(r'(Nk[A-Za-z0-9_-]+)', url)
            if m: out.add(m.group(1))
        elif '/poslovni-prostor/' in url or '/lokal/' in url:
            m = re.search(r'/([a-f0-9]{24})', url)
            if m: out.add(m.group(1))
    return out

def main():
    t0 = time.time()
    print('=== sweep ===', file=sys.stderr)
    # 4zida default-сортировка НЕ "newest first" (видимо featured/promo приоритет),
    # поэтому свежие лоты часто оказываются на стр. 4-6. Фикс 2026-05-08:
    # подняли pages=3 → 6 после того как пропустили лот 69fdca1b (Vračar/Južni Bulevar,
    # 210m²/1950€), опубликованный за 6 минут до цикла, но осевший на стр. 5.
    a = sweep_4zida(pages=6)
    n = sweep_nekretnine(pages=3)
    c = sweep_cityexpert(pages=3)
    h = sweep_halooglasi(pages=8)
    all_l = a + n + c + h
    print(f'\nTotal raw: {len(all_l)} ({len(a)} 4zida + {len(n)} nekretnine + {len(c)} cityexpert + {len(h)} halooglasi)', file=sys.stderr)
    print(f'Total time: {time.time()-t0:.2f}s', file=sys.stderr)

    # Apply filters
    filtered = []
    ZEMUN_SLUGS = ('zemun', 'altina', 'batajnica', 'galenika', 'kalvarija')
    for l in all_l:
        if not l.get('area') or not l.get('price'): continue
        if l['area'] < 100 or l['area'] > 220: continue
        if l['price'] < 1300 or l['price'] > 6000: continue
        # Type-based reject
        type_ = (l.get('type','') or '').lower()
        name = (l.get('name','') or '').lower()
        if any(b in type_ for b in ['kancelarij','magacin','skladiste','poslovna-zgrada','poslovna zgrada','hala','garaž']): continue
        if BAD_TYPES.search(name): continue
        # Zemun blacklist (already have a pizzeria there)
        url_lower = (l.get('url','') or '').lower()
        if any(slug in url_lower for slug in ZEMUN_SLUGS): continue
        if (l.get('municipality') or '').strip().lower() == 'zemun': continue
        # cityexpert-specific: floor must be prizemlje, skip salonac (converted apartment)
        if l.get('source') == 'cityexpert.rs':
            if (l.get('floor') or '').upper() != 'PR': continue
            if l.get('is_salonac') is True: continue
        # halooglasi-specific: prefer Lokal/Ugostiteljski; if floor present, must be PR
        if l.get('source') == 'halooglasi.com':
            ptype = (l.get('type') or '').lower()
            if ptype and ptype not in ('lokal', 'ugostiteljski objekat', 'poslovni prostor', 'salon', 'restoran', 'kafana'):
                continue
            floor = (l.get('floor') or '').upper().strip().rstrip('.')
            if floor and floor not in ('PR', 'PRIZEMLJE'):
                continue
            # Skip Beograd-area municipalities outside the 7km core (cheap pre-filter; final dist check on detail)
            mun = (l.get('municipality') or '').lower()
            if any(x in mun for x in ('grocka','lazarevac','obrenovac','mladenovac','sopot','barajevo','surčin','surcin')):
                continue
        filtered.append(l)
    print(f'After filter (area 100-220, price 1300-6000, no Zemun, not office/storage): {len(filtered)}', file=sys.stderr)

    # Dedupe against state
    known = known_ids()
    print(f'Known IDs in state: {len(known)}', file=sys.stderr)
    new = [l for l in filtered if l.get('id') and l['id'] not in known]
    print(f'\n=== NEW CANDIDATES ({len(new)}) ===')
    for l in new:
        print(f"  [{l['source']:13}] {l['id'][:24]:24} | {l['area']:>4}m² | {l['price']:>5}€ | {(l.get('name') or l.get('type') or '')[:50]}")

    # Sweep summary
    print(f'\n=== SUMMARY ===')
    print(f'Total fetched: {len(all_l)}')
    print(f'Passed filter: {len(filtered)}')
    print(f'New (not in state): {len(new)}')
    print(f'Wall time: {time.time()-t0:.2f}s')

if __name__ == '__main__':
    main()

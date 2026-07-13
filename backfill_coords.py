"""
One-off backfill: для лотов, у которых координаты пришли от Nominatim
(центроид района), идём на detail-страницу и достаём точные координаты.

Источники:
- 4zida — JSON-LD `"latitude":...,"longitude":...`
- halooglasi — `QuidditaEnvironment.CurrentClassified.GeoLocationRPT`
- cityexpert — `"mapLat":...,"mapLng":...` для нужного propId
- nekretnine.rs — не отдаёт coords в статике, оставляем что есть

Запись в state.json: обновляются geo_lat, geo_lon, geo_source='detail'.
Rate limit: ~1 req/s.
"""
import json, re, time, sys, os
from curl_cffi import requests

STATE_PATH = os.path.join(os.environ.get('BG_DATA', '/Users/dodo/pizzeria-location-monitor'), 'state.json')

UA_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def fetch(url, timeout=20):
    return requests.get(url, impersonate="chrome120", timeout=timeout)


def extract_4zida(html):
    # Бывает unescaped JSON-LD: "latitude":N,"longitude":N
    # И escaped в __NUXT__ state: \"latitude\":N,\"longitude\":N
    # Иногда массив coordinates: [lon, lat]
    pat = r'\\?"latitude\\?"\s*:\s*"?(-?\d+\.\d+)"?\s*,\s*\\?"longitude\\?"\s*:\s*"?(-?\d+\.\d+)"?'
    m = re.search(pat, html)
    if m:
        return float(m.group(1)), float(m.group(2))
    # fallback: GeoJSON [lon, lat]
    m = re.search(r'\\?"coordinates\\?"\s*:\s*\[\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\]', html)
    if m:
        return float(m.group(2)), float(m.group(1))
    return None, None


def extract_halooglasi(html):
    m = re.search(r'GeoLocationRPT["\s:]+["\'](-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)', html)
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def extract_cityexpert(html, prop_id):
    # Сначала пробуем найти конкретный propId, потом mapLat (первое вхождение).
    if prop_id:
        m = re.search(rf'propId["\s:]+{re.escape(str(prop_id))}\b.*?"location"\s*:\s*"(-?\d+\.\d+),\s*(-?\d+\.\d+)"', html, re.DOTALL)
        if m:
            return float(m.group(1)), float(m.group(2))
    m = re.search(r'"mapLat"\s*:\s*(-?\d+\.\d+)\s*,\s*"mapLng"\s*:\s*(-?\d+\.\d+)', html)
    return (float(m.group(1)), float(m.group(2))) if m else (None, None)


def detect_source(v):
    src = (v.get('source') or '').lower()
    if '4zida' in src: return '4zida'
    if 'halooglasi' in src: return 'halooglasi'
    if 'cityexpert' in src: return 'cityexpert'
    if 'nekretnine' in src: return 'nekretnine'
    # Иногда source пустой — определяем по URL
    url = v.get('url') or ''
    if '4zida.rs' in url: return '4zida'
    if 'halooglasi.com' in url: return 'halooglasi'
    if 'cityexpert.rs' in url: return 'cityexpert'
    if 'nekretnine.rs' in url: return 'nekretnine'
    return None


def cityexpert_prop_id(url):
    m = re.search(r'cityexpert\.rs/[^/]+/beograd/(\d+)', url or '')
    return m.group(1) if m else None


def run(state, limit=None):
    """Мутирует state, возвращает (updated, failed). limit — макс. число fetch-вызовов за прогон."""
    import math
    listings = state['listings']

    # Кандидаты: in_sheet, не removed, source ∈ {4zida, halooglasi, cityexpert}, geo_source='nominatim'/'nominatim_v2'/'district_centroid' ИЛИ нет координат
    REGEO_SOURCES = ('nominatim', 'nominatim_v2', 'district_centroid', None, '')
    todo = []
    for k, v in listings.items():
        if not v.get('in_sheet'): continue
        if v.get('removed_from_sheet'): continue
        src = detect_source(v)
        if src not in ('4zida', 'halooglasi', 'cityexpert'): continue
        if v.get('geo_source') == 'detail': continue  # уже точные
        if v.get('geo_source') not in REGEO_SOURCES and (v.get('geo_lat') and v.get('geo_lon')):
            continue
        todo.append((k, v, src))

    if limit:
        todo = todo[:limit]

    print(f"backfill candidates: {len(todo)} "
          f"(4zida={sum(1 for _,_,s in todo if s=='4zida')}, "
          f"halooglasi={sum(1 for _,_,s in todo if s=='halooglasi')}, "
          f"cityexpert={sum(1 for _,_,s in todo if s=='cityexpert')})")

    updated = failed = 0
    for k, v, src in todo:
        url = v.get('url') or ''
        if not url:
            print(f"  SKIP {k}: no url")
            continue
        try:
            r = fetch(url)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code} {k}: {url[:80]}")
                failed += 1
                continue
            html = r.text
            if src == '4zida':
                lat, lon = extract_4zida(html)
            elif src == 'halooglasi':
                lat, lon = extract_halooglasi(html)
            elif src == 'cityexpert':
                lat, lon = extract_cityexpert(html, cityexpert_prop_id(url))
            else:
                lat = lon = None
            if lat and lon:
                old_lat, old_lon = v.get('geo_lat'), v.get('geo_lon')
                # Sanity guard: detail-coord ≤7km от Trg Republike. Иначе паблишер ввёл мусор.
                TRG = (44.8167, 20.4583)
                d_trg_km = math.hypot((lat-TRG[0])*111, (lon-TRG[1])*111*math.cos(math.radians(lat)))
                if d_trg_km > 12:  # дальше 12km — точно мусор (наш радиус интереса 7km)
                    v.setdefault('flags', [])
                    if 'detail_coord_suspect' not in v['flags']:
                        v['flags'].append('detail_coord_suspect')
                    print(f"  ⚠ {src}/{k[:30]:<30} detail coord {lat:.5f},{lon:.5f} too far ({d_trg_km:.1f}km from TRG) — keeping old coord, flagging")
                    failed += 1
                    time.sleep(0.8)
                    continue
                v['geo_lat'] = lat
                v['geo_lon'] = lon
                v['geo_source'] = 'detail'
                updated += 1
                shift = ''
                if old_lat and old_lon:
                    dlat = (lat - old_lat) * 111
                    dlon = (lon - old_lon) * 111 * math.cos(math.radians(lat))
                    shift = f"  (shift {math.hypot(dlat, dlon):.2f} km)"
                print(f"  ✓ {src}/{k[:30]:<30} {lat:.5f},{lon:.5f}{shift}")
            else:
                print(f"  ✗ {src}/{k}: no coords in html")
                failed += 1
        except Exception as e:
            print(f"  ERR {k}: {e}")
            failed += 1
        time.sleep(0.8)

    return updated, failed


def main():
    state = json.load(open(STATE_PATH, encoding='utf-8'))
    updated, failed = run(state)
    if updated:
        state['version'] = state.get('version', 0) + 1
        json.dump(state, open(STATE_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    print(f"\nupdated={updated}  failed={failed}  state v{state.get('version', 0)}")


if __name__ == '__main__':
    main()

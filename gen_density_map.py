#!/usr/bin/env python3
"""Карта плотности населения Белграда (Kontur) + точки Dodo + наши лоты со скорингом.
Деплоится в тот же surge-сайт: https://dodo-belgrade-lokali.surge.sh/naselje.html
"""
import json, os, re, subprocess

STATE = os.path.join(os.environ.get('BG_DATA', '/Users/dodo/pizzeria-location-monitor'), 'state.json')
PUBLIC = os.environ.get('BG_PUBLIC', os.path.join(os.environ.get('BG_DATA', '/Users/dodo/pizzeria-location-monitor'), 'public'))
OUT = os.path.join(PUBLIC, 'naselje.html')
DODO = [
    {'name': 'Dodo · Земун / Новый Белград', 'lat': 44.8456204, 'lon': 20.4012657},
    {'name': 'Dodo · Стари Град', 'lat': 44.814985, 'lon': 20.4564471},
]
NOT_SUITABLE = {'не подходит', 'отклонено', 'отказ', 'reject', 'nope', 'no',
                'снят с сайта', 'снят', 'removed', 'inactive', 'dead'}


def in_play_lots():
    s = json.load(open(STATE, encoding='utf-8'))
    out = []
    for v in s['listings'].values():
        if not isinstance(v, dict):
            continue
        if v.get('rejected') or v.get('removed_from_sheet'):
            continue
        if v.get('score') is None or v.get('geo_lat') is None:
            continue
        out.append({
            'lat': v['geo_lat'], 'lon': v['geo_lon'], 'score': v['score'],
            'district': v.get('district') or '?',
            'area': v.get('area_m2'), 'price': v.get('price_eur'),
            'url': v.get('url', ''),
            'r500': (v.get('score_data') or {}).get('residents_500'),
        })
    return out


HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Плотность населения · Белград · Kontur vs OSM</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
  #map{position:absolute;inset:0}
  .legend{position:absolute;right:12px;top:12px;z-index:1000;background:#fff;padding:10px 12px;
    border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.2);font-size:13px;max-width:240px}
  .legend h4{margin:0 0 6px;font-size:13px}
  .legend .row{display:flex;align-items:center;margin:2px 0}
  .legend i{width:16px;height:12px;margin-right:7px;border-radius:2px;display:inline-block}
  .legend .sub{color:#6b7280;font-size:11px;margin-top:6px;line-height:1.4}
  .legend a{color:#2563eb;text-decoration:none}
  .title{position:absolute;left:12px;top:12px;z-index:1000;background:#fff;padding:8px 12px;
    border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.2);font-size:14px;font-weight:600;max-width:48%}
  .title small{display:block;font-weight:400;color:#6b7280;font-size:11px;margin-top:3px}
  .switch{position:absolute;left:12px;top:66px;z-index:1000;background:#fff;border-radius:10px;
    box-shadow:0 2px 10px rgba(0,0,0,.2);display:flex;overflow:hidden;font-size:13px}
  .switch button{border:0;background:#fff;padding:8px 14px;cursor:pointer;font-weight:600;color:#374151}
  .switch button.on{background:#ff6900;color:#fff}
  .dodo-marker{width:30px;height:30px;border-radius:50%;background:#fff;
    box-shadow:0 0 0 2px #ff6900,0 2px 6px rgba(0,0,0,.3);padding:2px;box-sizing:border-box}
  .dodo-marker img{width:100%;height:100%;border-radius:50%}
</style></head><body>
<div id="map"></div>
<div class="title" id="ttl"></div>
<div class="switch">
  <button id="bO" onclick="setMode('osm')">OSM · этажность</button>
  <button id="bK" onclick="setMode('kontur')">Kontur</button>
</div>
<div class="legend">
  <h4>Жителей на км²</h4>
  <div id="buckets"></div>
  <div class="row" style="margin-top:6px"><span style="width:16px;margin-right:7px;text-align:center">●</span>наш лот (цвет = скоринг)</div>
  <div class="row"><img class="dodo" src="dodo-logo.png" style="width:16px;height:16px;margin-right:7px"> точка Dodo</div>
  <div class="sub" id="src"></div>
</div>
<script>
const DODO = __DODO__;
const LOTS = __LOTS__;
const TITLES={
  osm:['🗺 Плотность · OSM (этажность)','Σ(footprint × этажей × 0.8) / м²-на-чел-по-типу (кварт. 40, дома 55). Различает высотки/частный сектор. Так считается скоринг.'],
  kontur:['🗺 Плотность · Kontur Population','Census, размазанный по площади застройки. НЕ видит этажность → частный сектор завышен.']
};
const map = L.map('map').setView([44.81, 20.46], 12);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
  {attribution:'© OpenStreetMap, © CARTO', maxZoom:19}).addTo(map);

const STOPS = [[12000,'#800026'],[8000,'#bd0026'],[5000,'#e31a1c'],[3000,'#fc4e2a'],
               [1500,'#fd8d3c'],[600,'#feb24c'],[150,'#fed976'],[0,'#ffeda0']];
function densColor(d){ for(const [t,c] of STOPS){ if(d>=t) return c; } return '#ffeda0'; }
function scoreColor(s){ return s>=70?'#16a34a':s>=50?'#eab308':s>=30?'#f97316':'#dc2626'; }

let GJ=null, densLayer=null, mode='osm';
function dval(p){ return mode==='osm'?p.osm_norm:p.kontur; }
function renderDens(){
  if(densLayer) map.removeLayer(densLayer);
  densLayer=L.geoJSON(GJ,{
    style:f=>({fillColor:densColor(dval(f.properties)),weight:0.3,color:'#999',fillOpacity:0.6}),
    onEachFeature:(f,l)=>{const p=f.properties;
      l.bindPopup(`<b>${dval(p).toLocaleString('ru')}</b> чел./км² (${mode==='osm'?'OSM':'Kontur'})`+
        `<hr style="margin:4px 0;border:0;border-top:1px solid #eee">`+
        `OSM (этажность): ${p.osm_norm.toLocaleString('ru')}/км²<br>Kontur: ${p.kontur.toLocaleString('ru')}/км²`);}
  });
  densLayer.addTo(map); densLayer.bringToBack();
}
function setMode(m){ mode=m;
  document.getElementById('bO').className=m==='osm'?'on':'';
  document.getElementById('bK').className=m==='kontur'?'on':'';
  document.getElementById('ttl').innerHTML=TITLES[m][0]+'<small>'+TITLES[m][1]+'</small>';
  if(GJ) renderDens();
}
fetch('density_compare_bg.geojson').then(r=>r.json()).then(gj=>{GJ=gj; renderDens();});

const dodoIcon = L.divIcon({className:'',html:'<div class="dodo-marker"><img src="dodo-logo.png"></div>',
  iconSize:[30,30],iconAnchor:[15,15]});
DODO.forEach(d=>L.marker([d.lat,d.lon],{icon:dodoIcon,zIndexOffset:1000})
  .bindPopup(d.name).addTo(map));

const lotLayer = L.layerGroup().addTo(map);
LOTS.forEach(p=>{
  L.circleMarker([p.lat,p.lon],{radius:6,fillColor:scoreColor(p.score),color:'#fff',
    weight:1.5,fillOpacity:0.95,zIndexOffset:500})
   .bindPopup(`<b>${p.district}</b><br>Скоринг <b>${p.score}/100</b>`+
     (p.r500!=null?` · ~${Number(p.r500).toLocaleString('ru')} жит. в 500м`:'')+
     `<br>${p.area||'—'} м² · ${p.price||'—'} €/мес`+
     (p.url?`<br><a href="${p.url}" target="_blank">объявление</a>`:''))
   .addTo(lotLayer);
});

const bk=document.getElementById('buckets');
const labels=['12 000+','8–12 тыс','5–8 тыс','3–5 тыс','1,5–3 тыс','600–1500','150–600','< 150'];
STOPS.forEach((s,i)=>{ bk.insertAdjacentHTML('beforeend',
  `<div class="row"><i style="background:${s[1]}"></i>${labels[i]}</div>`); });
document.getElementById('src').innerHTML='Переключай OSM ↔ Kontur — шкала общая.<br>'+
  '<a href="lokali.html">← карта помещений со скорингом</a> · <a href="naselje_compare.html">Нови-Сад</a>';
setMode('osm');
</script></body></html>"""


def main():
    import sys
    lots = in_play_lots()
    html = (HTML.replace('__DODO__', json.dumps(DODO, ensure_ascii=False))
                .replace('__LOTS__', json.dumps(lots, ensure_ascii=False)))
    open(OUT, 'w', encoding='utf-8').write(html)
    print(f"wrote {OUT} ({len(html)//1024} KB), lots={len(lots)}")
    surge = __import__('shutil').which('surge') or '/Users/dodo/.local/bin/surge'
    if '--no-deploy' in sys.argv:
        return  # деплой сделает gen_map (он шипит весь public/)
    if os.path.exists(surge) and os.path.exists(os.path.expanduser('~/.netrc')):
        res = subprocess.run([surge, PUBLIC, 'dodo-belgrade-lokali.surge.sh'],
                             capture_output=True, text=True, timeout=120)
        print('surge:', (res.stdout + res.stderr).strip().splitlines()[-1])


if __name__ == '__main__':
    main()

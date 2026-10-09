"""Distance from every project to the nearest river and canal (OpenStreetMap, via the Overpass API).

Rivers and canals rarely change, so this runs once a month or on demand (Actions > Rivers > Run workflow).
For each project it keeps:
  rv   nearest waterway=river   {n: name, d: km}
  cn   nearest waterway=canal   {n: name, d: km}
  near up to 3 named rivers or canals within 6 km, nearest first [{n, t: 'river'|'canal', d}]
The dashboard uses rv.d for the watch zones (<= 3 km เฝ้าระวังพิเศษ, <= 6 km เฝ้าระวัง) when a project
has no water-level numbers, and near[0] to group projects by the waterway next to them.

Writes docs/data/rivers.json and docs/data/waterways.json (simplified lines for the maps). Run: python scripts/rivers.py
Testing without network: RIVERS_FIXTURE=<overpass-style json> python scripts/rivers.py
"""
import json, math, os, sys, time, datetime, urllib.request, urllib.parse
import net

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'docs', 'data')
MAX_KM = 6.0
PAD = 0.07                      # degrees around each tile (about 7.7 km) so waterways just outside a tile are seen
TILE = 0.35                     # degrees per query tile
ENDPOINTS = ['https://overpass-api.de/api/interpreter', 'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
             'https://overpass.private.coffee/api/interpreter', 'https://overpass.kumi.systems/api/interpreter', 'https://overpass.osm.jp/api/interpreter']
# Overpass servers answer 406 to requests without a clear, non-browser User-Agent
OP_HEADERS = {'User-Agent': 'pmr-ert-flood/1.2 (+https://github.com/plusproperty-system/ert-flood)', 'Accept': 'application/json, */*;q=0.5'}
TH = datetime.timezone(datetime.timedelta(hours=7))


def _op(url, q, post):
    if post:
        req = urllib.request.Request(url, data=urllib.parse.urlencode({'data': q}).encode(), headers={**OP_HEADERS, 'Content-Type': 'application/x-www-form-urlencoded'})
    else:
        req = urllib.request.Request(url + '?' + urllib.parse.urlencode({'data': q}), headers=OP_HEADERS)
    with urllib.request.urlopen(req, timeout=200) as r:
        d = json.loads(r.read().decode('utf-8'))
    if d.get('remark') and 'error' in d['remark'].lower(): raise RuntimeError(d['remark'][:200])
    return d.get('elements', [])


def overpass(bbox):
    """one tile; tries every server, POST then GET, with growing pauses. Returns None when every attempt failed."""
    s, w, n, e = bbox
    q = f'[out:json][timeout:150];way["waterway"~"^(river|canal)$"]({s:.4f},{w:.4f},{n:.4f},{e:.4f});out tags geom qt;'
    for rnd in range(2):
        for url in ENDPOINTS:
            for post in (True, False):
                try:
                    els = _op(url, q, post); print(f'    {url} {"POST" if post else "GET"} ok'); return els
                except Exception as ex:
                    print(f'    {url} {"POST" if post else "GET"} failed: {str(ex)[:160]}'); net.TRIED.append(f'{url.split("/")[2]} {"POST" if post else "GET"}: {str(ex)[:120]}')
                    time.sleep(3)
        time.sleep(30)
    return None


def name_of(tags):
    return (tags.get('name:th') or tags.get('name') or tags.get('name:en') or '').strip()


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    x, y = ax + t * dx - px, ay + t * dy - py
    return math.hypot(x, y)


def rdp(pts, eps):
    """Douglas-Peucker simplification of [(lat, lon), ...]"""
    if len(pts) < 3: return pts
    keep = [False] * len(pts); keep[0] = keep[-1] = True; stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop(); (y1, x1), (y2, x2) = pts[i], pts[j]; dx, dy = x2 - x1, y2 - y1; L = math.hypot(dx, dy) or 1e-12
        k, dm = -1, eps
        for m in range(i + 1, j):
            d = abs(dy * (pts[m][1] - x1) - dx * (pts[m][0] - y1)) / L
            if d > dm: k, dm = m, d
        if k > 0: keep[k] = True; stack += [(i, k), (k, j)]
    return [p for p, f in zip(pts, keep) if f]


def map_layer(ways):
    """rivers and named canals, simplified (~150 m) for drawing on the dashboard maps"""
    out = []
    for w in ways:
        if w['t'] == 'canal' and not w['n']: continue
        g = [[round(a, 4), round(b, 4)] for a, b in rdp(w['g'], 0.0015)]
        if len(g) >= 2: out.append({'n': w['n'], 't': w['t'], 'g': [g]})
    return out


def compute(projects, ways):
    """ways: list of {t: 'river'|'canal', n: name, g: [(lat, lon), ...]}"""
    # precompute bounding boxes
    for w in ways:
        la = [p[0] for p in w['g']]; lo = [p[1] for p in w['g']]
        w['bb'] = (min(la), min(lo), max(la), max(lo))
    out = {}
    for p in projects:
        if p.get('la') is None: continue
        la0, lo0 = p['la'], p['lo']
        kx, ky = 111.32 * math.cos(math.radians(la0)), 110.57
        dla, dlo = MAX_KM * 2 / ky, MAX_KM * 2 / kx
        best = {'river': None, 'canal': None}; named = {}
        def dist(w):
            g = w['g']
            if len(g) == 1: return math.hypot((g[0][1] - lo0) * kx, (g[0][0] - la0) * ky)
            return min(seg_dist(0, 0, (o1 - lo0) * kx, (a1 - la0) * ky, (o2 - lo0) * kx, (a2 - la0) * ky) for (a1, o1), (a2, o2) in zip(g, g[1:]))
        near_box = lambda b: not (b[0] - dla > la0 or b[2] + dla < la0 or b[1] - dlo > lo0 or b[3] + dlo < lo0)
        for w in ways:
            if not near_box(w['bb']): continue
            d = dist(w); cur = best[w['t']]
            if cur is None or d < cur[1]: best[w['t']] = (w['n'], d)
            if w['n'] and d <= MAX_KM and (w['n'] not in named or d < named[w['n']][1]): named[w['n']] = (w['t'], d)
        for t in ('river', 'canal'):          # nothing close by: find the nearest one anywhere, for information only
            if best[t] is None:
                cand = [(w['n'], dist(w)) for w in ways if w['t'] == t]
                if cand: best[t] = min(cand, key=lambda x: x[1])
        r = {}
        if best['river']: r['rv'] = {'n': best['river'][0] or 'แม่น้ำไม่ระบุชื่อ', 'd': round(best['river'][1], 2)}
        if best['canal']: r['cn'] = {'n': best['canal'][0] or 'คลองไม่ระบุชื่อ', 'd': round(best['canal'][1], 2)}
        r['near'] = [{'n': n, 't': t, 'd': round(d, 2)} for n, (t, d) in sorted(named.items(), key=lambda x: x[1][1])[:3]]
        out[p['k']] = r
    return out


def main():
    projects = json.load(open(os.path.join(DATA, 'master.json'), encoding='utf-8'))['projects']
    pts = [p for p in projects if p.get('la') is not None]
    fixture = os.environ.get('RIVERS_FIXTURE')
    elements, failed = {}, []
    if fixture:
        for el in json.load(open(fixture, encoding='utf-8'))['elements']: elements[el['id']] = el
        src = 'fixture ' + os.path.basename(fixture)
    else:
        tiles = {}
        for p in pts: tiles.setdefault((math.floor(p['la'] / TILE), math.floor(p['lo'] / TILE)), []).append(p)
        print(len(tiles), 'tiles')
        for i, (key, ps) in enumerate(sorted(tiles.items())):
            bbox = (min(p['la'] for p in ps) - PAD, min(p['lo'] for p in ps) - PAD, max(p['la'] for p in ps) + PAD, max(p['lo'] for p in ps) + PAD)
            els = overpass(bbox)
            if els is None:
                failed += ps; print(f'  tile {i + 1}/{len(tiles)} {len(ps)} projects: FAILED, skipped'); continue
            for el in els: elements[el['id']] = el
            print(f'  tile {i + 1}/{len(tiles)} {len(ps)} projects, {len(els)} ways')
            time.sleep(3)
        if len(failed) == len(pts): raise RuntimeError('ดึงข้อมูลจาก OpenStreetMap ไม่ได้ทุกพื้นที่ (เซิร์ฟเวอร์ Overpass ตอบ error) ลองกด Re-run jobs อีกครั้งภายหลัง')
        src = 'OpenStreetMap (Overpass API)'
    ways = []
    for el in elements.values():
        g = [(pt['lat'], pt['lon']) for pt in el.get('geometry', []) if pt]
        t = el.get('tags', {}).get('waterway')
        if g and t in ('river', 'canal'): ways.append({'t': t, 'n': name_of(el.get('tags', {})), 'g': g})
    bad = {p['k'] for p in failed}
    pj = compute([p for p in pts if p['k'] not in bad], ways)
    old_p = os.path.join(DATA, 'rivers.json')
    if bad and os.path.exists(old_p):   # keep last month's values for areas that could not be fetched this time
        old = json.load(open(old_p, encoding='utf-8')).get('pj', {})
        for k in bad:
            if k in old: pj[k] = old[k]
    now = datetime.datetime.now(TH).strftime('%Y-%m-%d %H:%M')
    json.dump({'at': now, 'src': src, 'zones': {'watch2': 3, 'watch': 6}, 'ways': len(ways), 'pj': pj},
              open(os.path.join(DATA, 'rivers.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    layer = map_layer(ways)
    json.dump({'at': now, 'src': src, 'ways': layer}, open(os.path.join(DATA, 'waterways.json'), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    print('map layer:', len(layer), 'waterways,', os.path.getsize(os.path.join(DATA, 'waterways.json')) // 1024, 'KB')
    n3 = sum(1 for r in pj.values() if r.get('rv') and r['rv']['d'] <= 3); n6 = sum(1 for r in pj.values() if r.get('rv') and 3 < r['rv']['d'] <= 6)
    msg = f'ข้อมูล {now} · ทางน้ำ {len(ways)} เส้น · {len(pj)} โครงการ · ห่างแม่น้ำไม่เกิน 3 กม. {n3} · 3–6 กม. {n6}' + (f' · ดึงไม่ได้ {len(bad)} โครงการ (รันใหม่ภายหลังเพื่อเติม)' if bad else '')
    print(now, len(ways), 'ways;', len(pj), 'projects; river <=3 km:', n3, '3-6 km:', n6)
    return msg


if __name__ == '__main__':
    net.run(main, 'คำนวณระยะแม่น้ำและคลอง')

"""Bangkok drainage department water-level stations (weather.bangkok.go.th/water) mapped to PMR projects.

The page lists about 150 stations with the water level inside/outside the gate, the left and right bank
levels and the warning/critical levels (all in m MSL). For each station this script keeps the level, the
bank, and the gap between them in cm (positive = water is below the bank, negative = over the bank).

Mapping to projects:
  1. stations with coordinates (scripts/bma_coords.csv) within RADIUS km of the project
  2. stations without coordinates in the same district (เขต) as the project
The page itself has no coordinates. bma_coords.csv is filled automatically by matching station names with
ThaiWater's Bangkok canal stations; rows that could not be matched are left with empty lat/lon so they can be
filled by hand (manual rows are never overwritten).

Writes docs/data/bma/latest.json and docs/data/bma/<YYYY-MM-DD>.json. Run: python scripts/bma.py
Offline test: BMA_FIXTURE=<saved html> [BMA_TW_FIXTURE=<thaiwater canal json>] python scripts/bma.py
"""
import csv, datetime, difflib, html, json, math, os, re, sys, urllib.request
from html.parser import HTMLParser
import net

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'docs', 'data')
HERE = os.path.dirname(os.path.abspath(__file__))
URL = 'https://weather.bangkok.go.th/water/'
TW_CANAL = 'https://api-v3.thaiwater.net/api/v1/thaiwater30/public/canal_waterlevel'
RADIUS = float(os.environ.get('BMA_RADIUS', '3'))
MAX_AGE_H = 3
TH = datetime.timezone(datetime.timedelta(hours=7))
COORDS = os.path.join(HERE, 'bma_coords.csv')

# header label (whitespace removed) -> field
COLS = {'รหัสสถานี': 'c', 'เขต': 'dist', 'สถานี': 'n', 'วัน-เวลา': 't', 'สถานะ': 's', 'ด้านใน': 'li', 'ด้านนอก': 'lo', 'แม่น้ำ': 'lr',
        'ซ้าย': 'bl', 'ขวา': 'br', 'เตือนภัยด้านใน': 'wi', 'วิกฤติด้านใน': 'ci', 'วิกฤตด้านใน': 'ci', 'เตือนภัยด้านนอก': 'wo',
        'วิกฤติด้านนอก': 'co', 'วิกฤตด้านนอก': 'co', 'เตือนภัยแม่น้ำ': 'wr', 'วิกฤติแม่น้ำ': 'cr', 'วิกฤตแม่น้ำ': 'cr', 'ดูข้อมูล': 'link'}
ORDER = ['c', 'dist', 'n', 't', 's', 'li', 'lo', 'lr', 'bl', 'br', 'wi', 'ci', 'wo', 'co', 'wr', 'cr', 'link']
ICON = {'green': 'ปกติ', 'orange': 'เตือนภัย', 'yellow': 'เตือนภัย', 'red': 'วิกฤต', 'gray': 'ขัดข้อง', 'grey': 'ขัดข้อง', 'black': 'ขัดข้อง'}
# level code: 5 over the bank, 4 critical, 3 warning, 1 normal, 0 fault / no data / stale
LV = {'ล้นตลิ่ง': 5, 'วิกฤต': 4, 'วิกฤติ': 4, 'เตือนภัย': 3, 'ปกติ': 1, 'ขัดข้อง': 0, 'ขัดข้องชั่วคราว': 0}


class Tables(HTMLParser):
    """Collects every table as rows of cells: {'t': text, 'img': [alt/title/src], 'href': [...]}."""
    def __init__(self):
        super().__init__(convert_charrefs=True); self.tables = []; self.stack = []; self.cell = None
    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == 'table': self.stack.append([]); return
        if not self.stack: return
        if tag == 'tr': self.stack[-1].append([])
        elif tag in ('td', 'th') and self.stack[-1]:
            self.cell = {'t': '', 'img': [], 'href': [], 'th': tag == 'th', 'span': int(a.get('colspan') or 1)}; self.stack[-1][-1].append(self.cell)
        elif tag == 'img' and self.cell is not None: self.cell['img'] += [a.get('alt') or '', a.get('title') or '', a.get('src') or '']
        elif tag == 'a' and self.cell is not None and a.get('href'): self.cell['href'].append(a['href'])
        elif tag == 'br' and self.cell is not None: self.cell['t'] += ' '
    def handle_endtag(self, tag):
        if tag in ('td', 'th'): self.cell = None
        elif tag == 'table' and self.stack: self.tables.append(self.stack.pop())
    def handle_data(self, d):
        if self.cell is not None: self.cell['t'] += d


def clean(s): return re.sub(r'\s+', ' ', html.unescape(s or '')).strip()
def key(s): return re.sub(r'\s+', '', s or '')


def num(s):
    s = clean(s).replace(',', '')
    m = re.match(r'^[-+−]?\d*\.?\d+', s.replace('−', '-'))
    return float(m.group(0)) if m else None


def parse_time(s):
    """'08/10/2569 16:00' (Buddhist year) -> datetime (Bangkok local, naive)"""
    m = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2})[:.](\d{2})', s or '')
    if not m: return None
    d, mo, y, h, mi = map(int, m.groups())
    if y > 2400: y -= 543
    try: return datetime.datetime(y, mo, d, h, mi)
    except ValueError: return None


def status_of(cell):
    t = clean(cell['t'])
    for k in sorted(LV, key=len, reverse=True):
        if k in t: return k
    for v in cell['img']:
        v = v.strip()
        for k in sorted(LV, key=len, reverse=True):
            if k in v: return k
        for k, s in ICON.items():
            if k in v.lower(): return s
    return t or None


def parse(page):
    tp = Tables(); tp.feed(page)
    for tb in tp.tables:
        hi = next((i for i, r in enumerate(tb) if any(key(c['t']) == 'รหัสสถานี' for c in r)), None)
        if hi is None: continue
        head = []
        for c in tb[hi]: head += [COLS.get(key(c['t']))] * c['span']
        if head.count(None) > 3 and len(head) == len(ORDER): head = ORDER[:]   # unexpected labels: fall back to the known order
        out = []
        for r in tb[hi + 1:]:
            if not r or all(c['th'] for c in r): continue
            cells = []
            for c in r: cells += [c] * c['span']
            d = {f: cells[j] for j, f in enumerate(head) if f and j < len(cells)}
            code = clean(d['c']['t']) if 'c' in d else ''
            if not re.match(r'^[A-Z]{1,4}\.', code): continue
            st = {'c': code, 'dist': clean(d['dist']['t']) if 'dist' in d else '', 'n': clean(d['n']['t']) if 'n' in d else code,
                  'time': parse_time(d['t']['t']) if 't' in d else None, 'site': status_of(d['s']) if 's' in d else None}
            for f in ('li', 'lo', 'lr', 'bl', 'br', 'wi', 'ci', 'wo', 'co', 'wr', 'cr'):
                st[f] = num(d[f]['t']) if f in d else None
            hrefs = sum((d[f]['href'] for f in ('link', 'n', 'c') if f in d), [])
            m = next((re.search(r'[?&]id=(\d+)', h) for h in hrefs if re.search(r'[?&]id=(\d+)', h)), None)
            st['id'] = int(m.group(1)) if m else None
            out.append(st)
        if out: return out
    return []


def canal_of(name):
    """station name -> canal name, e.g. 'ค.แสนแสบ-วัดทรัพย์ฯ' -> 'คลองแสนแสบ', 'ปตร.คลองสิบ ตอนซอย...' -> 'คลองสิบ'"""
    s = re.sub(r'^(ค\.)', 'คลอง', name.strip())
    s = re.sub(r'(^|[\s.])ค\.', r'\1คลอง', s)
    m = re.search(r'คลอง[^\s\-–(*,]+', s)
    if not m: return ''
    c = re.sub(r'(ตอน|บริเวณ|ที่|หน้า|ถนน|ถ\.|ซอย|วัด).*$', '', m.group(0))
    return c if len(c) > 4 else ''


def norm_name(s):
    s = re.sub(r'^(ค\.|คลอง|ส\.|สถานีสูบน้ำ|ปตร\.|ประตูระบายน้ำ)', '', (s or '').strip())
    s = re.sub(r'(คลอง|ค\.|ตอน|บริเวณ|สถานีสูบน้ำ|ประตูระบายน้ำ|ปตร\.|ถนน|ถ\.|ซอย|ซ\.|\*|ฯ|[\s\-–(),.])', '', s)
    return s


def level_of(st, now):
    """returns (level code, label, gap to bank in cm, gap to critical in cm)"""
    li = st['li']
    banks = [b for b in (st['bl'], st['br']) if b is not None]
    bank = min(banks) if banks else None
    gap = round((bank - li) * 100) if li is not None and bank is not None else None
    gc = round((st['ci'] - li) * 100) if li is not None and st['ci'] is not None else None
    age = (now - st['time']).total_seconds() / 3600 if st['time'] else None
    if li is None: return 0, st['site'] or 'ไม่มีข้อมูล', gap, gc, bank
    if age is not None and age > MAX_AGE_H: return 0, f'ข้อมูลค้าง {int(age)} ชม.', gap, gc, bank
    if gap is not None and gap <= 0: return 5, 'ล้นตลิ่ง', gap, gc, bank
    lv = LV.get(st['site'] or '')
    if lv is None or lv == 0:   # site gave no usable status: use the thresholds
        lv = 4 if st['ci'] is not None and li >= st['ci'] else 3 if st['wi'] is not None and li >= st['wi'] else 1
    return lv, {5: 'ล้นตลิ่ง', 4: 'วิกฤต', 3: 'เตือนภัย', 1: 'ปกติ'}.get(lv, st['site'] or ''), gap, gc, bank


def dist_km(la1, lo1, la2, lo2):
    r = math.pi / 180
    a = math.sin((la2 - la1) * r / 2) ** 2 + math.cos(la1 * r) * math.cos(la2 * r) * math.sin((lo2 - lo1) * r / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def in_ring(x, y, ring):
    c = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1: c = not c
    return c


def district_of(la, lo, dists):
    for d in dists:
        if any(in_ring(lo, la, r) for r in d['r']): return d['n']
    return ''


def dnorm(s): return re.sub(r'^(เขต|อำเภอ|อ\.)\s*', '', (s or '').strip())


def load_coords():
    rows = {}
    if os.path.exists(COORDS):
        for r in csv.DictReader(open(COORDS, encoding='utf-8-sig')): rows[r['code']] = r
    return rows


def save_coords(rows):
    with open(COORDS, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['code', 'name', 'district', 'lat', 'lon', 'source', 'matched_to'])
        w.writeheader()
        for k in sorted(rows): w.writerow({f: rows[k].get(f, '') for f in w.fieldnames})


def thaiwater_canals():
    fx = os.environ.get('BMA_TW_FIXTURE')
    try:
        if fx: raw = json.load(open(fx, encoding='utf-8'))
        elif os.environ.get('BMA_FIXTURE'): return []
        else:
            raw = json.loads(net.get(TW_CANAL, headers={'Accept': 'application/json'}).decode('utf-8'))
    except Exception as ex:
        print('ThaiWater canal list not available:', ex); return []
    data = raw if isinstance(raw, list) else raw.get('data', []) if isinstance(raw, dict) else []
    out = []
    for r in data:
        s = r.get('station') or {}
        n = s.get('canal_name'); n = n.get('th') if isinstance(n, dict) else n
        try: la, lo = float(s.get('canal_lat')), float(s.get('canal_long'))
        except (TypeError, ValueError): continue
        if n: out.append({'n': n, 'la': la, 'lo': lo, 'code': s.get('canal_oldcode') or ''})
    return out


def match_coords(stations, coords, canals):
    """fill coordinates for stations not in bma_coords.csv yet (or auto rows that are still empty)"""
    pool = [(norm_name(c['n']), c) for c in canals]
    added = 0
    for st in stations:
        r = coords.get(st['c'])
        if r and (r.get('lat') or r.get('source') == 'manual'): continue
        best, score = None, 0
        nn = norm_name(st['n'])
        for k, c in pool:
            if not k or not nn: continue
            sc = difflib.SequenceMatcher(None, nn, k).ratio()
            if sc > score: best, score = c, sc
        r = {'code': st['c'], 'name': st['n'], 'district': st['dist'], 'lat': '', 'lon': '', 'source': '', 'matched_to': ''}
        if best and score >= 0.72:
            r.update(lat=f"{best['la']:.5f}", lon=f"{best['lo']:.5f}", source=f'auto {score:.2f}', matched_to=best['n']); added += 1
        coords[st['c']] = r
    return added


def main():
    now = datetime.datetime.now(TH).replace(tzinfo=None)
    fx = os.environ.get('BMA_FIXTURE')
    if fx: page = open(fx, encoding='utf-8').read()
    else:
        page = net.get(URL, headers={'Accept-Language': 'th,en;q=0.8', 'Accept': 'text/html'}).decode('utf-8', 'replace')
    stations = parse(page)
    if not stations:
        os.makedirs(os.path.join(ROOT, 'debug'), exist_ok=True)
        open(os.path.join(ROOT, 'debug', 'bma_page.html'), 'w', encoding='utf-8').write(page)
        sys.exit(f'เปิดเว็บได้แต่ไม่พบตารางจุดวัด (หน้าเว็บยาว {len(page)} ตัวอักษร เก็บไว้ใน Artifacts ชื่อ bma-page)')
    coords = load_coords()
    added = match_coords(stations, coords, thaiwater_canals())
    save_coords(coords)
    dists = json.load(open(os.path.join(HERE, 'bkk_districts.json'), encoding='utf-8'))
    projects = json.load(open(os.path.join(DATA, 'master.json'), encoding='utf-8'))['projects']

    S = []
    for st in stations:
        lv, lab, gap, gc, bank = level_of(st, now)
        c = coords.get(st['c'], {})
        la = float(c['lat']) if c.get('lat') else None; lo = float(c['lon']) if c.get('lon') else None
        S.append({'c': st['c'], 'id': st['id'], 'n': st['n'], 'k': canal_of(st['n']), 'd': dnorm(st['dist']),
                  't': st['time'].strftime('%Y-%m-%d %H:%M') if st['time'] else None, 'site': st['site'], 'lv': lv, 's': lab,
                  'l': st['li'], 'lo': st['lo'], 'b': bank, 'bl': st['bl'], 'br': st['br'], 'w': st['wi'], 'cr': st['ci'],
                  'g': gap, 'gc': gc, 'la': la, 'ln': lo, 'cs': (c.get('source') or '').split(' ')[0] or None})

    pj = {}
    for p in projects:
        d = dnorm(p.get('d'))
        la, lo = p.get('la'), p.get('lo')
        if la is not None and (not d or 'กรุงเทพ' in (p.get('pv') or '')):
            d2 = district_of(la, lo, dists)
            if d2: d = d2
        hits = []
        for j, s in enumerate(S):
            if s['la'] is not None and la is not None:
                km = dist_km(la, lo, s['la'], s['ln'])
                if km <= RADIUS: hits.append((j, round(km, 2), 'r'))
            elif s['la'] is None and d and s['d'] and s['d'] == d:
                hits.append((j, None, 'd'))
        if not hits: continue
        hits.sort(key=lambda h: (-S[h[0]]['lv'], S[h[0]]['g'] if S[h[0]]['g'] is not None else 9999, h[1] if h[1] is not None else 99))
        top = S[hits[0][0]]
        gaps = [S[h[0]]['g'] for h in hits if S[h[0]]['g'] is not None and S[h[0]]['lv'] > 0]
        pj[p['k']] = {'lv': top['lv'], 'g': min(gaps) if gaps else None, 'dist': d, 'st': [list(h) for h in hits[:8]], 'n': len(hits)}

    lvc = {k: sum(1 for s in S if s['lv'] == k) for k in (5, 4, 3, 1, 0)}
    out = {'at': now.strftime('%Y-%m-%d %H:%M'), 'src': URL, 'radius': RADIUS, 'n': len(S),
           'withxy': sum(1 for s in S if s['la'] is not None), 'lvc': lvc, 'stations': S, 'pj': pj}
    os.makedirs(os.path.join(DATA, 'bma'), exist_ok=True)
    for fn in ('latest.json', now.strftime('%Y-%m-%d') + '.json'):
        json.dump(out, open(os.path.join(DATA, 'bma', fn), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    idx_p = os.path.join(DATA, 'bma', 'index.json')
    idx = json.load(open(idx_p, encoding='utf-8')) if os.path.exists(idx_p) else {'days': []}
    day = now.strftime('%Y-%m-%d'); idx['days'] = [x for x in idx['days'] if x['day'] != day] + [{'day': day, 'at': out['at']}]
    json.dump(idx, open(idx_p, 'w', encoding='utf-8'), ensure_ascii=False)
    msg = f"ข้อมูล {out['at']} · {len(S)} จุดวัด · มีพิกัด {out['withxy']} · จับคู่โครงการ {len(pj)} · ล้นตลิ่ง {lvc[5]} วิกฤต {lvc[4]} เตือนภัย {lvc[3]}"
    print(out['at'], len(S), 'stations;', out['withxy'], 'with coordinates (+', added, 'matched now);', len(pj), 'projects mapped; levels', lvc)
    return msg


if __name__ == '__main__':
    net.run(main, 'ดึงระดับน้ำคลอง กทม.')

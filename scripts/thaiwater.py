"""Fetch ThaiWater water levels and map alert stations to PMR projects.

Same rules as PMR_ThaiWater_Mapping_PowerQuery.xlsx:
- river stations (waterlevel_load): alert radius 10 km, ignored when older than 24 h
- Bangkok canal stations (canal_waterlevel): alert radius 3 km, ignored when older than 6 h,
  level >= bank or >= critical = 5 (ล้นตลิ่ง / ถึงระดับวิกฤต กทม.), >= warning = 4
- per project: the nearest overflowing station in radius is the primary alert,
  the nearest high-water station in radius is supplementary

Writes docs/data/water/latest.json, docs/data/water/<YYYY-MM-DD>.json (last run of the day)
and docs/data/water/index.json. Run: python scripts/thaiwater.py
"""
import json, math, os, sys, datetime, urllib.request
import net

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'docs', 'data')
BASE = 'https://api-v3.thaiwater.net/api/v1/thaiwater30/public/'
RADIUS_RIVER, RADIUS_CANAL, AGE_RIVER, AGE_CANAL = 10, 3, 24, 6
TH = datetime.timezone(datetime.timedelta(hours=7))


def fetch(name):
    return json.loads(net.get(BASE + name, headers={'Accept': 'application/json'}).decode('utf-8'))


def rows(raw):
    if isinstance(raw, list): return raw
    if isinstance(raw, dict):
        if isinstance(raw.get('data'), list): return raw['data']
        for v in raw.values():
            if isinstance(v, dict) and isinstance(v.get('data'), list): return v['data']
    return []


def rec(v): return v if isinstance(v, dict) else {}


def num(v):
    if v is None or isinstance(v, bool): return None
    if isinstance(v, (int, float)): return float(v)
    try: return float(str(v).replace(',', ''))
    except ValueError: return None


def th(r, f):
    v = rec(r).get(f)
    if isinstance(v, dict): return v.get('th') if v.get('th') is not None else v.get('en')
    return v if isinstance(v, str) else None


def txt(v): return None if v is None else str(v)


def parse_dt(t):
    if not isinstance(t, str) or len(t) < 16: return None
    try: return datetime.datetime(int(t[0:4]), int(t[5:7]), int(t[8:10]), int(t[11:13]), int(t[14:16]))
    except ValueError: return None


def dist_km(la1, lo1, la2, lo2):
    r = math.pi / 180
    a = math.sin((la2 - la1) * r / 2) ** 2 + math.cos(la1 * r) * math.cos(la2 * r) * math.sin((lo2 - lo1) * r / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def river_stations(raw):
    out = []
    for r in raw:
        st = rec(r.get('station')); level = num(r.get('waterlevel_msl')); bank = num(st.get('min_bank')); sit = num(r.get('situation_level'))
        out.append(dict(type='แม่น้ำ/ลำน้ำ', code=txt(st.get('tele_station_oldcode')), name=th(st, 'tele_station_name'), river=txt(r.get('river_name')),
                        lat=num(st.get('tele_station_lat')), lon=num(st.get('tele_station_long')), level=level, bank=bank,
                        diff=round(level - bank, 2) if level is not None and bank is not None else None, pct=num(r.get('storage_percent')),
                        sit=int(sit) if sit is not None else None,
                        status={5: 'ล้นตลิ่ง', 4: 'น้ำมาก', 3: 'ปกติ', 2: 'น้ำน้อย', 1: 'น้ำน้อยวิกฤต'}.get(int(sit) if sit is not None else None),
                        time=parse_dt(r.get('waterlevel_datetime')), radius=RADIUS_RIVER, maxage=AGE_RIVER))
    return out


def canal_stations(raw):
    out = []
    for r in raw:
        st = rec(r.get('station')); level = num(r.get('canal_value')); bank = num(st.get('bank')); warn = num(st.get('warning_level')); crit = num(st.get('critical_level'))
        has = level is not None and (bank is not None or warn is not None or crit is not None)
        o_bank = level is not None and bank is not None and level >= bank
        o_crit = level is not None and crit is not None and level >= crit
        o_warn = level is not None and warn is not None and level >= warn
        sit = None if not has else 5 if (o_bank or o_crit) else 4 if o_warn else 3
        out.append(dict(type='คลอง กทม.', code=txt(st.get('canal_oldcode')), name=th(st, 'canal_name'), river=th(st, 'canal_name'),
                        lat=num(st.get('canal_lat')), lon=num(st.get('canal_long')), level=level, bank=bank,
                        diff=round(level - bank, 2) if level is not None and bank is not None else None, pct=None, sit=sit,
                        status=('ล้นตลิ่ง' if o_bank else 'ถึงระดับวิกฤต กทม.') if sit == 5 else 'ถึงระดับเตือนภัย กทม.' if sit == 4 else 'ปกติ' if sit == 3 else None,
                        time=parse_dt(r.get('canal_datetime')), radius=RADIUS_CANAL, maxage=AGE_CANAL))
    return out


def fmt(t): return t.strftime('%Y-%m-%d %H:%M') if t else None
def r2(x): return None if x is None else round(x, 2)


def build(projects, river_raw, canal_raw, now, canal_ok=True):
    st_all = river_stations(river_raw) + canal_stations(canal_raw)
    usable = [s for s in st_all if s['lat'] and s['lon'] and s['sit'] is not None and s['time'] is not None and (now - s['time']).total_seconds() / 3600 <= s['maxage']]
    alert = [s for s in usable if s['sit'] >= 4]
    note = f"แม่น้ำ/ลำน้ำ {len(river_raw)} สถานี | คลอง กทม. {str(len(canal_raw)) + ' สถานี' if canal_ok else 'ดึงข้อมูลไม่ได้'} | ใช้ได้ (ข้อมูลไม่ค้าง) {len(usable)} สถานี | อยู่ในเกณฑ์เตือน {len(alert)} สถานี"
    pj = {}
    for p in projects:
        la, lo = p.get('la'), p.get('lo')
        if la is None or lo is None:
            pj[p['k']] = {'a': 'nocoord', 'at': 'ไม่มีพิกัด'}; continue
        w = [(s, dist_km(la, lo, s['lat'], s['lon'])) for s in alert]
        l5 = [x for x in w if x[0]['sit'] == 5]; l4 = [x for x in w if x[0]['sit'] == 4]
        in5 = [x for x in l5 if x[1] <= x[0]['radius']]; in4 = [x for x in l4 if x[1] <= x[0]['radius']]
        near = lambda l: min(l, key=lambda x: x[1]) if l else None
        pp, ss, n5, n4 = near(in5), near(in4), near(l5), near(l4)
        call = sum(1 for u in usable if abs(u['lat'] - la) <= u['radius'] / 100 and abs(u['lon'] - lo) <= u['radius'] / 100 and dist_km(la, lo, u['lat'], u['lon']) <= u['radius'])
        a = 'over' if in5 else 'high' if in4 else 'norm' if call else 'none'
        t = {'a': a, 'at': {'over': 'ล้นตลิ่งในรัศมี', 'high': 'น้ำมากในรัศมี', 'norm': 'สถานีในรัศมีปกติ', 'none': 'ไม่มีสถานีในรัศมี'}[a], 'nov': len(in5), 'nhi': len(in4), 'nall': call}
        if pp:
            s, d = pp; t.update(on=s['name'], oc=s['code'] or '', orv=s['river'] or '', od=r2(d), oo=s['diff'], ol=s['level'], ob=s['bank'], op=r2(s['pct']), ot=fmt(s['time']), oty=s['type'])
        if ss:
            s, d = ss; t.update(hn=s['name'], hrv=s['river'] or '', hd=r2(d), hx=s['diff'], ht=fmt(s['time']), hty=s['type'])
        if n5: t['no'] = r2(n5[1])
        if n4: t['nh'] = r2(n4[1])
        pj[p['k']] = t
    stations = [dict(n=s['name'], c=s['code'] or '', ty=s['type'], rv=s['river'] or '', la=s['lat'], lo=s['lon'], sit=s['sit'], s=s['status'],
                     l=s['level'], b=s['bank'], x=s['diff'], pc=r2(s['pct']), t=fmt(s['time'])) for s in alert]
    return {'at': fmt(now), 'note': note, 'src': 'api-v3.thaiwater.net', 'stations': stations, 'pj': pj}


def main():
    now = datetime.datetime.now(TH).replace(tzinfo=None, second=0, microsecond=0)
    projects = json.load(open(os.path.join(DATA, 'master.json'), encoding='utf-8'))['projects']
    river = rows(fetch('waterlevel_load'))
    if not river: sys.exit('waterlevel_load returned no rows')
    try: canal, ok = rows(fetch('canal_waterlevel')), True
    except Exception as e: canal, ok = [], False; print('canal_waterlevel failed:', e)
    snap = build(projects, river, canal, now, ok)
    wd = os.path.join(DATA, 'water'); os.makedirs(wd, exist_ok=True)
    day = snap['at'][:10]
    for fn in ('latest.json', day + '.json'):
        json.dump(snap, open(os.path.join(wd, fn), 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
    ip = os.path.join(wd, 'index.json')
    idx = json.load(open(ip, encoding='utf-8')) if os.path.exists(ip) else {'days': []}
    idx['days'] = sorted([d for d in idx['days'] if d['day'] != day] + [{'day': day, 'at': snap['at']}], key=lambda d: d['day'])[-120:]
    json.dump(idx, open(ip, 'w', encoding='utf-8'), ensure_ascii=False)
    c = {}
    for t in snap['pj'].values(): c[t['a']] = c.get(t['a'], 0) + 1
    print(snap['at'], snap['note'], c)
    return f"ข้อมูล {snap['at']} · {snap['note']}"


if __name__ == '__main__':
    net.run(main, 'ดึงระดับน้ำ ThaiWater')

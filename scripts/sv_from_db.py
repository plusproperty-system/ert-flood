"""Rebuild docs/data/survey.json from the claude.ai dashboard's shared survey (artifact db collection "sv").

python3 scripts/sv_from_db.py <dir with the sv/*.json documents saved by ArtifactData list/query out_dir>
Prints NEW <at> when it wrote a newer survey, SAME <at> when the repo already has it.
Only numbers, status, equipment and building facts (fx: level vs road, basement, automated parking) go public:
no resident counts, no site notes (chk), no staff.
"""
import glob, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'docs', 'data', 'survey.json')


def comb(I, J, K, L):
    if any(v is not None and v < 0 for v in (I, J, K)):
        return 'กรอกข้อมูลผิดพลาด'   # negative water level = entry error
    i, j, k, l = I or 0, J or 0, K or 0, 1 if L == 1 else 0
    if i == 15 and j == 50: return 'เฝ้าระวังพิเศษ'
    if l == 1 or k >= 60: return 'วิกฤตรุนแรง'
    if k > 0 or i >= 15: return 'วิกฤต'
    if i <= 0 and (j == 0 or j > 50): return 'ปกติ'
    if j >= 30: return 'เฝ้าระวัง'
    return 'วิกฤต'


docs = []
for f in glob.glob(os.path.join(sys.argv[1], '**', '*.json'), recursive=True):
    d = json.load(open(f, encoding='utf-8'))
    d = d.get('data', d)
    if isinstance(d, dict) and d.get('json') and d.get('tag'): docs.append(d)
if not docs: sys.exit('no survey documents found')
tag = max(d['tag'] for d in docs)
mine = sorted((d for d in docs if d['tag'] == tag), key=lambda d: d['i'])
if len(mine) != mine[0]['n']: sys.exit(f'incomplete upload: {len(mine)} of {mine[0]["n"]} parts')
pj = {}
for d in mine: pj.update(json.loads(d['json']))

master = {m['io'] + '|' + m['n']: m['k'] for m in json.load(open(os.path.join(ROOT, 'docs', 'data', 'master.json'), encoding='utf-8'))['projects']}
rows, eq, rq, fx = {}, {}, {}, {}
# building facts for the 6-category risk export: basement, basement parking/plant room/lift, automated parking
FX = {'bsm': 'bs', 'bpark': 'bp', 'broom': 'br', 'blift': 'bl', 'apark': 'ap'}
for key, o in pj.items():
    k = master.get(key, key)
    if 'I' in o:
        r = {x: o[x] for x in ('at', 'I', 'J', 'K', 'L') if o.get(x) is not None}
        r['st'] = comb(o.get('I'), o.get('J'), o.get('K'), o.get('L'))
        rows[k] = r
    if o.get('eq'):
        eq[k], rq[k] = o['eq'], o.get('rq') or {}
    g, f = o.get('gen') or {}, {}
    if isinstance(g.get('elev'), (int, float)) and not isinstance(g.get('elev'), bool): f['e'] = g['elev']   # project level vs road (cm)
    for src, dst in FX.items():
        v = str(g.get(src) or '').strip()
        if v.startswith('มี'): f[dst] = 1
        elif v.startswith('ไม่'): f[dst] = 0
    if f: fx[k] = f
at =max([r.get('at', '') for r in rows.values()] + [mine[0].get('at', '')])
old = json.load(open(OUT, encoding='utf-8')) if os.path.exists(OUT) else {}
if str(old.get('at', '')) >= at:
    print('SAME', old.get('at')); sys.exit(0)
s = json.dumps({'at': at, 'file': mine[0].get('file', ''), 'rows': rows, 'eq': eq, 'rq': rq, 'fx': fx}, ensure_ascii=False, separators=(',', ':'))
for bad in ('"gen"', '"chk"', '"res"', '"vul"'): assert bad not in s, bad
open(OUT, 'w', encoding='utf-8').write(s)
print('NEW', at)

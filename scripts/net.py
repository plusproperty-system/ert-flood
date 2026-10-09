"""Shared helpers for the GitHub Actions jobs.

get(url)     download with fallbacks: normal HTTPS -> certifi CA bundle -> curl -> plain http (for sites whose
             certificate chain is incomplete or that reject Python's TLS settings). Every attempt is logged.
run(main, t) run a job, print a readable result (Thai) on the run's Summary page, exit 1 on failure.
"""
import os, ssl, subprocess, sys, time, traceback, urllib.request

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 pmr-ert-flood'
TRIED = []


def _urllib(url, data, headers, timeout, ctx):
    req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, **headers})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def get(url, data=None, headers=None, timeout=90):
    headers = headers or {}
    errs = []
    tries = [('https', url, None)]
    try:
        import certifi
        tries.append(('certifi', url, ssl.create_default_context(cafile=certifi.where())))
    except ImportError:
        pass
    tries.append(('curl', url, None))
    if url.startswith('https://'): tries.append(('http', 'http://' + url[8:], None))
    for name, u, ctx in tries:
        try:
            if name == 'curl':
                cmd = ['curl', '-sS', '-L', '--compressed', '--max-time', str(timeout), '-A', UA, '-f']
                for k, v in headers.items(): cmd += ['-H', f'{k}: {v}']
                if data is not None: cmd += ['--data-binary', '@-']
                out = subprocess.run(cmd + [u], input=data, capture_output=True, timeout=timeout + 15)
                if out.returncode: raise RuntimeError(out.stderr.decode('utf-8', 'replace').strip() or f'curl exit {out.returncode}')
                body = out.stdout
            else:
                body = _urllib(u, data, headers, timeout, ctx)
            TRIED.append(f'{name}: ok')
            print(f'  {name} ok {u} ({len(body)} bytes)')
            return body
        except Exception as e:
            msg = f'{type(e).__name__}: {e}'[:300]
            errs.append(f'{name}: {msg}'); TRIED.append(f'{name}: {msg}')
            print(f'  {name} failed {u}: {msg}')
            time.sleep(2)
    raise RuntimeError('เชื่อมต่อไม่ได้ทุกวิธี | ' + ' | '.join(errs))


def summary(md):
    p = os.environ.get('GITHUB_STEP_SUMMARY')
    if p:
        with open(p, 'a', encoding='utf-8') as f: f.write(md + '\n')


def run(main, title):
    try:
        result = main()
        summary(f'### ✅ {title}: สำเร็จ\n{result or ""}')
    except BaseException as e:
        if isinstance(e, SystemExit) and e.code in (0, None): raise
        msg = str(e) if not isinstance(e, SystemExit) else str(e.code)
        traceback.print_exc()
        tried = '\n'.join(f'- {t}' for t in TRIED[-8:])
        summary(f'### ❌ {title}: ไม่สำเร็จ\n**สาเหตุ:** {msg[:800]}\n\n**วิธีที่ลองเชื่อมต่อ:**\n{tried or "- (ยังไม่ได้เชื่อมต่อ)"}\n\nแคปหน้านี้ส่งให้ผู้ดูแลได้เลย')
        sys.exit(1)

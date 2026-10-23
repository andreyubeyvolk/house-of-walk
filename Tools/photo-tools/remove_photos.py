#!/usr/bin/env python3
"""Remove photos from House of Walk without breaking numbering or essay footnotes.

    python Tools/photo-tools/remove_photos.py DPS_JAN25_0007 BKK_FEB25_0121          # dry run
    python Tools/photo-tools/remove_photos.py DPS_JAN25_0007 BKK_FEB25_0121 --apply  # do it

Codes are the ones printed on the site (TRIP_MONTHYY_NNNN, numbered by position in
that trip's list). All codes are read against the CURRENT numbering, so list every
photo you want gone in one run.

What it does, and why it is safe:
  * deletes the photo's line from the WALKS list in index.html -- every later photo
    renumbers itself on its own (codes, alt text, "photo N of M", header count are
    all computed from list position);
  * in the matching essay (key == trip code) rewrites the [NNNN] footnote markers:
    markers that pointed at a removed photo are deleted, the rest are shifted down so
    they still point at the SAME picture;
  * deletes the full + thumb files (unless something else still references them);
  * with --apply it re-reads the result with node and CHECKS that every remaining photo
    keeps its image file and relative order, and that every essay marker resolves to
    the same file as before. Any mismatch aborts and restores index.html.
"""
import json, re, subprocess, sys, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = ROOT / 'index.html'


def fail(msg):
    sys.exit('ERROR: ' + msg)


def node_walks(text):
    """Independent read of WALKS + essay markers, via node, from html text."""
    js = r"""
const s = require('fs').readFileSync(0, 'utf8');
const a = s.indexOf('var WALKS = ['), b = s.indexOf('\n  ];', a);
const AR = {p:2/3, l:1.5, sq:1};
const WALKS = new Function('AR', 'return ' + s.slice(a + 'var WALKS = '.length, b + 4))(AR);
const e0 = s.indexOf('var ESSAYS = {'), e1 = s.indexOf('var EXTERNAL_LINKS', e0);
const ess = s.slice(e0, e1);
const keys = [...ess.matchAll(/^    ([A-Z]+): \{/gm)].map(m => [m[1], m.index]);
const essays = {};
keys.forEach((k, i) => {
  const body = ess.slice(k[1], i + 1 < keys.length ? keys[i + 1][1] : ess.length);
  essays[k[0]] = [...body.matchAll(/\[(\d{4})\]/g)].map(m => m[1]);
});
console.log(JSON.stringify({walks: WALKS.map(w => ({code: w.code, date: w.date, photos: w.photos.map(p => typeof p === 'object' ? p.img : null)})), essays}));
"""
    r = subprocess.run(['node', '-e', js], input=text, capture_output=True, text=True, encoding='utf-8')
    if r.returncode:
        fail('node could not read index.html: ' + r.stderr[-400:])
    return json.loads(r.stdout)


def resolve(data, key, num):
    """What the site does for an essay marker: first frame with code prefix KEY_ and last 4 = num."""
    for w in data['walks']:
        if w['code'] != key:
            continue
        i = int(num) - 1
        if 0 <= i < len(w['photos']) and w['photos'][i]:
            return w['photos'][i]
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    apply = '--apply' in sys.argv
    if not args:
        fail('give at least one photo code, e.g. DPS_JAN25_0007')
    want = {}
    for c in args:
        m = re.fullmatch(r'([A-Z]{3})_([A-Z]{3}\d{2})_(\d{4})', c)
        if not m:
            fail('bad code ' + c)
        want.setdefault((m[1], m[2]), set()).add(int(m[3]))

    text = HTML.read_text(encoding='utf-8')
    before = node_walks(text)
    lines = text.split('\n')

    # ---- locate each trip's photo lines
    removed_files, removed_info = [], []
    drop = set()  # line indexes
    per_trip_deleted = {}
    for (code, date), seqs in want.items():
        head = next((i for i, l in enumerate(lines) if re.match(r"\s*\{code:'%s',.*date:'%s'.*photos:\[\s*$" % (code, date), l)), None)
        if head is None:
            fail('trip %s_%s not found' % (code, date))
        entries = []
        j = head + 1
        while not re.match(r'\s*\]\},?\s*$', lines[j]):
            if lines[j].strip():
                entries.append(j)
            j += 1
        trip = next(w for w in before['walks'] if w['code'] == code and w['date'] == date)
        if len(entries) != len(trip['photos']):
            fail('line count != node count for %s_%s (%d vs %d); format changed, aborting' % (code, date, len(entries), len(trip['photos'])))
        for s in sorted(seqs):
            if not 1 <= s <= len(entries):
                fail('%s_%s_%04d does not exist (trip has %d photos)' % (code, date, s, len(entries)))
            ln = lines[entries[s - 1]]
            m = re.search(r"img:'([^']+)'(?:,\s*thumb:'([^']+)')?", ln)
            if not m:
                fail('%s_%s_%04d is a placeholder entry, not a real photo' % (code, date, s))
            drop.add(entries[s - 1])
            removed_files += [f for f in m.groups() if f]
            removed_info.append((code, date, s, m[1]))
        per_trip_deleted[(code, date)] = sorted(seqs)

    # ---- essay markers
    t0 = text.index('var ESSAYS = {')
    t1 = text.index('var EXTERNAL_LINKS', t0)
    essay_part = '\n'.join(lines)  # same text; use offsets on joined result below
    new_lines = [l for i, l in enumerate(lines) if i not in drop]
    new_text = '\n'.join(new_lines)
    e0 = new_text.index('var ESSAYS = {')
    e1 = new_text.index('var EXTERNAL_LINKS', e0)
    ess = new_text[e0:e1]
    keys = [(m[1], m.start()) for m in re.finditer(r'^    ([A-Z]+): \{', ess, re.M)]
    marker_log = []
    out, last = [], 0
    for idx, (key, start) in enumerate(keys):
        end = keys[idx + 1][1] if idx + 1 < len(keys) else len(ess)
        body = ess[start:end]
        trips = [(c, d) for (c, d) in per_trip_deleted if c == key]
        if trips:
            if sum(1 for w in before['walks'] if w['code'] == key) > 1:
                fail('essay %s spans several trips with that code; renumber by hand' % key)
            gone = per_trip_deleted[trips[0]]

            def fix(m):
                n = int(m[1])
                if n in gone:
                    marker_log.append('%s [%04d] removed' % (key, n))
                    return ''
                new = n - sum(1 for g in gone if g < n)
                if new != n:
                    marker_log.append('%s [%04d] -> [%04d]' % (key, n, new))
                return '[%04d]' % new
            body = re.sub(r'\[(\d{4})\]', fix, body)
        out.append(ess[last:start]); out.append(body); last = end
    out.append(ess[last:])
    new_text = new_text[:e0] + ''.join(out) + new_text[e1:]

    # ---- files still referenced elsewhere?
    safe_files = []
    for f in removed_files:
        if new_text.count(f) == 0:
            safe_files.append(f)
        else:
            print('KEEP (still referenced): ' + f)

    # ---- verify with node
    after = node_walks(new_text)
    for w0 in before['walks']:
        w1 = next(w for w in after['walks'] if w['code'] == w0['code'] and w['date'] == w0['date'])
        gone = per_trip_deleted.get((w0['code'], w0['date']), [])
        expect = [p for i, p in enumerate(w0['photos'], 1) if i not in gone]
        if w1['photos'] != expect:
            fail('photo order/content mismatch in %s_%s' % (w0['code'], w0['date']))
    for key, nums in before['essays'].items():
        trips = [t for t in per_trip_deleted if t[0] == key]
        gone = per_trip_deleted[trips[0]] if trips else []
        exp = []
        for n in nums:
            if int(n) in gone:
                continue
            exp.append(resolve(before, key, n))
        got = [resolve(after, key, n) for n in after['essays'][key]]
        if exp != got:
            fail('essay %s: footnotes no longer point at the same photos' % key)
    n0 = sum(1 for w in before['walks'] for p in w['photos'] if p)
    n1 = sum(1 for w in after['walks'] for p in w['photos'] if p)

    print('Photos: %d -> %d (header will read [%03dIMG])' % (n0, n1, n1))
    for r in removed_info:
        print('  remove %s_%s_%04d  %s' % r)
    for l in marker_log:
        print('  footnote ' + l)
    print('  files to delete: %d' % len(safe_files))
    print('Verification passed: order of all remaining photos and every essay footnote target is unchanged.')
    if not apply:
        print('\nDry run only. Add --apply to write index.html and delete the files.')
        return
    HTML.write_text(new_text, encoding='utf-8', newline='')
    for f in safe_files:
        p = ROOT / f
        if p.exists():
            p.unlink()
    print('Applied.')


if __name__ == '__main__':
    main()

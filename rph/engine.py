"""RPH engine: read module files into a lesson bank, and fill a school RPH template for one week.

Everything works on plain Python data (dicts, lists, bytes), so the web app and the command line share it.
"""
import copy
import datetime as dt
import io
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter

import docx
from docx.shared import Cm

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
XML_SPACE = '{http://www.w3.org/XML/1998/namespace}space'
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
FOCUS_BY_SKILL = {'1': 'Listening', '2': 'Speaking', '3': 'Reading', '4': 'Writing', '5': 'Language Arts'}
FOCUS_DROPDOWN = {'Language Arts': 'Language Art'}


class RPHError(Exception):
    """A problem the teacher can fix (shown as a message, not a crash)."""


# ============================================================ XML helpers
def T(el):
    return ''.join(t.text or '' for t in el.iter(W + 't'))


def clean(s):
    return re.sub(r'[\xa0\s]+', ' ', s).strip()


def paras_text(tc):
    return [s for s in (clean(T(p)) for p in tc.iter(W + 'p')) if s]


def rows_of(tbl):
    out = []
    for tr in tbl.iter(W + 'tr'):
        p = tr.getparent()
        while p.tag != W + 'tbl':
            p = p.getparent()
        if p is tbl:
            out.append(tr)
    return out


def cells_of(tr):
    out = []
    for tc in tr.iter(W + 'tc'):
        p = tc.getparent()
        while p.tag != W + 'tr':
            p = p.getparent()
        if p is tr:
            out.append(tc)
    return out


# ============================================================ .doc -> .docx
def to_docx_bytes(name, data):
    """Return .docx bytes. Old .doc files are converted with LibreOffice."""
    if name.lower().endswith('.docx'):
        return data
    if not name.lower().endswith('.doc'):
        raise RPHError(f'{name}: please upload Word files (.doc or .docx).')
    soffice = shutil.which('soffice') or shutil.which('libreoffice')
    if not soffice:
        raise RPHError(f'{name}: this is an old .doc file. Open it in Word and "Save As" .docx, then upload again.')
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'in.doc')
        with open(src, 'wb') as f:
            f.write(data)
        subprocess.run([soffice, '--headless', '--convert-to', 'docx', '--outdir', d, src],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        with open(os.path.join(d, 'in.docx'), 'rb') as f:
            return f.read()


def to_pdf_bytes(docx_bytes):
    soffice = shutil.which('soffice') or shutil.which('libreoffice')
    if not soffice:
        raise RPHError('PDF needs LibreOffice on the server.')
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, 'rph.docx')
        with open(src, 'wb') as f:
            f.write(docx_bytes)
        subprocess.run([soffice, '--headless', '--convert-to', 'pdf', '--outdir', d, src],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
        with open(os.path.join(d, 'rph.pdf'), 'rb') as f:
            return f.read()


# ============================================================ lesson bank
HEAD = re.compile(r'(pre\s*-?\s*lesson|lesson\s+delivery|post\s*-?\s*lesson)\s*:?', re.I)


def _strip_num(s):
    return re.sub(r'^\s*\d+\s*([.)]\s*|\s+(?=[A-Z]))', '', s).strip()


def _main_comp(lines):
    txt = ' '.join(lines)
    m = re.match(r'\s*Main\s*:?\s*(.*?)\s*Compl[ei]mentary\s*:?\s*(.*)$', txt, re.I | re.S)
    if m:
        return m.group(1).strip(' :'), m.group(2).strip(' :')
    return re.sub(r'^\s*Main\s*:?', '', txt).strip(), ''


def _activity(lines):
    sec = {'pre': [], 'del': [], 'post': []}
    cur = 'pre'
    for line in lines:
        for i, part in enumerate(HEAD.split(line)):
            if i % 2:
                cur = {'prelesson': 'pre', 'lessondelivery': 'del', 'postlesson': 'post'}[
                    part.lower().replace(' ', '').replace('-', '')]
            else:
                part = _strip_num(part.strip(' :'))
                if part:
                    sec[cur].append(part)
    notes = []
    if not sec['post'] and len(sec['del']) > 1:
        sec['post'] = [sec['del'].pop()]
        notes.append('post-lesson taken from last delivery step')
    if not sec['pre'] and len(sec['del']) > 1:
        sec['pre'] = [sec['del'].pop(0)]
        notes.append('pre-lesson taken from first delivery step')
    if not sec['del'] and len(sec['pre']) > 1:
        sec['del'], sec['pre'] = sec['pre'][1:], sec['pre'][:1]
        notes.append('delivery split from pre-lesson')
    return sec, notes


def _objectives(lines):
    out = []
    for line in lines:
        if line.lower().startswith('by the end'):
            continue
        line = re.sub(r'^\s*-?\s*(Main|Compl[ei]mentary)\s*:\s*', '', line, flags=re.I)
        line = re.sub(r'^\s*\d+(\.\d+)+\s*', '', line).lstrip('- ').strip()
        if line:
            out.append('-' + line)
    return out


def module_number(filename):
    m = re.search(r'(?:MODUL[E]?|UNIT|M)\s*[-_ ]?\s*(\d+)', filename, re.I) or re.search(r'(\d+)', filename)
    if not m:
        raise RPHError(f'{filename}: cannot tell the module number from the file name (e.g. "MODULE 3 ...").')
    return int(m.group(1))


def extract_module(filename, docx_bytes):
    """Return (lessons, topic) for one module file."""
    mod = module_number(filename)
    d = docx.Document(io.BytesIO(docx_bytes))
    lessons, cur, topics = [], None, Counter()
    for tbl in d.element.body.iter(W + 'tbl'):
        for tr in rows_of(tbl):
            cs = cells_of(tr)
            if not cs:
                continue
            key = clean(' '.join(paras_text(cs[0]))).upper()
            val = [x for c in cs[1:] for x in paras_text(c)]
            if key.startswith('DAY'):
                cur = {'module': mod}
                lessons.append(cur)
                continue
            if cur is None:
                continue
            if key.startswith('TOPIC'):
                topics[' '.join(val)] += 1
            elif key.startswith('THEME'):
                cur['theme'] = ' '.join(val)
            elif key.startswith('FOCUS'):
                m, c = _main_comp([' '.join(val).replace('Complimentary', 'Complementary')])
                cur['focus_main'], cur['focus_comp'] = m, c
            elif key.startswith('CONTENT STANDARD'):
                cur['cs_main'], cur['cs_comp'] = _main_comp(val)
            elif key.startswith('LEARNING STANDARD'):
                cur['ls_main'], cur['ls_comp'] = _main_comp(val)
            elif key.startswith('OBJECTIVE'):
                cur['objectives'] = _objectives(val)
            elif key.startswith('ACTIVITY'):
                lines = [x for c in cs for x in paras_text(c) if x.upper() != 'ACTIVITY']
                sec, notes = _activity(lines)
                cur.update(pre=sec['pre'], delivery=sec['del'], post=sec['post'], notes=notes)
    if not lessons:
        raise RPHError(f'{filename}: no lessons found. Each lesson should be a table starting with a "DAY" row.')
    for i, l in enumerate(lessons, 1):
        l['lesson'] = i
        l.setdefault('notes', [])
        for k in ('cs_main', 'cs_comp', 'ls_main', 'ls_comp', 'theme'):
            v = l.get(k, '') or ''
            l[k] = '' if v.upper().startswith(('CONTENT', 'LEARNING', 'THEME')) else v
        for k in ('objectives', 'pre', 'delivery', 'post'):
            l.setdefault(k, [])
        fm = l.get('focus_main', '') or ''
        if not fm or fm.upper().startswith('FOCUS'):
            fm = FOCUS_BY_SKILL.get((l['ls_main'] or l['cs_main'] or ' ')[0], '')
            l['focus_comp'] = ''
        l['focus_main'] = fm.strip().title()
        if not l['cs_main']:
            l['notes'].append('content standard missing in the module file')
    topic = ''
    if topics:
        t = topics.most_common(1)[0][0]
        m = re.match(r'\s*Module\s*(\d+)\s*[-:]\s*(.+)', t, re.I)
        if m and int(m.group(1)) == mod:
            topic = f'Module {mod} : {m.group(2).strip()}'
    return lessons, topic or f'Module {mod}'


def build_bank(files):
    """files: list of (filename, bytes). Returns (bank, module_titles, report)."""
    bank, titles, report = [], {}, []
    for name, data in files:
        lessons, topic = extract_module(name, to_docx_bytes(name, data))
        mod = lessons[0]['module']
        bank = [l for l in bank if l['module'] != mod] + lessons
        titles[str(mod)] = topic
        report.append(f'Module {mod}: {len(lessons)} lessons')
    bank.sort(key=lambda l: (l['module'], l['lesson']))
    return bank, titles, report


# ============================================================ school template
REQUIRED_LABELS = ['Week', 'Day', 'Date', 'Theme', 'Main Content', 'Com. Content', 'Main Learning',
                   'Com. Learning', 'LEARNING OBJECTIVE', 'Pre-lesson', 'Lesson Delivery', 'REFLECTION']


def _is_lesson_table(tbl):
    labels = {clean(T(cells_of(tr)[0])) for tr in rows_of(tbl) if cells_of(tr)}
    return all(any(l.startswith(req) for l in labels) for req in ('Week', 'Main Content'))


def make_template(docx_bytes):
    """Keep the page header and the first lesson table of a past RPH file. Returns (template bytes, info)."""
    d = docx.Document(io.BytesIO(docx_bytes))
    body = d.element.body
    tbls = [e for e in body.iterchildren() if e.tag == W + 'tbl']
    lesson = next((t for t in tbls if _is_lesson_table(t)), None)
    if lesson is None:
        raise RPHError('No lesson table found. Upload one of your past weekly RPH files (e.g. M41.docx).')
    labels = [clean(T(cells_of(tr)[0])) for tr in rows_of(lesson) if cells_of(tr)]
    missing = [r for r in REQUIRED_LABELS if not any(l.startswith(r) for l in labels)]
    if missing:
        raise RPHError('This RPH table is missing these rows: ' + ', '.join(missing) +
                       '. The app currently supports the English Year 3 lesson table format.')
    sect = body.find(W + 'sectPr')
    for e in list(body.iterchildren()):
        if e is not lesson and e is not sect:
            body.remove(e)
    out = io.BytesIO()
    d.save(out)
    title = clean(T(rows_of(lesson)[0]))
    return out.getvalue(), {'title': title}


def read_past_rph(docx_bytes):
    """Read the lesson tables of a past RPH file: day, classes, times, learning standard, pre-lesson."""
    d = docx.Document(io.BytesIO(docx_bytes))
    out = []
    for tbl in (e for e in d.element.body.iterchildren() if e.tag == W + 'tbl'):
        if not _is_lesson_table(tbl):
            continue
        info = {}
        for tr in rows_of(tbl):
            cs = cells_of(tr)
            lab = clean(T(cs[0])) if cs else ''
            vals = [paras_text(c) for c in cs]
            if lab == 'Week':
                info['week'] = ' '.join(vals[1])
                info['classes'] = vals[3] if len(vals) > 3 else []
                info['times'] = vals[5] if len(vals) > 5 else []
            elif lab == 'Day':
                info['day'] = ' '.join(vals[1])
            elif lab.startswith('Main Learning'):
                info['ls_main'] = ' '.join(vals[1])
            elif lab == 'Date':
                info['date'] = ' '.join(vals[1])
            elif lab == 'Pre-lesson':
                info['pre'] = ' '.join(vals[1]) if len(vals) > 1 else ''
            elif lab.startswith('Theme'):
                info['_topic_next'] = True
            elif info.pop('_topic_next', False) and len(vals) > 1:
                info['topic'] = ' '.join(vals[1])
        rs = rows_of(tbl)
        ri = next((i for i, tr in enumerate(rs) if clean(T(tr)).startswith('REFLECTION')), None)
        if ri is not None and ri + 1 < len(rs):
            info['reflection'] = paras_text(cells_of(rs[ri + 1])[0])
        out.append(info)
    return out


def guess_setup_from_past(docx_bytes, bank):
    """From a past week's file: the timetable, and which bank lesson each class finished on."""
    import difflib
    tables = read_past_rph(docx_bytes)
    timetable, last, titles, refl = [], {}, {}, {}
    week, first_date = None, None
    for t in tables:
        week = week or t.get('week')
        if not first_date and t.get('date') and t.get('day') in DAYS:
            try:
                d0 = dt.datetime.strptime(t['date'], '%d-%b-%y').date()
                first_date = d0 - dt.timedelta(days=DAYS.index(t['day']))
            except ValueError:
                pass
        for line in t.get('reflection', []):
            for c in t.get('classes', []):
                if line.startswith(c):
                    refl[c] = line
        for i, c in enumerate(t.get('classes', [])):
            tm = t['times'][i] if i < len(t.get('times', [])) else (t['times'][-1] if t.get('times') else '')
            if t.get('day') in DAYS:
                timetable.append({'day': t['day'], 'cls': c, 'time': tm})
        if bank:
            key = (t.get('pre', '') + ' ' + t.get('ls_main', '')).lower()
            best = max(bank, key=lambda b: difflib.SequenceMatcher(
                None, key, (' '.join(b['pre']) + ' ' + b['ls_main']).lower()).ratio())
            for c in t.get('classes', []):
                last[c] = (best['module'], best['lesson'])
            if t.get('topic', '').lower().startswith('module'):
                titles.setdefault(str(best['module']), t['topic'])
    seen, tt = set(), []
    for r in timetable:
        k = (r['day'], r['cls'])
        if k not in seen:
            seen.add(k)
            tt.append(r)
    tt.sort(key=lambda r: (DAYS.index(r['day']), time_key(r['time'])))
    try:
        week = int(re.sub(r'\D', '', week or '')) if week else None
    except ValueError:
        week = None
    return {'week': week, 'monday': first_date, 'timetable': tt, 'last': last, 'titles': titles, 'reflection': refl}


# ============================================================ planning
def time_key(t):
    m = re.match(r'\s*(\d+)[.:](\d+)', t or '')
    if not m:
        return 99
    h, mi = int(m.group(1)), int(m.group(2))
    if h < 7:
        h += 12
    return h + mi / 60


def _date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v)[:10])


def monday_of(week, profile):
    return _date(profile['ref_monday']) + dt.timedelta(days=7 * (week - int(profile['ref_week'])))


def plan_week(week, profile, bank):
    if not bank:
        raise RPHError('Upload your module files first.')
    if not profile.get('timetable'):
        raise RPHError('Fill in your timetable first.')
    holidays = {_date(h['date']): h for h in profile.get('holidays', []) if h.get('date')}
    index = {(int(l['module']), int(l['lesson'])): i for i, l in enumerate(bank)}
    tt = profile['timetable']
    classes = sorted({t['cls'] for t in tt})
    pos = {}
    for c in classes:
        anchors = [a for a in profile.get('progress', []) if a['cls'] == c and int(a['week']) < week]
        if not anchors:
            raise RPHError(f'Set where {c} starts (Starting lesson) for a week before week {week}.')
        a = max(anchors, key=lambda a: int(a['week']))
        key = (int(a['module']), int(a['lesson']))
        if key == (0, 0):
            p = -1  # nothing taught yet
        elif key in index:
            p = index[key]
        else:
            raise RPHError(f"{c}: module {a['module']} lesson {a['lesson']} is not in your lesson bank.")
        for w in range(int(a['week']) + 1, week):
            mon = monday_of(w, profile)
            for t in tt:
                if t['cls'] == c and mon + dt.timedelta(days=DAYS.index(t['day'])) not in holidays:
                    p += 1
        pos[c] = p
    mon = monday_of(week, profile)
    items = []
    for di, day in enumerate(DAYS):
        date = mon + dt.timedelta(days=di)
        if date in holidays:
            name = holidays[date].get('name') or 'Cuti'
            if not any(i['kind'] == 'holiday' and i['name'] == name for i in items):
                items.append({'kind': 'holiday', 'date': date, 'tk': -1, 'name': name})
            continue
        groups = {}
        for t in sorted((t for t in tt if t['day'] == day), key=lambda t: time_key(t['time'])):
            pos[t['cls']] += 1
            seq = pos[t['cls']]
            if seq >= len(bank):
                items.append({'kind': 'end', 'date': date, 'tk': time_key(t['time']), 'cls': t['cls']})
                continue
            g = groups.setdefault(seq, {'kind': 'lesson', 'date': date, 'tk': time_key(t['time']),
                                        'lesson': bank[seq], 'classes': [], 'times': []})
            g['classes'].append(t['cls'])
            g['times'].append(t['time'])
        items.extend(groups.values())
    items.sort(key=lambda i: (i['date'], i['tk']))
    return mon, items


# ============================================================ filling
def _set_par_text(p, text):
    ts = list(p.iter(W + 't'))
    for br in list(p.iter(W + 'lastRenderedPageBreak')):
        br.getparent().remove(br)
    if not ts:
        r = p.makeelement(W + 'r', {})
        t = r.makeelement(W + 't', {})
        r.append(t)
        p.append(r)
        ts = [t]
    main = max(ts, key=lambda t: len(t.text or ''))
    for t in ts:
        t.text = ''
    main.text = text
    main.set(XML_SPACE, 'preserve')


def _strip_numbering(p):
    ppr = p.find(W + 'pPr')
    if ppr is not None:
        for tag in ('numPr', 'ind'):
            e = ppr.find(W + tag)
            if e is not None:
                ppr.remove(e)


def set_cell(tc, lines, keep_first=False, numbered=False, size=None):
    ps = list(tc.iter(W + 'p'))
    start = 1 if keep_first else 0
    src = ps[start] if len(ps) > start else ps[-1]
    proto = copy.deepcopy(src)
    if keep_first:
        anchor = ps[0]
    else:
        parent, idx = src.getparent(), src.getparent().index(src)
    for p in ps[start:]:
        p.getparent().remove(p)
    for i, line in enumerate(lines or ['']):
        p = copy.deepcopy(proto)
        if numbered:
            _strip_numbering(p)
            line = f'{i + 1}. {line}'
        _set_par_text(p, line)
        if size:
            for rpr in p.iter(W + 'rPr'):
                for tag in ('sz', 'szCs'):
                    e = rpr.find(W + tag)
                    if e is None:
                        e = rpr.makeelement(W + tag, {})
                        rpr.append(e)
                    e.set(W + 'val', str(size))
        if keep_first or i > 0:
            anchor.addnext(p)
        else:
            parent.insert(idx, p)
        anchor = p


def _row(rs, label):
    for tr in rs:
        cs = cells_of(tr)
        if cs and clean(T(cs[0])).lower().startswith(label.lower()):
            return cs
    raise RPHError(f'Template row "{label}" not found.')


def _after(rs, label):
    i = next(i for i, tr in enumerate(rs) if clean(T(tr)).startswith(label))
    return cells_of(rs[i + 1])


def fill_lesson(tbl, week, item, profile):
    l, date = item['lesson'], item['date']
    rs = rows_of(tbl)
    cs = _row(rs, 'Week')
    set_cell(cs[1], [str(week)])
    set_cell(cs[3], item['classes'])
    if len(cs) > 5:
        set_cell(cs[5], item['times'])
    set_cell(_row(rs, 'Day')[1], [date.strftime('%A')])
    cs = _row(rs, 'Date')
    set_cell(cs[1], [date.strftime('%d-%b-%y')])
    for d in cs[1].iter(W + 'date'):
        d.set(W + 'fullDate', date.strftime('%Y-%m-%dT00:00:00Z'))
    focus = l.get('focus_main') or ''
    set_cell(cs[-1], [FOCUS_DROPDOWN.get(focus, focus)])
    cs = _after(rs, 'Theme')
    set_cell(cs[0], [l.get('theme') or ''])
    set_cell(cs[1], [profile.get('modules', {}).get(str(l['module']), f"Module {l['module']}")])
    set_cell(_row(rs, 'Main Content')[1], [l.get('cs_main') or ''])
    set_cell(_row(rs, 'Com. Content')[1], [l.get('cs_comp') or ''])
    set_cell(_row(rs, 'Main Learning')[1], [l.get('ls_main') or ''])
    set_cell(_row(rs, 'Com. Learning')[1], [l.get('ls_comp') or ''])
    set_cell(_row(rs, 'LEARNING OBJECTIVE')[0], l.get('objectives') or [], keep_first=True)
    set_cell(_row(rs, 'Pre-lesson')[1], l.get('pre') or [])
    long_text = len(' '.join(l.get('delivery') or [])) > 780
    cs = _after(rs, 'Lesson Delivery')
    set_cell(cs[0], l.get('delivery') or [], numbered=True, size=12 if long_text else None)
    if len(cs) > 1:
        set_cell(cs[1], l.get('post') or [], size=12 if long_text else None)
    refl = profile.get('reflection', {})
    set_cell(_after(rs, 'REFLECTION')[0],
             [refl.get(c) or f'{c} : ___ pupils did not achieve the objectives. Need guidance.' for c in item['classes']])


def _page_break(el):
    p = el.makeelement(W + 'p', {})
    r = p.makeelement(W + 'r', {})
    r.append(r.makeelement(W + 'br', {W + 'type': 'page'}))
    p.append(r)
    return p


def build_week(week, profile, bank, template_bytes, holiday_images=None):
    """Return (docx bytes, monday, summary lines)."""
    mon, items = plan_week(week, profile, bank)
    if not any(i['kind'] == 'lesson' for i in items):
        if any(i['kind'] == 'end' for i in items):
            last = bank[-1]
            raise RPHError(f"Week {week}: every class has finished the lesson bank (last lesson: Module "
                           f"{last['module']} lesson {last['lesson']}). Upload the next set of modules.")
    doc = docx.Document(io.BytesIO(template_bytes))
    body = doc.element.body
    tpl = next(e for e in body.iterchildren() if e.tag == W + 'tbl')
    sect = body.find(W + 'sectPr')
    for e in list(body.iterchildren()):
        if e is not sect:
            body.remove(e)
    summary = []
    for n, it in enumerate(items):
        if it['kind'] == 'lesson':
            t = copy.deepcopy(tpl)
            fill_lesson(t, week, it, profile)
            sect.addprevious(t)
            l = it['lesson']
            summary.append({'date': it['date'], 'what': ', '.join(it['classes']),
                            'detail': f"Module {l['module']} lesson {l['lesson']} · {l.get('focus_main', '')}"})
        else:
            p = doc.add_paragraph()
            img = (holiday_images or {}).get(it.get('name')) if it['kind'] == 'holiday' else None
            if img:
                p.add_run().add_picture(io.BytesIO(img), height=Cm(19))
            elif it['kind'] == 'holiday':
                p.add_run(it['name'].upper()).bold = True
            else:
                p.add_run(f"{it['cls']} has finished all lessons in the lesson bank.").bold = True
            body.remove(p._p)
            sect.addprevious(p._p)
            summary.append({'date': it['date'], 'what': 'Holiday' if it['kind'] == 'holiday' else it['cls'],
                            'detail': it.get('name') or 'lesson bank finished'})
        last = n == len(items) - 1
        sect.addprevious(body.makeelement(W + 'p', {}) if last else _page_break(body))
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue(), mon, summary

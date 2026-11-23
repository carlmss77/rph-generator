"""RPH Generator web app.

Environment:
  SECRET_KEY            any long random string (required in production)
  GOOGLE_CLIENT_ID      } Google sign-in. When both are set, teachers sign in with Google and
  GOOGLE_CLIENT_SECRET  } their files are kept in an "RPH Generator" folder in their own Drive.
                          When not set, the app runs in single-user local mode (files in DATA_DIR).
  DATA_DIR              folder for local mode (default ./data)
"""
import datetime as dt
import functools
import io
import os
import time

from flask import (Flask, abort, flash, redirect, render_template, request, send_file, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

from rph import engine
from rph.engine import DAYS, RPHError
from rph.storage import DriveStore, LocalStore

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-change-me')
app.config['MAX_CONTENT_LENGTH'] = 60 * 1024 * 1024
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = dt.timedelta(days=30)
DATA_DIR = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(__file__), 'data'))
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '').strip()
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '').strip()
GOOGLE = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

DRIVE_SCOPE = 'https://www.googleapis.com/auth/drive.file'

if GOOGLE:
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    oauth.register(
        'google',
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile ' + DRIVE_SCOPE},
        authorize_params={'access_type': 'offline', 'prompt': 'consent'},
    )


# ------------------------------------------------------------ users and storage
def current_user():
    if not GOOGLE:
        return {'email': 'local', 'name': 'Teacher'}
    return session.get('user')


def _fresh_token():
    tok = session.get('token') or {}
    if tok.get('expires_at', 0) - 60 < time.time() and tok.get('refresh_token'):
        new = oauth.google.fetch_access_token(grant_type='refresh_token', refresh_token=tok['refresh_token'])
        new.setdefault('refresh_token', tok['refresh_token'])
        session['token'] = tok = dict(new)
    return tok.get('access_token')


def store():
    u = current_user()
    if not GOOGLE:
        return LocalStore(DATA_DIR, 'local')
    return DriveStore(_fresh_token(), u['email'])


def login_required(f):
    @functools.wraps(f)
    def wrap(*a, **k):
        if not current_user():
            return redirect(url_for('login', next=request.path))
        try:
            return f(*a, **k)
        except RPHError as e:
            flash(str(e), 'error')
            return redirect(request.referrer or url_for('home'))
    return wrap


def load():
    s = store()
    profile = s.read_json('profile.json', {}) or {}
    bank = s.read_json('lesson_bank.json', []) or []
    has_tpl = profile.get('template_ok', False)
    return s, profile, bank, has_tpl


def this_week(profile):
    """Week number of the current school week (None if week numbering is not set)."""
    if not profile.get('ref_week') or not profile.get('ref_monday'):
        return None
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    ref = dt.date.fromisoformat(str(profile['ref_monday'])[:10])
    return int(profile['ref_week']) + (monday - ref).days // 7


@app.context_processor
def inject():
    return {'user': current_user(), 'google': GOOGLE, 'DAYS': DAYS}


@app.errorhandler(Exception)
def on_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception('Unhandled error on %s', request.path)
    resp = getattr(e, 'response', None)
    status = getattr(resp, 'status_code', None)
    if status in (401, 403) and GOOGLE:
        detail = ''
        try:
            detail = resp.json().get('error', {}).get('message', '')
        except Exception:
            pass
        session.clear()
        flash('Google Drive refused access' + (f': {detail}' if detail else '') +
              '. Please sign in again and tick the box for Google Drive.', 'error')
        return redirect(url_for('login'))
    return render_template('error.html', kind=type(e).__name__, msg=str(e)[:400]), 500


# ------------------------------------------------------------ sign in
@app.route('/login')
def login():
    if current_user():
        return redirect(url_for('home'))
    return render_template('login.html')


@app.route('/auth/google')
def auth_google():
    if not GOOGLE:
        return redirect(url_for('home'))
    return oauth.google.authorize_redirect(url_for('auth_callback', _external=True))


@app.route('/auth/callback')
def auth_callback():
    try:
        tok = oauth.google.authorize_access_token()
        info = tok.get('userinfo') or oauth.google.userinfo()
    except Exception as e:
        app.logger.exception('Google sign-in failed')
        session.clear()
        flash(f'Google sign-in did not finish ({type(e).__name__}: {e}). Please try again.', 'error')
        return redirect(url_for('login'))
    if DRIVE_SCOPE not in (tok.get('scope') or ''):
        session.clear()
        flash('The app needs permission to save files in your Google Drive. '
              'Sign in again and tick the box for Google Drive.', 'error')
        return redirect(url_for('login'))
    session.permanent = True
    session['user'] = {'email': info['email'], 'name': info.get('given_name') or info.get('name') or info['email']}
    session['token'] = {k: tok[k] for k in ('access_token', 'refresh_token', 'expires_at') if k in tok}
    return redirect(url_for('home'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# ------------------------------------------------------------ home
@app.route('/')
def home():
    if not current_user():
        return render_template('login.html')  # public landing page (also Google's "homepage" URL)
    return _home()


@login_required
def _home():
    s, profile, bank, has_tpl = load()
    steps = {
        'modules': bool(bank),
        'past': has_tpl,
        'setup': bool(profile.get('timetable')) and bool(profile.get('progress')),
    }
    week = this_week(profile)
    next_week = week + 1 if week and dt.date.today().weekday() >= 3 else week
    recent = profile.get('generated', [])[-6:][::-1]
    folder = s.folder_link() if hasattr(s, 'folder_link') else None
    return render_template('home.html', steps=steps, ready=all(steps.values()), bank=bank, profile=profile,
                           next_week=next_week, recent=recent, folder=folder)


# ------------------------------------------------------------ step 1: modules
@app.route('/setup/modules', methods=['GET', 'POST'])
@login_required
def setup_modules():
    s, profile, bank, _ = load()
    if request.method == 'POST':
        files = [(f.filename, f.read()) for f in request.files.getlist('files') if f and f.filename]
        if not files:
            raise RPHError('Choose your module files first.')
        new, titles, report = engine.build_bank(files)
        mods = {l['module'] for l in new}
        if request.form.get('mode') == 'add':
            bank = [l for l in bank if l['module'] not in mods] + new
        else:
            bank = new
        bank.sort(key=lambda l: (l['module'], l['lesson']))
        s.write_json('lesson_bank.json', bank)
        mt = profile.setdefault('modules', {})
        for k, v in titles.items():
            if k not in mt or mt[k] == f'Module {k}':
                mt[k] = v
        s.write_json('profile.json', profile)
        flash(f'Read {len(new)} lessons: ' + '; '.join(report), 'ok')
        return redirect(url_for('home'))
    return render_template('modules.html', bank=bank)


@app.route('/lessons')
@login_required
def lessons():
    s, profile, bank, _ = load()
    return render_template('lessons.html', bank=bank, profile=profile)


# ------------------------------------------------------------ step 2: a past RPH file
@app.route('/setup/past', methods=['GET', 'POST'])
@login_required
def setup_past():
    s, profile, bank, _ = load()
    if request.method == 'POST':
        f = request.files.get('file')
        if not f or not f.filename:
            raise RPHError('Choose one of your past weekly RPH files.')
        data = engine.to_docx_bytes(f.filename, f.read())
        tpl, info = engine.make_template(data)
        s.write('template.docx', tpl)
        g = engine.guess_setup_from_past(data, bank)
        profile['template_ok'] = True
        profile['template_title'] = info['title']
        if g['timetable']:
            profile['timetable'] = g['timetable']
        if g['week'] and g['monday']:
            profile['ref_week'], profile['ref_monday'] = g['week'], g['monday'].isoformat()
        if g['week'] and g['last']:
            prog = [p for p in profile.get('progress', []) if int(p['week']) != g['week']]
            prog += [{'week': g['week'], 'cls': c, 'module': m, 'lesson': l} for c, (m, l) in g['last'].items()]
            profile['progress'] = prog
        profile.setdefault('modules', {}).update(g['titles'])
        profile.setdefault('reflection', {}).update(g['reflection'])
        s.write_json('profile.json', profile)
        flash('Template saved. Your timetable and where each class is up to were read from the file. Please check them below.', 'ok')
        return redirect(url_for('setup_review'))
    return render_template('past.html', bank=bank)


# ------------------------------------------------------------ step 3: review settings
@app.route('/setup/review', methods=['GET', 'POST'])
@login_required
def setup_review():
    s, profile, bank, _ = load()
    if request.method == 'POST':
        fm = request.form
        profile['ref_week'] = int(fm['ref_week'])
        profile['ref_monday'] = fm['ref_monday']
        tt = []
        for day, cls, tm in zip(fm.getlist('tt_day'), fm.getlist('tt_cls'), fm.getlist('tt_time')):
            if cls.strip():
                tt.append({'day': day, 'cls': cls.strip(), 'time': tm.strip()})
        tt.sort(key=lambda r: (DAYS.index(r['day']), engine.time_key(r['time'])))
        profile['timetable'] = tt
        prog = []
        for w, c, m, l in zip(fm.getlist('pg_week'), fm.getlist('pg_cls'), fm.getlist('pg_mod'), fm.getlist('pg_les')):
            if c.strip() and w.strip():
                prog.append({'week': int(w), 'cls': c.strip(), 'module': int(m or 0), 'lesson': int(l or 0)})
        profile['progress'] = prog
        profile['modules'] = {k[4:]: v for k, v in fm.items() if k.startswith('mod_') and v.strip()}
        profile['reflection'] = {c: fm.get(f'refl_{c}', '') for c in sorted({r['cls'] for r in tt})}
        s.write_json('profile.json', profile)
        flash('Saved.', 'ok')
        return redirect(url_for('home'))
    classes = sorted({r['cls'] for r in profile.get('timetable', [])})
    mods = sorted({l['module'] for l in bank})
    return render_template('review.html', profile=profile, classes=classes, mods=mods, bank=bank)


# ------------------------------------------------------------ holidays
@app.route('/holidays', methods=['GET', 'POST'])
@login_required
def holidays():
    s, profile, bank, _ = load()
    if request.method == 'POST':
        hol = []
        for d, n in zip(request.form.getlist('h_date'), request.form.getlist('h_name')):
            if d:
                hol.append({'date': d, 'name': n.strip() or 'Cuti'})
        hol.sort(key=lambda h: h['date'])
        profile['holidays'] = hol
        img = request.files.get('image')
        name = request.form.get('image_for')
        if img and img.filename and name:
            ext = os.path.splitext(img.filename)[1].lower()
            if ext not in ('.png', '.jpg', '.jpeg'):
                raise RPHError('Holiday pictures must be PNG or JPG.')
            fname = f'holiday_{abs(hash(name)) % 10**8}{ext}'
            s.write(fname, img.read())
            profile.setdefault('holiday_images', {})[name] = fname
        s.write_json('profile.json', profile)
        flash('Holidays saved.', 'ok')
        return redirect(url_for('holidays'))
    names = sorted({h['name'] for h in profile.get('holidays', [])})
    return render_template('holidays.html', profile=profile, names=names)


# ------------------------------------------------------------ generate
def _week_file(profile, week, ext):
    return f"{profile.get('prefix', 'M')}{week}.{ext}"


@app.route('/generate', methods=['POST'])
@login_required
def generate():
    s, profile, bank, has_tpl = load()
    if not has_tpl:
        raise RPHError('Upload a past RPH file first (step 2).')
    week = int(request.form['week'])
    imgs = {n: s.read(f) for n, f in profile.get('holiday_images', {}).items()}
    data, mon, summary = engine.build_week(week, profile, bank, s.read('template.docx'), imgs)
    name = _week_file(profile, week, 'docx')
    s.write(name, data)
    if s.read(_week_file(profile, week, 'pdf')):
        s.write(_week_file(profile, week, 'pdf'), b'')  # old PDF is out of date
    gen = [g for g in profile.get('generated', []) if g['week'] != week]
    gen.append({'week': week, 'monday': mon.isoformat(), 'at': dt.datetime.now().isoformat(timespec='minutes')})
    profile['generated'] = gen[-30:]
    s.write_json('profile.json', profile)
    session['last_summary'] = {'week': week, 'monday': mon.isoformat(),
                               'rows': [{'date': r['date'].strftime('%a %d %b'), 'what': r['what'], 'detail': r['detail']} for r in summary]}
    return redirect(url_for('week_view', week=week))


@app.route('/week/<int:week>')
@login_required
def week_view(week):
    s, profile, bank, _ = load()
    summ = session.get('last_summary')
    if not summ or summ['week'] != week:
        try:
            mon, items = engine.plan_week(week, profile, bank)
            summ = {'week': week, 'monday': mon.isoformat(), 'rows': [
                {'date': i['date'].strftime('%a %d %b'),
                 'what': ', '.join(i['classes']) if i['kind'] == 'lesson' else ('Holiday' if i['kind'] == 'holiday' else i['cls']),
                 'detail': (f"Module {i['lesson']['module']} lesson {i['lesson']['lesson']} · {i['lesson'].get('focus_main', '')}"
                            if i['kind'] == 'lesson' else i.get('name', 'lesson bank finished'))} for i in items]}
        except RPHError as e:
            flash(str(e), 'error')
            return redirect(url_for('home'))
    exists = s.read(_week_file(profile, week, 'docx')) is not None
    return render_template('week.html', summ=summ, week=week, exists=exists,
                           docx_link=s.link(_week_file(profile, week, 'docx')),
                           monday=dt.date.fromisoformat(summ['monday']))


@app.route('/download/<int:week>.<ext>')
@login_required
def download(week, ext):
    if ext not in ('docx', 'pdf'):
        abort(404)
    s, profile, _, _ = load()
    data = s.read(_week_file(profile, week, 'docx'))
    if data is None:
        raise RPHError(f'Week {week} has not been generated yet.')
    name = _week_file(profile, week, ext)
    if ext == 'pdf':
        pdf = s.read(name)
        if not pdf or request.args.get('fresh'):
            pdf = engine.to_pdf_bytes(data)
            s.write(name, pdf)
        data = pdf
    mime = 'application/pdf' if ext == 'pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=(ext == 'docx'), download_name=name)


# ------------------------------------------------------------ public pages (needed to publish the Google sign-in)
@app.route('/privacy')
def privacy():
    return render_template('privacy.html', contact=os.environ.get('SUPPORT_EMAIL', ''))


@app.route('/terms')
def terms():
    return render_template('terms.html', contact=os.environ.get('SUPPORT_EMAIL', ''))


@app.route('/healthz')
def healthz():
    return 'ok'


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=bool(os.environ.get('DEBUG')))

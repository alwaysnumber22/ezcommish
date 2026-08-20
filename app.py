from functools import wraps
import os, sqlite3, secrets, logging, sys, traceback, uuid, re
from datetime import datetime, timedelta
from flask import Flask, g, render_template, request, redirect, url_for, session, flash, abort, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)

# Production diagnostics: send errors to Render stdout/stderr so they appear in Logs.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    stream=sys.stdout,
    force=True,
)
app.logger.setLevel(logging.INFO)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['DATABASE_URL'] = os.environ.get('DATABASE_URL', '')
app.config['DATABASE'] = os.environ.get('DATABASE_PATH', os.path.join(os.path.dirname(__file__), 'ezcommish.db'))
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024  # 1 MB form/request ceiling
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)


def current_commissioner_id():
    return session.get('commissioner_id')

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_commissioner_id():
            flash('Please sign in to manage your leagues.')
            return redirect(url_for('index'))
        return view(*args, **kwargs)
    return wrapped

def csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token

app.jinja_env.globals['csrf_token'] = csrf_token

@app.before_request
def security_before_request():
    # Commissioner sessions expire after 12 hours of inactivity.
    session.permanent = True
    # Require a session-bound token for all browser POST forms.
    if request.method == 'POST':
        sent = request.form.get('_csrf_token', '')
        expected = session.get('_csrf_token', '')
        if not sent or not expected or not secrets.compare_digest(sent, expected):
            abort(400, description='This form expired or could not be verified. Refresh the page and try again.')

@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(), camera=(), microphone=()')
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response

@app.get('/health')
def health():
    return {'ok': True, 'service': 'EZCommish'}, 200


@app.errorhandler(413)
def request_too_large(exc):
    return render_template('error.html', error_id='request-too-large'), 413

@app.errorhandler(Exception)
def handle_unexpected_error(exc):
    # Preserve normal HTTP errors such as 404 instead of turning them into 500s.
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        return exc
    error_id = uuid.uuid4().hex[:8]
    app.logger.exception(
        'EZCommish error_id=%s path=%s method=%s',
        error_id, request.path, request.method
    )
    # Show a useful reference to beta testers without exposing stack traces/secrets.
    return render_template('error.html', error_id=error_id), 500

TIME_WINDOWS = {
    'Midday': ('11:00 AM', '2:00 PM'),
    'Afternoon': ('2:00 PM', '5:00 PM'),
    'Evening': ('5:00 PM', '9:00 PM'),
}
SPORTS = ['Football', 'Baseball', 'Basketball', 'Hockey', 'Other']
VOTE_VALUES = ['preferred', 'available', 'cant']

def format_us_date(value):
    """Display stored YYYY-MM-DD dates as MM-DD-YYYY without changing storage."""
    if value is None:
        return ''
    s = str(value)
    try:
        # Exact stored date.
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', s):
            return datetime.strptime(s, '%Y-%m-%d').strftime('%m-%d-%Y')
        # Date/time-local value: preserve the time portion, convert only the date.
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}T.*', s):
            date_part, time_part = s.split('T', 1)
            return datetime.strptime(date_part, '%Y-%m-%d').strftime('%m-%d-%Y') + ' ' + time_part
    except (ValueError, TypeError):
        pass
    return s

app.jinja_env.filters['usdate'] = format_us_date


def parse_iso_date(value):
    try:
        return datetime.strptime((value or '').strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None

def parse_local_datetime(value):
    try:
        return datetime.strptime((value or '').strip(), '%Y-%m-%dT%H:%M')
    except (ValueError, TypeError):
        return None

def valid_phone(value):
    raw = (value or '').strip()
    digits = re.sub(r'\D', '', raw)
    return 7 <= len(digits) <= 15

def safe_text(value, max_len):
    return (value or '').strip()[:max_len]

def open_round_or_none(league_id, round_num):
    rnd = get_active_round(league_id, round_num)
    return rnd if rnd and rnd['status'] == 'open' else None




class CursorProxy:
    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self.lastrowid = lastrowid if lastrowid is not None else getattr(cursor, 'lastrowid', None)
    def fetchone(self):
        return self.cursor.fetchone()
    def fetchall(self):
        return self.cursor.fetchall()


class Database:
    def __init__(self):
        self.url = app.config['DATABASE_URL']
        self.is_postgres = self.url.startswith('postgres://') or self.url.startswith('postgresql://')
        if self.is_postgres:
            import psycopg
            from psycopg.rows import dict_row
            self.conn = psycopg.connect(self.url, row_factory=dict_row)
        else:
            self.conn = sqlite3.connect(app.config['DATABASE'])
            self.conn.row_factory = sqlite3.Row
            self.conn.execute('PRAGMA foreign_keys=ON')

    def _sql(self, sql):
        return sql.replace('?', '%s') if self.is_postgres else sql

    def execute(self, sql, params=()):
        if self.is_postgres:
            stmt = self._sql(sql)
            is_insert = stmt.lstrip().upper().startswith('INSERT INTO')
            if is_insert and ' RETURNING ' not in stmt.upper():
                stmt = stmt.rstrip().rstrip(';') + ' RETURNING id'
                cur = self.conn.execute(stmt, params)
                row = cur.fetchone()
                return CursorProxy(cur, row['id'] if row else None)
            return CursorProxy(self.conn.execute(stmt, params))
        return CursorProxy(self.conn.execute(sql, params))

    def executemany(self, sql, seq):
        if self.is_postgres:
            # psycopg3 exposes executemany() on cursors, not Connection.
            # Round 1/2 ballot submission uses this path to persist all votes.
            cur = self.conn.cursor()
            cur.executemany(self._sql(sql), seq)
            return CursorProxy(cur)
        return self.conn.executemany(sql, seq)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def db():
    if 'db' not in g:
        g.db = Database()
    return g.db


@app.teardown_appcontext
def close_db(exc):
    c = g.pop('db', None)
    if c is not None:
        c.close()


def init_db():
    if db().is_postgres:
        statements = [
            """CREATE TABLE IF NOT EXISTS commissioners(
              id BIGSERIAL PRIMARY KEY,
              name TEXT NOT NULL,
              phone TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS leagues(
              id BIGSERIAL PRIMARY KEY,
              commissioner_id BIGINT NOT NULL REFERENCES commissioners(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              sport TEXT NOT NULL,
              team_count INTEGER NOT NULL,
              timezone TEXT NOT NULL,
              season INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'setup',
              share_token TEXT NOT NULL UNIQUE,
              deadline TEXT,
              final_date TEXT,
              final_time TEXT,
              final_window TEXT,
              location TEXT,
              online_url TEXT,
              final_message TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS managers(
              id BIGSERIAL PRIMARY KEY,
              league_id BIGINT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              phone TEXT NOT NULL,
              team_name TEXT,
              is_commissioner INTEGER NOT NULL DEFAULT 0,
              active INTEGER NOT NULL DEFAULT 1
            )""",
            """CREATE TABLE IF NOT EXISTS poll_rounds(
              id BIGSERIAL PRIMARY KEY,
              league_id BIGINT NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
              round_num INTEGER NOT NULL,
              attempt INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL DEFAULT 'open',
              selected_option_id BIGINT,
              created_at TEXT NOT NULL,
              UNIQUE(league_id, round_num, attempt)
            )""",
            """CREATE TABLE IF NOT EXISTS poll_options(
              id BIGSERIAL PRIMARY KEY,
              round_id BIGINT NOT NULL REFERENCES poll_rounds(id) ON DELETE CASCADE,
              date_value TEXT NOT NULL,
              window_name TEXT,
              time_value TEXT,
              sort_order INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS responses(
              id BIGSERIAL PRIMARY KEY,
              round_id BIGINT NOT NULL REFERENCES poll_rounds(id) ON DELETE CASCADE,
              manager_id BIGINT NOT NULL REFERENCES managers(id) ON DELETE CASCADE,
              option_id BIGINT NOT NULL REFERENCES poll_options(id) ON DELETE CASCADE,
              vote TEXT NOT NULL,
              submitted_at TEXT NOT NULL,
              UNIQUE(round_id, manager_id, option_id)
            )"""
        ]
        for statement in statements:
            db().execute(statement)
    else:
        schema = '''
        CREATE TABLE IF NOT EXISTS commissioners(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          phone TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leagues(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          commissioner_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          sport TEXT NOT NULL,
          team_count INTEGER NOT NULL,
          timezone TEXT NOT NULL,
          season INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'setup',
          share_token TEXT NOT NULL UNIQUE,
          deadline TEXT,
          final_date TEXT,
          final_time TEXT,
          final_window TEXT,
          location TEXT,
          online_url TEXT,
          final_message TEXT,
          FOREIGN KEY(commissioner_id) REFERENCES commissioners(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS managers(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          league_id INTEGER NOT NULL,
          name TEXT NOT NULL,
          phone TEXT NOT NULL,
          team_name TEXT,
          is_commissioner INTEGER NOT NULL DEFAULT 0,
          active INTEGER NOT NULL DEFAULT 1,
          FOREIGN KEY(league_id) REFERENCES leagues(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS poll_rounds(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          league_id INTEGER NOT NULL,
          round_num INTEGER NOT NULL,
          attempt INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'open',
          selected_option_id INTEGER,
          created_at TEXT NOT NULL,
          UNIQUE(league_id, round_num, attempt),
          FOREIGN KEY(league_id) REFERENCES leagues(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS poll_options(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          round_id INTEGER NOT NULL,
          date_value TEXT NOT NULL,
          window_name TEXT,
          time_value TEXT,
          sort_order INTEGER NOT NULL,
          FOREIGN KEY(round_id) REFERENCES poll_rounds(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS responses(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          round_id INTEGER NOT NULL,
          manager_id INTEGER NOT NULL,
          option_id INTEGER NOT NULL,
          vote TEXT NOT NULL,
          submitted_at TEXT NOT NULL,
          UNIQUE(round_id, manager_id, option_id),
          FOREIGN KEY(round_id) REFERENCES poll_rounds(id) ON DELETE CASCADE,
          FOREIGN KEY(manager_id) REFERENCES managers(id) ON DELETE CASCADE,
          FOREIGN KEY(option_id) REFERENCES poll_options(id) ON DELETE CASCADE
        );
        '''
        db().conn.executescript(schema)
    db().commit()

@app.before_request
def ensure_db():
    init_db()


def current_commissioner():
    cid = session.get('commissioner_id')
    return db().execute('SELECT * FROM commissioners WHERE id=?', (cid,)).fetchone() if cid else None


def owned_league(league_id):
    c = current_commissioner()
    if not c: return None
    return db().execute('SELECT * FROM leagues WHERE id=? AND commissioner_id=?', (league_id, c['id'])).fetchone()


def get_active_round(league_id, round_num):
    return db().execute('SELECT * FROM poll_rounds WHERE league_id=? AND round_num=? ORDER BY attempt DESC LIMIT 1', (league_id, round_num)).fetchone()


def manager_submitted(round_id, manager_id):
    total = db().execute('SELECT COUNT(*) c FROM poll_options WHERE round_id=?', (round_id,)).fetchone()['c']
    got = db().execute('SELECT COUNT(*) c FROM responses WHERE round_id=? AND manager_id=?', (round_id, manager_id)).fetchone()['c']
    return total > 0 and got == total


def result_rows(round_id):
    managers_n = db().execute('SELECT COUNT(*) c FROM managers m JOIN poll_rounds r ON r.league_id=m.league_id WHERE r.id=? AND m.active=1', (round_id,)).fetchone()['c']
    opts = db().execute('SELECT * FROM poll_options WHERE round_id=? ORDER BY sort_order', (round_id,)).fetchall()
    rows=[]
    for o in opts:
        counts = {v:0 for v in VOTE_VALUES}
        rs = db().execute('SELECT vote, COUNT(*) c FROM responses WHERE option_id=? GROUP BY vote', (o['id'],)).fetchall()
        for r in rs: counts[r['vote']] = r['c']
        attendance = counts['preferred'] + counts['available']
        rows.append({**dict(o), **counts, 'attendance':attendance, 'pct': round(attendance*100/managers_n) if managers_n else 0,
                     'total_managers':managers_n})
    rows.sort(key=lambda x:(x['attendance']==managers_n if managers_n else False, x['attendance'], x['preferred']), reverse=True)
    return rows

@app.route('/', methods=['GET','POST'])
def index():
    c = current_commissioner()
    if request.method == 'POST':
        name=request.form.get('name','').strip(); phone=request.form.get('phone','').strip()
        if not name or not phone:
            flash('Enter your name and mobile number.')
        else:
            row=db().execute('SELECT * FROM commissioners WHERE phone=?',(phone,)).fetchone()
            if not row:
                cur=db().execute('INSERT INTO commissioners(name,phone,created_at) VALUES(?,?,?)',(name,phone,datetime.utcnow().isoformat()))
                db().commit(); cid=cur.lastrowid
            else:
                cid=row['id']
            session['commissioner_id']=cid
            return redirect(url_for('home'))
    return render_template('index.html', c=c)

@app.route('/logout')
def logout():
    session.clear(); return redirect(url_for('index'))

@app.route('/home')
@login_required
def home():
    c=current_commissioner()
    if not c: return redirect(url_for('index'))
    leagues=db().execute('SELECT * FROM leagues WHERE commissioner_id=? ORDER BY id DESC',(c['id'],)).fetchall()
    return render_template('home.html', c=c, leagues=leagues)

@app.route('/league/new', methods=['GET','POST'])
@login_required
def new_league():
    c=current_commissioner()
    if not c: return redirect(url_for('index'))
    if request.method=='POST':
        name=safe_text(request.form.get('name'), 80)
        sport=request.form.get('sport','').strip()
        tz=safe_text(request.form.get('timezone'), 60) or 'Pacific Time (PT)'
        try:
            team_count=int(request.form.get('team_count',''))
        except (TypeError, ValueError):
            team_count=0
        if not name:
            flash('Enter a league name.'); return redirect(request.url)
        if sport not in SPORTS:
            flash('Choose a valid fantasy sport.'); return redirect(request.url)
        if team_count<4 or team_count>32:
            flash('League size must be 4-32.'); return redirect(request.url)
        token=secrets.token_urlsafe(8)
        cur=db().execute('INSERT INTO leagues(commissioner_id,name,sport,team_count,timezone,season,status,share_token) VALUES(?,?,?,?,?,?,?,?)',
            (c['id'],name,sport,team_count,tz,datetime.now().year,'setup',token)); lid=cur.lastrowid
        db().execute('INSERT INTO managers(league_id,name,phone,is_commissioner) VALUES(?,?,?,1)',(lid,c['name'],c['phone']))
        db().commit(); return redirect(url_for('edit_managers',league_id=lid))
    return render_template('new_league.html', sports=SPORTS)

@app.route('/league/<int:league_id>/managers', methods=['GET','POST'])
@login_required
def edit_managers(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    if request.method=='POST':
        name=safe_text(request.form.get('name'), 60)
        phone=safe_text(request.form.get('phone'), 30)
        current_count=db().execute('SELECT COUNT(*) c FROM managers WHERE league_id=? AND active=1',(league_id,)).fetchone()['c']
        if current_count >= league['team_count']:
            flash(f'This league is already at its {league["team_count"]}-manager limit.')
            return redirect(request.url)
        if not name or not valid_phone(phone):
            flash('Enter a manager name and a valid phone number.')
            return redirect(request.url)
        duplicate_name=db().execute('SELECT id FROM managers WHERE league_id=? AND LOWER(name)=LOWER(?) AND active=1',(league_id,name)).fetchone()
        duplicate_phone=db().execute('SELECT id FROM managers WHERE league_id=? AND phone=? AND active=1',(league_id,phone)).fetchone()
        if duplicate_name:
            flash('That manager name is already in this league.')
            return redirect(request.url)
        if duplicate_phone:
            flash('That phone number is already assigned to a manager in this league.')
            return redirect(request.url)
        db().execute('INSERT INTO managers(league_id,name,phone) VALUES(?,?,?)',(league_id,name,phone))
        db().commit()
        return redirect(request.url)
    managers=db().execute('SELECT * FROM managers WHERE league_id=? ORDER BY id',(league_id,)).fetchall()
    return render_template('managers.html', league=league, managers=managers)

@app.post('/league/<int:league_id>/manager/<int:manager_id>/delete')
@login_required
def delete_manager(league_id, manager_id):
    league=owned_league(league_id)
    if not league: abort(404)
    db().execute('DELETE FROM managers WHERE id=? AND league_id=? AND is_commissioner=0',(manager_id,league_id)); db().commit()
    return redirect(url_for('edit_managers',league_id=league_id))

@app.route('/league/<int:league_id>/dates', methods=['GET','POST'])
@login_required
def choose_dates(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    if request.method=='POST':
        dates=[(request.form.get(f'date{i}') or '').strip() for i in range(1,4)]
        deadline=(request.form.get('deadline') or '').strip()
        parsed_dates=[parse_iso_date(d) for d in dates]
        parsed_deadline=parse_local_datetime(deadline)
        if any(d is None for d in parsed_dates) or len(set(dates))<3:
            flash('Choose three different valid dates.'); return redirect(request.url)
        if any(d < datetime.now().date() for d in parsed_dates):
            flash('Draft-date options cannot be in the past.'); return redirect(request.url)
        if not parsed_deadline:
            flash('Choose a valid response deadline.'); return redirect(request.url)
        if parsed_deadline <= datetime.now():
            flash('The response deadline must be in the future.'); return redirect(request.url)
        existing_open=db().execute("SELECT id FROM poll_rounds WHERE league_id=? AND status='open' LIMIT 1",(league_id,)).fetchone()
        if existing_open:
            flash('This league already has an open voting round. Close or restart it before creating another.')
            return redirect(url_for('league_dashboard', league_id=league_id))
        prev=get_active_round(league_id,1); attempt=(prev['attempt']+1) if prev else 1
        cur=db().execute('INSERT INTO poll_rounds(league_id,round_num,attempt,status,created_at) VALUES(?,?,?,?,?)',(league_id,1,attempt,'open',datetime.utcnow().isoformat())); rid=cur.lastrowid
        order=0
        for d in dates:
            for w in TIME_WINDOWS:
                order+=1; db().execute('INSERT INTO poll_options(round_id,date_value,window_name,sort_order) VALUES(?,?,?,?)',(rid,d,w,order))
        db().execute("UPDATE leagues SET deadline=?, status='round1' WHERE id=?",(deadline,league_id)); db().commit()
        # Round 1 is now ready. Keep voting and league sharing as two clear,
        # separate commissioner actions on the same Ready / Progress screen.
        return redirect(url_for('share_poll',league_id=league_id))
    return render_template('dates.html', league=league, windows=TIME_WINDOWS)

@app.route('/league/<int:league_id>/share')
@login_required
def share_poll(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    managers=db().execute('SELECT * FROM managers WHERE league_id=? AND active=1 ORDER BY id',(league_id,)).fetchall()
    link=url_for('manager_entry',token=league['share_token'],_external=True)

    # This page is the commissioner's single Round Ready / Progress hub.
    # It deliberately keeps "Cast My Vote" and "Share to League" separate.
    round_num = 2 if league['status']=='round2' else 1
    rnd=get_active_round(league_id, round_num)
    commissioner_manager=next((m for m in managers if m['is_commissioner']), None)
    commissioner_done=bool(rnd and commissioner_manager and manager_submitted(rnd['id'], commissioner_manager['id']))
    responded=0
    other_responded=0
    other_total=0
    if rnd:
        for m in managers:
            done=manager_submitted(rnd['id'],m['id'])
            responded += 1 if done else 0
            if not m['is_commissioner']:
                other_total += 1
                other_responded += 1 if done else 0

    return render_template(
        'share.html',
        league=league,
        managers=managers,
        link=link,
        rnd=rnd,
        round_num=round_num,
        commissioner_done=commissioner_done,
        responded=responded,
        other_responded=other_responded,
        other_total=other_total,
        round2_date_display=format_us_date(league['final_date']) if league['final_date'] else '',
        round2_window_display=league['final_window'] or ''
    )

@app.route('/league/<int:league_id>/dashboard')
@login_required
def league_dashboard(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    managers=db().execute('SELECT * FROM managers WHERE league_id=? ORDER BY id',(league_id,)).fetchall()
    r1=get_active_round(league_id,1); r2=get_active_round(league_id,2)
    active=r2 if r2 and r2['status']=='open' else r1
    statuses=[]
    responded=0
    if active:
        for m in managers:
            done=manager_submitted(active['id'],m['id'])
            statuses.append((m,done))
            responded += 1 if done else 0
    commissioner_manager=next((m for m in managers if m['is_commissioner']), None)
    commissioner_done=bool(active and commissioner_manager and manager_submitted(active['id'], commissioner_manager['id']))
    return render_template('dashboard.html',league=league,statuses=statuses,responded=responded,active=active,r1=r1,r2=r2,commissioner_manager=commissioner_manager,commissioner_done=commissioner_done)

@app.route('/league/<int:league_id>/my-vote')
@login_required
def commissioner_vote(league_id):
    league=owned_league(league_id)
    if not league:
        abort(404)
    if league['status'] in ('canceled', 'confirmed'):
        return redirect(url_for('league_dashboard', league_id=league_id))
    manager=db().execute(
        'SELECT * FROM managers WHERE league_id=? AND is_commissioner=1 AND active=1 ORDER BY id LIMIT 1',
        (league_id,)
    ).fetchone()
    if not manager:
        flash('Commissioner manager record was not found.')
        return redirect(url_for('league_dashboard', league_id=league_id))
    session[f'manager_{league["share_token"]}']=manager['id']
    session[f'commissioner_voting_{league["share_token"]}']=True
    if not manager['team_name']:
        return redirect(url_for('team_name', token=league['share_token']))
    return redirect(url_for('vote', token=league['share_token']))

@app.route('/league/<int:league_id>/results/<int:round_num>')
@login_required
def results(league_id,round_num):
    if round_num not in (1,2): abort(404)
    league=owned_league(league_id)
    if not league: abort(404)
    rnd=get_active_round(league_id,round_num)
    if not rnd: abort(404)
    rows=result_rows(rnd['id'])
    for row in rows:
        conflicts=db().execute("SELECT m.name,m.team_name FROM responses x JOIN managers m ON m.id=x.manager_id WHERE x.option_id=? AND x.vote='cant'",(row['id'],)).fetchall()
        row['conflicts']=conflicts
    return render_template('results.html',league=league,rnd=rnd,rows=rows)

@app.post('/league/<int:league_id>/round1/select/<int:option_id>')
@login_required
def select_round1(league_id,option_id):
    league=owned_league(league_id)
    if not league: abort(404)
    r1=open_round_or_none(league_id,1)
    if not r1:
        flash('Round 1 voting is no longer open.')
        return redirect(url_for('league_dashboard',league_id=league_id))
    opt=db().execute('SELECT * FROM poll_options WHERE id=? AND round_id=?',(option_id,r1['id'])).fetchone()
    if not opt: abort(404)
    existing_r2=db().execute('SELECT id FROM poll_rounds WHERE league_id=? AND round_num=2 LIMIT 1',(league_id,)).fetchone()
    if existing_r2:
        flash('Round 2 has already been created for this draft poll.')
        return redirect(url_for('share_poll',league_id=league_id))
    db().execute("UPDATE poll_rounds SET status='closed',selected_option_id=? WHERE id=?",(option_id,r1['id']))
    cur=db().execute('INSERT INTO poll_rounds(league_id,round_num,attempt,status,created_at) VALUES(?,?,?,?,?)',(league_id,2,1,'open',datetime.utcnow().isoformat())); r2id=cur.lastrowid
    # inclusive 30 minute choices within selected time window
    start_s,end_s=TIME_WINDOWS[opt['window_name']]
    start=datetime.strptime(start_s,'%I:%M %p'); end=datetime.strptime(end_s,'%I:%M %p'); i=0
    while start<=end:
        i+=1; db().execute('INSERT INTO poll_options(round_id,date_value,window_name,time_value,sort_order) VALUES(?,?,?,?,?)',(r2id,opt['date_value'],opt['window_name'],start.strftime('%-I:%M %p'),i)); start+=timedelta(minutes=30)
    db().execute("UPDATE leagues SET status='round2', final_date=?, final_window=? WHERE id=?",(opt['date_value'],opt['window_name'],league_id)); db().commit()
    # Round 2 follows the same pattern as Round 1: first show a clear Ready
    # screen, then let the commissioner vote and/or share the new round.
    flash('Round 2 is ready. Cast your time vote and share the exact-time poll with your league.')
    return redirect(url_for('share_poll',league_id=league_id))

@app.post('/league/<int:league_id>/new-dates')
@login_required
def new_dates(league_id):
    if not owned_league(league_id): abort(404)
    return redirect(url_for('choose_dates',league_id=league_id))

@app.post('/league/<int:league_id>/reset/<int:manager_id>/<int:round_num>')
@login_required
def reset_ballot(league_id,manager_id,round_num):
    if not owned_league(league_id): abort(404)
    if round_num not in (1,2): abort(404)
    manager=db().execute('SELECT id FROM managers WHERE id=? AND league_id=?',(manager_id,league_id)).fetchone()
    if not manager: abort(404)
    rnd=get_active_round(league_id,round_num)
    if rnd:
        db().execute('DELETE FROM responses WHERE round_id=? AND manager_id=?',(rnd['id'],manager_id)); db().commit()
    return redirect(url_for('league_dashboard',league_id=league_id))


@app.post('/league/<int:league_id>/poll/close')
@login_required
def close_draft_poll(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    if league['status'] in ('confirmed','canceled','closed'):
        flash('There is no open draft voting round to close.')
        return redirect(url_for('league_dashboard',league_id=league_id))
    db().execute("UPDATE poll_rounds SET status='closed' WHERE league_id=? AND status='open'",(league_id,))
    db().execute("UPDATE leagues SET status='closed' WHERE id=?",(league_id,))
    db().commit()
    flash('Draft poll closed. No additional votes can be submitted.')
    return redirect(url_for('league_dashboard',league_id=league_id))

@app.post('/league/<int:league_id>/delete')
@login_required
def delete_league(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    league_name=league['name']
    # Foreign-key cascades remove managers, rounds, options, and responses.
    db().execute('DELETE FROM leagues WHERE id=? AND commissioner_id=?',(league_id, session['commissioner_id']))
    db().commit()
    flash(f'{league_name} was permanently deleted.')
    return redirect(url_for('home'))

@app.post('/league/<int:league_id>/poll/reset')
@login_required
def reset_draft_poll(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    if league['status'] == 'confirmed':
        flash('Draft Day is already confirmed. Delete the league or intentionally restart scheduling from the league settings.')
    # Delete every poll round for this league. Cascading FKs remove options/responses.
    # Managers, phone numbers, and team names are intentionally preserved.
    db().execute('DELETE FROM poll_rounds WHERE league_id=?',(league_id,))
    db().execute("""UPDATE leagues SET status='setup', deadline=NULL, final_date=NULL,
                 final_time=NULL, final_window=NULL, location=NULL, online_url=NULL,
                 final_message=NULL WHERE id=?""",(league_id,))
    db().commit()
    flash('Draft poll reset. Manager roster and team names were kept.')
    return redirect(url_for('choose_dates',league_id=league_id))

@app.post('/league/<int:league_id>/poll/cancel')
@login_required
def cancel_draft_poll(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    if league['status'] in ('confirmed','canceled'):
        flash('This draft poll cannot be canceled in its current state.')
        return redirect(url_for('league_dashboard',league_id=league_id))
    db().execute("UPDATE poll_rounds SET status='canceled' WHERE league_id=? AND status='open'",(league_id,))
    db().execute("UPDATE leagues SET status='canceled' WHERE id=?",(league_id,))
    db().commit()
    flash('Draft poll canceled. Existing league and manager information were kept.')
    return redirect(url_for('league_dashboard',league_id=league_id))

@app.post('/league/<int:league_id>/round2/select/<int:option_id>')
@login_required
def select_round2(league_id,option_id):
    league=owned_league(league_id)
    if not league: abort(404)
    r2=open_round_or_none(league_id,2)
    if not r2:
        flash('Round 2 voting is no longer open.')
        return redirect(url_for('league_dashboard',league_id=league_id))
    opt=db().execute('SELECT * FROM poll_options WHERE id=? AND round_id=?',(option_id,r2['id'])).fetchone()
    if not opt: abort(404)
    db().execute("UPDATE poll_rounds SET status='closed',selected_option_id=? WHERE id=?",(option_id,r2['id']))
    db().execute("UPDATE leagues SET status='ready', final_date=?, final_time=?, final_window=? WHERE id=?",(opt['date_value'],opt['time_value'],opt['window_name'],league_id)); db().commit()
    return redirect(url_for('confirm_draft',league_id=league_id))

@app.route('/league/<int:league_id>/confirm', methods=['GET','POST'])
@login_required
def confirm_draft(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    if league['status'] not in ('ready','confirmed'):
        flash('Choose the final Round 2 start time before finalizing Draft Day.')
        return redirect(url_for('league_dashboard',league_id=league_id))
    if request.method=='POST':
        location=safe_text(request.form.get('location'), 160)
        online=safe_text(request.form.get('online_url'), 500)
        msg=safe_text(request.form.get('message'), 1000)
        if online and not re.match(r'^https?://', online, re.I):
            flash('Online draft link must begin with http:// or https://')
            return redirect(request.url)
        db().execute("UPDATE leagues SET status='confirmed',location=?,online_url=?,final_message=? WHERE id=?",(location,online,msg,league_id)); db().commit()
        return redirect(url_for('final_notice',league_id=league_id))
    return render_template('confirm.html',league=league)

@app.route('/league/<int:league_id>/final-notice')
@login_required
def final_notice(league_id):
    league=owned_league(league_id)
    if not league: abort(404)
    if league['status'] != 'confirmed':
        return redirect(url_for('league_dashboard', league_id=league_id))
    managers=db().execute('SELECT * FROM managers WHERE league_id=? AND active=1 ORDER BY name',(league_id,)).fetchall()
    draft_link=url_for('draft_day', token=league['share_token'], _external=True)
    return render_template('final_notice.html', league=league, managers=managers, draft_link=draft_link)

@app.route('/p/<token>', methods=['GET','POST'])
def manager_entry(token):
    league=db().execute('SELECT * FROM leagues WHERE share_token=?',(token,)).fetchone()
    if not league: abort(404)
    if league['status']=='confirmed': return redirect(url_for('draft_day',token=token))
    if league['status'] in ('canceled','closed','ready','setup'):
        return render_template('poll_unavailable.html', league=league)
    managers=db().execute('SELECT * FROM managers WHERE league_id=? AND active=1 ORDER BY name',(league['id'],)).fetchall()
    if request.method=='POST':
        mid=int(request.form['manager_id']); m=db().execute('SELECT * FROM managers WHERE id=? AND league_id=?',(mid,league['id'])).fetchone()
        if not m: abort(404)
        session[f'manager_{token}']=mid
        if not m['team_name']: return redirect(url_for('team_name',token=token))
        return redirect(url_for('vote',token=token))
    return render_template('manager_entry.html',league=league,managers=managers)

@app.route('/p/<token>/team', methods=['GET','POST'])
def team_name(token):
    league=db().execute('SELECT * FROM leagues WHERE share_token=?',(token,)).fetchone(); mid=session.get(f'manager_{token}')
    if not league or not mid: return redirect(url_for('manager_entry',token=token))
    if league['status'] not in ('round1','round2'):
        return render_template('poll_unavailable.html', league=league)
    m=db().execute('SELECT * FROM managers WHERE id=? AND league_id=?',(mid,league['id'])).fetchone()
    if not m: return redirect(url_for('manager_entry',token=token))
    if request.method=='POST':
        team=request.form.get('team_name','').strip()
        if team:
            db().execute('UPDATE managers SET team_name=? WHERE id=?',(team,mid)); db().commit(); app.logger.info('Team name saved manager_id=%s league_id=%s; redirecting to vote', mid, league['id']); return redirect(url_for('vote',token=token))
    return render_template('team.html',league=league,m=m)

@app.route('/p/<token>/vote', methods=['GET','POST'])
def vote(token):
    app.logger.info('Vote page requested token=%s', token[:6] + '...')
    league=db().execute('SELECT * FROM leagues WHERE share_token=?',(token,)).fetchone(); mid=session.get(f'manager_{token}')
    if not league or not mid: return redirect(url_for('manager_entry',token=token))
    if league['status'] not in ('round1','round2'):
        return render_template('poll_unavailable.html', league=league)
    manager=db().execute('SELECT id FROM managers WHERE id=? AND league_id=? AND active=1',(mid,league['id'])).fetchone()
    if not manager: return redirect(url_for('manager_entry',token=token))
    round_num=2 if league['status']=='round2' else 1
    rnd=open_round_or_none(league['id'],round_num)
    if not rnd: return render_template('poll_unavailable.html', league=league)
    opts=db().execute('SELECT * FROM poll_options WHERE round_id=? ORDER BY sort_order',(rnd['id'],)).fetchall(); app.logger.info('Vote page league_id=%s round_id=%s round_num=%s options=%s manager_id=%s', league['id'], rnd['id'], round_num, len(opts), mid)
    if manager_submitted(rnd['id'],mid): return redirect(url_for('submitted',token=token))

    ballot_key=f'ballot_nonce_{token}_{rnd["id"]}'
    if request.method=='GET':
        session[ballot_key]=secrets.token_urlsafe(24)

    if request.method=='POST':
        posted_nonce=request.form.get('_ballot_nonce','')
        expected_nonce=session.get(ballot_key,'')
        if not posted_nonce or not expected_nonce or not secrets.compare_digest(posted_nonce, expected_nonce):
            if manager_submitted(rnd['id'],mid):
                return redirect(url_for('submitted',token=token))
            flash('This ballot was already submitted or expired. Please review your selections again.')
            return redirect(request.url)
        # Consume the one-time token before writing so a double-click cannot resubmit it.
        session.pop(ballot_key, None)
        votes=[]
        for o in opts:
            v=request.form.get(f'o{o["id"]}')
            if v not in VOTE_VALUES:
                flash('Please answer every option.'); return redirect(request.url)
            votes.append((rnd['id'],mid,o['id'],v,datetime.utcnow().isoformat()))
        # Replace this manager's current round responses atomically. This protects
        # against accidental double-submits while preserving the locked-ballot UX.
        db().execute('DELETE FROM responses WHERE round_id=? AND manager_id=?', (rnd['id'], mid))
        db().executemany('INSERT INTO responses(round_id,manager_id,option_id,vote,submitted_at) VALUES(?,?,?,?,?)', votes)
        db().commit()
        app.logger.info('Ballot submitted league_id=%s round_id=%s manager_id=%s votes=%s', league['id'], rnd['id'], mid, len(votes))
        if session.pop(f'commissioner_voting_{token}', False):
            flash('Your commissioner vote has been submitted.')
            return redirect(url_for('share_poll', league_id=league['id']))
        return redirect(url_for('submitted',token=token))
    return render_template('vote.html',league=league,rnd=rnd,opts=opts,windows=TIME_WINDOWS,
                           ballot_nonce=session.get(ballot_key,''))

@app.route('/p/<token>/submitted')
def submitted(token):
    league=db().execute('SELECT * FROM leagues WHERE share_token=?',(token,)).fetchone(); mid=session.get(f'manager_{token}')
    if not league or not mid: return redirect(url_for('manager_entry',token=token))
    round_num=2 if league['status']=='round2' else 1; rnd=get_active_round(league['id'],round_num)
    total=db().execute('SELECT COUNT(*) c FROM managers WHERE league_id=? AND active=1',(league['id'],)).fetchone()['c']
    responded=0
    for m in db().execute('SELECT id FROM managers WHERE league_id=? AND active=1',(league['id'],)).fetchall():
        if rnd and manager_submitted(rnd['id'],m['id']): responded+=1
    return render_template('submitted.html',league=league,responded=responded,total=total)

@app.route('/d/<token>')
def draft_day(token):
    league=db().execute('SELECT * FROM leagues WHERE share_token=?',(token,)).fetchone()
    if not league: abort(404)
    if league['status'] != 'confirmed':
        return render_template('poll_unavailable.html', league=league)
    return render_template('draft_day.html',league=league)

@app.route('/d/<token>/calendar.ics')
def calendar_ics(token):
    league=db().execute('SELECT * FROM leagues WHERE share_token=?',(token,)).fetchone()
    if not league or league['status'] != 'confirmed' or not league['final_date'] or not league['final_time']: abort(404)
    start=datetime.strptime(league['final_date']+' '+league['final_time'],'%Y-%m-%d %I:%M %p'); end=start+timedelta(hours=3)
    def esc(s): return (s or '').replace('\\','\\\\').replace(',','\\,').replace(';','\\;').replace('\n','\\n')
    body='\r\n'.join([
        'BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//EZCommish//Draft Day//EN','BEGIN:VEVENT',
        f'UID:{league["share_token"]}@ezcommish',f'DTSTAMP:{datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")}',
        f'DTSTART:{start.strftime("%Y%m%dT%H%M%S")}',f'DTEND:{end.strftime("%Y%m%dT%H%M%S")}',
        f'SUMMARY:{esc(league["name"])} Fantasy Draft',f'LOCATION:{esc(league["location"])}',
        f'DESCRIPTION:{esc(league["final_message"])}','END:VEVENT','END:VCALENDAR',''])
    path=os.path.join(os.path.dirname(__file__),'draft_day.ics'); open(path,'w').write(body)
    return send_file(path,as_attachment=True,download_name='EZCommish-Draft-Day.ics',mimetype='text/calendar')

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=True)

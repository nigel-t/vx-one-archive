"""A small, password-protected archive for the VX One fleet."""
import functools
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

from archive import ImportProblem, connect, display_name, import_zip, initialize, merge_participants


def load_environment():
    file = Path(__file__).parent / '.env'
    if file.exists():
        for line in file.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def create_app(test_config=None):
    load_environment()
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get('SECRET_KEY'),
        READER_PASSWORD_HASH=os.environ.get('READER_PASSWORD_HASH'),
        ADMIN_PASSWORD_HASH=os.environ.get('ADMIN_PASSWORD_HASH'),
        DATA_DIR=os.environ.get('DATA_DIR', str(Path(__file__).parent / 'data')),
        SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', 'true').lower() != 'false',
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_NAME='vx_archive_session',
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=512 * 1024**2,
        MAX_FORM_MEMORY_SIZE=128 * 1024,
        MAX_FORM_PARTS=20,
        TRUSTED_HOSTS=[h.strip() for h in os.environ.get('TRUSTED_HOSTS', 'localhost,127.0.0.1,vxonetech.currentdesign.ca').split(',')],
    )
    if test_config:
        app.config.update(test_config)
    if os.environ.get('TRUST_PROXY', 'false').lower() == 'true':
        # Production listens on loopback behind exactly one trusted web server.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    if not app.config['SECRET_KEY'] or len(app.config['SECRET_KEY']) < 32:
        raise RuntimeError('Run setup.py to create the private environment settings before starting the archive.')
    if not app.config['READER_PASSWORD_HASH'] or not app.config['ADMIN_PASSWORD_HASH']:
        raise RuntimeError('Both fleet and administrator password hashes are required. Run setup.py first.')
    if app.config['READER_PASSWORD_HASH'] == app.config['ADMIN_PASSWORD_HASH']:
        raise RuntimeError('Fleet and administrator credentials must be different.')
    initialize(app.config['DATA_DIR'])

    def db():
        if 'db' not in g:
            g.db = connect(app.config['DATA_DIR'])
        return g.db

    @app.teardown_appcontext
    def close_database(error=None):
        connection = g.pop('db', None)
        if connection:
            connection.close()

    def csrf_token():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return session['csrf']

    app.jinja_env.globals['csrf_token'] = csrf_token

    def auth_version(role):
        key = 'ADMIN_PASSWORD_HASH' if role == 'admin' else 'READER_PASSWORD_HASH'
        return hashlib.sha256(app.config[key].encode()).hexdigest()[:24]

    @app.before_request
    def protect_requests():
        role = session.get('role')
        if role and session.get('auth_version') != auth_version(role):
            session.clear()
        if request.method == 'POST':
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token', '')
            expected = session.get('csrf', '')
            if not expected or not secrets.compare_digest(supplied, expected):
                abort(400, description='This form expired. Reload the page and try again.')

    @app.after_request
    def security_headers(response):
        response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'; object-src 'none'"
        if app.config['SESSION_COOKIE_SECURE']:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'private, no-store'
        return response

    def require_login(admin=False):
        def decorator(function):
            @functools.wraps(function)
            def wrapped(*args, **kwargs):
                role = session.get('role')
                if not role:
                    if request.path.startswith('/api/') or request.path.startswith('/media/'):
                        return jsonify(error='Please sign in to the archive.'), 401
                    return redirect(url_for('admin_login' if admin else 'login'))
                if admin and role != 'admin':
                    abort(403, description='Administrator access is required.')
                return function(*args, **kwargs)
            return wrapped
        return decorator

    def login_page(admin=False):
        error = None
        if request.method == 'POST':
            # ProxyFix is enabled only when a trusted proxy is the sole route to this process.
            address = request.remote_addr or 'unknown'
            now = time.time()
            recent = db().execute('SELECT * FROM login_attempts WHERE address=?', (address,)).fetchone()
            if recent and recent['failures'] >= 15 and now - recent['updated_at'] < 300:
                return render_template('login.html', admin=admin, error='Too many attempts. Please try again in a few minutes.'), 429
            password = request.form.get('password', '')
            key = 'ADMIN_PASSWORD_HASH' if admin else 'READER_PASSWORD_HASH'
            if len(password) <= 256 and check_password_hash(app.config[key], password):
                db().execute('DELETE FROM login_attempts WHERE address=?', (address,))
                db().commit()
                session.clear()
                role = 'admin' if admin else 'reader'
                session.update(role=role, auth_version=auth_version(role), csrf=secrets.token_urlsafe(32))
                session.permanent = bool(request.form.get('remember'))
                return redirect(url_for('admin' if admin else 'index'))
            failures = recent['failures'] + 1 if recent and now - recent['updated_at'] < 300 else 1
            db().execute('INSERT INTO login_attempts VALUES(?,?,?) ON CONFLICT(address) DO UPDATE SET failures=excluded.failures,updated_at=excluded.updated_at',
                         (address, failures, now))
            db().execute('DELETE FROM login_attempts WHERE updated_at < ?', (now - 3600,))
            db().commit()
            error = 'That password did not match. Please try again.'
        return render_template('login.html', admin=admin, error=error)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        return login_page()

    @app.route('/admin/login', methods=['GET', 'POST'])
    def admin_login():
        return login_page(admin=True)

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    def stats():
        row = db().execute('SELECT COUNT(*) AS count,MIN(timestamp) AS earliest,MAX(timestamp) AS latest FROM messages').fetchone()
        last_import = db().execute('SELECT imported_at FROM imports ORDER BY id DESC LIMIT 1').fetchone()
        return {**dict(row), 'last_import': last_import[0] if last_import else None,
                'senders': db().execute('SELECT COUNT(*) FROM participants WHERE merged_into IS NULL').fetchone()[0]}

    @app.get('/')
    @require_login()
    def index():
        return render_template('feed.html', stats=stats(), admin=session.get('role') == 'admin')

    def cursor(row):
        return row['timestamp'] + '~' + str(row['id'])

    def parse_cursor(value):
        try:
            timestamp, number = value.rsplit('~', 1)
            if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d', timestamp):
                raise ValueError()
            return timestamp, int(number)
        except (ValueError, AttributeError):
            abort(400, description='The message position is invalid.')

    def serialized(rows):
        if not rows:
            return []
        ids = [row['id'] for row in rows]
        placeholders = ','.join('?' for _ in ids)
        attachments = {}
        for item in db().execute('SELECT * FROM media WHERE message_id IN (' + placeholders + ') ORDER BY position', ids):
            attachments.setdefault(item['message_id'], []).append({
                'kind': item['kind'], 'available': bool(item['digest']),
                'url': url_for('media_file', digest=item['digest']) if item['digest'] else None,
                'filename': item['filename'] if item['digest'] else None,
            })
        result = []
        for row in rows:
            result.append({'id': row['id'], 'timestamp': row['timestamp'], 'sender': display_name(row),
                           'sender_id': row['participant_id'], 'body': row['body'], 'media': attachments.get(row['id'], [])})
        return result

    SELECT_MESSAGES = '''SELECT m.*,p.phone,p.raw_name,p.display_name FROM messages m
                         JOIN participants p ON p.id=m.participant_id'''

    @app.get('/api/messages')
    @require_login()
    def messages():
        limit = 50
        before, after, around = request.args.get('before'), request.args.get('after'), request.args.get('around')
        if sum(bool(x) for x in (before, after, around)) > 1:
            abort(400, description='Choose one message position.')
        if around:
            try:
                mid = int(around)
            except ValueError:
                abort(400)
            target = db().execute('SELECT timestamp,id FROM messages WHERE id=?', (mid,)).fetchone()
            if not target:
                abort(404, description='This message was not found.')
            older = db().execute(SELECT_MESSAGES + ' WHERE (m.timestamp,m.id)<(?,?) ORDER BY m.timestamp DESC,m.id DESC LIMIT 20', tuple(target)).fetchall()
            newer = db().execute(SELECT_MESSAGES + ' WHERE (m.timestamp,m.id)>=(?,?) ORDER BY m.timestamp,m.id LIMIT 31', tuple(target)).fetchall()
            rows = list(reversed(older)) + newer
        elif before:
            rows = list(reversed(db().execute(SELECT_MESSAGES + ' WHERE (m.timestamp,m.id)<(?,?) ORDER BY m.timestamp DESC,m.id DESC LIMIT ?', (*parse_cursor(before), limit)).fetchall()))
        elif after:
            rows = db().execute(SELECT_MESSAGES + ' WHERE (m.timestamp,m.id)>(?,?) ORDER BY m.timestamp,m.id LIMIT ?', (*parse_cursor(after), limit)).fetchall()
        else:
            rows = list(reversed(db().execute(SELECT_MESSAGES + ' ORDER BY m.timestamp DESC,m.id DESC LIMIT ?', (limit,)).fetchall()))
        older = bool(rows and db().execute('SELECT 1 FROM messages WHERE (timestamp,id)<(?,?) LIMIT 1', (rows[0]['timestamp'], rows[0]['id'])).fetchone())
        newer = bool(rows and db().execute('SELECT 1 FROM messages WHERE (timestamp,id)>(?,?) LIMIT 1', (rows[-1]['timestamp'], rows[-1]['id'])).fetchone())
        return jsonify(messages=serialized(rows), before=cursor(rows[0]) if rows else None,
                       after=cursor(rows[-1]) if rows else None, has_older=older, has_newer=newer)

    @app.get('/api/search')
    @require_login()
    def search():
        query = request.args.get('q', '').strip()[:300]
        words = re.findall(r'\w+', query, flags=re.UNICODE)[:12]
        try:
            offset = max(0, min(int(request.args.get('offset', 0)), 500000))
        except ValueError:
            abort(400)
        if not words:
            return jsonify(results=[], total=0, query=query, has_more=False)
        expression = ' AND '.join('"' + word + '"*' for word in words)
        total = db().execute('SELECT COUNT(*) FROM message_search WHERE message_search MATCH ?', (expression,)).fetchone()[0]
        rows = db().execute(SELECT_MESSAGES + ''' JOIN message_search ON message_search.rowid=m.id
            WHERE message_search MATCH ? ORDER BY m.timestamp DESC,m.id DESC LIMIT 40 OFFSET ?''', (expression, offset)).fetchall()
        results = serialized(rows)
        for result in results:
            body = result['body']
            lower = body.casefold()
            first = min((lower.find(word.casefold()) for word in words if word.casefold() in lower), default=0)
            start = max(0, first - 70)
            result['excerpt'] = ('…' if start else '') + body[start:start + 280] + ('…' if len(body) > start + 280 else '')
            result.pop('body')
        return jsonify(results=results, total=total, query=query, has_more=offset + len(results) < total)

    @app.get('/media/<digest>')
    @require_login()
    def media_file(digest):
        if not re.fullmatch(r'[a-f0-9]{64}', digest):
            abort(404)
        item = db().execute('SELECT * FROM media WHERE digest=? LIMIT 1', (digest,)).fetchone()
        path = Path(app.config['DATA_DIR']) / 'media' / digest
        if not item or not path.is_file():
            abort(404)
        extension = item['extension'] or ''
        mime = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp', '.gif': 'image/gif',
                '.avif': 'image/avif', '.mp4': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm', '.m4v': 'video/mp4',
                '.3gp': 'video/3gpp', '.opus': 'audio/ogg', '.ogg': 'audio/ogg', '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4',
                '.aac': 'audio/aac', '.wav': 'audio/wav'}.get(extension)
        return send_file(path, mimetype=mime or 'application/octet-stream', as_attachment=not bool(mime),
                         download_name=item['filename'] or 'attachment', conditional=True, max_age=0)

    @app.get('/admin')
    @require_login(admin=True)
    def admin():
        imports = [dict(row) for row in db().execute('SELECT * FROM imports ORDER BY id DESC LIMIT 10')]
        for record in imports:
            record['details'] = json.loads(record['report'])
        return render_template('admin.html', stats=stats(), imports=imports)

    @app.post('/admin/import')
    @require_login(admin=True)
    def upload():
        file = request.files.get('archive')
        if not file or not file.filename or not file.filename.lower().endswith('.zip'):
            flash('Choose a WhatsApp ZIP export to upload.', 'error')
            return redirect(url_for('admin'))
        order = request.form.get('date_order', 'day-first')
        if order not in ('day-first', 'month-first'):
            abort(400)
        temporary = None
        try:
            handle, name = tempfile.mkstemp(dir=Path(app.config['DATA_DIR']) / 'staging', suffix='.zip')
            os.close(handle)
            temporary = Path(name)
            file.save(temporary)
            report = import_zip(app.config['DATA_DIR'], temporary, file.filename, order)
            if report['already_imported']:
                flash('This ZIP was already imported. Your archive is unchanged.', 'success')
            else:
                flash(f"Import complete: {report['added']:,} new messages, {report['duplicates']:,} existing messages, and {report['media_added']:,} attachments added.", 'success')
                if report['missing_media']:
                    flash(f"{report['missing_media']:,} attachments were not included in this export. Their messages are still available.", 'notice')
        except ImportProblem as error:
            flash(str(error), 'error')
        except Exception:
            app.logger.exception('Archive import failed')
            flash('The import could not finish. Your previous messages are safe. Please try again or contact the server administrator.', 'error')
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return redirect(url_for('admin'))

    @app.get('/admin/senders')
    @require_login(admin=True)
    def senders():
        query = request.args.get('q', '').strip()[:100]
        try:
            page = max(1, int(request.args.get('page', 1)))
        except ValueError:
            abort(400)
        term = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        condition = "p.merged_into IS NULL AND (COALESCE(p.raw_name,'') LIKE ? ESCAPE '\\' OR COALESCE(p.phone,'') LIKE ? ESCAPE '\\' OR COALESCE(p.display_name,'') LIKE ? ESCAPE '\\')"
        rows = db().execute('SELECT p.*,(SELECT COUNT(*) FROM messages m WHERE m.participant_id=p.id) AS count FROM participants p WHERE ' + condition + ' ORDER BY COALESCE(p.raw_name,p.phone) LIMIT 100 OFFSET ?', (term, term, term, (page - 1) * 100)).fetchall()
        total = db().execute('SELECT COUNT(*) FROM participants p WHERE ' + condition, (term, term, term)).fetchone()[0]
        people = [{**dict(row), 'label': display_name(row)} for row in rows]
        targets = [{**dict(row), 'label': display_name(row)} for row in db().execute('SELECT * FROM participants WHERE merged_into IS NULL ORDER BY COALESCE(raw_name,phone)')]
        return render_template('senders.html', people=people, targets=targets, query=query, total=total, page=page)

    @app.post('/admin/senders/<int:pid>')
    @require_login(admin=True)
    def rename_sender(pid):
        name = request.form.get('display_name', '').strip()
        if len(name) > 80 or any(ord(c) < 32 for c in name):
            abort(400, description='Display names must be shorter than 80 characters.')
        updated = db().execute('UPDATE participants SET display_name=? WHERE id=? AND merged_into IS NULL', (name or None, pid))
        if not updated.rowcount:
            abort(404)
        db().commit()
        flash('Display name saved for every message by this sender.', 'success')
        return redirect(url_for('senders'))

    @app.post('/admin/senders/link')
    @require_login(admin=True)
    def link_senders():
        try:
            source = int(request.form.get('source', ''))
            target = int(request.form.get('target', ''))
            if request.form.get('confirm') != 'yes':
                raise ImportProblem('Confirm that both records belong to the same person.')
            with db():
                merge_participants(db(), source, target)
            flash('Sender records linked. Both sets of messages now use the same display name.', 'success')
        except (ValueError, ImportProblem) as error:
            flash(str(error) or 'Choose two sender records.', 'error')
        return redirect(url_for('senders'))

    @app.get('/robots.txt')
    def robots():
        return 'User-agent: *\nDisallow: /\n', 200, {'Content-Type': 'text/plain'}

    @app.get('/healthz')
    def health():
        db().execute('SELECT 1').fetchone()
        return jsonify(status='ok')

    @app.errorhandler(413)
    def too_large(error):
        return render_template('error.html', code=413, message='This upload is too large. The ZIP limit is 512 MB.'), 413

    for code in (400, 403, 404, 500):
        def handle_error(error, status=code):
            message = error.description if status != 500 else 'The archive is temporarily unavailable. Please try again.'
            if request.path.startswith('/api/'):
                return jsonify(error=message), status
            return render_template('error.html', code=status, message=message), status
        app.register_error_handler(code, handle_error)
    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=int(os.environ.get('PORT', '8765')), debug=False)

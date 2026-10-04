import io
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from archive import ImportProblem, connect, import_zip, merge_participants, parse_chat


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path / 'private-data'


@pytest.fixture
def app(data_dir):
    return create_app({'TESTING': True, 'SECRET_KEY': 'test-secret-key-with-at-least-thirty-two-characters',
                       'READER_PASSWORD_HASH': generate_password_hash('fleet-test', method='pbkdf2:sha256:1000'),
                       'ADMIN_PASSWORD_HASH': generate_password_hash('admin-test', method='pbkdf2:sha256:1000'),
                       'DATA_DIR': str(data_dir), 'SESSION_COOKIE_SECURE': False})


def make_zip(tmp_path, text, extra=None, name='export.zip'):
    path = tmp_path / name
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('_chat.txt', text)
        for filename, content in (extra or {}).items():
            archive.writestr(filename, content)
    return path


def sign_in(client, admin=False):
    route = '/admin/login' if admin else '/login'
    page = client.get(route).text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    response = client.post(route, data={'csrf_token': csrf, 'password': 'admin-test' if admin else 'fleet-test'})
    assert response.status_code == 302
    with client.session_transaction() as cookie:
        return cookie['csrf']


def test_iphone_dates_multiline_and_midnight():
    rows = parse_chat('\u200e[2025-08-04, 12:00:00\u202fAM] ~\u202fJohn Porter: Hiking strap\nSecond line\n[2025-08-04, 12:00:01 PM] Kelly Pike: Good.')
    assert rows[0]['timestamp'] == '2025-08-04T00:00:00'
    assert rows[0]['sender'] == 'John Porter'
    assert rows[0]['body'] == 'Hiking strap\nSecond line'
    assert rows[1]['timestamp'] == '2025-08-04T12:00:01'


def test_android_and_ambiguous_dates():
    text = '4/8/25, 10:23 - +1 (555) 123-4567: Hello\nAn extra line'
    assert parse_chat(text)[0]['timestamp'] == '2025-08-04T10:23:00'
    assert parse_chat(text, 'month-first')[0]['timestamp'] == '2025-04-08T10:23:00'


def test_empty_message_keeps_its_sender():
    row = parse_chat('[2025-08-04, 10:23:07 AM] Kelly Pike:')[0]
    assert row['sender'] == 'Kelly Pike' and row['body'] == ''


def test_reimport_and_repeat_occurrences(tmp_path, data_dir):
    text = '[2025-08-04, 10:23:07 AM] John Porter: Same message\n' * 2
    first = import_zip(data_dir, make_zip(tmp_path, text))
    again = import_zip(data_dir, tmp_path / 'export.zip')
    changed_zip = make_zip(tmp_path, text, {'unreferenced.txt': 'extra'}, 'changed.zip')
    changed = import_zip(data_dir, changed_zip)
    assert first['added'] == 2
    assert again['already_imported'] and again['added'] == 0
    assert changed['added'] == 0 and changed['duplicates'] == 2
    with connect(data_dir) as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2


def test_incremental_new_and_old_imports_and_context(tmp_path, app, data_dir):
    initial = '[2026-04-01, 10:00:00 AM] Kelly Pike: Newer\n[2026-04-02, 10:00:00 AM] Kelly Pike: Latest'
    older = '[2024-04-01, 10:00:00 AM] Kelly Pike: Hiking straps advice\n[2026-04-01, 10:00:00 AM] Kelly Pike: Newer'
    import_zip(data_dir, make_zip(tmp_path, initial, name='new.zip'))
    report = import_zip(data_dir, make_zip(tmp_path, older, name='old.zip'))
    assert report['added'] == 1 and report['duplicates'] == 1
    client = app.test_client()
    sign_in(client)
    rows = client.get('/api/messages').json['messages']
    assert [r['body'] for r in rows] == ['Hiking straps advice', 'Newer', 'Latest']
    hit = client.get('/api/search?q=hiking+strap').json
    assert hit['total'] == 1
    context = client.get('/api/messages?around=' + str(hit['results'][0]['id'])).json
    assert context['messages'][-1]['body'] == 'Latest'
    assert all(r['sender'] == 'Kelly P.' for r in rows)


def test_missing_attachment_filled_later(tmp_path, data_dir):
    omitted = '[2025-08-04, 10:23:07 AM] John Porter: Here is the rig. image omitted'
    attached = '[2025-08-04, 10:23:07 AM] John Porter: Here is the rig. <attached: new-photo.jpg>'
    import_zip(data_dir, make_zip(tmp_path, omitted, name='omitted.zip'))
    report = import_zip(data_dir, make_zip(tmp_path, attached, {'new-photo.jpg': b'photo bytes'}, 'complete.zip'))
    assert report['added'] == 0 and report['media_added'] == 1
    with connect(data_dir) as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
        assert db.execute('SELECT digest FROM media').fetchone()[0]


def test_confident_phone_to_name_link(tmp_path, data_dir, app):
    body1 = 'A distinctive recommendation about adjusting the hiking straps.'
    body2 = 'Another distinctive recommendation about boat rigging and fittings.'
    phone_text = f'[2025-08-04, 10:23:07 AM] +1 555 123 4567: {body1}\n[2025-08-04, 10:24:07 AM] +1 555 123 4567: {body2}'
    named_text = phone_text.replace('+1 555 123 4567', 'Trevor Campbell')
    import_zip(data_dir, make_zip(tmp_path, phone_text, name='phone.zip'))
    client = app.test_client()
    sign_in(client)
    before = client.get('/api/messages')
    assert '15551234567' not in before.text
    assert before.json['messages'][0]['sender'] == 'Member · 4567'
    report = import_zip(data_dir, make_zip(tmp_path, named_text, name='named.zip'))
    assert report['names_linked'] == 1 and report['added'] == 0
    rows = client.get('/api/messages').json['messages']
    assert all(row['sender'] == 'Trevor C.' for row in rows)
    with connect(data_dir) as db:
        assert db.execute('SELECT phone FROM participants').fetchone()[0] == '15551234567'


def test_single_overlap_does_not_guess_identity(tmp_path, data_dir):
    text = '[2025-08-04, 10:23:07 AM] +1 555 123 4567: A distinctive message about sailing and boat tuning.'
    import_zip(data_dir, make_zip(tmp_path, text, name='phone.zip'))
    report = import_zip(data_dir, make_zip(tmp_path, text.replace('+1 555 123 4567', 'John Porter'), name='name.zip'))
    assert report['names_linked'] == 0


def test_authentication_covers_data_and_media(tmp_path, data_dir, app):
    text = '[2025-08-04, 10:23:07 AM] +1 555 123 4567: <attached: rig.jpg>'
    import_zip(data_dir, make_zip(tmp_path, text, {'rig.jpg': b'bytes'}))
    with connect(data_dir) as db:
        digest = db.execute('SELECT digest FROM media').fetchone()[0]
    client = app.test_client()
    for path in ('/api/messages', '/api/search?q=rig', '/media/' + digest):
        assert client.get(path).status_code == 401
    assert client.get('/').status_code == 302
    sign_in(client)
    assert client.get('/admin').status_code == 403
    assert client.get('/admin/senders').status_code == 403
    response = client.get('/media/' + digest, headers={'Range': 'bytes=0-1'})
    assert response.status_code == 206 and response.data == b'by'
    assert 'no-store' in response.headers['Cache-Control']


def test_csrf_and_wrong_admin_password(app):
    client = app.test_client()
    assert client.post('/login', data={'password': 'fleet-test'}).status_code == 400
    page = client.get('/admin/login').text
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page)[1]
    response = client.post('/admin/login', data={'csrf_token': csrf, 'password': 'fleet-test'})
    assert 'did not match' in response.text
    with client.session_transaction() as cookie:
        assert 'role' not in cookie


def test_admin_renaming_and_reader_privacy(tmp_path, app, data_dir):
    import_zip(data_dir, make_zip(tmp_path, '[2025-08-04, 10:23:07 AM] +1 555 123 4567: Useful advice'))
    admin = app.test_client()
    csrf = sign_in(admin, True)
    page = admin.get('/admin/senders')
    assert '+15551234567' in page.text
    response = admin.post('/admin/senders/1', data={'csrf_token': csrf, 'display_name': 'Nigel Thomas'})
    assert response.status_code == 302
    reader = app.test_client()
    sign_in(reader)
    assert reader.get('/api/messages').json['messages'][0]['sender'] == 'Nigel T.'
    assert '15551234567' not in reader.get('/api/messages').text


@pytest.mark.parametrize('filename', ['../escape.jpg', '/absolute.jpg', 'folder\\bad.jpg'])
def test_unsafe_zip_rejected_without_changes(tmp_path, data_dir, filename):
    zip_path = make_zip(tmp_path, '[2025-08-04, 10:23:07 AM] John Porter: Hi', {filename: b'bad'})
    with pytest.raises(ImportProblem):
        import_zip(data_dir, zip_path)
    with connect(data_dir) as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


def test_invalid_dates_not_silently_skipped(tmp_path, data_dir):
    path = make_zip(tmp_path, '[2025-99-04, 10:23:07 AM] John Porter: Invalid')
    with pytest.raises(ImportProblem):
        import_zip(data_dir, path)
    with connect(data_dir) as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0


def test_html_attachment_is_downloaded_not_executed(tmp_path, data_dir, app):
    path = make_zip(tmp_path, '[2025-08-04, 10:23:07 AM] John Porter: <attached: dangerous.html>', {'dangerous.html': '<script>alert(1)</script>'})
    import_zip(data_dir, path)
    client = app.test_client()
    sign_in(client)
    url = client.get('/api/messages').json['messages'][0]['media'][0]['url']
    response = client.get(url)
    assert response.headers['Content-Type'].startswith('application/octet-stream')
    assert response.headers['Content-Disposition'].startswith('attachment;')
    assert response.headers['X-Content-Type-Options'] == 'nosniff'


def test_upload_route_is_idempotent(tmp_path, app, data_dir):
    archive = make_zip(tmp_path, '[2025-08-04, 10:23:07 AM] John Porter: Uploaded advice')
    client = app.test_client()
    csrf = sign_in(client, True)
    for _ in range(2):
        response = client.post('/admin/import', data={'csrf_token': csrf, 'archive': (io.BytesIO(archive.read_bytes()), 'forum.zip')}, follow_redirects=True)
        assert response.status_code == 200
    assert 'already imported' in response.text
    with connect(data_dir) as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1


def test_durable_storage_across_app_restart(tmp_path, app, data_dir):
    import_zip(data_dir, make_zip(tmp_path, '[2025-08-04, 10:23:07 AM] John Porter: Persisted advice'))
    restarted = create_app(dict(app.config))
    client = restarted.test_client()
    sign_in(client)
    assert client.get('/api/search?q=persisted').json['total'] == 1


def test_linking_different_phone_numbers_refused(tmp_path, data_dir):
    text = '[2025-08-04, 10:23:07 AM] +1 555 123 4567: First\n[2025-08-04, 10:24:07 AM] +1 555 987 6543: Second'
    import_zip(data_dir, make_zip(tmp_path, text))
    with connect(data_dir) as db:
        with pytest.raises(ImportProblem):
            merge_participants(db, 1, 2)


def test_pagination_uses_dates_not_import_order(tmp_path, app, data_dir):
    def text(year):
        return '\n'.join(f'[{year}-08-04, 10:{i:02}:00 AM] John Porter: Advice {year}-{i}' for i in range(55))
    import_zip(data_dir, make_zip(tmp_path, text(2026), name='new.zip'))
    import_zip(data_dir, make_zip(tmp_path, text(2024), name='old.zip'))
    client = app.test_client()
    sign_in(client)
    latest = client.get('/api/messages').json
    assert latest['messages'][0]['timestamp'].startswith('2026')
    older = client.get('/api/messages', query_string={'before': latest['before']}).json
    assert older['messages'][0]['timestamp'].startswith('2024')
    assert older['messages'][-1]['timestamp'] < latest['messages'][0]['timestamp']
    after = client.get('/api/messages', query_string={'after': older['after']}).json
    assert after['messages'][0]['id'] == latest['messages'][0]['id']


def test_password_rotation_revokes_sessions(app):
    client = app.test_client()
    sign_in(client)
    app.config['READER_PASSWORD_HASH'] = generate_password_hash('changed-password', method='pbkdf2:sha256:1000')
    assert client.get('/api/messages').status_code == 401

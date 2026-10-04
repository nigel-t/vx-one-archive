"""WhatsApp export parsing, private storage, and incremental imports."""
import collections
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS participants (
 id INTEGER PRIMARY KEY, phone TEXT UNIQUE, raw_name TEXT,
 display_name TEXT, merged_into INTEGER REFERENCES participants(id)
);
CREATE TABLE IF NOT EXISTS aliases (
 alias TEXT PRIMARY KEY, participant_id INTEGER NOT NULL REFERENCES participants(id)
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,
 participant_id INTEGER NOT NULL REFERENCES participants(id),
 body TEXT NOT NULL, body_key TEXT NOT NULL, occurrence INTEGER NOT NULL DEFAULT 0,
 UNIQUE(timestamp, participant_id, body_key, occurrence)
);
CREATE INDEX IF NOT EXISTS idx_messages_chronology ON messages(timestamp, id);
CREATE INDEX IF NOT EXISTS idx_messages_identity ON messages(timestamp, body_key);
CREATE TABLE IF NOT EXISTS media (
 id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL REFERENCES messages(id),
 position INTEGER NOT NULL, kind TEXT NOT NULL, digest TEXT,
 filename TEXT, extension TEXT, UNIQUE(message_id, position)
);
CREATE TABLE IF NOT EXISTS imports (
 id INTEGER PRIMARY KEY, digest TEXT UNIQUE NOT NULL, filename TEXT NOT NULL,
 imported_at TEXT NOT NULL, report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
 address TEXT PRIMARY KEY, failures INTEGER NOT NULL, updated_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS message_search USING fts5(
 body, content='messages', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS message_insert AFTER INSERT ON messages BEGIN
 INSERT INTO message_search(rowid,body) VALUES(new.id,new.body);
END;
CREATE TRIGGER IF NOT EXISTS message_update AFTER UPDATE OF body ON messages BEGIN
 INSERT INTO message_search(message_search,rowid,body) VALUES('delete',old.id,old.body);
 INSERT INTO message_search(rowid,body) VALUES(new.id,new.body);
END;
CREATE TRIGGER IF NOT EXISTS message_delete AFTER DELETE ON messages BEGIN
 INSERT INTO message_search(message_search,rowid,body) VALUES('delete',old.id,old.body);
END;
"""

INVISIBLE = dict.fromkeys(map(ord, '\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff'), None)
HEADER = re.compile(r'^\[([^\]]+)\]\s*(.*)$')
ANDROID_HEADER = re.compile(r'^(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}),?\s+(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap][Mm])?)\s+-\s+(.*)$')
ATTACHED = re.compile(r'<attached:\s*([^>]+)>', re.I)
OMITTED = re.compile(r'\b(image|video|audio|sticker|document|GIF|media) omitted\b', re.I)
FILE_ATTACHED = re.compile(r'([^\n<>]+?)\s+\(file attached\)', re.I)
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm', '.m4v', '.3gp'}
AUDIO_EXTENSIONS = {'.opus', '.ogg', '.mp3', '.m4a', '.aac', '.wav'}


class ImportProblem(ValueError):
    pass


def connect(data_dir):
    db = sqlite3.connect(str(Path(data_dir) / 'archive.sqlite3'), timeout=60)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=60000')
    return db


def initialize(data_dir):
    for folder in ('media', 'originals', 'staging'):
        (Path(data_dir) / folder).mkdir(parents=True, exist_ok=True)
        (Path(data_dir) / folder).chmod(0o700)
    Path(data_dir).chmod(0o700)
    with connect(data_dir) as db:
        db.executescript(SCHEMA)
    (Path(data_dir) / 'archive.sqlite3').chmod(0o600)


def clean_sender(value):
    return unicodedata.normalize('NFC', value.translate(INVISIBLE)).replace('\u202f', ' ').strip().lstrip('~').strip()


def phone_number(value):
    value = clean_sender(value)
    if not re.fullmatch(r'\+?[\d\s().-]{7,25}', value):
        return None
    digits = re.sub(r'\D', '', value)
    return digits if 7 <= len(digits) <= 15 else None


def alias_key(value):
    phone = phone_number(value)
    return 'phone:' + phone if phone else 'name:' + clean_sender(value).casefold()


def short_name(value):
    parts = clean_sender(value).split()
    if not parts:
        return 'Forum'
    return parts[0] + (' ' + parts[-1][0].upper() + '.' if len(parts) > 1 else '')


def display_name(participant):
    if participant['display_name']:
        override_phone = phone_number(participant['display_name'])
        return 'Member · ' + override_phone[-4:] if override_phone else short_name(participant['display_name'])
    if participant['raw_name']:
        return short_name(participant['raw_name'])
    return 'Member · ' + participant['phone'][-4:] if participant['phone'] else 'Forum'


def parse_timestamp(value, date_order='day-first'):
    value = value.replace('\u202f', ' ').replace('\xa0', ' ').strip()
    value = re.sub(r'\s+', ' ', value)
    pieces = re.match(r'^(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}),?\s+(.+)$', value)
    if not pieces:
        raise ImportProblem('A message date could not be read: ' + value[:80])
    raw_date, raw_time = pieces.groups()
    nums = list(map(int, re.split(r'[-/.]', raw_date)))
    if len(re.split(r'[-/.]', raw_date)[0]) == 4:
        year, month, day = nums
    else:
        first, second, year = nums
        if first > 12:
            day, month = first, second
        elif second > 12:
            month, day = first, second
        elif date_order == 'month-first':
            month, day = first, second
        else:
            day, month = first, second
        if year < 100:
            year += 2000
    raw_time = raw_time.upper().replace('.', '')
    raw_time = re.sub(r'(\d)(AM|PM)$', r'\1 \2', raw_time)
    for fmt in ('%I:%M:%S %p', '%I:%M %p', '%H:%M:%S', '%H:%M'):
        try:
            time = datetime.strptime(raw_time, fmt).time()
            return datetime(year, month, day, time.hour, time.minute, time.second).isoformat()
        except ValueError:
            pass
    raise ImportProblem('A message date or time is invalid: ' + value[:80])


def kind_for(filename):
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return 'image'
    if ext in VIDEO_EXTENSIONS:
        return 'video'
    if ext in AUDIO_EXTENSIONS:
        return 'audio'
    return 'document'


def parse_chat(text, date_order='day-first'):
    records = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line.translate(INVISIBLE)
        match = HEADER.match(line)
        android = ANDROID_HEADER.match(line) if not match else None
        if match and re.match(r'^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}', match[1]):
            date, rest = match.groups()
        elif android:
            date, time, rest = android.groups()
            date += ', ' + time
        else:
            if current is not None:
                current['raw_body'] += '\n' + line
            elif line.strip():
                raise ImportProblem('The chat text does not begin with a supported WhatsApp message date.')
            continue
        sender, separator, body = rest.partition(': ')
        if not separator and rest.endswith(':'):
            sender, separator, body = rest[:-1], ':', ''
        if not separator:
            sender, body = 'Forum', rest
        current = {'timestamp': parse_timestamp(date, date_order),
                   'sender': clean_sender(sender), 'raw_body': body}
        records.append(current)
    if not records:
        raise ImportProblem('No WhatsApp messages were found in this ZIP.')
    if len(records) > 500000:
        raise ImportProblem('This export is too large. Please split it into smaller exports.')
    for record in records:
        body = record.pop('raw_body')
        media = []

        def attachment(match):
            filename = Path(match[1].strip()).name
            kind = kind_for(filename)
            media.append({'filename': filename, 'kind': kind})
            return '\x00' + kind + '\x00'

        body = ATTACHED.sub(attachment, body)
        body = FILE_ATTACHED.sub(attachment, body)

        def omitted(match):
            kind = match[1].lower()
            kind = 'image' if kind in ('sticker', 'gif') else kind
            media.append({'filename': None, 'kind': kind})
            return '\x00' + kind + '\x00'

        body = OMITTED.sub(omitted, body)
        record['body_key'] = re.sub(r'\s+', ' ', body).strip()
        record['body'] = re.sub(r'\x00[^\x00]+\x00', '', body).strip()
        record['media'] = media
    return records


def _validated_zip(path):
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ImportProblem('This file is not a readable ZIP export.')
    items = z.infolist()
    if len(items) > 20000 or sum(i.file_size for i in items) > 2 * 1024**3:
        z.close()
        raise ImportProblem('The expanded ZIP exceeds the safe import limit (2 GB or 20,000 files).')
    index = {}
    for item in items:
        p = PurePosixPath(item.filename)
        if p.is_absolute() or '..' in p.parts or '\\' in item.filename or '\x00' in item.filename:
            z.close()
            raise ImportProblem('The ZIP contains an unsafe file path.')
        if item.flag_bits & 1 or (item.external_attr >> 16) & 0o170000 == 0o120000:
            z.close()
            raise ImportProblem('Encrypted ZIPs and symbolic links are not supported.')
        if item.file_size > 512 * 1024**2 or (item.file_size > 1024**2 and item.file_size / max(item.compress_size, 1) > 200):
            z.close()
            raise ImportProblem('An attachment is too large or too highly compressed.')
        if not item.is_dir() and '__MACOSX' not in p.parts and not p.name.startswith('.'):
            if p.name in index:
                z.close()
                raise ImportProblem('Two files have the same name. Please upload a single chat export.')
            index[p.name] = item
    return z, index


def resolve_sender(db, sender):
    key = alias_key(sender)
    known = db.execute('SELECT participant_id FROM aliases WHERE alias=?', (key,)).fetchone()
    if known:
        return known[0]
    phone = phone_number(sender)
    cursor = db.execute('INSERT INTO participants(phone,raw_name) VALUES(?,?)',
                        (phone, None if phone else clean_sender(sender)))
    pid = cursor.lastrowid
    db.execute('INSERT INTO aliases VALUES(?,?)', (key, pid))
    return pid


def _learn_aliases(db, records):
    """Only link a new name/number when two distinctive overlaps agree."""
    by_sender = collections.defaultdict(list)
    for record in records:
        by_sender[record['sender']].append(record)
    learned = 0
    for sender, messages in by_sender.items():
        key = alias_key(sender)
        if db.execute('SELECT 1 FROM aliases WHERE alias=?', (key,)).fetchone():
            continue
        candidates = set()
        evidence = set()
        for message in messages:
            if len(message['body']) < 24:
                continue
            matches = db.execute('SELECT DISTINCT participant_id FROM messages WHERE timestamp=? AND body_key=?',
                                 (message['timestamp'], message['body_key'])).fetchall()
            if len(matches) != 1:
                continue
            candidates.add(matches[0][0])
            evidence.add((message['timestamp'], message['body_key']))
        if len(candidates) != 1 or len(evidence) < 2:
            continue
        pid = candidates.pop()
        participant = db.execute('SELECT * FROM participants WHERE id=?', (pid,)).fetchone()
        incoming_phone = phone_number(sender)
        # Named exports need a phone-backed record; phone exports need a name-backed record.
        if incoming_phone:
            if participant['phone'] or not participant['raw_name']:
                continue
            db.execute('UPDATE participants SET phone=? WHERE id=?', (incoming_phone, pid))
        else:
            if not participant['phone'] or participant['raw_name']:
                continue
            db.execute('UPDATE participants SET raw_name=? WHERE id=?', (clean_sender(sender), pid))
        db.execute('INSERT INTO aliases VALUES(?,?)', (key, pid))
        learned += 1
    return learned


def import_zip(data_dir, path, filename=None, date_order='day-first'):
    data_dir, path = Path(data_dir), Path(path)
    initialize(data_dir)
    archive_hash = _digest_file(path)
    with connect(data_dir) as db:
        prior = db.execute('SELECT report FROM imports WHERE digest=?', (archive_hash,)).fetchone()
    if prior:
        report = json.loads(prior[0])
        return {**report, 'added': 0, 'duplicates': report['messages'], 'media_added': 0, 'already_imported': True}
    z, index = _validated_zip(path)
    chat_names = [name for name in index if name.endswith('.txt')]
    if '_chat.txt' in index:
        chat_name = '_chat.txt'
    elif len(chat_names) == 1:
        chat_name = chat_names[0]
    else:
        z.close()
        raise ImportProblem('Please upload one WhatsApp chat export containing a single chat text file.')
    if index[chat_name].file_size > 50 * 1024**2:
        z.close()
        raise ImportProblem('The chat text exceeds the 50 MB limit.')
    staging = Path(tempfile.mkdtemp(dir=data_dir / 'staging'))
    try:
        with z:
            try:
                records = parse_chat(z.read(index[chat_name]).decode('utf-8-sig'), date_order)
            except UnicodeDecodeError:
                raise ImportProblem('The chat text must use UTF-8 encoding.')
            attachments = {}
            for record in records:
                for media in record['media']:
                    name = media['filename']
                    if not name or name not in index or name in attachments:
                        continue
                    h = hashlib.sha256()
                    dest = staging / str(len(attachments))
                    with z.open(index[name]) as src, dest.open('wb') as out:
                        while True:
                            chunk = src.read(1024**2)
                            if not chunk:
                                break
                            h.update(chunk)
                            out.write(chunk)
                    attachments[name] = {'digest': h.hexdigest(), 'extension': Path(name).suffix.lower(), 'staged': dest}
        with connect(data_dir) as db:
            db.execute('BEGIN IMMEDIATE')
            # Another worker may have completed the same import while this worker staged files.
            prior = db.execute('SELECT report FROM imports WHERE digest=?', (archive_hash,)).fetchone()
            if prior:
                report = json.loads(prior[0])
                return {**report, 'added': 0, 'duplicates': report['messages'], 'media_added': 0, 'already_imported': True}
            learned = _learn_aliases(db, records)
            counts = collections.Counter()
            added = duplicates = media_added = missing = 0
            for record in records:
                pid = resolve_sender(db, record['sender'])
                key = (record['timestamp'], pid, record['body_key'])
                occurrence = counts[key]
                counts[key] += 1
                existing = db.execute('SELECT id FROM messages WHERE timestamp=? AND participant_id=? AND body_key=? AND occurrence=?',
                                      (*key, occurrence)).fetchone()
                if existing:
                    mid = existing[0]
                    duplicates += 1
                else:
                    mid = db.execute('INSERT INTO messages(timestamp,participant_id,body,body_key,occurrence) VALUES(?,?,?,?,?)',
                                     (record['timestamp'], pid, record['body'], record['body_key'], occurrence)).lastrowid
                    added += 1
                for position, media in enumerate(record['media']):
                    file = attachments.get(media['filename'])
                    digest = file['digest'] if file else None
                    existing_media = db.execute('SELECT digest FROM media WHERE message_id=? AND position=?', (mid, position)).fetchone()
                    if digest and (not existing_media or not existing_media['digest']):
                        destination = data_dir / 'media' / digest
                        if not destination.exists():
                            shutil.copyfile(file['staged'], destination)
                            os.chmod(destination, 0o600)
                        media_added += 1
                    elif not digest:
                        missing += 1
                    db.execute('''INSERT INTO media(message_id,position,kind,digest,filename,extension) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(message_id,position) DO UPDATE SET
                        digest=COALESCE(media.digest,excluded.digest),
                        filename=CASE WHEN media.digest IS NULL THEN excluded.filename ELSE media.filename END,
                        extension=CASE WHEN media.digest IS NULL THEN excluded.extension ELSE media.extension END''',
                        (mid, position, media['kind'], digest, media['filename'], file['extension'] if file else None))
            report = {'messages': len(records), 'added': added, 'duplicates': duplicates,
                      'media_added': media_added, 'missing_media': missing, 'names_linked': learned,
                      'earliest': min(r['timestamp'] for r in records), 'latest': max(r['timestamp'] for r in records),
                      'already_imported': False}
            db.execute('INSERT INTO imports(digest,filename,imported_at,report) VALUES(?,?,?,?)',
                       (archive_hash, Path(filename or path.name).name, datetime.now().isoformat(), json.dumps(report)))
            original = data_dir / 'originals' / (archive_hash + '.zip')
            if not original.exists():
                shutil.copyfile(path, original)
                os.chmod(original, 0o600)
            db.execute('PRAGMA optimize')
        return report
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise ImportProblem('The ZIP is damaged or could not be decompressed.') from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            h.update(block)
    return h.hexdigest()


def merge_participants(db, source_id, target_id):
    if source_id == target_id:
        raise ImportProblem('Choose two different sender records.')
    source = db.execute('SELECT * FROM participants WHERE id=? AND merged_into IS NULL', (source_id,)).fetchone()
    target = db.execute('SELECT * FROM participants WHERE id=? AND merged_into IS NULL', (target_id,)).fetchone()
    if not source or not target:
        raise ImportProblem('One of these sender records is no longer available.')
    if source['phone'] and target['phone'] and source['phone'] != target['phone']:
        raise ImportProblem('These records have different phone numbers. They cannot be linked automatically.')
    # Keep both message sets and repeat occurrences, so identity linking never drops content.
    for message in db.execute('SELECT * FROM messages WHERE participant_id=? ORDER BY timestamp,id', (source_id,)).fetchall():
        occurrence = db.execute('SELECT COALESCE(MAX(occurrence),-1)+1 FROM messages WHERE timestamp=? AND participant_id=? AND body_key=?',
                                (message['timestamp'], target_id, message['body_key'])).fetchone()[0]
        db.execute('UPDATE messages SET participant_id=?,occurrence=? WHERE id=?', (target_id, occurrence, message['id']))
    db.execute('UPDATE aliases SET participant_id=? WHERE participant_id=?', (target_id, source_id))
    db.execute('UPDATE participants SET phone=NULL,merged_into=? WHERE id=?', (target_id, source_id))
    db.execute('UPDATE participants SET phone=COALESCE(phone,?),raw_name=COALESCE(raw_name,?),display_name=COALESCE(display_name,?) WHERE id=?',
               (source['phone'], source['raw_name'], source['display_name'], target_id))

"""Operational tools. Originals, media, and database backups stay private."""
import argparse
import json
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import load_environment
from archive import connect, import_zip, initialize


def main():
    load_environment()
    parser = argparse.ArgumentParser(description='Manage the private VX One archive.')
    commands = parser.add_subparsers(dest='command', required=True)
    upload = commands.add_parser('import', help='Import a WhatsApp ZIP locally.')
    upload.add_argument('zip_file', type=Path)
    upload.add_argument('--date-order', choices=['day-first', 'month-first'], default='day-first')
    backup = commands.add_parser('backup', help='Create a consistent, portable backup ZIP.')
    backup.add_argument('destination', type=Path)
    commands.add_parser('info', help='Show archive counts and date coverage.')
    args = parser.parse_args()
    data_dir = Path(os.environ.get('DATA_DIR', Path(__file__).parent / 'data'))
    initialize(data_dir)
    if args.command == 'import':
        print(json.dumps(import_zip(data_dir, args.zip_file, date_order=args.date_order), indent=2))
    elif args.command == 'info':
        with connect(data_dir) as db:
            row = db.execute('SELECT COUNT(*) AS messages,MIN(timestamp) AS earliest,MAX(timestamp) AS latest FROM messages').fetchone()
            print(json.dumps(dict(row), indent=2))
    else:
        destination = args.destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise SystemExit('The destination already exists. Choose a new backup filename.')
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp) / 'archive.sqlite3'
            import sqlite3
            with connect(data_dir) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
            temporary = destination.with_name(destination.name + '.part')
            try:
                with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
                    archive.write(snapshot, 'archive.sqlite3')
                    archive.writestr('manifest.json', json.dumps({'format': 1, 'created_at': datetime.now(timezone.utc).isoformat()}))
                    # Data files are immutable; extras written during backup are harmless.
                    for directory in ('media', 'originals'):
                        for file in (data_dir / directory).iterdir():
                            if file.is_file():
                                archive.write(file, directory + '/' + file.name)
                temporary.chmod(0o600)
                temporary.rename(destination)
            finally:
                temporary.unlink(missing_ok=True)
        print('Backup saved to ' + str(destination))


if __name__ == '__main__':
    main()

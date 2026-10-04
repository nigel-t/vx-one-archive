"""Create private configuration interactively, without storing plaintext passwords."""
import argparse
import getpass
import secrets
from pathlib import Path
from werkzeug.security import generate_password_hash


def main():
    parser = argparse.ArgumentParser(description='Configure the VX One archive.')
    parser.add_argument('--local', action='store_true', help='Allow cookies over local HTTP for development only.')
    parser.add_argument('--data-dir', help='Private data directory (default: ./data).')
    args = parser.parse_args()
    target = Path(__file__).parent / '.env'
    if target.exists():
        raise SystemExit('.env already exists. Keep it, or back it up before creating new credentials.')
    reader = getpass.getpass('Choose the fleet password (at least 8 characters): ')
    admin = getpass.getpass('Choose a different administrator password (at least 12 characters): ')
    if len(reader) < 8 or len(admin) < 12 or reader == admin:
        raise SystemExit('Use distinct passwords of the required lengths.')
    data = Path(args.data_dir or Path(__file__).parent / 'data').resolve()
    # PBKDF2 is portable across supported Python installations.
    values = {'SECRET_KEY': secrets.token_hex(32),
              'READER_PASSWORD_HASH': generate_password_hash(reader, method='pbkdf2:sha256:1000000'),
              'ADMIN_PASSWORD_HASH': generate_password_hash(admin, method='pbkdf2:sha256:1000000'),
              'DATA_DIR': str(data), 'COOKIE_SECURE': 'false' if args.local else 'true',
              'TRUST_PROXY': 'false' if args.local else 'true',
              'TRUSTED_HOSTS': 'localhost,127.0.0.1,vxonetech.currentdesign.ca'}
    target.write_text('\n'.join(k + '=' + v for k, v in values.items()) + '\n')
    target.chmod(0o600)
    print('Private settings saved. You can now start the archive.')


if __name__ == '__main__':
    main()

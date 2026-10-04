# Deploy on the existing DigitalOcean server

The application is ready for a small Linux server. Inspect the machine's operating system, existing sites, available disk space, reverse proxy, and DNS before applying this guide. These files have not been installed on `143.198.33.91`. Reuse the existing web server so other sites keep working.

## 1. Install the application

Use a private repository under `nigel-t`, or copy the source package to `/opt/vx-one-archive`. Keep production data separate at `/var/lib/vx-one-archive`. The repository contains no chat history or credentials.

On a Debian/Ubuntu server with Python and systemd:

```sh
sudo useradd --system --home /var/lib/vx-one-archive --shell /usr/sbin/nologin vxarchive
sudo install -d -o vxarchive -g vxarchive -m 700 /var/lib/vx-one-archive
cd /opt/vx-one-archive
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python setup.py --data-dir /var/lib/vx-one-archive
sudo install -m 600 .env /etc/vx-one-archive.env
sudo install -m 644 deploy/vx-one-archive.service /etc/systemd/system/vx-one-archive.service
sudo systemctl daemon-reload
sudo systemctl enable --now vx-one-archive
```

Review the commands for existing users and folders rather than rerunning account creation. Keep application code readable by the service account and not writable by website users. After placing `.env` in `/etc`, remove its duplicate from the source checkout and keep the `/etc` copy protected. The service reads that file directly. Do not run the development server in production.

Setup prompts for a fleet password of at least 8 characters and a different administrator password of at least 12 characters. It stores password hashes and a random signing key, not plaintext passwords. Use a memorable fleet passphrase. Give the administrator password only to the uploader. Changing a password hash revokes sessions using the old password. Keep `COOKIE_SECURE=true` in production.

## 2. Configure the subdomain and HTTPS

Point the DNS A record for `vxonetech.currentdesign.ca` to `143.198.33.91` once the server destination is confirmed. Remove a stale AAAA record if IPv6 is not configured. Serve the archive only through HTTPS.

If the machine uses Caddy, add the block in `deploy/Caddyfile` to its existing configuration, validate it, and reload Caddy. It provisions and renews the domain certificate automatically when DNS and ports 80/443 are correct.

If the machine already uses Nginx, use `deploy/nginx-location.conf` inside that subdomain's existing HTTPS server block and configure its certificate through the server's normal certificate process. Keep other sites' blocks intact.

Gunicorn binds to `127.0.0.1:8765`, not a public interface. `TRUST_PROXY=true` trusts exactly one reverse proxy for client IPs and HTTPS. Do not enable it if the app is directly exposed or proxy headers can be spoofed. If the server has more proxy layers, adjust the configuration after inspecting them. The supplied Nginx snippet replaces client-supplied forwarding headers; Caddy sets its forwarding headers itself.

The password gate protects the page, search API, photos, videos, and downloads. `robots.txt` and no-index headers supplement authentication. Shared credentials do not identify individual readers or automatically enforce WhatsApp membership.

## 3. Load and verify the archive

Sign in at `/admin/login` and upload the original WhatsApp ZIP. Review the reported date coverage and missing media count. Later exports can overlap prior history; upload the same ZIP twice to check that it adds no new messages.

Before handing the URL to the fleet, verify:

- A signed-out browser cannot retrieve messages, search results, or a known media URL.
- Fleet credentials open the conversation but cannot open the administrator screens.
- Search finds an old message, opens it within the conversation, and allows reading onward.
- The latest message appears on entry; scrolling upward preserves position while older messages load.
- Photos render, videos play or download, and external links open normally.
- Sender overrides apply to existing history and the phone-backed sender stays linked on later imports.
- Restarting the service leaves all messages and attachments intact.
- A backup can be restored successfully to a separate test data directory.

## 4. Backups and restoration

The `manage.py backup` command uses SQLite's online backup API and includes the database, immutable media, and original exports. It excludes staging files and credentials.

```sh
# Run with DATA_DIR set to /var/lib/vx-one-archive, or through the service's protected environment.
.venv/bin/python manage.py backup /private-backup-location/vx-one-archive-2026-10-03.zip
```

Copy backups to a private location outside this server. Keep the environment settings separately in a protected password manager or server configuration backup. Neither data backups nor `.env` belong in GitHub or a public web directory. The server owner should arrange daily backups and retention using the existing backup service.

To restore, stop the archive service, keep a copy of the existing data directory, and restore the backup's `archive.sqlite3`, `media/`, and `originals/` into a new private data directory. Restore ownership to `vxarchive`, set the correct `DATA_DIR`, and start the service. Do not retain an old database's `-wal` or `-shm` files alongside the restored database. Start with an empty destination. Keep the fleet and administrator passwords unless you intend to rotate them.

## 5. Updates and maintenance

Deploy source updates without touching `/var/lib/vx-one-archive` or `/etc/vx-one-archive.env`. Install pinned requirements and run tests before restarting. Take a backup before changes to import logic or schema. Check service logs with the server's normal monitoring. No scheduled WhatsApp synchronization is enabled in this release.

The health endpoint `/healthz` returns only an availability status, not counts, names, or messages. Uploaded content is escaped as text; HTML and other non-media attachments are forced downloads. Video streaming supports byte-range requests.

## Required access to finish publication

- An SSH username and an existing SSH key/connection setup for `143.198.33.91`.
- Permission to administer that subdomain and its DNS.
- A private GitHub repository under `nigel-t`, accessible to the connected GitHub app, or an authenticated Git session that can create it.

Do not paste private keys, passwords, or tokens into chat. Use an existing local connection or the provider's normal secure access flow.

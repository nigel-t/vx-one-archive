# VX One Tech Forum Archive

A lightweight, private website that preserves the fleet's WhatsApp technical conversations. It opens at the latest archived message, loads older messages as you scroll upward, and searches every imported message. Selecting a search result opens the original conversation around that message.

## Included

- A phone-friendly chat feed with dates, shortened sender names, inline photos, video players, downloadable attachments, and clickable links.
- Whole-archive keyword search with word variants and partial-word matching. Multiple search words must all occur in the same message. Results show recent matches first; search includes text and captions, not text inside pictures, PDFs, or videos.
- A fleet password for readers and a separate administrator password for uploads and sender names.
- Incremental ZIP imports in either date direction, duplicate detection, missing-media recovery, and an import history showing actual coverage.
- Sender display names such as `Trevor C.`. Unnamed numbers display as `Member · 4567`. Full phone numbers and original contact names stay in private storage and the administrator interface.
- Administrator display-name overrides and manual linking of phone/name identities. A new contact name is linked automatically only when at least two distinctive overlapping messages uniquely identify the same phone-backed sender. WhatsApp exports do not always include both a contact name and its number, so the importer does not guess from a single overlap.
- Original ZIP retention and a consistent database-and-media backup tool.

The initial scope is one group, about 200 readers, and one administrator. Multiple administrator accounts and continuous WhatsApp synchronization are later work. Readers use the shared fleet password; membership is managed through password distribution rather than checked against WhatsApp.

## Local setup

Python 3.9+ is supported; Python 3.12 is recommended for deployment. There is no Node build, external font, hosted search service, or frontend framework to install.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python setup.py --local
.venv/bin/python app.py
```

Open `http://127.0.0.1:8765`. Sign in with the fleet password chosen during setup, or open `/admin/login` for the administrator. Local HTTP cookies are permitted only with `--local`; deployment uses secure HTTPS cookies.

Import through the administrator screen or from the command line:

```sh
.venv/bin/python manage.py import /path/to/export.zip
.venv/bin/python manage.py info
```

An iPhone export typically includes `_chat.txt` and media. iPhone bracketed and Android-style timestamps are accepted. ISO dates are automatic; slash dates require the correct day/month order in the import screen. Times remain as written in the export because the text export does not specify a timezone.

## Source coverage and import limits

The supplied sample contains **412 messages dated 2025-08-04 through 2026-04-17**, with **50 supplied attachments** (47 JPGs, one MP4, two WebP stickers) and **50 media omissions**. The importer preserves omission placeholders. The desired three years of history is a collection target, not a claim about this sample.

The site preserves what is present in exports. It cannot restore messages deleted before export or media omitted from all supplied files. Request older exports or other members' exports to extend coverage. A later export can fill a missing media placeholder when the sender, timestamp, caption, and media type match.

ZIP uploads are limited to 512 MB total request size, 2 GB expanded content, 20,000 entries, and 512 MB per file. Text is capped at 50 MB and 500,000 messages per import. Unsafe paths, symlinks, encrypted archives, excessive compression, unreadable dates, and ambiguous chat files are rejected. Files are staged before the database changes; failed imports leave the existing archive available. Imports are transactional and serialized in SQLite. Original exports and content-addressed media are stored outside the public static directory.

There are no stable WhatsApp message IDs in a text export. Deduplication uses timestamp, resolved sender, normalized text/media markers, and repeated-message occurrence. This preserves intentional repeats in a supplied export. Renamed contact identities, edited message text, and exports with inconsistent repeated-message subsets can require administrator attention; differing text is retained rather than silently overwritten. Manual identity linking preserves both message sets, including any duplicates already imported under different identities.

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for the existing DigitalOcean server, HTTPS configuration, passwords, backups, and launch checks. Production uses Gunicorn bound to loopback, behind the existing web server or Caddy. The domain is `vxonetech.currentdesign.ca`; the proposed server is `143.198.33.91`.

GitHub stores source only. `.env`, passwords, databases, media, ZIP exports, and backups are excluded. Runtime storage survives source updates and service restarts. Choose private repository visibility under `nigel-t`.

## Verification

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

Tests cover parsing, date interpretation, overlapping and out-of-order imports, repeated messages, media recovery, sender identity evidence, reader privacy, administrator authorization, CSRF, password rotation, malicious ZIP paths, file-download safety, video byte ranges, pagination, and persistence across application restarts.

Browser checks cover search-to-conversation navigation, jump to latest, lazy history, phone and desktop layouts, image rendering, and the administrator screens. Browsers supporting WebMCP can use the same search and message navigation as the interface; other browsers ignore that enhancement.

# Odoo Database Tool

A desktop app for **Windows**, **macOS** and **Linux** to dump and restore Odoo databases and filestores.
Add your servers once, then pull a database and its filestore, restore it into your local PostgreSQL and neutralize it, all with a few clicks and without opening a terminal.

## Features

- **Four ways to connect**: Odoo in a Docker container over SSH, PostgreSQL on a host over SSH, a direct PostgreSQL connection (optionally through an SSH tunnel), or the PostgreSQL on this computer.
- **Dump** a database (`pg_dump`, custom format) and/or its filestore (`.tar` or `.tar.gz`) into a folder, with a progress bar, a live log, a SHA256 checksum and a small manifest next to each file.
- **Restore** a dump into a local or direct PostgreSQL server: pick or create the target database, review a summary, confirm, and get a quick check of the result (size, tables, active users, installed modules).
- **Neutralize** a restored database with Odoo's own `neutralize.sql` files so it cannot send mail or run scheduled actions.
- **Safe by default**: see [Safety features](#safety-features).
- Light, dark or system theme.

## Download

Get the latest version from the **[Releases page](https://github.com/khaid2849/odoo_database_tool/releases)**:

| System | File | How to open |
|---|---|---|
| Windows 10 / 11 | `OdooDatabaseTool-windows-x64.zip` | Unzip and run `OdooDatabaseTool.exe`. If Windows SmartScreen appears, click **More info → Run anyway**. |
| macOS (Apple Silicon) | `OdooDatabaseTool-macos-arm64.zip` | Unzip and drag `OdooDatabaseTool.app` to **Applications**. The first time, right-click the app and choose **Open**. |
| Linux | `OdooDatabaseTool-linux-x64.tar.gz` | Extract it and run `OdooDatabaseTool/OdooDatabaseTool`. On Ubuntu you may need `sudo apt install libxcb-cursor0`. |

## Requirements

- **PostgreSQL client tools** (`psql`, `pg_dump`, `pg_restore`, `createdb`, `dropdb`, `vacuumdb`) on the computer running the app. Their major version must be **equal to or newer than the server's**. The app finds them on your `PATH` and in the usual install folders; if it cannot, set the folder in **Settings**.
- **OpenSSH** client (`ssh`) for the SSH connection types and tunnels. Logging in with an SSH *password* needs **OpenSSH 8.4 or newer**; keys and the SSH agent work with any version.
- **Docker** on the remote host for the *SSH + Docker* type. `pg_dump` runs inside the Odoo container, so the database password never leaves the server.

## How to use

1. Open the **Connections** tab, click **Add…** and choose a connection type. Fill in the details and click **Test** to check the connection.
2. Open the **Dump** tab, pick a connection, click **Load databases** and choose one, tick *Database* and/or *Filestore*, optionally press **Check**, then press **Dump**. Files land in `~/Odoo DB Dumps` by default (change it in **Settings**).
3. Open the **Restore** tab, pick a dump file, the target connection and database (an existing one or a new name), optionally a filestore, press **Restore**, review the summary and confirm.
4. When the restore finishes, the app offers to neutralize the new database. For that, first add your Odoo source folders for each version (for example 18.0) in **Settings → Odoo addons sets**.

## Connection types

| Type | Use it when | Dump | Restore target | Filestore |
|---|---|---|---|---|
| SSH + Docker | Odoo runs in a container on a remote host | Yes | No | Yes (from the container) |
| SSH + host Postgres | PostgreSQL is installed on the remote host | Yes | No | If you set the filestore root |
| Direct Postgres | You can reach the PostgreSQL port, directly or through an SSH tunnel | Yes | Yes | No |
| Local | PostgreSQL on this computer | Yes | Yes | Yes |

## Safety features

- A connection marked **Production** can never be a restore target, and dumping from it asks for confirmation first.
- Restore refuses when the target database has other open connections, and lists them (pid, application, user, client).
- Dropping and recreating a database always shows a summary first; for a remote server you must type the database name.
- Dump files are written as `.part` and renamed only when complete; a lost connection or full disk never leaves a half file looking valid.
- SHA256 is checked on restore, and a dump that cannot be read or has no table data is refused.
- The dump's server version is compared with your `pg_dump` / `pg_restore` before anything starts.
- An optional **expected hostname** per SSH connection stops the app from touching a cloned or wrong machine.
- Passwords are stored in the OS keychain, never in files, logs or command lines.
- Filestore paths must end in `filestore/<database>`; nothing outside it is deleted.
- If a restore fails after the old database was dropped, the app says so clearly and offers to drop the incomplete database or keep it.

## Where data is stored

- **Connections** (no passwords) are saved as `connections.json` in:
  - macOS: `~/Library/Application Support/OdooDatabaseTool/`
  - Windows: `%APPDATA%\OdooDatabaseTool\`
  - Linux: `~/.config/odoo_database_tool/`
- **Passwords** go to the OS keychain (macOS Keychain, Windows Credential Manager, Secret Service on Linux) under the service name `odoo_database_tool`. If your system has no keychain, the app asks for the password each time and keeps it in memory only.
- **Dumps, checksums (`SHA256SUMS`), manifests and logs** go to the dump folder you choose.

## Build from source

```bash
python -m pip install ".[build]"
python scripts/build.py
```

The app and a zip (macOS, Windows) or tar.gz (Linux) appear in `dist/`.
Pushing a `v*` tag builds all three systems on GitHub and publishes a release.

## Development

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

Integration tests run against a real PostgreSQL server and are skipped unless `ODT_TEST_PG_URL` is set, for example:

```bash
ODT_TEST_PG_URL=postgresql://postgres:postgres@localhost:5432/postgres python -m pytest -m integration -v
```

They create and drop their own `odt_it_*` databases, so only point them at a throw-away server. GitHub Actions runs them against a `postgres:14` service container.

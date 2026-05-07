# mysql-backup-manager

Created to be used in a project, this package is published to github for ease of management and installation across different modules.

`mysql-backup-manager` is a Python library for backing up and restoring MySQL databases with the native MySQL client tools.

It uses:

- `mysqldump` for backups
- `mysql` for restores

The package is built for application code, scheduled jobs, and operational tooling. It provides typed Pydantic v2 configuration, async APIs, sync convenience methods, gzip compression, checksum files, retention cleanup, scheduling, safe subprocess execution, and testable command builders.

## Table of Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Connection Configuration](#connection-configuration)
- [Backup Usage](#backup-usage)
- [Restore Usage](#restore-usage)
- [Retention Cleanup](#retention-cleanup)
- [Scheduled Backups](#scheduled-backups)
- [Helper Functions](#helper-functions)
- [Configuration Reference](#configuration-reference)
- [Result Models](#result-models)
- [Logging](#logging)
- [Security Notes](#security-notes)
- [Testing](#testing)
- [Limitations](#limitations)

## Requirements

- Python `>=3.11`
- MySQL client tools installed on the host:
  - `mysqldump`
  - `mysql`

Check that the tools are available:

```bash
mysqldump --version
mysql --version
```

If they are not on `PATH`, pass custom executable paths with `DumpConfig.mysqldump_path`, `DumpConfig.mysql_path`, and `RestoreConfig.mysql_path`. The backup path uses `mysql` for the default database-existence preflight check before running `mysqldump`.

Paths such as `Path("~/Downloads/backups")` are expanded with `Path.expanduser()`.

## Installation

From PyPI:

```bash
pip install mysql-backup-manager
```

For local development from this repository:

```bash
python -m pip install -e ".[test]"
```

Run tests:

```bash
python -m pytest
```

## Quick Start

### Back Up One Database

```python
from pathlib import Path

from mysql_backup_manager import (
    DumpConfig,
    MySQLBackupManager,
    MySQLConnectionConfig,
)

manager = MySQLBackupManager(
    connection=MySQLConnectionConfig(
        host="localhost",
        port=3306,
        user="root",
        password="secret",
    ),
    dump=DumpConfig(
        databases=["app"],
        output_dir=Path("./backups"),
        compress=True,
        generate_checksum=True,
        command_timeout=3600,
    ),
)

result = manager.backup_database_sync("app")

if result.success:
    print("Backup written to:", result.compressed_file or result.output_file)
    print("Checksum:", result.checksum)
else:
    print("Backup failed:", result.error)
```

A compressed backup creates files similar to:

```text
backups/app_20260506_120000.sql.gz
backups/app_20260506_120000.sql.gz.sha256
```

### Restore a Backup

```python
from pathlib import Path

from mysql_backup_manager import (
    DumpConfig,
    MySQLBackupManager,
    MySQLConnectionConfig,
    RestoreConfig,
)

manager = MySQLBackupManager(
    connection=MySQLConnectionConfig(user="root", password="secret"),
    dump=DumpConfig(databases=["app"], output_dir=Path("./backups")),
)

result = manager.restore_sync(
    RestoreConfig(
        database="app",
        input_file=Path("./backups/app_20260506_120000.sql.gz"),
        command_timeout=3600,
    )
)

print(result.success)
print(result.error)
```

## Connection Configuration

Use `MySQLConnectionConfig` for connection options shared by backups and restores.

```python
from mysql_backup_manager import MySQLConnectionConfig

connection = MySQLConnectionConfig(
    host="localhost",
    port=3306,
    user="backup_user",
    password="secret",
    default_character_set="utf8mb4",
    connect_timeout=10,
)
```

### Passwords

Passwords are never added to command arguments. When a password is available, the subprocess receives it through the `MYSQL_PWD` environment variable.

You can pass the password directly:

```python
MySQLConnectionConfig(user="root", password="secret")
```

Or provide it through the environment:

```bash
export MYSQL_PWD="secret"
```

```python
connection = MySQLConnectionConfig(user="root")
```

If `password` is omitted, `MySQLConnectionConfig` will read `MYSQL_PWD` from the current process environment when available.

### Unix Socket Connections

```python
connection = MySQLConnectionConfig(
    user="root",
    socket="/var/run/mysqld/mysqld.sock",
)
```

When `socket` is set, the generated command includes `--socket=...`.

## Backup Usage

Backups are handled by `MySQLBackupManager` and `BackupService`.

Use `MySQLBackupManager` for normal application code. Use `BackupService` directly when you want to test or inspect command building.

### Back Up All Configured Databases

```python
from pathlib import Path

from mysql_backup_manager import DumpConfig, MySQLBackupManager, MySQLConnectionConfig

manager = MySQLBackupManager(
    connection=MySQLConnectionConfig(user="root", password="secret"),
    dump=DumpConfig(
        databases=["app", "billing", "analytics"],
        output_dir=Path("./backups"),
        compress=True,
    ),
)

results = manager.backup_all_sync()

for result in results:
    print(result.database, result.success, result.compressed_file or result.output_file)
```

### Async Backup

```python
import asyncio
from pathlib import Path

from mysql_backup_manager import DumpConfig, MySQLBackupManager, MySQLConnectionConfig

async def main() -> None:
    manager = MySQLBackupManager(
        connection=MySQLConnectionConfig(user="root", password="secret"),
        dump=DumpConfig(databases=["app"], output_dir=Path("./backups")),
    )

    result = await manager.backup_database("app")
    print(result.success)

asyncio.run(main())
```

### Common Backup Options

```python
from pathlib import Path

from mysql_backup_manager import DumpConfig

backup_config = DumpConfig(
    databases=["app"],
    output_dir=Path("./backups"),
    filename_template="{database}_{timestamp}.sql",
    timestamp_format="%Y%m%d_%H%M%S",
    mysql_path="mysql",
    validate_database_exists=True,
    single_transaction=True,
    routines=True,
    triggers=True,
    events=True,
    add_drop_table=True,
    lock_tables=False,
    ignore_tables=[
        "app.audit_log",
        "app.sessions",
    ],
    extra_options=[
        "--hex-blob",
        "--quick",
    ],
    compress=True,
    generate_checksum=True,
    checksum_algorithm="sha256",
    command_timeout=3600,
    overwrite=False,
)
```

### Output Filenames

The default filename template is:

```text
{database}_{timestamp}.sql
```

The template must include both `{database}` and `{timestamp}`.

Example:

```python
DumpConfig(
    databases=["app"],
    output_dir=Path("./backups"),
    filename_template="{database}_{timestamp}.sql",
    timestamp_format="%Y%m%d_%H%M%S",
)
```

For safety, the rendered filename must be a plain filename. It cannot include path traversal such as `../backup.sql`.


### Database Existence Preflight

By default, backups first run small `mysql` queries against `INFORMATION_SCHEMA` to verify that the requested database exists and that at least one table or view is visible to the configured user. After `mysqldump` completes, the raw SQL is also checked for table/view definitions or inserted row data before it is moved, compressed, or checksummed. This prevents typos such as `amazon_x`, catches grant mistakes where the user can see the schema but cannot see/dump its tables, and rejects misleading header-only dump files.

If the database is missing, not visible, has no visible tables/views, or produces a header-only dump while visible objects exist, `BackupResult.success` is `False` and no final backup file is written. The helper `backup()` function raises `RuntimeError` in that case.

```python
DumpConfig(
    databases=["app"],
    output_dir=Path("./backups"),
    validate_database_exists=True,
    validate_database_has_objects=True,
    validate_dump_content=True,
    mysql_path="mysql",
)
```

Set `validate_database_exists=False`, `validate_database_has_objects=False`, or `validate_dump_content=False` only if you deliberately want to skip those checks, for example when backing up an intentionally empty database or using unusual `mysqldump` options that produce nonstandard SQL output.

### Compression

Set `compress=True` to create `.sql.gz` files:

```python
DumpConfig(
    databases=["app"],
    output_dir=Path("./backups"),
    compress=True,
)
```

The backup flow writes the raw dump to a hidden temporary `.part` file in `output_dir`, moves it into place, then compresses through another hidden temporary file. Normal failures and Ctrl+C cancellation clean up the active temp file. A non-catchable kill such as `kill -9` or a power loss can still leave stale `.part` files; by default, future backups make a best-effort pass to remove library-created UUID-shaped `.part` files older than one day.

### Checksums

Checksums are enabled by default.

```python
DumpConfig(
    databases=["app"],
    output_dir=Path("./backups"),
    generate_checksum=True,
    checksum_algorithm="sha256",
)
```

Supported algorithms:

- `sha256`
- `md5`

Checksum files are written next to the backup:

```text
app_20260506_120000.sql.gz
app_20260506_120000.sql.gz.sha256
```

The checksum file format is:

```text
<checksum>  <filename>
```

### Inspect the Generated mysqldump Command

```python
from pathlib import Path

from mysql_backup_manager.backup import BackupService
from mysql_backup_manager import DumpConfig, MySQLConnectionConfig

service = BackupService(
    connection=MySQLConnectionConfig(user="root", password="secret"),
    config=DumpConfig(databases=["app"], output_dir=Path("./backups")),
)

command = service.build_command("app")
print(command)
```

The password will not appear in the command.

## Restore Usage

Restores are handled by `RestoreConfig`, `RestoreService`, and the manager restore methods.

### Restore Into an Existing Database

```python
from pathlib import Path

from mysql_backup_manager import RestoreConfig

restore_config = RestoreConfig(
    database="app",
    input_file=Path("./backups/app_20260506_120000.sql.gz"),
    force=False,
    command_timeout=3600,
)
```

When `database` is set and `create_database_if_missing=False`, the generated `mysql` command ends with that database name. MySQL requires that database to already exist.

### Restore Into a New Database

If you want to restore a dump into a database that may not exist yet, enable `create_database_if_missing`:

```python
result = manager.restore_sync(
    RestoreConfig(
        database="app_copy",
        input_file=Path("./backups/app_20260506_120000.sql.gz"),
        create_database_if_missing=True,
        command_timeout=3600,
    )
)
```

With this option enabled, the restore command connects without a database argument and prefixes the SQL stream with:

```sql
CREATE DATABASE IF NOT EXISTS `app_copy`;
USE `app_copy`;
```

### Let the SQL File Select the Database

If the dump contains `CREATE DATABASE` or `USE` statements, set `database=None`:

```python
RestoreConfig(
    database=None,
    input_file=Path("./backups/full_dump.sql.gz"),
)
```

### Restore `.sql` or `.sql.gz`

Both formats are supported:

```python
RestoreConfig(input_file=Path("./backups/app.sql"))
RestoreConfig(input_file=Path("./backups/app.sql.gz"))
```

For `.sql.gz`, the file is decompressed as it is streamed into `mysql`.

### Restore Dumps With GTID_PURGED Statements

Some MySQL servers add `SET @@GLOBAL.GTID_PURGED=...` to dumps. Restoring that dump into a server that already has overlapping GTID history can fail with an error like:

```text
ERROR 3546 (HY000): @@GLOBAL.GTID_PURGED cannot be changed
```

For non-replication restores, test restores, or restoring into a new local database, enable `strip_gtid_purged`. The filter removes actual `@@GLOBAL.GTID_PURGED` statements while preserving normal data rows that merely mention `GTID_PURGED`:

```python
result = manager.restore_sync(
    RestoreConfig(
        database="app_copy",
        input_file=Path("./backups/app.sql.gz"),
        create_database_if_missing=True,
        strip_gtid_purged=True,
        command_timeout=3600,
    )
)
```

For future backups where GTID state is not needed, you can also prevent the line at backup time:

```python
DumpConfig(
    databases=["app"],
    output_dir=Path("./backups"),
    set_gtid_purged="OFF",
)
```

Do not strip GTID state for replication bootstrap workflows unless you understand the GTID implications.

### Async Restore

```python
import asyncio
from pathlib import Path

from mysql_backup_manager import (
    DumpConfig,
    MySQLBackupManager,
    MySQLConnectionConfig,
    RestoreConfig,
)

async def main() -> None:
    manager = MySQLBackupManager(
        connection=MySQLConnectionConfig(user="root", password="secret"),
        dump=DumpConfig(databases=["app"], output_dir=Path("./backups")),
    )

    result = await manager.restore(
        RestoreConfig(database="app", input_file=Path("./backups/app.sql.gz"))
    )
    print(result.success)

asyncio.run(main())
```

### Inspect the Generated mysql Command

```python
from pathlib import Path

from mysql_backup_manager import MySQLConnectionConfig, RestoreConfig
from mysql_backup_manager.restore import RestoreService

service = RestoreService(
    connection=MySQLConnectionConfig(user="root", password="secret"),
    config=RestoreConfig(database="app", input_file=Path("./backups/app.sql")),
)

command = service.build_command()
print(command)
```

The password will not appear in the command.

## Retention Cleanup

Retention cleanup deletes old matching backup files inside the configured backup directory only.

```python
from pathlib import Path

from mysql_backup_manager import (
    DumpConfig,
    MySQLBackupManager,
    MySQLConnectionConfig,
    RetentionConfig,
)

manager = MySQLBackupManager(
    connection=MySQLConnectionConfig(user="root"),
    dump=DumpConfig(databases=["app"], output_dir=Path("./backups")),
    retention=RetentionConfig(
        enabled=True,
        keep_last=10,
        keep_days=30,
        match_pattern="*.sql*",
    ),
)

result = manager.cleanup_retention_sync()

print("Deleted:", result.deleted_files)
print("Kept:", result.kept_files)
```

Retention rules are deletion limits. If both `keep_last` and `keep_days` are set, a backup is deleted when it exceeds either limit:

- It is beyond the newest `keep_last` backup artifacts.
- It is older than `keep_days` days.

Set either option to `None` to disable that specific rule. For example, `keep_last=5, keep_days=None` keeps only the newest 5 matching backup artifacts. Files outside `output_dir` are never deleted.

## Scheduled Backups

`SchedulerService` can run backups forever until cancelled.

It supports:

- interval schedules
- cron schedules
- optional immediate first run
- non-overlapping execution
- retention cleanup after successful backup runs

### Interval Schedule

```python
import asyncio
from pathlib import Path

from mysql_backup_manager import (
    DumpConfig,
    MySQLBackupManager,
    MySQLConnectionConfig,
    ScheduleConfig,
    SchedulerService,
)

async def main() -> None:
    manager = MySQLBackupManager(
        connection=MySQLConnectionConfig(user="root", password="secret"),
        dump=DumpConfig(
            databases=["app"],
            output_dir=Path("./backups"),
            compress=True,
        ),
    )

    scheduler = SchedulerService(
        manager=manager,
        config=ScheduleConfig(
            enabled=True,
            interval_seconds=3600,
            run_immediately=True,
        ),
    )

    await scheduler.run_forever()

asyncio.run(main())
```

### Cron Schedule

```python
scheduler = SchedulerService(
    manager=manager,
    config=ScheduleConfig(
        enabled=True,
        cron="0 3 * * *",
        timezone="UTC",
        run_immediately=False,
    ),
)
```

The scheduler skips a run if the previous backup is still active.


## Helper Functions

`mysql_backup_manager.helper` provides the easiest synchronous API for starting backup, restore, and scheduled backup sessions without manually creating `MySQLBackupManager`, `DumpConfig`, `RestoreConfig`, `ScheduleConfig`, or `RetentionConfig`.

The helper functions are general-purpose. They expose the options most callers need and keep lower-level details on sensible defaults: UTF-8 character set, 10 second connection timeout, standard timestamped filenames, `mysqldump`/`mysql` executable names from `PATH`, gzip compression format, and SHA-256 checksums. Use `MySQLBackupManager`, `DumpConfig`, and `RestoreConfig` directly when you need custom native executable paths.

### One-Off Backup

```python
from pathlib import Path

from mysql_backup_manager.helper import backup

backup_file = backup(
    user="backup_user",
    password="secret",
    host="db.example.com",
    port=3306,
    database="app",
    backup_dir=Path("~/backups"),
    compress=True,
    generate_checksum=True,
    hex_blob=True,
    command_timeout=3600,
)

print("Backup file:", backup_file)
```

For multiple databases, pass a list to the same `database` argument. The helper returns a list of paths unless `return_results=True` is used:

```python
from mysql_backup_manager.helper import backup

results = backup(
    user="backup_user",
    password="secret",
    host="db.example.com",
    database=["app", "analytics"],
    backup_dir="~/backups",
    compress=True,
    return_results=True,
)

for result in results:
    print(result.database, result.success, result.compressed_file or result.output_file)
```

Common backup options available directly on `backup()` include:

- Connection: `host`, `port`, `user`, `password`, `socket`
- Output: `backup_dir`, `overwrite`
- Native client: `command_timeout`
- Dump behavior: `single_transaction`, `routines`, `triggers`, `events`, `add_drop_database`, `add_drop_table`, `create_options`, `lock_tables`, `flush_logs`, `master_data`, `set_gtid_purged`, `where`, `ignore_tables`, `extra_options`, `quick`, `hex_blob`
- Safety checks: `validate_database_exists`, `validate_database_has_objects`, `validate_dump_content`, `cleanup_stale_temp_files`, `stale_temp_file_age_seconds`
- Artifacts: `compress`, `generate_checksum`
- Return behavior: `raise_on_failure`, `return_results`

`quick=True` by default, which adds `--quick` so `mysqldump` streams rows instead of buffering large tables in memory. Set `quick=False` if you explicitly do not want that option. Set `hex_blob=True` to add `--hex-blob`, which dumps binary string columns using hexadecimal notation.

For a GTID-aware replica bootstrap dump, opt into the relevant MySQL flags explicitly:

```python
backup_file = backup(
    user="backup_user",
    password="secret",
    host="source.example.com",
    database="app",
    backup_dir="~/replica-bootstrap",
    compress=True,
    generate_checksum=True,
    set_gtid_purged="AUTO",
    hex_blob=True,
    extra_options=["--databases"],
    command_timeout=7200,
)
```

### One-Off Restore

```python
from pathlib import Path

from mysql_backup_manager.helper import restore

restore(
    user="restore_user",
    password="secret",
    host="db.example.com",
    backup_file=Path("~/backups/app_20260507_020000.sql.gz"),
    decompress=True,
    command_timeout=3600,
)
```

The simplified helper streams the backup file into `mysql` without passing a target database name. For database selection, create dumps with `--databases` so the file contains `CREATE DATABASE` and `USE` statements. Use the lower-level `RestoreConfig` API when you need to force a separate target database or inject `CREATE DATABASE IF NOT EXISTS`.

`restore()` can also return the full `RestoreResult` model:

```python
result = restore(
    user="restore_user",
    password="secret",
    host="db.example.com",
    backup_file="~/backups/app.sql.gz",
    verify_checksum_before_restore=True,
    return_result=True,
)

print(result.success, result.error)
```

Common restore options available directly on `restore()` include:

- Connection: `host`, `port`, `user`, `password`, `socket`
- Input: `backup_file`, `decompress`
- Native client: `command_timeout`, `extra_options`
- Restore behavior: `strip_gtid_purged`, `force`
- Checksum: `verify_checksum_before_restore`
- Return behavior: `raise_on_failure`, `return_result`

### Scheduled Backup

For scheduled backups, pass the same backup options plus schedule and retention options:

```python
from mysql_backup_manager.helper import scheduled_backup

scheduled_backup(
    user="backup_user",
    password="secret",
    host="db.example.com",
    database="app",
    backup_dir="~/backups",
    cron="0 2 * * *",
    timezone="Asia/Shanghai",
    run_immediately=False,
    compress=True,
    generate_checksum=True,
    hex_blob=True,
    retention_enabled=True,
    keep_last=7,
    keep_days=30,
    match_pattern="app_*.sql*",
    command_timeout=3600,
)
```

Use either `cron` or `interval_seconds`, not both. Cron schedules use `timezone`; interval schedules simply wait the configured number of seconds. Scheduled backups skip overlapping runs and can run retention cleanup after successful backup cycles.

### Checksum Verification

```python
from mysql_backup_manager.helper import verify_checksum

verify_checksum("~/backups/app.sql.gz")
```

`verify_checksum()` defaults to SHA-256 and expects a sidecar next to the backup file, such as `app.sql.gz.sha256`. Pass `"md5"` only when verifying a sidecar produced outside the simplified helper API.

## Configuration Reference

### `MySQLConnectionConfig`

| Field | Default | Description |
| --- | --- | --- |
| `host` | `"localhost"` | MySQL host. |
| `port` | `3306` | MySQL port. |
| `user` | required | MySQL user. |
| `password` | `None` | Optional password. Hidden from repr and command args. |
| `socket` | `None` | Optional Unix socket path. |
| `default_character_set` | `"utf8mb4"` | Passed as `--default-character-set`. |
| `connect_timeout` | `10` | MySQL client connection timeout. Used by restore commands; `mysqldump` compatibility varies, so backup runtime should be bounded with `DumpConfig.command_timeout`. |

### `DumpConfig`

| Field | Default | Description |
| --- | --- | --- |
| `databases` | required | Databases available for backup. Must not be empty. |
| `output_dir` | required | Backup directory. Created if missing. |
| `filename_template` | `"{database}_{timestamp}.sql"` | Output filename template. |
| `timestamp_format` | `"%Y%m%d_%H%M%S"` | `datetime.strftime` format. |
| `mysqldump_path` | `"mysqldump"` | Path or executable name for `mysqldump`. |
| `mysql_path` | `"mysql"` | Path or executable name for `mysql`, used by backup database-existence preflight validation. |
| `command_timeout` | `None` | Optional subprocess timeout in seconds. |
| `validate_database_exists` | `True` | Verify that the requested database exists and is visible before running `mysqldump`. |
| `validate_database_has_objects` | `True` | Verify that at least one table or view is visible before running `mysqldump`. Disable for intentionally empty databases. |
| `validate_dump_content` | `True` | Verify that a dump with visible objects contains table/view definitions or row data before finalizing the backup artifact. |
| `cleanup_stale_temp_files` | `True` | Best-effort removal of old hidden UUID-shaped `.part` files from interrupted backup attempts before starting a new backup. |
| `stale_temp_file_age_seconds` | `86400` | Minimum `.part` file age before stale temp cleanup deletes it. Set `None` to disable age-based deletion. |
| `single_transaction` | `True` | Add `--single-transaction`. |
| `routines` | `True` | Add `--routines`. |
| `triggers` | `True` | Add `--triggers`. |
| `events` | `True` | Add `--events`. |
| `add_drop_database` | `False` | Add `--add-drop-database`. |
| `add_drop_table` | `True` | Add `--add-drop-table`. |
| `create_options` | `True` | If false, add `--no-create-options`. |
| `lock_tables` | `False` | Add `--lock-tables`; otherwise add `--skip-lock-tables`. |
| `flush_logs` | `False` | Add `--flush-logs`. |
| `master_data` | `None` | Add `--master-data=<value>`. |
| `set_gtid_purged` | `None` | Add `--set-gtid-purged=<value>`. Supported values are `AUTO`, `ON`, and `OFF`; lowercase input is normalized. |
| `where` | `None` | Add `--where=<condition>`. |
| `ignore_tables` | `[]` | Tables to ignore, formatted as `db.table`. |
| `extra_options` | `[]` | Raw options appended before database name. |
| `compress` | `False` | Produce `.sql.gz`. |
| `compression_format` | `"gzip"` | Compression format. Currently only gzip. |
| `generate_checksum` | `True` | Write checksum sidecar file. |
| `checksum_algorithm` | `"sha256"` | `sha256` or `md5`. |
| `overwrite` | `False` | Whether existing final backup files may be overwritten. |

### `RestoreConfig`

| Field | Default | Description |
| --- | --- | --- |
| `database` | `None` | Target database. If omitted, SQL may choose database. |
| `input_file` | required | `.sql` or `.sql.gz` file. Must exist. |
| `mysql_path` | `"mysql"` | Path or executable name for `mysql`. |
| `command_timeout` | `None` | Optional subprocess timeout in seconds. |
| `create_database_if_missing` | `False` | Create and select `database` before streaming the dump. Requires `database` and is useful when restoring into a new database. |
| `strip_gtid_purged` | `False` | Remove dump statements that mutate `@@GLOBAL.GTID_PURGED` while streaming restore input. Useful for non-replication restores into servers with existing GTID history. |
| `force` | `False` | Add `--force`. |
| `extra_options` | `[]` | Raw options appended before database name. |
| `decompress` | `True` | Decompress `.sql.gz` while streaming into `mysql`. |

### `ScheduleConfig`

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `False` | Whether scheduling is enabled. |
| `cron` | `None` | Cron expression such as `0 3 * * *`. |
| `interval_seconds` | `None` | Interval in seconds. |
| `timezone` | `"UTC"` | Time zone used for cron schedules. |
| `run_immediately` | `False` | Run once before waiting for the first schedule. |

Use either `cron` or `interval_seconds`, not both. If `enabled=True`, one of them is required.

### `RetentionConfig`

| Field | Default | Description |
| --- | --- | --- |
| `enabled` | `True` | Whether cleanup is enabled. |
| `keep_last` | `10` | Delete matching backup artifacts beyond the newest N files. Use `None` to disable. |
| `keep_days` | `30` | Delete matching backup artifacts older than this many days. Use `None` to disable. |
| `match_pattern` | `"*.sql*"` | Glob pattern inside `output_dir`. |

## Result Models

### `BackupResult`

Important fields:

- `database`
- `success`
- `output_file`
- `compressed_file`
- `checksum_file`
- `checksum`
- `started_at`
- `finished_at`
- `elapsed_seconds`
- `file_size_bytes`
- `command`
- `stderr`
- `error`

Example:

```python
result = manager.backup_database_sync("app")

if not result.success:
    print(result.error)
    print(result.stderr)
```

### `RestoreResult`

Important fields:

- `success`
- `input_file`
- `database`
- `started_at`
- `finished_at`
- `elapsed_seconds`
- `command`
- `stderr`
- `error`

### `RetentionResult`

Important fields:

- `success`
- `deleted_files`
- `kept_files`
- `error`

## Logging

The library uses standard Python logging and does not configure global logging automatically.

Example application setup:

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
```

You can pass your own logger to `MySQLBackupManager`:

```python
import logging

logger = logging.getLogger("myapp.backups")

manager = MySQLBackupManager(
    connection=connection,
    dump=dump_config,
    logger=logger,
)
```

## Security Notes

- Passwords are never placed in command arguments.
- Passwords are passed to subprocesses through `MYSQL_PWD` when configured.
- Password-bearing `extra_options` such as `--password=...` or `-psecret` are rejected.
- `BackupResult.command` and `RestoreResult.command` do not contain passwords.
- The library never uses `shell=True`.
- Backup and compression output use hidden `.part` temporary files before replacing final files. Ctrl+C cancellation cleans up the active temp file when Python can handle the signal.
- Retention cleanup validates paths and will not delete files outside `output_dir`.
- Prefer a dedicated MySQL user with the minimum privileges needed for backup or restore.

Example backup user privileges depend on your use case, but commonly include permissions such as `SELECT`, `SHOW VIEW`, `TRIGGER`, `EVENT`, and `LOCK TABLES` when relevant.

## Testing

Install test dependencies:

```bash
python -m pip install -e ".[test]"
```

Run the test suite:

```bash
python -m pytest
```

The unit tests do not require a real MySQL server. They focus on configuration validation, command building, retention behavior, checksum generation, compression helpers, and scheduler behavior.

## Limitations

- A real backup requires `mysqldump` installed on the host. With the default `validate_database_exists=True`, backup also requires the `mysql` client for preflight validation.
- A real restore requires `mysql` installed on the host.
- Gzip is the only compression format currently supported.
- Command timeouts are opt-in; set `command_timeout` for strict runtime limits.
- `KeyboardInterrupt`/Ctrl+C can be handled gracefully, but `kill -9`, power loss, or host crashes cannot run Python cleanup handlers. Stale library-created `.part` files from those events are cleaned on later backups once they exceed `stale_temp_file_age_seconds`.
- The core restore service does not verify checksum files automatically before restore; use `restore(..., verify_checksum_before_restore=True)` or `verify_checksum()` when you want helper-level verification.
- This package intentionally does not provide its own command-line interface; use the Python API from your application, worker, or scheduler process.

## Operational Checklist

Before using this in production, confirm:

- `mysqldump` and `mysql` are installed on the backup host.
- `MYSQL_PWD` or another secret-injection mechanism is configured securely.
- The backup user has the required database privileges.
- `output_dir` is on storage with enough capacity.
- `RetentionConfig` matches your recovery policy.
- Backups are periodically restored into a test environment.
- `command_timeout` is set to a value appropriate for your database size.
- Logs are collected by your normal logging system.

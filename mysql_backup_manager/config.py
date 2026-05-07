"""Configuration models for mysql-backup-manager.

This module contains the Pydantic v2 models used to configure database
connections, backup behavior, restore behavior, scheduled execution, and
retention cleanup. The models validate unsafe or unsupported inputs early so
command builders and services can stay focused on execution.
"""

from __future__ import annotations

import os
from pathlib import Path
from string import Formatter
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from mysql_backup_manager.exceptions import BackupConfigError, RestoreConfigError


def _reject_password_options(options: list[str]) -> list[str]:
    """Reject raw MySQL password options in user-supplied CLI arguments.

    :param options: Raw option strings supplied through ``DumpConfig.extra_options`` or ``RestoreConfig.extra_options``.
    :return: Stripped option strings when no password-bearing option is present.
    :raises ValueError: If an option is blank, contains a null byte, or tries to pass a password with ``--password``, ``--password=...``, ``-p``, or ``-psecret``.

    ## Example:
    ```python
    _reject_password_options(["--quick"])
    # ['--quick']
    ```
    """

    normalized_options: list[str] = []
    for option in options:
        normalized = option.strip()
        if not normalized:
            raise ValueError("extra_options entries must not be empty")
        if "\x00" in normalized:
            raise ValueError("extra_options entries must not contain null bytes")
        has_password_option = (
            normalized == "--password"
            or normalized.startswith("--password=")
            or normalized == "-p"
            or normalized.startswith("-p")
        )
        if has_password_option:
            raise ValueError(
                "password options are not allowed in extra_options; "
                "use MySQLConnectionConfig.password or MYSQL_PWD"
            )
        normalized_options.append(normalized)
    return normalized_options


class MySQLConnectionConfig(BaseModel):
    """Connection settings shared by backup and restore commands.

    :param host: MySQL server hostname or IP address. Defaults to ``"localhost"``.
    :param port: TCP port for the server. Defaults to ``3306``.
    :param user: MySQL account name used by ``mysqldump`` and ``mysql``.
    :param password: Optional password. It is stored as ``SecretStr`` and passed to subprocesses through ``MYSQL_PWD`` rather than command arguments.
    :param socket: Optional Unix socket path. When provided, ``--socket=...`` is added to native client commands.
    :param default_character_set: Optional character set passed as ``--default-character-set``. Defaults to ``"utf8mb4"``.
    :param connect_timeout: Optional connection timeout for restore ``mysql`` commands. It is intentionally not used for ``mysqldump`` because some MySQL client builds reject that option.
    :return: A validated ``MySQLConnectionConfig`` instance.

    ## Example:
    ```python
    connection = MySQLConnectionConfig(user="root", password="secret")
    connection.host
    # 'localhost'
    ```
    """

    model_config = ConfigDict(extra="forbid")

    host: str = "localhost"
    port: int = Field(default=3306, ge=1, le=65535)
    user: str
    password: SecretStr | None = Field(default=None, repr=False)
    socket: str | None = None
    default_character_set: str | None = "utf8mb4"
    connect_timeout: int | None = Field(default=10, ge=1)

    @field_validator("host", "user")
    @classmethod
    def text_fields_must_not_be_empty(cls, value: str) -> str:
        """Trim and validate required connection text fields.

        :param value: The raw ``host`` or ``user`` value provided to the model.
        :return: The stripped non-empty value.
        :raises ValueError: If the value is empty after trimming whitespace.
        """

        stripped = value.strip()
        if not stripped:
            raise ValueError("connection text fields must not be empty")
        if "\x00" in stripped:
            raise ValueError("connection text fields must not contain null bytes")
        return stripped

    @field_validator("socket", "default_character_set")
    @classmethod
    def optional_text_fields_must_not_be_empty(cls, value: str | None) -> str | None:
        """Trim optional text fields when provided and reject blank strings.

        :param value: The raw optional ``socket`` or ``default_character_set`` value.
        :return: ``None`` when no value was provided, otherwise the stripped string.
        :raises ValueError: If the value is provided but empty after trimming whitespace.
        """

        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("optional connection text fields must not be empty when provided")
        if "\x00" in stripped:
            raise ValueError("optional connection text fields must not contain null bytes")
        return stripped

    @model_validator(mode="after")
    def load_password_from_environment(self) -> "MySQLConnectionConfig":
        """Load ``MYSQL_PWD`` when no password was provided explicitly.

        :return: The current ``MySQLConnectionConfig`` instance, possibly updated with a password read from the environment.

        ## Example:
        ```python
        # Export ``MYSQL_PWD`` before constructing the model when you do not want the
        # password in application code.
        ```
        """

        if self.password is None:
            env_password = os.getenv("MYSQL_PWD")
            if env_password:
                self.password = SecretStr(env_password)
        return self

    def password_value(self) -> str | None:
        """Return the plain password for internal subprocess environment setup.

        :return: The password string when configured, otherwise ``None``.
        Security:
            The returned value must never be logged or placed in command arguments. The
            process layer uses it only to populate ``MYSQL_PWD``.
        """

        return self.password.get_secret_value() if self.password is not None else None


class DumpConfig(BaseModel):
    """Configuration for creating backups with ``mysqldump``.

    :param databases: Non-empty list of database names that this config may back up.
    :param output_dir: Directory where backup files, compressed files, and checksum sidecars are written. ``~`` is expanded and the directory is created.
    :param filename_template: Template used for uncompressed SQL filenames. It must include ``{database}`` and ``{timestamp}`` and render to a plain filename.
    :param timestamp_format: ``datetime.strftime`` format used by ``filename_template``.
    :param mysqldump_path: Executable name or absolute path for ``mysqldump``.
    :param mysql_path: Executable name or absolute path for ``mysql`` used by backup preflight validation.
    :param command_timeout: Optional maximum runtime, in seconds, for each dump command.
    :param validate_database_exists: Check that the database exists and is visible to the configured user before running ``mysqldump``.
    :param validate_database_has_objects: Check that at least one table or view is visible before running ``mysqldump``. Disable this only when backing up intentionally empty databases.
    :param validate_dump_content: Check that the produced SQL contains table/view definitions or row data when visible objects exist.
    :param single_transaction: Add ``--single-transaction`` for consistent InnoDB dumps.
    :param routines: Include stored routines.
    :param triggers: Include triggers.
    :param events: Include events.
    :param add_drop_database: Add ``DROP DATABASE`` statements when supported by the dump mode.
    :param add_drop_table: Add ``DROP TABLE`` statements before table creation.
    :param create_options: Preserve MySQL-specific table options when true.
    :param lock_tables: Add ``--lock-tables`` when true; otherwise add ``--skip-lock-tables``.
    :param flush_logs: Add ``--flush-logs``.
    :param master_data: Optional ``--master-data`` value for replication workflows.
    :param set_gtid_purged: Optional ``--set-gtid-purged`` value such as ``"ON"`` or ``"OFF"``.
    :param where: Optional ``--where`` clause for partial table dumps.
    :param ignore_tables: Tables to skip, each formatted as ``db.table``.
    :param extra_options: Additional raw mysqldump options appended before the database name. Password options are rejected.
    :param compress: Produce ``.sql.gz`` output when true.
    :param compression_format: Compression format. Currently only ``"gzip"``.
    :param generate_checksum: Write a checksum sidecar when true.
    :param checksum_algorithm: ``"sha256"`` or ``"md5"``.
    :param overwrite: Allow an existing final backup artifact to be replaced.
    :return: A validated ``DumpConfig`` instance.

    ## Example:
    ```python
    from pathlib import Path
    dump = DumpConfig(databases=["app"], output_dir=Path("./backups"), compress=True)
    dump.output_dir.name
    # 'backups'
    ```
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    databases: list[str]
    output_dir: Path
    filename_template: str = "{database}_{timestamp}.sql"
    timestamp_format: str = "%Y%m%d_%H%M%S"
    mysqldump_path: str = "mysqldump"
    mysql_path: str = "mysql"
    command_timeout: float | None = Field(default=None, gt=0)
    validate_database_exists: bool = True
    validate_database_has_objects: bool = True
    validate_dump_content: bool = True
    single_transaction: bool = True
    routines: bool = True
    triggers: bool = True
    events: bool = True
    add_drop_database: bool = False
    add_drop_table: bool = True
    create_options: bool = True
    lock_tables: bool = False
    flush_logs: bool = False
    master_data: int | None = Field(default=None, ge=1, le=2)
    set_gtid_purged: str | None = None
    where: str | None = None
    ignore_tables: list[str] = Field(default_factory=list)
    extra_options: list[str] = Field(default_factory=list)
    compress: bool = False
    compression_format: Literal["gzip"] = "gzip"
    generate_checksum: bool = True
    checksum_algorithm: Literal["sha256", "md5"] = "sha256"
    overwrite: bool = False

    @field_validator("databases")
    @classmethod
    def databases_must_not_be_empty(cls, value: list[str]) -> list[str]:
        """Normalize configured database names and require at least one name.

        :param value: Raw database names supplied to ``DumpConfig.databases``.
        :return: A list of stripped database names.
        :raises BackupConfigError: If the list is empty or contains a blank name.
        """

        normalized = [db.strip() for db in value]
        if not normalized or not all(normalized):
            raise BackupConfigError("databases must contain at least one non-empty database name")
        if any("\x00" in db for db in normalized):
            raise BackupConfigError("database names must not contain null bytes")
        return normalized

    @field_validator("output_dir")
    @classmethod
    def create_output_dir(cls, value: Path) -> Path:
        """Expand and create the backup output directory if needed.

        :param value: Raw output directory path.
        :return: The expanded ``Path`` that exists on disk after validation.
        :raises OSError: If the directory cannot be created by the operating system.
        """

        expanded = value.expanduser()
        expanded.mkdir(parents=True, exist_ok=True)
        return expanded

    @field_validator("filename_template")
    @classmethod
    def filename_template_must_support_fields(cls, value: str) -> str:
        """Validate that filename templates support database and timestamp fields.

        :param value: Template string passed to ``DumpConfig.filename_template``.
        :return: The validated template string.
        :raises BackupConfigError: If the template omits ``{database}`` or ``{timestamp}``, or references unsupported format fields.
        """

        allowed_fields = {"database", "timestamp"}
        try:
            parsed_fields = [field_name for _, field_name, _, _ in Formatter().parse(value) if field_name is not None]
        except ValueError as exc:
            raise BackupConfigError("filename_template is not a valid format string") from exc

        if set(parsed_fields) - allowed_fields:
            raise BackupConfigError("filename_template must only use {database} and {timestamp}")
        if not allowed_fields.issubset(parsed_fields):
            raise BackupConfigError("filename_template must include {database} and {timestamp}")
        try:
            value.format(database="db", timestamp="20260101_000000")
        except (IndexError, KeyError, ValueError) as exc:
            raise BackupConfigError("filename_template must only use {database} and {timestamp}") from exc
        return value

    @field_validator("mysqldump_path", "mysql_path")
    @classmethod
    def executable_path_must_not_be_empty(cls, value: str) -> str:
        """Ensure native client executable path/name fields are not blank.

        :param value: Executable name or path supplied to ``mysqldump_path`` or ``mysql_path``.
        :return: The stripped executable name or path.
        :raises BackupConfigError: If the value is empty, whitespace only, or contains a null byte.
        """

        stripped = value.strip()
        if not stripped:
            raise BackupConfigError("native client executable paths must not be empty")
        if "\x00" in stripped:
            raise BackupConfigError("native client executable paths must not contain null bytes")
        return stripped

    @field_validator("ignore_tables")
    @classmethod
    def ignore_tables_must_be_safe(cls, value: list[str]) -> list[str]:
        """Normalize and validate ``--ignore-table`` values as ``db.table``.

        :param value: Raw ignore-table strings from ``DumpConfig.ignore_tables``.
        :return: A list of stripped ``db.table`` entries, with blank entries removed.
        :raises BackupConfigError: If any non-blank entry is not exactly ``database.table``.
        """

        normalized: list[str] = []
        for item in value:
            table = item.strip()
            if not table:
                continue
            if "\x00" in table:
                raise BackupConfigError("ignore_tables entries must not contain null bytes")
            if table.count(".") != 1 or any(part.strip() == "" for part in table.split(".")):
                raise BackupConfigError("ignore_tables entries must be formatted as db.table")
            normalized.append(table)
        return normalized

    @field_validator("extra_options")
    @classmethod
    def dump_extra_options_must_not_contain_password(cls, value: list[str]) -> list[str]:
        """Reject password-bearing raw mysqldump options.

        :param value: Raw mysqldump option strings supplied through ``extra_options``.
        :return: The validated list of option strings.
        :raises ValueError: If any option tries to pass a password on the command line.
        """

        return _reject_password_options(value)


class RestoreConfig(BaseModel):
    """Configuration for restoring SQL files with the ``mysql`` client.

    :param database: Optional target database. Leave as ``None`` when the dump contains ``CREATE DATABASE`` and ``USE`` statements, or provide a name to restore into one database.
    :param input_file: Existing ``.sql`` or ``.sql.gz`` file to stream into ``mysql``.
    :param mysql_path: Executable name or absolute path for the ``mysql`` client.
    :param command_timeout: Optional maximum restore runtime, in seconds.
    :param create_database_if_missing: Inject ``CREATE DATABASE IF NOT EXISTS`` and ``USE`` before the dump stream. Requires ``database``.
    :param strip_gtid_purged: Remove ``@@GLOBAL.GTID_PURGED`` statements while streaming. Keep this false for real GTID replica bootstrap restores.
    :param force: Add ``--force`` so mysql continues after SQL errors.
    :param extra_options: Additional raw mysql options. Password options are rejected.
    :param decompress: Automatically decompress ``.sql.gz`` files when true.
    :return: A validated ``RestoreConfig`` instance.

    ## Example:
    ```python
    from pathlib import Path
    path = Path("backup.sql")
    path.write_text("SELECT 1;", encoding="utf-8")
    # 9
    RestoreConfig(database="app", input_file=path).database
    # 'app'
    path.unlink()
    ```
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    database: str | None = None
    input_file: Path
    mysql_path: str = "mysql"
    command_timeout: float | None = Field(default=None, gt=0)
    create_database_if_missing: bool = False
    strip_gtid_purged: bool = False
    force: bool = False
    extra_options: list[str] = Field(default_factory=list)
    decompress: bool = True

    @field_validator("database")
    @classmethod
    def database_must_not_be_empty(cls, value: str | None) -> str | None:
        """Normalize the optional restore database name when provided.

        :param value: Raw database name or ``None``.
        :return: ``None`` or the stripped database name.
        :raises RestoreConfigError: If the provided name is blank or contains a null byte.
        """

        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise RestoreConfigError("database must not be empty when provided")
        if "\x00" in stripped:
            raise RestoreConfigError("database must not contain null bytes")
        return stripped

    @field_validator("input_file")
    @classmethod
    def input_file_must_exist_and_be_supported(cls, value: Path) -> Path:
        """Expand and validate the restore input path.

        :param value: Raw restore file path.
        :return: Expanded ``Path`` to an existing ``.sql`` or ``.sql.gz`` file.
        :raises RestoreConfigError: If the path does not exist or has an unsupported suffix.
        """

        expanded = value.expanduser()
        if not expanded.exists():
            raise RestoreConfigError(f"input_file does not exist: {expanded}")
        if not expanded.is_file():
            raise RestoreConfigError(f"input_file must be a file: {expanded}")
        suffixes = expanded.suffixes
        if expanded.suffix == ".sql" or suffixes[-2:] == [".sql", ".gz"]:
            return expanded
        raise RestoreConfigError("input_file must be a .sql or .sql.gz file")

    @field_validator("mysql_path")
    @classmethod
    def mysql_path_must_not_be_empty(cls, value: str) -> str:
        """Ensure the mysql executable path/name is not blank.

        :param value: Executable name or path supplied to ``mysql_path``.
        :return: The validated value.
        :raises RestoreConfigError: If the value is empty or whitespace only.
        """

        stripped = value.strip()
        if not stripped:
            raise RestoreConfigError("mysql_path must not be empty")
        if "\x00" in stripped:
            raise RestoreConfigError("mysql_path must not contain null bytes")
        return stripped

    @field_validator("extra_options")
    @classmethod
    def restore_extra_options_must_not_contain_password(cls, value: list[str]) -> list[str]:
        """Reject password-bearing raw mysql restore options.

        :param value: Raw mysql option strings supplied through ``extra_options``.
        :return: The validated list of option strings.
        :raises ValueError: If any option tries to pass a password on the command line.
        """

        return _reject_password_options(value)

    @model_validator(mode="after")
    def validate_restore_options(self) -> "RestoreConfig":
        """Validate restore option combinations that depend on multiple fields.

        :return: The current ``RestoreConfig`` instance after cross-field validation.
        :raises RestoreConfigError: If ``create_database_if_missing`` is true without a target ``database``.
        """

        if self.create_database_if_missing and not self.database:
            raise RestoreConfigError("create_database_if_missing requires database")
        return self


class ScheduleConfig(BaseModel):
    """Configuration for running backups repeatedly.

    :param enabled: Whether the scheduler should run. Disabled schedules return immediately from ``SchedulerService.run_forever``.
    :param cron: Optional cron expression such as ``"0 3 * * *"``.
    :param interval_seconds: Optional fixed interval in seconds.
    :param timezone: IANA timezone used for cron evaluation. Defaults to ``"UTC"``.
    :param run_immediately: Run one backup cycle before waiting for the next schedule.
    :return: A validated ``ScheduleConfig`` instance.

    ## Example:
    ```python
    ScheduleConfig(enabled=True, interval_seconds=3600).interval_seconds
    # 3600
    ```
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str = "UTC"
    run_immediately: bool = False

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduleConfig":
        """Validate interval/cron exclusivity and timezone/cron syntax.

        :return: The current ``ScheduleConfig`` instance after cross-field validation.
        :raises ValueError: If both schedule types are provided, if an enabled schedule has no timing rule, if the cron expression is invalid, or if the timezone is unknown.
        """

        if self.cron and self.interval_seconds is not None:
            raise ValueError("cron and interval_seconds are mutually exclusive")
        if self.enabled and not self.cron and self.interval_seconds is None:
            raise ValueError("enabled schedules require cron or interval_seconds")
        if self.cron and not croniter.is_valid(self.cron):
            raise ValueError("cron is not a valid cron expression")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"timezone is not valid: {self.timezone}") from exc
        return self


class RetentionConfig(BaseModel):
    """Configuration for deleting old backup files from an output directory.

    :param enabled: Disable cleanup entirely when false.
    :param keep_last: Delete matching backup artifacts beyond the newest N files. Set to ``None`` to disable this deletion rule.
    :param keep_days: Delete matching backup artifacts older than this many days. Set to ``None`` to disable this deletion rule.
    :param match_pattern: Relative glob used inside the backup directory, for example ``"*.sql*"`` or ``"app_*.sql*"``.
    :return: A validated ``RetentionConfig`` instance.

    ## Example:
    ```python
    RetentionConfig(keep_last=5, keep_days=14).keep_last
    # 5
    ```
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    keep_last: int | None = Field(default=10, ge=0)
    keep_days: int | None = Field(default=30, ge=0)
    match_pattern: str = "*.sql*"

    @field_validator("match_pattern")
    @classmethod
    def match_pattern_must_be_safe(cls, value: str) -> str:
        """Reject blank, absolute, or parent-traversing retention patterns.

        :param value: Raw glob pattern supplied to ``RetentionConfig.match_pattern``.
        :return: The stripped, relative pattern.
        :raises ValueError: If the pattern is empty, absolute, or contains ``..`` path parts.
        """

        stripped = value.strip()
        if not stripped:
            raise ValueError("match_pattern must not be empty")
        pattern_path = Path(stripped)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise ValueError("match_pattern must stay inside output_dir")
        return stripped


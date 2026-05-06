"""Pydantic v2 configuration models."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from mysql_backup_manager.exceptions import BackupConfigError, RestoreConfigError


def _reject_password_options(options: list[str]) -> list[str]:
    for option in options:
        normalized = option.strip()
        if normalized == "--password" or normalized.startswith("--password=") or normalized == "-p" or normalized.startswith("-p"):
            raise ValueError("password options are not allowed in extra_options; use MySQLConnectionConfig.password or MYSQL_PWD")
    return options


class MySQLConnectionConfig(BaseModel):
    """Connection options shared by mysqldump and mysql."""

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
        stripped = value.strip()
        if not stripped:
            raise ValueError("connection text fields must not be empty")
        return stripped

    @field_validator("socket", "default_character_set")
    @classmethod
    def optional_text_fields_must_not_be_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("optional connection text fields must not be empty when provided")
        return stripped

    @model_validator(mode="after")
    def load_password_from_environment(self) -> "MySQLConnectionConfig":
        """Load MYSQL_PWD when no password was provided explicitly."""

        if self.password is None:
            env_password = os.getenv("MYSQL_PWD")
            if env_password:
                self.password = SecretStr(env_password)
        return self

    def password_value(self) -> str | None:
        """Return the secret password value for subprocess environment use."""

        return self.password.get_secret_value() if self.password is not None else None


class DumpConfig(BaseModel):
    """Options controlling mysqldump backup behavior."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    databases: list[str]
    output_dir: Path
    filename_template: str = "{database}_{timestamp}.sql"
    timestamp_format: str = "%Y%m%d_%H%M%S"
    mysqldump_path: str = "mysqldump"
    command_timeout: float | None = Field(default=None, gt=0)
    single_transaction: bool = True
    routines: bool = True
    triggers: bool = True
    events: bool = True
    add_drop_database: bool = False
    add_drop_table: bool = True
    create_options: bool = True
    lock_tables: bool = False
    flush_logs: bool = False
    master_data: int | None = None
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
        normalized = [db.strip() for db in value]
        if not normalized or not all(normalized):
            raise BackupConfigError("databases must contain at least one non-empty database name")
        return normalized

    @field_validator("output_dir")
    @classmethod
    def create_output_dir(cls, value: Path) -> Path:
        value.mkdir(parents=True, exist_ok=True)
        return value

    @field_validator("filename_template")
    @classmethod
    def filename_template_must_support_fields(cls, value: str) -> str:
        try:
            value.format(database="db", timestamp="20260101_000000")
        except KeyError as exc:
            raise BackupConfigError("filename_template must only use {database} and {timestamp}") from exc
        if "{database}" not in value or "{timestamp}" not in value:
            raise BackupConfigError("filename_template must include {database} and {timestamp}")
        return value

    @field_validator("mysqldump_path")
    @classmethod
    def mysqldump_path_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise BackupConfigError("mysqldump_path must not be empty")
        return value

    @field_validator("ignore_tables")
    @classmethod
    def ignore_tables_must_be_safe(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            table = item.strip()
            if not table:
                continue
            if table.count(".") != 1 or any(part.strip() == "" for part in table.split(".")):
                raise BackupConfigError("ignore_tables entries must be formatted as db.table")
            normalized.append(table)
        return normalized

    @field_validator("extra_options")
    @classmethod
    def dump_extra_options_must_not_contain_password(cls, value: list[str]) -> list[str]:
        return _reject_password_options(value)


class RestoreConfig(BaseModel):
    """Options controlling mysql restore behavior."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    database: str | None = None
    input_file: Path
    mysql_path: str = "mysql"
    command_timeout: float | None = Field(default=None, gt=0)
    force: bool = False
    extra_options: list[str] = Field(default_factory=list)
    decompress: bool = True

    @field_validator("database")
    @classmethod
    def database_must_not_be_empty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise RestoreConfigError("database must not be empty when provided")
        return stripped

    @field_validator("input_file")
    @classmethod
    def input_file_must_exist_and_be_supported(cls, value: Path) -> Path:
        if not value.exists():
            raise RestoreConfigError(f"input_file does not exist: {value}")
        suffixes = value.suffixes
        if value.suffix == ".sql" or suffixes[-2:] == [".sql", ".gz"]:
            return value
        raise RestoreConfigError("input_file must be a .sql or .sql.gz file")

    @field_validator("mysql_path")
    @classmethod
    def mysql_path_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise RestoreConfigError("mysql_path must not be empty")
        return value

    @field_validator("extra_options")
    @classmethod
    def restore_extra_options_must_not_contain_password(cls, value: list[str]) -> list[str]:
        return _reject_password_options(value)


class ScheduleConfig(BaseModel):
    """Options for scheduled backup execution."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    cron: str | None = None
    interval_seconds: int | None = Field(default=None, ge=1)
    timezone: str = "UTC"
    run_immediately: bool = False

    @model_validator(mode="after")
    def validate_schedule(self) -> "ScheduleConfig":
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
    """Options controlling backup retention cleanup."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    keep_last: int | None = Field(default=10, ge=0)
    keep_days: int | None = Field(default=30, ge=0)
    match_pattern: str = "*.sql*"

    @field_validator("match_pattern")
    @classmethod
    def match_pattern_must_be_safe(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("match_pattern must not be empty")
        pattern_path = Path(stripped)
        if pattern_path.is_absolute() or ".." in pattern_path.parts:
            raise ValueError("match_pattern must stay inside output_dir")
        return stripped


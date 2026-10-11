from __future__ import annotations

import io
import os
import re
import shutil
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

import pandas as pd


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("GROWTH_ARCHIVE_DATA_DIR", APP_DIR / "data"))
DATABASE_FILE = DATA_DIR / "growth_archive.db"
LEGACY_CSV_FILE = DATA_DIR / "records.csv"

CATEGORIES = ["荣誉", "证书", "经历", "工作记录"]
CATEGORY_COLORS = {
    "荣誉": "#B68D8D",
    "证书": "#8796A5",
    "经历": "#AAA2B1",
    "工作记录": "#82998A",
}
COLUMNS = ["id", "title", "category", "notes", "record_date", "created_at", "certificate_file"]

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS records (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id   INTEGER NOT NULL,
    category_id  INTEGER NOT NULL,
    title        TEXT NOT NULL CHECK(length(trim(title)) > 0),
    notes        TEXT NOT NULL DEFAULT '',
    record_date  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS certificates (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id      INTEGER NOT NULL UNIQUE,
    file_path      TEXT NOT NULL,
    original_name  TEXT NOT NULL,
    uploaded_at    TEXT NOT NULL,
    FOREIGN KEY (record_id) REFERENCES records(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_records_profile ON records(profile_id);
CREATE INDEX IF NOT EXISTS idx_records_category ON records(category_id);
CREATE INDEX IF NOT EXISTS idx_records_date ON records(record_date);
"""


def empty_records() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def connect_database(data_file: Path = DATABASE_FILE) -> sqlite3.Connection:
    """Create a SQLite connection and enable foreign-key constraints."""
    data_file = Path(data_file)
    data_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(data_file, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


@contextmanager
def database_session(data_file: Path = DATABASE_FILE):
    """Yield a connection and always release the database file on Windows."""
    connection = connect_database(data_file)
    try:
        yield connection
    finally:
        connection.close()


def _category_id(connection: sqlite3.Connection, category: str) -> int:
    row = connection.execute("SELECT id FROM categories WHERE name = ?", (category,)).fetchone()
    if row is None:
        raise ValueError("请选择有效的记录类型。")
    return int(row["id"])


def _normalize_legacy(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    for column in COLUMNS:
        if column not in data.columns:
            data[column] = ""
    return data[COLUMNS].fillna("")


def _migrate_legacy_csv(connection: sqlite3.Connection, csv_file: Path) -> int:
    """Import the former CSV once when the database has no records."""
    existing = int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
    if existing or not csv_file.is_file() or csv_file.stat().st_size == 0:
        return 0

    try:
        legacy = _normalize_legacy(pd.read_csv(csv_file, encoding="utf-8-sig"))
    except Exception:
        return 0

    imported = 0
    for _, row in legacy.iterrows():
        title = str(row["title"]).strip()
        category = str(row["category"]).strip()
        if not title or category not in CATEGORIES:
            continue
        category_id = _category_id(connection, category)
        created_at = str(row["created_at"]).strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record_date = str(row["record_date"]).strip() or date.today().isoformat()
        cursor = connection.execute(
            """
            INSERT INTO records
                (profile_id, category_id, title, notes, record_date, created_at, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (category_id, title, str(row["notes"]), record_date, created_at, created_at),
        )
        certificate_file = str(row["certificate_file"]).strip()
        if certificate_file:
            certificate = _certificate_path(certificate_file, Path(connection.execute("PRAGMA database_list").fetchone()[2]))
            if certificate is not None:
                connection.execute(
                    """
                    INSERT INTO certificates (record_id, file_path, original_name, uploaded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cursor.lastrowid, certificate_file, certificate.name, created_at),
                )
        imported += 1
    return imported


def initialize_database(data_file: Path = DATABASE_FILE) -> int:
    """Create the schema, seed lookup data and migrate the previous CSV."""
    data_file = Path(data_file)
    with database_session(data_file) as connection:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT OR IGNORE INTO profiles (id, name, created_at) VALUES (1, ?, ?)",
            ("默认用户", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.executemany(
            "INSERT OR IGNORE INTO categories (name, color) VALUES (?, ?)",
            [(name, CATEGORY_COLORS[name]) for name in CATEGORIES],
        )
        imported = _migrate_legacy_csv(connection, data_file.parent / "records.csv")
        connection.commit()
        return imported


def load_records(data_file: Path = DATABASE_FILE) -> tuple[pd.DataFrame, str | None]:
    """Read all records with SQL JOINs and recover a malformed database safely."""
    data_file = Path(data_file)
    try:
        imported = initialize_database(data_file)
        with database_session(data_file) as connection:
            rows = connection.execute(
                """
                SELECT
                    r.id,
                    r.title,
                    c.name AS category,
                    r.notes,
                    r.record_date,
                    r.created_at,
                    COALESCE(a.file_path, '') AS certificate_file
                FROM records AS r
                JOIN categories AS c ON c.id = r.category_id
                LEFT JOIN certificates AS a ON a.record_id = r.id
                WHERE r.profile_id = 1
                ORDER BY r.record_date DESC, r.id DESC
                """
            ).fetchall()
        frame = pd.DataFrame([dict(row) for row in rows], columns=COLUMNS)
        message = f"已将旧 CSV 中的 {imported} 条记录迁移到 SQLite 数据库。" if imported else None
        return frame, message
    except sqlite3.DatabaseError as error:
        text = str(error).lower()
        is_corrupt = "malformed" in text or "not a database" in text or "file is encrypted" in text
        if not is_corrupt:
            raise
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = data_file.with_name(f"growth_archive_broken_{timestamp}.db")
        if data_file.exists():
            shutil.copy2(data_file, backup)
            data_file.unlink()
        initialize_database(data_file)
        return empty_records(), f"数据库异常，原文件已备份为 {backup.name}，系统已重建空数据库。"


def _validate_record(title: str, category: str) -> str:
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("记录名称不能为空。")
    if category not in CATEGORIES:
        raise ValueError("请选择有效的记录类型。")
    return clean_title


def _write_pdf(
    record_id: int,
    certificate_name: str | None,
    certificate_bytes: bytes | None,
    data_file: Path,
) -> tuple[str, str, Path | None]:
    if not certificate_bytes:
        return "", "", None
    if len(certificate_bytes) > 15 * 1024 * 1024:
        raise ValueError("PDF 证书不能超过 15 MB。")
    if not certificate_bytes.startswith(b"%PDF-"):
        raise ValueError("上传的文件不是有效的 PDF。")

    original_name = Path(certificate_name or "certificate.pdf").name
    safe_stem = re.sub(r"[^\w\-.\u4e00-\u9fff]", "_", Path(original_name).stem)
    safe_stem = safe_stem.strip("._")[:60] or "certificate"
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    certificate_dir = data_file.parent / "certificates"
    certificate_dir.mkdir(parents=True, exist_ok=True)
    certificate_path = certificate_dir / f"record_{record_id}_{timestamp}_{safe_stem}.pdf"
    temporary = certificate_path.with_suffix(".pdf.tmp")
    temporary.write_bytes(certificate_bytes)
    temporary.replace(certificate_path)
    relative_path = certificate_path.relative_to(data_file.parent).as_posix()
    return relative_path, original_name, certificate_path


def add_record(
    frame: pd.DataFrame,
    title: str,
    category: str,
    notes: str,
    record_date: date,
    data_file: Path = DATABASE_FILE,
    certificate_name: str | None = None,
    certificate_bytes: bytes | None = None,
) -> pd.DataFrame:
    """Insert a new record and optional certificate with parameterized SQL."""
    del frame
    clean_title = _validate_record(title, category)
    data_file = Path(data_file)
    initialize_database(data_file)
    saved_pdf: Path | None = None
    with database_session(data_file) as connection:
        try:
            category_id = _category_id(connection, category)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor = connection.execute(
                """
                INSERT INTO records
                    (profile_id, category_id, title, notes, record_date, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                (category_id, clean_title, notes.strip(), record_date.isoformat(), now, now),
            )
            relative_path, original_name, saved_pdf = _write_pdf(
                int(cursor.lastrowid), certificate_name, certificate_bytes, data_file
            )
            if relative_path:
                connection.execute(
                    """
                    INSERT INTO certificates (record_id, file_path, original_name, uploaded_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cursor.lastrowid, relative_path, original_name, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            if saved_pdf is not None:
                saved_pdf.unlink(missing_ok=True)
            raise
    return load_records(data_file)[0]


def update_record(
    frame: pd.DataFrame,
    record_id: int,
    title: str,
    category: str,
    notes: str,
    record_date: date,
    data_file: Path = DATABASE_FILE,
    certificate_name: str | None = None,
    certificate_bytes: bytes | None = None,
    remove_certificate: bool = False,
) -> pd.DataFrame:
    """Update a record and replace or remove its certificate in one transaction."""
    del frame
    clean_title = _validate_record(title, category)
    data_file = Path(data_file)
    initialize_database(data_file)
    saved_pdf: Path | None = None
    old_relative_path = ""

    with database_session(data_file) as connection:
        try:
            existing = connection.execute(
                """
                SELECT r.id, COALESCE(a.file_path, '') AS file_path
                FROM records AS r
                LEFT JOIN certificates AS a ON a.record_id = r.id
                WHERE r.id = ? AND r.profile_id = 1
                """,
                (int(record_id),),
            ).fetchone()
            if existing is None:
                raise ValueError("没有找到要编辑的记录。")
            old_relative_path = str(existing["file_path"])
            category_id = _category_id(connection, category)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            connection.execute(
                """
                UPDATE records
                SET category_id = ?, title = ?, notes = ?, record_date = ?, updated_at = ?
                WHERE id = ? AND profile_id = 1
                """,
                (category_id, clean_title, notes.strip(), record_date.isoformat(), now, int(record_id)),
            )

            if certificate_bytes:
                relative_path, original_name, saved_pdf = _write_pdf(
                    int(record_id), certificate_name, certificate_bytes, data_file
                )
                connection.execute(
                    """
                    INSERT INTO certificates (record_id, file_path, original_name, uploaded_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(record_id) DO UPDATE SET
                        file_path = excluded.file_path,
                        original_name = excluded.original_name,
                        uploaded_at = excluded.uploaded_at
                    """,
                    (int(record_id), relative_path, original_name, now),
                )
            elif remove_certificate:
                connection.execute("DELETE FROM certificates WHERE record_id = ?", (int(record_id),))
            connection.commit()
        except Exception:
            connection.rollback()
            if saved_pdf is not None:
                saved_pdf.unlink(missing_ok=True)
            raise

    if old_relative_path and (certificate_bytes or remove_certificate):
        old_pdf = _certificate_path(old_relative_path, data_file)
        if old_pdf is not None:
            old_pdf.unlink(missing_ok=True)
    return load_records(data_file)[0]


def delete_record(
    frame: pd.DataFrame,
    record_id: int,
    data_file: Path = DATABASE_FILE,
) -> pd.DataFrame:
    """Delete one record; the related certificate row is removed by cascade."""
    del frame
    data_file = Path(data_file)
    initialize_database(data_file)
    certificate_relative = ""
    with database_session(data_file) as connection:
        row = connection.execute(
            """
            SELECT COALESCE(a.file_path, '') AS file_path
            FROM records AS r
            LEFT JOIN certificates AS a ON a.record_id = r.id
            WHERE r.id = ? AND r.profile_id = 1
            """,
            (int(record_id),),
        ).fetchone()
        if row is None:
            raise ValueError("没有找到要删除的记录。")
        certificate_relative = str(row["file_path"])
        connection.execute("DELETE FROM records WHERE id = ? AND profile_id = 1", (int(record_id),))
        connection.commit()
    if certificate_relative:
        certificate = _certificate_path(certificate_relative, data_file)
        if certificate is not None:
            certificate.unlink(missing_ok=True)
    return load_records(data_file)[0]


def _certificate_path(relative_path: str, data_file: Path = DATABASE_FILE) -> Path | None:
    if not relative_path:
        return None
    data_root = Path(data_file).parent.resolve()
    candidate = (data_root / relative_path).resolve()
    if candidate != data_root and data_root not in candidate.parents:
        return None
    return candidate if candidate.is_file() and candidate.suffix.lower() == ".pdf" else None


def build_certificate_archive(frame: pd.DataFrame, data_file: Path = DATABASE_FILE) -> bytes | None:
    buffer = io.BytesIO()
    added = 0
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for _, row in frame.iterrows():
            certificate = _certificate_path(str(row.get("certificate_file", "")), data_file)
            if certificate is None:
                continue
            safe_title = re.sub(r"[^\w\-.\u4e00-\u9fff]", "_", str(row.get("title", "证书"))).strip("._")
            archive_name = f"{int(row['id']):03d}_{safe_title or '证书'}.pdf"
            archive.writestr(archive_name, certificate.read_bytes())
            added += 1
    return buffer.getvalue() if added else None

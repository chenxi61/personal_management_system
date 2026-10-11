PRAGMA foreign_keys = ON;

-- 个人基本信息表
CREATE TABLE IF NOT EXISTS profiles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 记录类型表
CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    color       TEXT NOT NULL
);

-- 成长记录表
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

-- PDF 证书附件表，一条成长记录最多对应一份证书
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

-- 初始化一名默认用户与四种记录类型
INSERT OR IGNORE INTO profiles (id, name) VALUES (1, '默认用户');
INSERT OR IGNORE INTO categories (name, color) VALUES
    ('荣誉', '#B68D8D'),
    ('证书', '#8796A5'),
    ('经历', '#AAA2B1'),
    ('工作记录', '#82998A');

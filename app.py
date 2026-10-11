from __future__ import annotations

import html
import io
import os
import re
import shutil
import sqlite3
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("GROWTH_ARCHIVE_DATA_DIR", APP_DIR / "data"))
DATA_FILE = DATA_DIR / "records.csv"

CATEGORIES = ["荣誉", "证书", "经历", "工作记录"]
COLUMNS = ["id", "title", "category", "notes", "record_date", "created_at", "certificate_file"]
CATEGORY_COLORS = {
    "荣誉": "#B68D8D",
    "证书": "#8796A5",
    "经历": "#AAA2B1",
    "工作记录": "#82998A",
}


def empty_records() -> pd.DataFrame:
    """Return an empty record table with a stable schema."""
    return pd.DataFrame(columns=COLUMNS)


def _normalize_records(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize legacy or partially complete CSV data."""
    data = frame.copy()
    for column in COLUMNS:
        if column not in data.columns:
            data[column] = ""

    data = data[COLUMNS]
    data["title"] = data["title"].fillna("").astype(str).str.strip()
    data["category"] = data["category"].fillna("").astype(str).str.strip()
    data["notes"] = data["notes"].fillna("").astype(str)
    data["record_date"] = data["record_date"].fillna("").astype(str)
    data["created_at"] = data["created_at"].fillna("").astype(str)
    data["certificate_file"] = data["certificate_file"].fillna("").astype(str)

    ids = pd.to_numeric(data["id"], errors="coerce")
    if ids.isna().any():
        data["id"] = range(1, len(data) + 1)
    else:
        data["id"] = ids.astype(int)
    return data


def save_records(frame: pd.DataFrame, data_file: Path = DATA_FILE) -> None:
    """Atomically save the complete dataset to CSV."""
    data_file.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_records(frame)
    temporary_file = data_file.with_suffix(".tmp")
    normalized.to_csv(temporary_file, index=False, encoding="utf-8-sig")
    temporary_file.replace(data_file)


def load_records(data_file: Path = DATA_FILE) -> tuple[pd.DataFrame, str | None]:
    """Load records and recover gracefully from missing, empty or damaged files."""
    data_file.parent.mkdir(parents=True, exist_ok=True)

    if not data_file.exists() or data_file.stat().st_size == 0:
        data = empty_records()
        save_records(data, data_file)
        return data, None

    try:
        data = pd.read_csv(data_file, encoding="utf-8-sig")
        missing_core = {"id", "title", "category", "notes"} - set(data.columns)
        if missing_core:
            raise ValueError(f"缺少必要字段：{', '.join(sorted(missing_core))}")
        data = _normalize_records(data)
        invalid_categories = set(data.loc[data["category"] != "", "category"]) - set(CATEGORIES)
        if invalid_categories:
            raise ValueError("记录类型不符合系统约定")
        return data, None
    except Exception:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup = data_file.with_name(f"records_broken_{timestamp}.csv")
        shutil.copy2(data_file, backup)
        data = empty_records()
        save_records(data, data_file)
        message = f"检测到数据文件异常，原文件已备份为 {backup.name}，系统已创建新的空数据表。"
        return data, message


def add_record(
    frame: pd.DataFrame,
    title: str,
    category: str,
    notes: str,
    record_date: date,
    data_file: Path = DATA_FILE,
    certificate_name: str | None = None,
    certificate_bytes: bytes | None = None,
) -> pd.DataFrame:
    """Validate, append and persist one record using the complete dataset."""
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("记录名称不能为空。")
    if category not in CATEGORIES:
        raise ValueError("请选择有效的记录类型。")

    complete_data = _normalize_records(frame)
    next_id = 1 if complete_data.empty else int(complete_data["id"].max()) + 1
    certificate_relative_path = ""
    if certificate_bytes:
        if len(certificate_bytes) > 15 * 1024 * 1024:
            raise ValueError("PDF 证书不能超过 15 MB。")
        if not certificate_bytes.startswith(b"%PDF-"):
            raise ValueError("上传的文件不是有效的 PDF。")
        safe_stem = re.sub(r"[^\w\-.\u4e00-\u9fff]", "_", Path(certificate_name or "certificate.pdf").stem)
        safe_stem = safe_stem.strip("._")[:60] or "certificate"
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        certificate_dir = data_file.parent / "certificates"
        certificate_dir.mkdir(parents=True, exist_ok=True)
        certificate_path = certificate_dir / f"record_{next_id}_{timestamp}_{safe_stem}.pdf"
        temporary_pdf = certificate_path.with_suffix(".pdf.tmp")
        temporary_pdf.write_bytes(certificate_bytes)
        temporary_pdf.replace(certificate_path)
        certificate_relative_path = certificate_path.relative_to(data_file.parent).as_posix()

    new_row = {
        "id": next_id,
        "title": clean_title,
        "category": category,
        "notes": notes.strip(),
        "record_date": record_date.isoformat(),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "certificate_file": certificate_relative_path,
    }
    updated = pd.concat([complete_data, pd.DataFrame([new_row])], ignore_index=True)
    try:
        save_records(updated, data_file)
    except Exception:
        if certificate_relative_path:
            (data_file.parent / certificate_relative_path).unlink(missing_ok=True)
        raise
    return updated


def _certificate_path(relative_path: str, data_file: Path = DATA_FILE) -> Path | None:
    """Resolve a stored certificate path without allowing it to leave data/."""
    if not relative_path:
        return None
    data_root = data_file.parent.resolve()
    candidate = (data_root / relative_path).resolve()
    if candidate != data_root and data_root not in candidate.parents:
        return None
    return candidate if candidate.is_file() and candidate.suffix.lower() == ".pdf" else None


def update_record(
    frame: pd.DataFrame,
    record_id: int,
    title: str,
    category: str,
    notes: str,
    record_date: date,
    data_file: Path = DATA_FILE,
    certificate_name: str | None = None,
    certificate_bytes: bytes | None = None,
    remove_certificate: bool = False,
) -> pd.DataFrame:
    """Edit one saved record and optionally replace or remove its PDF certificate."""
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("记录名称不能为空。")
    if category not in CATEGORIES:
        raise ValueError("请选择有效的记录类型。")

    updated = _normalize_records(frame)
    matched = updated.index[updated["id"] == int(record_id)].tolist()
    if not matched:
        raise ValueError("没有找到要编辑的记录。")

    row_index = matched[0]
    old_relative_path = str(updated.at[row_index, "certificate_file"])
    new_relative_path = old_relative_path
    newly_saved_pdf: Path | None = None

    if certificate_bytes:
        if len(certificate_bytes) > 15 * 1024 * 1024:
            raise ValueError("PDF 证书不能超过 15 MB。")
        if not certificate_bytes.startswith(b"%PDF-"):
            raise ValueError("上传的文件不是有效的 PDF。")
        safe_stem = re.sub(r"[^\w\-.\u4e00-\u9fff]", "_", Path(certificate_name or "certificate.pdf").stem)
        safe_stem = safe_stem.strip("._")[:60] or "certificate"
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        certificate_dir = data_file.parent / "certificates"
        certificate_dir.mkdir(parents=True, exist_ok=True)
        newly_saved_pdf = certificate_dir / f"record_{record_id}_{timestamp}_{safe_stem}.pdf"
        temporary_pdf = newly_saved_pdf.with_suffix(".pdf.tmp")
        temporary_pdf.write_bytes(certificate_bytes)
        temporary_pdf.replace(newly_saved_pdf)
        new_relative_path = newly_saved_pdf.relative_to(data_file.parent).as_posix()
    elif remove_certificate:
        new_relative_path = ""

    updated.at[row_index, "title"] = clean_title
    updated.at[row_index, "category"] = category
    updated.at[row_index, "notes"] = notes.strip()
    updated.at[row_index, "record_date"] = record_date.isoformat()
    updated.at[row_index, "certificate_file"] = new_relative_path

    try:
        save_records(updated, data_file)
    except Exception:
        if newly_saved_pdf is not None:
            newly_saved_pdf.unlink(missing_ok=True)
        raise

    if old_relative_path and old_relative_path != new_relative_path:
        old_pdf = _certificate_path(old_relative_path, data_file)
        if old_pdf is not None:
            old_pdf.unlink(missing_ok=True)
    return updated


def build_certificate_archive(frame: pd.DataFrame, data_file: Path = DATA_FILE) -> bytes | None:
    """Package all existing PDF certificates referenced by the selected records."""
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


def filter_records(
    frame: pd.DataFrame,
    keyword: str,
    categories: list[str],
    start_date: date,
    end_date: date,
    newest_first: bool,
) -> pd.DataFrame:
    """Return a display-only filtered copy without mutating the source."""
    result = frame.copy()
    if categories:
        result = result[result["category"].isin(categories)]
    else:
        result = result.iloc[0:0]

    clean_keyword = keyword.strip()
    if clean_keyword:
        title_match = result["title"].str.contains(clean_keyword, case=False, na=False, regex=False)
        notes_match = result["notes"].str.contains(clean_keyword, case=False, na=False, regex=False)
        result = result[title_match | notes_match]

    parsed_dates = pd.to_datetime(result["record_date"], errors="coerce").dt.date
    date_mask = parsed_dates.between(start_date, end_date)
    result = result[date_mask]
    return result.sort_values("record_date", ascending=not newest_first).reset_index(drop=True)


def generate_insight(frame: pd.DataFrame, detail_level: int = 2) -> str:
    """Generate replaceable local-rule insights without coupling to the data layer."""
    if frame.empty:
        return "目前还没有成长记录。可以先添加一项近期完成的证书、比赛或实践经历。"

    counts = frame["category"].value_counts().reindex(CATEGORIES, fill_value=0)
    total = int(counts.sum())
    strongest = str(counts.idxmax())
    weakest = str(counts.idxmin())
    latest_dates = pd.to_datetime(frame["record_date"], errors="coerce")
    recent_count = int((latest_dates >= pd.Timestamp.today().normalize() - pd.Timedelta(days=90)).sum())

    sentences = [f"你已经积累了 {total} 条成长记录，其中“{strongest}”最丰富。"]
    if counts[weakest] == 0:
        sentences.append(f"“{weakest}”暂时没有记录，可以把它作为下一阶段的补充方向。")
    else:
        sentences.append(f"“{weakest}”相对较少，后续可以有意识地补充相关成果。")

    if detail_level >= 2:
        if recent_count == 0:
            sentences.append("近 90 天还没有新增记录，建议从最近完成的一件小事开始整理。")
        else:
            sentences.append(f"近 90 天新增了 {recent_count} 条记录，成长档案保持了持续更新。")

    if detail_level >= 3:
        if counts.max() >= max(3, counts.min() * 2 + 1):
            sentences.append("当前类别分布存在一定差异，可以为记录较少的类别设定一个可完成的小目标。")
        sentences.append("建议每月固定一次回顾，把新成果、过程经验和下一步行动一并记录下来。")
    return "".join(sentences)


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
            --ivory: #F4F1EC;
            --paper: #FCFAF7;
            --sage: #82998A;
            --mist: #8796A5;
            --rose: #B68D8D;
            --lavender: #AAA2B1;
            --ink: #484540;
            --muted: #77726C;
            --line: #DDD7CF;
            --primary-color: #82998A;
            --text-color: #484540;
        }
        /* 隐藏 Streamlit 顶部工具栏，避免深色主题下出现黑色遮挡。 */
        header[data-testid="stHeader"],
        [data-testid="stToolbar"],
        [data-testid="stDecoration"],
        #MainMenu {
            display: none !important;
            height: 0 !important;
        }
        .stApp {
            background: var(--ivory);
            color: var(--ink) !important;
            top: 0 !important;
        }
        [data-testid="stAppViewContainer"] {
            background: var(--ivory);
        }
        .block-container {
            max-width: 1450px;
            padding-top: 1.4rem !important;
            padding-bottom: 3rem;
        }

        /* 固定侧边栏为浅色主题，防止跟随浏览器深色模式变成白字黑框。 */
        [data-testid="stSidebar"] {
            background: #EFEEE9 !important;
            border-right: 1px solid var(--line);
            color: var(--ink) !important;
        }
        [data-testid="stSidebar"] > div { padding-top: 1.4rem; }
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] small,
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
        [data-testid="stSidebar"] [data-testid="stWidgetLabel"] {
            color: var(--ink) !important;
        }
        [data-testid="stSidebar"] input,
        [data-testid="stSidebar"] textarea {
            color: var(--ink) !important;
            background: var(--paper) !important;
            -webkit-text-fill-color: var(--ink) !important;
        }
        [data-testid="stSidebar"] input::placeholder,
        [data-testid="stSidebar"] textarea::placeholder {
            color: #8B857E !important;
            opacity: 1 !important;
        }
        [data-testid="stSidebar"] [data-baseweb="input"],
        [data-testid="stSidebar"] [data-baseweb="select"] > div,
        [data-testid="stSidebar"] [data-baseweb="base-input"] {
            color: var(--ink) !important;
            background: var(--paper) !important;
            border-color: var(--line) !important;
        }
        [data-testid="stSidebar"] [data-baseweb="select"] svg,
        [data-testid="stSidebar"] [data-testid="stIconMaterial"] {
            color: var(--ink) !important;
            fill: var(--ink) !important;
        }
        [data-testid="stSidebar"] hr { border-color: var(--line) !important; }

        /* 筛选控件使用柔和灰豆绿，替代默认高饱和红色。 */
        [data-testid="stSidebar"] [data-baseweb="tag"] {
            background: #D5DED7 !important;
            border: 1px solid #B9C8BD !important;
        }
        [data-testid="stSidebar"] [data-baseweb="tag"] span,
        [data-testid="stSidebar"] [data-baseweb="tag"] svg {
            color: #405047 !important;
            fill: #405047 !important;
        }
        [data-testid="stSidebar"] [data-testid="stSlider"] [role="slider"] {
            background-color: var(--sage) !important;
            border-color: #6F8577 !important;
            box-shadow: 0 0 0 1px #6F8577 !important;
        }
        [data-testid="stSidebar"] [data-testid="stSlider"] [data-testid="stThumbValue"],
        [data-testid="stSidebar"] [data-testid="stSlider"] [data-testid="stTickBarMin"],
        [data-testid="stSidebar"] [data-testid="stSlider"] [data-testid="stTickBarMax"] {
            color: #53675B !important;
        }
        [data-testid="stSidebar"] input[type="radio"],
        [data-testid="stSidebar"] input[type="checkbox"] {
            accent-color: var(--sage) !important;
        }
        [data-testid="stSidebar"] label[data-baseweb="radio"]:has(input:checked) > div:first-child {
            background-color: var(--sage) !important;
            border-color: #6F8577 !important;
        }
        [data-testid="stSidebar"] [data-baseweb="slider"] [style*="rgb(255, 75, 75)"],
        [data-testid="stSidebar"] [data-baseweb="slider"] [style*="#ff4b4b"] {
            background-color: var(--sage) !important;
            color: #53675B !important;
        }
        h1, h2, h3 { color: var(--ink); letter-spacing: .01em; }
        .hero {
            padding: 1.7rem 2rem;
            border: 1px solid var(--line);
            border-radius: 22px;
            background: linear-gradient(115deg, rgba(130,153,138,.17), rgba(170,162,177,.10), rgba(252,250,247,.94));
            margin-bottom: 1rem;
        }
        .hero h1 { margin: 0 0 .35rem 0; font-size: 2.25rem; }
        .hero p { margin: 0; color: var(--muted); font-size: 1.02rem; }
        .metric-card {
            min-height: 112px;
            padding: 1rem 1.1rem;
            border: 1px solid var(--line);
            border-top: 5px solid var(--accent);
            border-radius: 17px;
            background: rgba(252,250,247,.94);
            box-shadow: 0 7px 20px rgba(72,69,64,.045);
        }
        .metric-label { color: var(--muted); font-size: .88rem; margin-bottom: .4rem; }
        .metric-value { color: var(--ink); font-size: 1.85rem; font-weight: 700; line-height: 1; }
        .metric-note { color: var(--muted); font-size: .75rem; margin-top: .55rem; }
        .section-card {
            padding: 1.2rem 1.25rem;
            border: 1px solid var(--line);
            border-radius: 18px;
            background: rgba(252,250,247,.94);
            box-shadow: 0 7px 20px rgba(72,69,64,.035);
        }
        .insight-box {
            padding: 1.1rem 1.25rem;
            border-left: 5px solid var(--lavender);
            border-radius: 14px;
            background: rgba(170,162,177,.14);
            color: var(--ink);
            line-height: 1.8;
        }
        .record-card {
            padding: .95rem 1rem;
            margin-bottom: .65rem;
            border: 1px solid var(--line);
            border-left: 6px solid var(--accent);
            border-radius: 14px;
            background: var(--paper);
        }
        .record-title { font-weight: 650; color: var(--ink); }
        .record-meta { margin-top: .25rem; color: var(--muted); font-size: .82rem; }
        .record-notes { margin-top: .45rem; color: var(--ink); font-size: .9rem; }

        /* 记录表格采用浅米灰分层，不受浏览器深色模式影响。 */
        .records-table-wrap {
            max-height: 520px;
            overflow: auto;
            border: 1px solid #D4CEC5;
            border-radius: 14px;
            background: #F8F5F0;
        }
        .records-table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            color: var(--ink);
            font-size: .9rem;
        }
        .records-table th {
            position: sticky;
            top: 0;
            z-index: 1;
            padding: .72rem .78rem;
            background: #DED8CF;
            color: #3F3C38;
            text-align: left;
            font-weight: 700;
            border-bottom: 1px solid #C9C2B8;
            white-space: nowrap;
        }
        .records-table td {
            padding: .68rem .78rem;
            background: #FAF8F4;
            color: #4C4944;
            border-bottom: 1px solid #E4DED6;
            vertical-align: top;
        }
        .records-table tbody tr:nth-child(even) td { background: #F0ECE6; }
        .records-table tbody tr:hover td { background: #E5EBE6; }

        /* 图表下方选项卡始终使用深色文字和灰豆绿指示线。 */
        [data-baseweb="tab-list"] { gap: .4rem; }
        button[data-baseweb="tab"],
        button[data-baseweb="tab"] p,
        button[data-baseweb="tab"] span {
            color: var(--ink) !important;
            opacity: 1 !important;
        }
        [data-baseweb="tab-highlight"] { background-color: var(--sage) !important; }

        /* 主表单固定为浅色输入区域。 */
        [data-testid="stMain"] input,
        [data-testid="stMain"] textarea,
        [data-testid="stMain"] [data-baseweb="select"] > div {
            background: var(--paper) !important;
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
            border-color: var(--line) !important;
        }
        [data-testid="stMain"] [data-testid="stWidgetLabel"],
        [data-testid="stMain"] [data-testid="stWidgetLabel"] p,
        [data-testid="stMain"] label,
        [data-testid="stMain"] label p {
            color: var(--ink) !important;
            opacity: 1 !important;
        }

        /* AI 滑块的标题、刻度和值保持深色。 */
        [data-testid="stMain"] [data-testid="stSlider"] p,
        [data-testid="stMain"] [data-testid="stSlider"] span,
        [data-testid="stMain"] [data-testid="stSlider"] div,
        [data-testid="stMain"] [data-testid="stThumbValue"],
        [data-testid="stMain"] [data-testid="stTickBarMin"],
        [data-testid="stMain"] [data-testid="stTickBarMax"] {
            color: var(--ink) !important;
        }
        [data-testid="stMain"] [data-testid="stSlider"] [role="slider"] {
            background-color: var(--sage) !important;
            border-color: #6F8577 !important;
        }

        /* PDF 上传框与新增记录表单使用同一套浅色莫兰迪样式。 */
        [data-testid="stFileUploader"] {
            color: var(--ink) !important;
        }
        [data-testid="stFileUploaderDropzone"] {
            background: var(--paper) !important;
            border: 1px dashed #BFC9C1 !important;
            color: var(--ink) !important;
        }
        [data-testid="stFileUploaderDropzone"] p,
        [data-testid="stFileUploaderDropzone"] span,
        [data-testid="stFileUploaderDropzone"] small,
        [data-testid="stFileUploaderDropzoneInstructions"] {
            color: #5D5953 !important;
            opacity: 1 !important;
        }
        [data-testid="stFileUploaderDropzone"] button {
            background: #E5EBE6 !important;
            border: 1px solid #B9C8BD !important;
            color: #3F4D44 !important;
        }
        [data-testid="stFileUploaderFile"] {
            background: #F0ECE6 !important;
            color: var(--ink) !important;
        }

        /* 保存与下载按钮使用深色文字，提升浅色页面中的可读性。 */
        [data-testid="stFormSubmitButton"] button,
        [data-testid="stDownloadButton"] button,
        [data-testid="stDownloadButton"] button p,
        [data-testid="stDownloadButton"] button span {
            color: #2F3933 !important;
            -webkit-text-fill-color: #2F3933 !important;
        }
        [data-testid="stDownloadButton"] button {
            width: 100% !important;
            min-height: 3.15rem !important;
            padding: .55rem .7rem !important;
            background: var(--sage) !important;
            border: 1.5px solid #758D7E !important;
            border-radius: 10px !important;
            font-size: .94rem !important;
            font-weight: 600 !important;
            line-height: 1.15 !important;
            box-shadow: inset 0 0 0 1px rgba(130,153,138,.08) !important;
        }
        [data-testid="stDownloadButton"] button:disabled {
            background: var(--sage) !important;
            border-color: #758D7E !important;
            color: #2F3933 !important;
            opacity: 1 !important;
        }
        [data-testid="stFormSubmitButton"] button {
            background: var(--sage) !important;
            border-color: #758D7E !important;
        }
        div.stButton > button, div.stDownloadButton > button {
            border-radius: 10px;
            border: 1px solid #758D7E;
            background: var(--sage);
            color: white;
        }
        div.stButton > button:hover, div.stDownloadButton > button:hover {
            border-color: #6E8376;
            background: #758D7E;
            color: white;
        }
        [data-testid="stForm"], [data-testid="stExpander"] {
            border-color: var(--line);
            border-radius: 16px;
            background: rgba(252,250,247,.76);
        }
        [data-testid="stExpander"],
        [data-testid="stExpander"] details,
        [data-testid="stExpander"] summary {
            background: var(--paper) !important;
            color: var(--ink) !important;
            border-color: var(--line) !important;
        }
        [data-testid="stExpander"] summary p,
        [data-testid="stExpander"] summary span,
        [data-testid="stExpander"] summary svg {
            color: var(--ink) !important;
            fill: var(--ink) !important;
        }
        [data-baseweb="tab"] { border-radius: 10px 10px 0 0; }
        hr { border-color: var(--line); }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _metric_card(label: str, value: int, note: str, color: str) -> None:
    st.markdown(
        f"""
        <div class="metric-card" style="--accent:{color}">
          <div class="metric-label">{html.escape(label)}</div>
          <div class="metric-value">{value}</div>
          <div class="metric-note">{html.escape(note)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _donut_chart(frame: pd.DataFrame) -> go.Figure:
    counts = frame["category"].value_counts().reindex(CATEGORIES, fill_value=0)
    figure = go.Figure(
        go.Pie(
            labels=counts.index,
            values=counts.values,
            hole=0.64,
            marker_colors=[CATEGORY_COLORS[item] for item in counts.index],
            textinfo="percent" if counts.sum() else "none",
            hovertemplate="%{label}<br>%{value} 条（%{percent}）<extra></extra>",
        )
    )
    figure.add_annotation(
        text=f"<b>{int(counts.sum())}</b><br><span style='font-size:12px'>总记录</span>",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(color="#484540", size=22),
    )
    figure.update_layout(
        template="plotly_white",
        height=335,
        margin=dict(l=12, r=12, t=30, b=12),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(
            orientation="h",
            y=-0.08,
            x=0.5,
            xanchor="center",
            font=dict(color="#484540", size=13),
        ),
        font=dict(color="#484540"),
    )
    return figure


def _monthly_bar_chart(frame: pd.DataFrame) -> go.Figure:
    dates = pd.to_datetime(frame["record_date"], errors="coerce")
    valid = frame.assign(_date=dates).dropna(subset=["_date"])
    if valid.empty:
        labels: list[str] = []
        values: list[int] = []
    else:
        monthly = valid.groupby(valid["_date"].dt.to_period("M")).size().sort_index()
        labels = [period.strftime("%Y-%m") for period in monthly.index]
        values = monthly.astype(int).tolist()

    figure = go.Figure(
        go.Bar(
            x=labels,
            y=values,
            marker_color="#8796A5",
            marker_line_width=0,
            hovertemplate="%{x}<br>%{y} 条记录<extra></extra>",
        )
    )
    figure.update_layout(
        template="plotly_white",
        height=335,
        margin=dict(l=18, r=12, t=30, b=30),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        showlegend=False,
        xaxis=dict(showgrid=False, title="", tickfont=dict(color="#5E5A54")),
        yaxis=dict(
            gridcolor="rgba(119,114,108,.14)",
            title="记录数",
            title_font=dict(color="#5E5A54"),
            tickfont=dict(color="#5E5A54"),
            rangemode="tozero",
        ),
        font=dict(color="#484540"),
    )
    return figure


def _display_pdf(certificate: Path, title: str, key: str) -> None:
    """Show a direct PDF download button."""
    pdf_bytes = certificate.read_bytes()
    st.download_button(
        "下载这份 PDF 证书",
        data=pdf_bytes,
        file_name=f"{title}.pdf",
        mime="application/pdf",
        key=key,
        use_container_width=True,
    )


def _display_cards(frame: pd.DataFrame, key_prefix: str = "card") -> None:
    if frame.empty:
        st.info("当前筛选条件下暂无记录。")
        return
    for _, row in frame.iterrows():
        category = str(row["category"])
        color = CATEGORY_COLORS.get(category, "#82998A")
        notes = html.escape(str(row["notes"])) if str(row["notes"]).strip() else "暂无备注"
        st.markdown(
            f"""
            <div class="record-card" style="--accent:{color}">
              <div class="record-title">{html.escape(str(row['title']))}</div>
              <div class="record-meta">{html.escape(category)} · {html.escape(str(row['record_date']))}</div>
              <div class="record-notes">{notes}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        certificate = _certificate_path(str(row.get("certificate_file", "")))
        if certificate is not None:
            with st.expander("查看 PDF 证书"):
                _display_pdf(certificate, str(row["title"]), f"{key_prefix}_pdf_{int(row['id'])}")


# 第五周升级：页面继续使用 DataFrame 展示，但所有持久化操作改由 SQLite 完成。
from database import (  # noqa: E402
    DATABASE_FILE as SQL_DATABASE_FILE,
    _certificate_path as sql_certificate_path,
    add_record as sql_add_record,
    build_certificate_archive as sql_build_certificate_archive,
    delete_record as sql_delete_record,
    empty_records as sql_empty_records,
    load_records as sql_load_records,
    update_record as sql_update_record,
)

DATA_FILE = SQL_DATABASE_FILE
_certificate_path = sql_certificate_path
add_record = sql_add_record
build_certificate_archive = sql_build_certificate_archive
delete_record = sql_delete_record
empty_records = sql_empty_records
load_records = sql_load_records
update_record = sql_update_record


def main() -> None:
    st.set_page_config(page_title="个人成长档案", page_icon="🌿", layout="wide")
    _inject_styles()

    records, recovery_message = load_records()
    if recovery_message:
        st.warning(recovery_message)

    with st.sidebar:
        st.markdown("## 🌿 个人成长档案")
        st.caption("安静地记录每一次成长")
        st.divider()
        st.markdown("### 记录筛选")
        keyword = st.text_input("关键词搜索", placeholder="搜索名称或备注")
        selected_categories = st.multiselect("选择类型", CATEGORIES, default=CATEGORIES)

        parsed = pd.to_datetime(records["record_date"], errors="coerce").dropna()
        default_end = date.today()
        default_start = parsed.min().date() if not parsed.empty else default_end - timedelta(days=365)
        range_min = min(default_start, default_end - timedelta(days=365))
        range_max = max(default_end, parsed.max().date() if not parsed.empty else default_end)
        selected_range = st.slider(
            "时间范围",
            min_value=range_min,
            max_value=range_max,
            value=(range_min, range_max),
            format="YYYY-MM-DD",
        )
        st.markdown("**电脑系统时间**")
        components.html(
            """
            <div id="system-clock" style="font-family: sans-serif; color:#484540; font-size:14px;
                 background:#FCFAF7; border:1px solid #DDD7CF; border-radius:9px;
                 padding:8px 10px; text-align:center;"></div>
            <script>
              function updateClock() {
                const now = new Date();
                document.getElementById('system-clock').textContent = now.toLocaleString('zh-CN', {
                  year:'numeric', month:'2-digit', day:'2-digit',
                  hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false
                });
              }
              function refreshAtNextDay() {
                const now = new Date();
                const nextDay = new Date(now);
                nextDay.setHours(24, 0, 2, 0);
                setTimeout(() => window.parent.location.reload(), nextDay - now);
              }
              updateClock();
              setInterval(updateClock, 1000);
              refreshAtNextDay();
            </script>
            """,
            height=43,
            scrolling=False,
        )
        order = st.radio("排序方式", ["最新优先", "最早优先"], horizontal=True)
        if st.button("重置筛选", use_container_width=True):
            st.session_state.clear()
            st.rerun()
        st.divider()
        st.caption(f"数据库：data/{DATA_FILE.name}")
        if DATA_FILE.exists():
            saved_time = datetime.fromtimestamp(DATA_FILE.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            st.caption(f"最近保存：{saved_time}")

    filtered = filter_records(
        records,
        keyword,
        selected_categories,
        selected_range[0],
        selected_range[1],
        newest_first=order == "最新优先",
    )

    st.markdown(
        """
        <div class="hero">
          <h1>个人成长档案</h1>
          <p>记录每一次成长与收获，让零散经历变成清晰的成长轨迹</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    metric_columns = st.columns(5)
    category_counts = records["category"].value_counts().reindex(CATEGORIES, fill_value=0)
    metrics = [
        ("全部记录", len(records), "完整成长档案", "#82998A"),
        ("荣誉", int(category_counts["荣誉"]), "竞赛与获奖", "#B68D8D"),
        ("证书", int(category_counts["证书"]), "专业能力证明", "#8796A5"),
        ("经历", int(category_counts["经历"]), "学习与实践", "#AAA2B1"),
        ("工作记录", int(category_counts["工作记录"]), "项目与工作", "#82998A"),
    ]
    for column, (label, value, note, color) in zip(metric_columns, metrics):
        with column:
            _metric_card(label, value, note, color)

    st.write("")
    left, middle, right = st.columns([1.05, 1.35, 1.05], gap="large")
    with left:
        st.subheader("类型分布")
        st.plotly_chart(_donut_chart(filtered), use_container_width=True, config={"displayModeBar": False})
    with middle:
        st.subheader("月度记录")
        st.plotly_chart(_monthly_bar_chart(filtered), use_container_width=True, config={"displayModeBar": False})
    with right:
        st.subheader("AI 成长洞察")
        detail_level = st.select_slider(
            "洞察详细程度",
            options=[1, 2, 3],
            value=2,
            format_func=lambda value: {1: "简洁", 2: "标准", 3: "详细"}[value],
        )
        if st.button("生成成长建议", use_container_width=True):
            st.session_state["insight"] = generate_insight(records, detail_level)
        insight = st.session_state.get("insight", "点击按钮，根据当前完整档案生成一份本地成长建议。")
        st.markdown(f'<div class="insight-box">{html.escape(insight)}</div>', unsafe_allow_html=True)

    st.subheader("各类型记录活跃度")
    st.caption("进度条按照当前数量最多的类别进行相对比较，仅用于观察记录分布。")
    maximum = max(int(category_counts.max()), 1)
    progress_columns = st.columns(4)
    for column, category in zip(progress_columns, CATEGORIES):
        with column:
            count = int(category_counts[category])
            st.write(f"**{category}**　{count} 条")
            st.progress(count / maximum)

    st.divider()
    records_column, form_column = st.columns([1.65, 1], gap="large")
    with records_column:
        st.subheader("成长记录")
        table_tab, card_tab, edit_tab = st.tabs(["记录表格", "成长卡片", "编辑记录"])
        with table_tab:
            if filtered.empty:
                st.info("当前筛选条件下暂无记录。")
            else:
                display = filtered.copy()
                display["certificate_file"] = display["certificate_file"].apply(
                    lambda value: "可查看" if _certificate_path(str(value)) is not None else "—"
                )
                display = display.rename(
                    columns={
                        "id": "编号",
                        "title": "记录名称",
                        "category": "类型",
                        "notes": "备注",
                        "record_date": "记录日期",
                        "created_at": "创建时间",
                        "certificate_file": "PDF证书",
                    }
                )
                table_html = display.to_html(index=False, escape=True, classes="records-table", border=0)
                st.markdown(f'<div class="records-table-wrap">{table_html}</div>', unsafe_allow_html=True)

                pdf_rows = [
                    row for _, row in filtered.iterrows()
                    if _certificate_path(str(row.get("certificate_file", ""))) is not None
                ]
                if pdf_rows:
                    pdf_ids = [int(row["id"]) for row in pdf_rows]
                    title_by_id = {
                        int(row["id"]): f"{row['title']}（{row['category']}）" for row in pdf_rows
                    }
                    selected_pdf_id = st.selectbox(
                        "选择要查看的 PDF 证书",
                        options=pdf_ids,
                        format_func=lambda record_id: title_by_id[record_id],
                        key="table_pdf_selector",
                    )
                    selected_row = next(row for row in pdf_rows if int(row["id"]) == selected_pdf_id)
                    selected_path = _certificate_path(str(selected_row["certificate_file"]))
                    if selected_path is not None:
                        with st.expander("查看 PDF 证书文件"):
                            _display_pdf(
                                selected_path,
                                str(selected_row["title"]),
                                f"table_pdf_{selected_pdf_id}",
                            )
        with card_tab:
            _display_cards(filtered, key_prefix="card")
        with edit_tab:
            if filtered.empty:
                st.info("当前筛选条件下没有可编辑的记录。")
            else:
                editable_ids = filtered["id"].astype(int).tolist()
                editable_titles = {
                    int(row["id"]): f"{row['title']}（{row['category']}）"
                    for _, row in filtered.iterrows()
                }
                edit_id = st.selectbox(
                    "选择要修改的记录",
                    options=editable_ids,
                    format_func=lambda record_id: editable_titles[record_id],
                    key="edit_record_selector",
                )
                edit_row = filtered.loc[filtered["id"].astype(int) == edit_id].iloc[0]
                parsed_edit_date = pd.to_datetime(edit_row["record_date"], errors="coerce")
                initial_edit_date = date.today() if pd.isna(parsed_edit_date) else parsed_edit_date.date()
                current_certificate = _certificate_path(str(edit_row.get("certificate_file", "")))

                with st.form(f"edit_record_form_{edit_id}"):
                    edit_title = st.text_input(
                        "记录名称 *",
                        value=str(edit_row["title"]),
                        key=f"edit_title_{edit_id}",
                    )
                    edit_category = st.selectbox(
                        "记录类型 *",
                        CATEGORIES,
                        index=CATEGORIES.index(str(edit_row["category"])),
                        key=f"edit_category_{edit_id}",
                    )
                    edit_date = st.date_input(
                        "记录日期",
                        value=initial_edit_date,
                        max_value=date.today(),
                        key=f"edit_date_{edit_id}",
                    )
                    edit_notes = st.text_area(
                        "记录内容",
                        value=str(edit_row["notes"]),
                        height=120,
                        key=f"edit_notes_{edit_id}",
                    )
                    replacement_pdf = st.file_uploader(
                        "替换 PDF 证书（选填）",
                        type=["pdf"],
                        key=f"edit_pdf_{edit_id}",
                    )
                    remove_pdf = st.checkbox(
                        "移除当前 PDF 证书",
                        value=False,
                        disabled=current_certificate is None,
                        key=f"remove_pdf_{edit_id}",
                    )
                    if current_certificate is not None:
                        st.caption(f"当前证书：{current_certificate.name}")
                    confirm_delete = st.checkbox(
                        "确认删除这条记录",
                        value=False,
                        key=f"confirm_delete_{edit_id}",
                    )
                    save_column, delete_column = st.columns(2)
                    with save_column:
                        edit_submitted = st.form_submit_button("保存修改", use_container_width=True)
                    with delete_column:
                        delete_submitted = st.form_submit_button("删除记录", use_container_width=True)

                if delete_submitted:
                    if not confirm_delete:
                        st.error("请先勾选“确认删除这条记录”。")
                    else:
                        try:
                            delete_record(records, edit_id)
                            st.success("记录及其关联证书已删除。")
                            st.rerun()
                        except ValueError as error:
                            st.error(str(error))
                        except (OSError, sqlite3.DatabaseError):
                            st.error("删除失败，请检查数据库文件是否可写。")
                elif edit_submitted:
                    try:
                        update_record(
                            records,
                            edit_id,
                            edit_title,
                            edit_category,
                            edit_notes,
                            edit_date,
                            certificate_name=replacement_pdf.name if replacement_pdf else None,
                            certificate_bytes=replacement_pdf.getvalue() if replacement_pdf else None,
                            remove_certificate=remove_pdf,
                        )
                        st.success("记录修改已保存。")
                        st.rerun()
                    except ValueError as error:
                        st.error(str(error))
                    except (OSError, sqlite3.DatabaseError):
                        st.error("修改保存失败，请检查数据库文件是否可写。")

    with form_column:
        st.subheader("新增记录")
        with st.form("record_form", clear_on_submit=True):
            title = st.text_input("记录名称 *", placeholder="例如：全国大学生创新创业比赛")
            category = st.selectbox("记录类型 *", CATEGORIES)
            chosen_date = st.date_input("记录日期", value=date.today(), max_value=date.today())
            notes = st.text_area("记录内容", placeholder="写下过程、收获或值得记住的细节", height=130)
            uploaded_pdf = st.file_uploader(
                "上传 PDF 证书（选填，最大 15 MB）",
                type=["pdf"],
                help="可上传获奖证明、荣誉证书或技能证书。文件会保存在 data/certificates 目录。",
            )
            submitted = st.form_submit_button("保存记录", use_container_width=True)
        if submitted:
            try:
                add_record(
                    records,
                    title,
                    category,
                    notes,
                    chosen_date,
                    certificate_name=uploaded_pdf.name if uploaded_pdf else None,
                    certificate_bytes=uploaded_pdf.getvalue() if uploaded_pdf else None,
                )
                st.success("记录已保存。")
                st.rerun()
            except ValueError as error:
                st.error(str(error))
            except (OSError, sqlite3.DatabaseError):
                st.error("保存失败，请检查数据库文件是否可写。")

        st.markdown("#### 数据导出")
        all_csv = _normalize_records(records).to_csv(index=False).encode("utf-8-sig")
        filtered_csv = _normalize_records(filtered).to_csv(index=False).encode("utf-8-sig")
        certificate_zip = build_certificate_archive(records)
        download_left, download_middle, download_right = st.columns(3)
        with download_left:
            st.download_button(
                "下载全部",
                all_csv,
                file_name="个人成长档案_全部.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with download_middle:
            st.download_button(
                "下载筛选结果",
                filtered_csv,
                file_name="个人成长档案_筛选结果.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with download_right:
            st.download_button(
                "导出证书",
                certificate_zip or b"",
                file_name="个人成长档案_PDF证书.zip",
                mime="application/zip",
                disabled=certificate_zip is None,
                help="将所有已保存的 PDF 证书打包为 ZIP 文件。",
                use_container_width=True,
            )


if __name__ == "__main__":
    main()

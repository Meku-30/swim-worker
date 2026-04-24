"""空港関連パーサー (一覧/詳細/AIP)

parse_list/parse_detail/parse_aip は DB 依存なし → Worker でも使える。
store_* のみ SQLAlchemy + models を関数内 import で使用する。
"""
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

logger = logging.getLogger(__name__)


# --- AIP HTMLパーサー ---

class _TableParser(HTMLParser):
    """HTMLテーブルをパースするパーサー (標準ライブラリのみ)"""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._in_cell = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._current_cell = []
            self._in_cell = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._in_cell:
            cell_text = "".join(self._current_cell or []).strip()
            if self._current_row is not None:
                self._current_row.append(cell_text)
            self._current_cell = None
            self._in_cell = False
        elif tag == "tr" and self._current_row is not None:
            if self._current_table is not None:
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            self.tables.append(self._current_table)
            self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._in_cell and self._current_cell is not None:
            self._current_cell.append(data)


def parse_list(raw_data: dict) -> list[dict]:
    records = []
    for item in (raw_data.get("locationInfoList") or []):
        icao = (item.get("id") or "").strip()
        if not icao:
            continue
        records.append({"icao_code": icao, "name": item.get("nameJp") or item.get("nameEn") or icao})
    return records


async def store_list(session_factory, records: list[dict]) -> int:
    if not records:
        return 0
    from sqlalchemy import select
    from coordinator.db.models import Airport
    async with session_factory() as session:
        existing = await session.execute(select(Airport.icao_code))
        existing_codes = {row[0] for row in existing.all()}
    now = datetime.now(timezone.utc)
    count = 0
    async with session_factory() as session:
        for r in records:
            if r["icao_code"] in existing_codes:
                continue
            session.add(Airport(icao_code=r["icao_code"], name=r["name"], updated_at=now))
            count += 1
        await session.commit()
    return count


def parse_detail(raw_data: dict) -> list[dict]:
    # raw_data contains icao_code from the task params + the API response
    icao = raw_data.get("_icao_code", "")
    ret = raw_data.get("ret", raw_data)
    return [{
        "icao_code": icao,
        "service_start_time": ret.get("serviceStartTime"),
        "service_end_time": ret.get("serviceEndTime"),
        "sunrise_time": ret.get("sunriseTime"),
        "sunset_time": ret.get("sunsetTime"),
        "departure_delay": ret.get("departureDelayTime"),
        "arrival_delay": ret.get("arrivalDelayTime"),
        "airport_usage": ret.get("airportUsage") or [],
        "airport_usage_en": ret.get("airportUsageEn") or [],
        "runway_dep": ret.get("runwayNoDep") or [],
        "runway_ldg": ret.get("runwayNoLdg") or [],
        "approach": ret.get("approach") or [],
    }]


async def store_detail(session_factory, records: list[dict]) -> int:
    if not records:
        return 0
    from coordinator.db.models import AirportDetail
    now = datetime.now(timezone.utc)
    async with session_factory() as session:
        for r in records:
            session.add(AirportDetail(**r, collected_at=now))
        await session.commit()
    return len(records)


def parse_aip(raw_data: dict) -> list[dict]:
    """AIP HTMLレスポンスからエントリを抽出する

    raw_data には {"html": "<html>..."} の形式でHTMLが入る。
    """
    html = raw_data.get("html", "")
    if not html:
        return []
    return _parse_aip_html(html)


def _parse_aip_html(html: str) -> list[dict]:
    """AIP閲覧ページのHTMLをパースしてエントリリストを返す"""
    parser = _TableParser()
    parser.feed(html)

    results: list[dict] = []

    for table in parser.tables:
        if len(table) < 2:
            continue

        header_row = table[0]
        header_lower = [h.lower().strip() for h in header_row]

        date_col = _find_column_index(header_lower, ["effective", "date", "eff", "airac"])
        section_col = _find_column_index(header_lower, ["section", "amendment", "amdt"])
        desc_col = _find_column_index(header_lower, ["description", "reason", "subject", "remarks"])

        for row in table[1:]:
            if not any(cell.strip() for cell in row):
                continue

            entry: dict = {"effective_date": None, "section": "", "description": ""}

            if date_col is not None and date_col < len(row):
                entry["effective_date"] = _parse_date_string(row[date_col].strip())
            if section_col is not None and section_col < len(row):
                entry["section"] = row[section_col].strip()
            if desc_col is not None and desc_col < len(row):
                entry["description"] = row[desc_col].strip()

            # フォールバック
            if date_col is None and len(row) >= 1:
                parsed_date = _parse_date_string(row[0].strip())
                if parsed_date is not None:
                    entry["effective_date"] = parsed_date
            if section_col is None and len(row) >= 2:
                entry["section"] = entry["section"] or row[1].strip()
            if desc_col is None and len(row) >= 3:
                entry["description"] = entry["description"] or row[2].strip()

            results.append(entry)

    return results


def _find_column_index(headers: list[str], keywords: list[str]) -> int | None:
    for i, header in enumerate(headers):
        for keyword in keywords:
            if keyword in header:
                return i
    return None


def _parse_date_string(value: str) -> datetime | None:
    if not value:
        return None
    formats = ["%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d"]
    for fmt in formats:
        try:
            dt = datetime.strptime(value.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _extract_icao_from_section(section: str) -> str:
    """セクション文字列からICAOコードを抽出する (例: "AD 2 RJTT" -> "RJTT")"""
    icao_pattern = re.compile(r"\b([A-Z]{4})\b")
    match = icao_pattern.search(section)
    if match:
        candidate = match.group(1)
        if candidate[:2] in ("RJ", "RO"):
            return candidate
    return ""


async def store_aip(session_factory, records: list[dict]) -> int:
    """AIPエントリをDBに保存する (重複チェック: effective_date + section)"""
    if not records:
        return 0
    from sqlalchemy import select
    from coordinator.db.models import AipEntry
    saved = 0
    async with session_factory() as session:
        for entry in records:
            effective_date = entry.get("effective_date")
            section = entry.get("section", "")
            description = entry.get("description", "")

            if not section and not effective_date:
                continue

            icao_code = _extract_icao_from_section(section)

            # 重複チェック
            query = select(AipEntry).where(AipEntry.section == section)
            if effective_date is not None:
                query = query.where(AipEntry.effective_date == effective_date)
            existing = await session.execute(query)
            if existing.scalar_one_or_none() is not None:
                continue

            session.add(AipEntry(
                icao_code=icao_code,
                section=section,
                content=description,
                pdf_url=None,
                effective_date=effective_date,
            ))
            saved += 1

        await session.commit()
    return saved

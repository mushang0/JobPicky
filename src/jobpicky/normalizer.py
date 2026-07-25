from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from .locations import canonical_location_name, known_location_names

_LEGACY_CITY_NAMES = [
    "北京",
    "上海",
    "天津",
    "重庆",
    "深圳",
    "广州",
    "杭州",
    "成都",
    "南京",
    "苏州",
    "武汉",
    "西安",
    "长沙",
    "合肥",
    "郑州",
    "青岛",
    "济南",
    "大连",
    "宁波",
    "厦门",
    "福州",
    "无锡",
    "常州",
    "东莞",
    "佛山",
    "珠海",
    "中山",
    "惠州",
    "南昌",
    "南宁",
    "昆明",
    "贵阳",
    "太原",
    "石家庄",
    "沈阳",
    "长春",
    "哈尔滨",
    "呼和浩特",
    "兰州",
    "银川",
    "西宁",
    "乌鲁木齐",
    "海口",
    "三亚",
    "安庆",
]

# Backwards-compatible export for callers that still need a flat list.
KNOWN_CITY_NAMES = known_location_names()


def normalize_date(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    dotted = re.search(r"(20\d{2})\.(\d{1,2})\.(\d{1,2})", text)
    if dotted:
        year, month, day = dotted.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日?", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"(\d{1,2})月\s*(\d{1,2})日?", text)
    if match:
        year = datetime.now().year
        month, day = match.groups()
        return f"{year:04d}-{int(month):02d}-{int(day):02d}"
    return None


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def normalize_company(company: str | None, aliases: dict[str, list[str]] | None = None) -> str:
    text = (company or "").strip()
    aliases = aliases or {}
    for canonical, names in aliases.items():
        if canonical and canonical in text:
            return canonical
        for alias in names:
            if alias and alias in text:
                return canonical
    return re.sub(r"\s+", " ", text)


def company_group_key(company: str | None, aliases: dict[str, list[str]] | None = None) -> str:
    """Return a conservative identity key for company-level presentation.

    This deliberately normalizes punctuation, legal suffixes, and leading
    administrative names, but does not perform fuzzy matching. Fuzzy matching
    is appropriate for external-link enrichment; it is too risky as the sole
    rule for merging user-visible company cards.
    """
    text = normalize_company(company, aliases).casefold()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    suffixes = (
        "\u6709\u9650\u8d23\u4efb\u516c\u53f8",
        "\u80a1\u4efd\u6709\u9650\u516c\u53f8",
        "\u6709\u9650\u516c\u53f8",
        "\u96c6\u56e2\u6709\u9650\u516c\u53f8",
        "\u96c6\u56e2",
        "\u516c\u53f8",
    )
    for suffix in suffixes:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    # “上海艾为电子技术股份有限公司” and “艾为电子技术” should have a
    # stable presentation key, while short names such as “上海” remain intact.
    for location in sorted(KNOWN_CITY_NAMES, key=len, reverse=True):
        location_key = re.sub(r"[^\w\u4e00-\u9fff]+", "", location.casefold())
        if location_key and text.startswith(location_key) and len(text) - len(location_key) >= 4:
            text = text[len(location_key):]
            break
    return text


def build_dedupe_key(
    *,
    source: str,
    source_job_id: str | None,
    detail_url: str | None,
    company_normalized: str,
    title: str,
    batch: str | None,
    collected_date: str | None,
) -> str:
    if source_job_id:
        return f"{source}:id:{source_job_id}"
    normalized_url = normalize_url(detail_url)
    if normalized_url:
        return f"{source}:url:{normalized_url}"
    return f"{source}:combo:{company_normalized}|{title}|{batch or ''}|{collected_date or ''}"


def infer_graduate_year(text: str) -> str | None:
    match = re.search(r"(20\d{2})\s*届", text or "")
    return f"{match.group(1)}届" if match else None


def infer_batch(text: str) -> str | None:
    for keyword in ("提前批", "秋招", "春招", "实习", "校招"):
        if keyword in (text or ""):
            return keyword
    return None


def infer_city(text: str) -> str | None:
    if not text:
        return None
    hits: list[tuple[int, str]] = []
    for name in KNOWN_CITY_NAMES:
        short_name = re.sub(r"(?:特别行政区|自治区|自治州|地区|盟|省|市)$", "", name)
        for variant in (name, short_name):
            index = text.find(variant)
            if index >= 0:
                city = canonical_location_name(name) or name
                hits.append((index, city))
                break
    cities: list[str] = []
    for _, city in sorted(hits, key=lambda item: item[0]):
        if city not in cities:
            cities.append(city)
    return ";".join(cities) if cities else None

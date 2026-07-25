from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files
from typing import Any


@lru_cache(maxsize=1)
def location_taxonomy() -> dict[str, Any]:
    resource = files("jobpicky.resources").joinpath("china_locations_v1.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def location_options() -> list[dict[str, Any]]:
    return [
        {
            "value": f"province:{province['code']}",
            "label": province["name"],
            "cities": [
                {"value": f"city:{city['code']}", "label": city["name"]}
                for city in province.get("cities", [])
            ],
        }
        for province in location_taxonomy()["provinces"]
    ]


@lru_cache(maxsize=1)
def _location_index() -> tuple[dict[str, dict[str, str]], dict[str, str], dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    aliases: dict[str, str] = {}
    city_to_province: dict[str, str] = {}
    for province in location_taxonomy()["provinces"]:
        province_id = f"province:{province['code']}"
        records[province_id] = {"id": province_id, "name": province["name"], "province_id": province_id}
        for alias in _name_aliases(province["name"]):
            aliases.setdefault(alias, province_id)
        for city in province.get("cities", []):
            city_id = f"city:{city['code']}"
            records[city_id] = {"id": city_id, "name": city["name"], "province_id": province_id}
            city_to_province[city_id] = province_id
            for alias in _name_aliases(city["name"]):
                # For direct-administered municipalities the province and city
                # share a name. Free text should resolve to the city; an
                # explicit province selection keeps its stable province ID.
                aliases[alias] = city_id
    return records, aliases, city_to_province


def known_location_names() -> list[str]:
    records, _, _ = _location_index()
    return list(dict.fromkeys(record["name"] for record in records.values()))


def canonical_location_id(value: str) -> str | None:
    text = str(value or "").strip()
    records, aliases, _ = _location_index()
    if text in records:
        return text
    return aliases.get(text) or aliases.get(_strip_suffix(text))


def canonical_location_name(value: str) -> str | None:
    location_id = canonical_location_id(value)
    records, _, _ = _location_index()
    return records.get(location_id or "", {}).get("name")


def province_city_names(province: str) -> tuple[str, ...]:
    """Return names usable when filtering stored free-text city fields."""
    province_id = canonical_location_id(province)
    if not province_id or not province_id.startswith("province:"):
        return ()
    records, _, _ = _location_index()
    names = [record["name"] for record in records.values() if record["province_id"] == province_id]
    return tuple(dict.fromkeys(names))


def match_target_location(job_city: str | None, targets: list[str]) -> str | None:
    if not job_city:
        return None
    records, _, city_to_province = _location_index()
    job_ids = {
        location_id
        for part in re.split(r"[;；、,，/]+", job_city)
        if (location_id := canonical_location_id(part))
    }
    for target in targets:
        target_id = canonical_location_id(target)
        if not target_id:
            if str(target).strip() and str(target).strip() in job_city:
                return str(target).strip()
            continue
        if target_id in job_ids:
            return records[target_id]["name"]
        if target_id.startswith("province:") and any(city_to_province.get(job_id) == target_id for job_id in job_ids):
            return records[target_id]["name"]
    return None


def _match_target_location_embedded(job_city: str | None, targets: list[str]) -> str | None:
    """Match province/city targets inside administrative free-text values."""
    if not job_city:
        return None
    records, _, city_to_province = _location_index()
    parts = [part.strip() for part in re.split(r"[;；、,，]+", job_city) if part.strip()]
    parts.append(job_city.strip())
    job_ids: set[str] = set()
    for part in parts:
        if location_id := canonical_location_id(part):
            job_ids.add(location_id)
        for location_id, record in records.items():
            name = record["name"]
            variants = (name, re.sub(r"[省市]$", "", name))
            if any(variant and len(variant) >= 2 and variant in part for variant in variants):
                job_ids.add(location_id)
    for target in targets:
        target_id = canonical_location_id(target)
        if target_id and target_id in job_ids:
            return records[target_id]["name"]
        if target_id and target_id.startswith("province:") and any(
            city_to_province.get(job_id) == target_id for job_id in job_ids
        ):
            return records[target_id]["name"]
        if not target_id and str(target).strip() in job_city:
            return str(target).strip()
    return None


match_target_location = _match_target_location_embedded


def _name_aliases(name: str) -> set[str]:
    return {name, _strip_suffix(name)}


def _strip_suffix(name: str) -> str:
    return re.sub(r"(?:特别行政区|维吾尔自治区|壮族自治区|回族自治区|自治区|自治州|地区|盟|省|市)$", "", name)

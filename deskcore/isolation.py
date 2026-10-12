"""Project marker checks; deterministic, no model or database calls."""
from __future__ import annotations

import re
import unicodedata
import json


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold().strip()


def markers(project: dict) -> set[str]:
    brand, name = normalize(project.get("brand")), normalize(project.get("name"))
    params = project.get("default_params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (ValueError, TypeError):
            params = {}
    terms = params.get("isolation_terms", []) if isinstance(params, dict) else []
    if not isinstance(terms, list):
        terms = []
    out = {brand, name} | {normalize(t) for t in terms if isinstance(t, str)}
    # A named product after its brand (e.g. Hatherine-痘痘贴) is also an explicit marker.
    if brand and name.startswith(brand):
        suffix = name[len(brand):].strip(" -_/·:：")
        if re.fullmatch(r"[\u3400-\u9fff]{3,12}", suffix):
            out.add(suffix)
    return {t for t in out if len(t) >= 2}


def foreign_markers(text: str, target: dict, projects: list[dict], *, global_scope: bool = False) -> list[dict]:
    value = normalize(text)
    allowed = set() if global_scope else markers(target)
    out = []
    for project in projects:
        if not global_scope and str(project.get("id")) == str(target.get("id")):
            continue
        matched = []
        for term in sorted(markers(project) - allowed):
            # ASCII brand fragments must not match inside unrelated English words.
            pattern = (r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
                       if re.fullmatch(r"[a-z0-9 ._/-]+", term) else re.escape(term))
            if re.search(pattern, value):
                matched.append(term)
        if matched:
            out.append({"project_id": project["id"], "project_name": project.get("name") or "",
                        "terms": matched})
    return out

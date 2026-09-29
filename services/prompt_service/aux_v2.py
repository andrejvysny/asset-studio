"""Pure request models, prompt builders and strict output parsers for the aux v2 endpoints (no torch, unit-testable).

VLM output is untrusted data: everything is type-checked, bounded and normalised here before it leaves the service.
"""
from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

NOTE_MAX = 500
MAX_LABEL = 60
MAX_CHANGE = 400
MAX_ITEMS = 32
MAX_TEXT = 1000


def extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in model output")
    body = json.loads(match.group(0))
    if not isinstance(body, dict):
        raise ValueError("model output is not a JSON object")
    return body


# ---- request models -------------------------------------------------------------------------------------------

class RefImage(BaseModel):
    b64: str
    role: Literal["source", "reference"] = "reference"
    note: str = Field(default="", max_length=NOTE_MAX)


class CompareImage(BaseModel):
    b64: str
    label: Literal["source", "candidate", "reference"]
    note: str = Field(default="", max_length=NOTE_MAX)


class CompareQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=500)


class CompareRequest(BaseModel):
    images: list[CompareImage] = Field(min_length=2, max_length=5)
    questions: list[CompareQuestion] = Field(min_length=1, max_length=8)
    context: str = Field(default="", max_length=4000)


class ViewImage(BaseModel):
    b64: str
    view: str = Field(default="", max_length=60)


class AnalyzeRequest(BaseModel):
    images: list[ViewImage] = Field(min_length=1, max_length=4)
    kind: str = Field(default="", max_length=60)
    user_facts: str = Field(default="", max_length=4000)


class SuggestRequest(BaseModel):
    images: list[ViewImage] = Field(default=[], max_length=4)
    request: str = Field(min_length=1, max_length=4000)
    count: int = Field(ge=1, le=32)
    intent: Literal["subtle", "related", "exploratory"] = "related"
    preserve: str = Field(default="", max_length=2000)
    kind: str = Field(default="", max_length=60)
    observations: list[str] = Field(default=[], max_length=32)


# ---- helpers ---------------------------------------------------------------------------------------------------

def _clip(s: str, n: int) -> str:
    return " ".join(s.split())[:n]


def _str_list(v: Any, field: str, maxlen: int = MAX_TEXT) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{field} must be a list of strings")
    return [c for c in (_clip(x, maxlen) for x in v) if c][:MAX_ITEMS]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48]


def _dedupe(items: list[str], limit: int, sep: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cand, n = item, 1
        while cand.lower() in seen:
            n += 1
            suffix = f"{sep}{n}"
            cand = item[:limit - len(suffix)] + suffix
        seen.add(cand.lower())
        out.append(cand)
    return out


# ---- /enhance --------------------------------------------------------------------------------------------------

DROPPED_NOTE = "model proposed additions; dropped (conservative)"


def parse_enhance_output(raw: str, preset: str, n_images: int | None = None) -> dict[str, Any]:
    body = extract_json(raw)
    desc = body.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError("description must be a non-empty string")
    title = body.get("short_title", "")
    if not isinstance(title, str):
        raise ValueError("short_title must be a string")
    assumptions = _str_list(body.get("assumptions"), "assumptions")
    additions = _str_list(body.get("additions"), "additions")
    if preset != "creative":
        if additions:
            assumptions.append(DROPPED_NOTE)
        additions = []
    cues_raw = body.get("reference_cues", [])
    if cues_raw is None:
        cues_raw = []
    if not isinstance(cues_raw, list):
        raise ValueError("reference_cues must be a list")
    cues: list[dict[str, Any]] = []
    for c in cues_raw:
        if not isinstance(c, dict) or isinstance(c.get("index"), bool) or not isinstance(c.get("index"), int) \
                or not isinstance(c.get("cue"), str):
            raise ValueError("reference_cues entries need int index and str cue")
        if c["index"] < 0 or (n_images is not None and c["index"] >= n_images) or not c["cue"].strip():
            continue  # out-of-range / empty: untrusted, dropped
        cues.append({"index": c["index"], "cue": _clip(c["cue"], MAX_TEXT)})
    return {"description": _clip(desc, 4000), "short_title": _clip(title, 80),
            "tags": _str_list(body.get("tags"), "tags", 60), "facts": _str_list(body.get("facts"), "facts"),
            "additions": additions, "assumptions": assumptions, "reference_cues": cues[:MAX_ITEMS]}


# ---- /compare --------------------------------------------------------------------------------------------------

def parse_compare(raw: str, ids: list[str]) -> dict[str, Any]:
    """Never raises: advisory output, anything unusable becomes "unsure"."""
    try:
        body = extract_json(raw)
    except (ValueError, json.JSONDecodeError):
        body = {}
    checks_in = body.get("checks") if isinstance(body.get("checks"), dict) else {}
    reasons_in = body.get("reasons") if isinstance(body.get("reasons"), dict) else {}
    checks: dict[str, bool | str] = {}
    reasons: dict[str, str] = {}
    for qid in ids:
        v = checks_in.get(qid, None) if qid in checks_in else None
        reason = reasons_in.get(qid)
        reason = _clip(reason, 500) if isinstance(reason, str) else ""
        if qid not in checks_in:
            checks[qid], reasons[qid] = "unsure", "not answered"
        elif isinstance(v, bool):
            checks[qid], reasons[qid] = v, reason
        elif isinstance(v, str) and v.strip().lower() == "unsure":
            checks[qid], reasons[qid] = "unsure", reason or "model unsure"
        else:
            checks[qid], reasons[qid] = "unsure", f"invalid answer {str(v)[:40]!r}"
    return {"checks": checks, "reasons": reasons}


# ---- /analyze_source -------------------------------------------------------------------------------------------

def parse_analysis(raw: str, n_images: int) -> dict[str, Any]:
    """Image indices in the prompt are 1-based; the output uses 0-based indices into the request images."""
    body = extract_json(raw)
    obs_raw = body.get("observations")
    if not isinstance(obs_raw, list):
        raise ValueError("observations must be a list")
    observations: list[dict[str, Any]] = []
    dropped = 0
    for o in obs_raw:
        if not isinstance(o, dict) or not isinstance(o.get("text"), str) or not isinstance(o.get("images"), list) \
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in o["images"]):
            raise ValueError("observations entries need str text and int list images")
        idx = sorted({i - 1 for i in o["images"] if 1 <= i <= n_images})
        text = _clip(o["text"], MAX_TEXT)
        if not text or not idx:
            dropped += 1  # uncited or empty observations are not evidence
            continue
        observations.append({"text": text, "images": idx})
    pres_raw = body.get("proposed_preserve", [])
    if not isinstance(pres_raw, list):
        raise ValueError("proposed_preserve must be a list")
    texts: list[tuple[str, str]] = []
    for p in pres_raw:
        if not isinstance(p, dict) or not isinstance(p.get("text"), str) or not isinstance(p.get("id", ""), str):
            raise ValueError("proposed_preserve entries need str id and text")
        text = _clip(p["text"], MAX_TEXT)
        if text:
            texts.append((_slug(p.get("id", "")) or _slug(text)[:32] or "item", text))
    ids = _dedupe([t[0] for t in texts], 48, "_")
    return {"observations": observations[:MAX_ITEMS], "dropped_observations": dropped,
            "uncertainties": _str_list(body.get("uncertainties"), "uncertainties"),
            "proposed_preserve": [{"id": i, "text": t[1]} for i, t in zip(ids, texts, strict=True)][:MAX_ITEMS],
            "proposed_changeable": _str_list(body.get("proposed_changeable"), "proposed_changeable")}


# ---- /suggest_variants -----------------------------------------------------------------------------------------

def parse_suggestions(raw: str, count: int) -> dict[str, Any]:
    body = extract_json(raw)
    rows_raw = body.get("rows")
    if not isinstance(rows_raw, list):
        raise ValueError("rows must be a list")
    rows: list[dict[str, str]] = []
    for r in rows_raw:
        if not isinstance(r, dict) or not isinstance(r.get("label"), str) \
                or not isinstance(r.get("change_request"), str):
            raise ValueError("rows entries need str label and change_request")
        label, change = _clip(r["label"], MAX_LABEL), _clip(r["change_request"], MAX_CHANGE)
        if label and change:
            rows.append({"label": label, "change_request": change})
    if not rows:
        raise ValueError("no usable rows")
    rows = rows[:count]
    labels = _dedupe([r["label"] for r in rows], MAX_LABEL, " ")
    rows = [{"label": lb, "change_request": r["change_request"]} for lb, r in zip(labels, rows, strict=True)]
    return {"rows": rows, "short_by": count - len(rows), "notes": _str_list(body.get("notes"), "notes")}

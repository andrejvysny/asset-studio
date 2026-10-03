"""Publication endpoints of the fake AssetStudio server (AS-09): preview upload, commit with compare-and-swap and
idempotency, operation query. Mixed into fake_server.Handler, which supplies `state`, `IDS`, `_err`, `_json`."""
from __future__ import annotations

import hashlib
import json
import os
import re


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def parse_multipart(content_type: str, body: bytes) -> dict[str, bytes] | None:
    """name -> bytes of a multipart/form-data body, or None when malformed or a part name repeats."""
    m = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not content_type.startswith("multipart/form-data") or not m:
        return None
    delim = b"--" + m.group(1).encode()
    parts: dict[str, bytes] = {}
    for chunk in body.split(delim)[1:]:
        if chunk.startswith(b"--"):
            break
        head, sep, data = chunk.lstrip(b"\r\n").partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if not sep or not name or b"filename=" not in head or name.group(1).decode() in parts:
            return None
        parts[name.group(1).decode()] = data[:-2] if data.endswith(b"\r\n") else data
    return parts


class PublicationMixin:
    # --- publication (AS-09) ------------------------------------------------------------------------------
    def _preview(self, body: bytes) -> None:
        s = self.state
        if s.scenario == "preview_busy":
            return self._err(503, "temporarily_unavailable", "staging area is full", True,
                             {"reason": "staging_capacity"})
        parts = parse_multipart(self.headers.get("Content-Type", ""), body)
        names = set(parts or {})
        if parts is None or not names <= {"source", "portable", "descriptor", "thumbnail", "report"}:
            return self._err(400, "invalid_request", "unknown, repeated or malformed multipart part")
        if not {"portable", "descriptor"} <= names or ("source" in names) != ("report" in names):
            return self._err(400, "invalid_request", "missing or unexpected multipart parts",
                             details={"parts": sorted({"portable", "descriptor"} - names)})
        problem = self._check_parts(parts)
        if problem:
            return self._err(422, *problem)
        draft = json.loads(parts["descriptor"])
        preview_id = "ipv_" + hashlib.sha256(os.urandom(16)).hexdigest()[:16]
        sha = {n: hashlib.sha256(b).hexdigest() for n, b in parts.items()}
        receipt = {
            "preview_id": preview_id, "library_id": self.IDS["library"], "expires_at": "2099-01-01T00:00:00.000Z",
            "parts": {n: {"sha256": sha[n], "size": len(parts[n])} for n in sorted(parts)},
            "package_sha256": sha.get("source"), "portable_sha256": sha["portable"],
            "descriptor_draft_sha256": hashlib.sha256(canonical(draft)).hexdigest(),
            "descriptor_draft": canonical(draft).decode(), "bounds": {"min": ["0", "0", "0"], "max": ["1", "1", "1"]},
            "budget": {"within_ipad_budget": True}, "report_sha256": sha.get("report"),
            "source": {"manifest_sha256": None, "detected_capabilities": [], "dependency_closure": [],
                       "asset_dependencies": [], "warnings": [], "evidence": {}} if "source" in parts else None,
            "warnings": sorted(set(draft.get("preview_warnings", [])))}
        s.previews[preview_id] = {"receipt": receipt, "parts": parts}
        return self._json(200, receipt)

    @staticmethod
    def _check_parts(parts: dict[str, bytes]) -> tuple[str, str, bool, dict] | None:
        if not parts["portable"].startswith(b"glTF"):
            return ("unsafe_package", "portable is not a GLB", False, {})
        if "source" in parts and not parts["source"].startswith(b"PK\x03\x04"):
            return ("unsafe_package", "source is not a zip archive", False, {})
        try:
            draft, report = json.loads(parts["descriptor"]), json.loads(parts.get("report", b"{}"))
        except ValueError:
            return ("invalid_request", "descriptor or report is not JSON", False, {})
        if not isinstance(draft, dict) or draft.get("schema_version") != 1 or not isinstance(report, dict):
            return ("invalid_request", "descriptor draft failed validation", False, {})
        return None

    def _commit(self, body: bytes) -> None:
        s = self.state
        try:
            req = json.loads(body)
        except ValueError:
            return self._err(400, "invalid_request", "body is not JSON")
        allowed = {"preview_id", "target_asset_id", "expected_current_version", "package_sha256", "portable_sha256",
                   "descriptor_draft_sha256", "name", "category_id", "tags", "licence", "source_uri", "credit",
                   "idempotency_key"}
        needed = {"preview_id", "portable_sha256", "descriptor_draft_sha256", "name", "idempotency_key"}
        key = req.get("idempotency_key", "")
        if not set(req) <= allowed or not needed <= set(req) or not 8 <= len(str(key)) <= 100:
            return self._err(422, "invalid_request", "commit body failed validation")
        if (req.get("target_asset_id") is None) != (req.get("expected_current_version") is None):
            return self._err(422, "invalid_request", "target_asset_id and expected_current_version go together")
        digest = hashlib.sha256(canonical({k: v for k, v in req.items() if k != "idempotency_key"})).hexdigest()
        prior = s.ops.get(key)
        if prior is not None:
            if prior["digest"] != digest:
                return self._err(409, "idempotency_conflict", "idempotency key reused with a different request")
            return self._commit_reply(prior["response"])
        outcome = self._commit_new(req, key)
        if isinstance(outcome, tuple):
            return self._err(*outcome)
        s.ops[key] = {"digest": digest, "response": outcome}
        return self._commit_reply(outcome)

    def _commit_reply(self, response: dict) -> None:
        if self.state.scenario == "drop_commit_response":
            self.close_connection = True
            self.connection.shutdown(2)
            return
        self._json(200, response)

    def _commit_new(self, req: dict, key: str) -> dict | tuple:
        s = self.state
        pv = s.previews.get(req["preview_id"])
        if pv is None:
            return (410, "preview_expired", "preview expired or unknown; upload again")
        rc = pv["receipt"]
        if (req.get("package_sha256"), req["portable_sha256"], req["descriptor_draft_sha256"]) != (
                rc["package_sha256"], rc["portable_sha256"], rc["descriptor_draft_sha256"]):
            return (409, "integrity_mismatch", "request hashes differ from the preview receipt")
        target = req.get("target_asset_id")
        digest = hashlib.sha256(key.encode()).hexdigest()
        if target is None:
            asset_id = "ast_" + digest[:16]
        elif target != self.IDS["asset"] and target not in s.assets:
            return (404, "asset_not_found", "asset not found")
        else:
            asset_id = target
            current = s.current if target == self.IDS["asset"] else s.assets[target]["current"]
            if current != req["expected_current_version"]:
                return (409, "stale_pointer", "the asset changed since it was read", False,
                        {"current_version_id": current})
        version_id = "ver_" + hashlib.sha256((digest + "ver").encode()).hexdigest()[:16]
        s.version_count[asset_id] = s.version_count.get(asset_id, 0) + 1
        if asset_id == self.IDS["asset"]:
            s.current = version_id
        else:
            s.assets[asset_id] = {"current": version_id}
        display = str(s.version_count[asset_id])
        s.published.append({"asset_id": asset_id, "version_id": version_id, "key": key, "name": req["name"],
                            "preview_id": req["preview_id"], "tags": req.get("tags", []),
                            "licence": req.get("licence", "unknown")})
        ref = {"server_id": self.IDS["server"], "library_id": self.IDS["library"], "asset_id": asset_id,
               "version_id": version_id}
        return {"operation": {"idempotency_key": key, "state": "committed"}, "asset_ref": ref,
                "display_version": display,
                "descriptor_sha256": hashlib.sha256(rc["descriptor_draft"].encode()).hexdigest(),
                "deliveries": []}

    def _operation(self, key: str) -> None:
        if not 8 <= len(key) <= 100:
            return self._err(400, "invalid_request", "idempotency key must be 8..100 characters")
        op = self.state.ops.get(key)
        if op is None:
            return self._json(200, {"idempotency_key": key, "state": "unknown"})
        ref = op["response"]["asset_ref"]
        return self._json(200, {"idempotency_key": key, "state": "committed", "asset_id": ref["asset_id"],
                                "version_id": ref["version_id"], "display_version": op["response"]["display_version"]})

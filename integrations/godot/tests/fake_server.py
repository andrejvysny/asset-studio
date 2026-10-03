"""Fake AssetStudio integration server for client tests (stdlib only).

Usage: fake_server.py [port] [--token T] [--contracts-dir DIR]   (port 0 or omitted: pick one; prints "PORT <n>")
Control: POST /__scenario {"name": ...}; GET /__log (request log); POST /__log/clear;
POST /__current {"version_id": ...} sets the asset's current version (default v2);
POST /__events {"events": [...]} queues change events for the next GET /changes.
POST /__mutate {"version_id", "required_capabilities": [...], "dependency_on": version_id} rewrites that version's
manifest (new sha256); mutate the dependency first. POST /__reset_manifests restores the fixtures.
Scenarios: normal, corrupt_content, ignore_range, wrong_server_id, forbidden, slow, drop_midway,
legacy_resolve (no `representations` map), rep_unsupported (requested rep unsupported, mobile_glb_v1 ready),
rep_unsupported_no_error (as rep_unsupported, error null), rep_missing_key (map lacks the requested rep),
preview_busy (publications:preview answers 503 temporarily_unavailable, reason staging_capacity),
drop_commit_response (publications:commit is processed, then the connection is closed without a response).
Publication (AS-09): POST /libraries/{L}/publications:preview (multipart source/portable/descriptor/thumbnail/report),
POST .../publications:commit (CAS on expected_current_version, idempotency key replay / conflict),
GET .../publication-operations/{key}. Control: GET /__publications lists commits, POST /__publish_reset clears them
and restores the asset's current version.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fake_publication import PublicationMixin

PREFIX = "/api/integration/v1"
SERVER_ID = "6f1c2a52-3c2e-4d4b-9a57-0b6f6f0c1d2e"
OTHER_SERVER_ID = "11111111-2222-4333-8444-555555555555"
LIBRARY = "prj_0000000000000001"
DEFAULT_CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "godot-integration" / "v1"
# (descriptor fixture, manifest fixture) per exact version served
PAIRS = {
    "ver_00000000000000v1": ("descriptors/valid/primitive_prop.json", "manifests/valid/portable_primitive_prop.json"),
    "ver_00000000000000v2": (
        "descriptors/valid/primitive_prop_v2.json", "manifests/valid/portable_primitive_prop_v2.json"),
}
ASSET_ID = "ast_00000000000000aa"
SCENARIOS = {"normal", "corrupt_content", "ignore_range", "wrong_server_id", "forbidden", "slow", "drop_midway",
             "legacy_resolve", "rep_unsupported", "rep_unsupported_no_error", "rep_missing_key", "preview_busy",
             "drop_commit_response"}


class State:
    def __init__(self, contracts: Path, token: str) -> None:
        self.token = token
        self.scenario = "normal"
        self.current = "ver_00000000000000v2"
        self.events: list[dict] = []
        self.log: list[dict] = []
        self.lock = threading.Lock()
        fx = contracts / "fixtures"
        glbs = {hashlib.sha256(p.read_bytes()).hexdigest(): p.read_bytes() for p in (fx / "glb").glob("*.glb")}
        self.versions: dict[str, dict] = {}
        self.artifacts: dict[str, bytes] = {}
        self.originals: dict[str, bytes] = {}
        for ver, (desc_rel, man_rel) in PAIRS.items():
            desc, man = (fx / desc_rel).read_bytes(), (fx / man_rel).read_bytes()
            doc = json.loads(man)
            self.originals[ver] = man
            self.versions[ver] = {"descriptor": desc, "manifest": man, "delivery_id": doc["delivery_id"],
                                  "total": sum(f["size"] for f in doc["files"])}
            for f in doc["files"]:
                self.artifacts[f["artifact_id"]] = glbs[f["sha256"]]
        self.sync_manifests()
        self.reset_publications()

    def reset_publications(self) -> None:
        self.current = "ver_00000000000000v2"
        self.previews: dict[str, dict] = {}
        self.ops: dict[str, dict] = {}
        self.published: list[dict] = []
        self.assets: dict[str, dict] = {}  # new assets: asset_id -> {"current": version_id, "count": int}
        self.version_count: dict[str, int] = {ASSET_ID: 2}

    def sync_manifests(self) -> None:
        self.manifests = {v["delivery_id"]: v["manifest"] for v in self.versions.values()}

    def mutate(self, ver: str, caps: list[str] | None, dependency_on: str | None) -> None:
        doc = json.loads(self.originals[ver])
        if caps is not None:
            doc["required_capabilities"] = caps
        if dependency_on is not None:
            dep_ver = self.versions[dependency_on]
            dep_doc = json.loads(dep_ver["manifest"])
            ref = {"server_id": SERVER_ID, "library_id": LIBRARY, "asset_id": ASSET_ID, "version_id": dependency_on}
            doc["dependencies"] = [{"asset_key": _asset_key(ref), "asset_ref": ref,
                                    "descriptor_sha256": hashlib.sha256(dep_ver["descriptor"]).hexdigest(),
                                    "representation": "portable_glb_v1", "delivery_id": dep_doc["delivery_id"],
                                    "manifest_sha256": hashlib.sha256(dep_ver["manifest"]).hexdigest()}]
        self.versions[ver]["manifest"] = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
        self.sync_manifests()

    def reset_manifests(self) -> None:
        for ver, raw in self.originals.items():
            self.versions[ver]["manifest"] = raw
        self.sync_manifests()


def envelope(code: str, message: str, retryable: bool = False, details: dict | None = None) -> bytes:
    return json.dumps({"error": {"code": code, "message": message, "retryable": retryable,
                                 "details": details or {}}}).encode()


class Handler(PublicationMixin, BaseHTTPRequestHandler):
    server_version = "FakeAssetStudio/1"
    protocol_version = "HTTP/1.1"
    state: State
    IDS = {"asset": ASSET_ID, "server": SERVER_ID, "library": LIBRARY}

    def log_message(self, *_args: object) -> None:  # keep test output quiet; never log headers
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):  # clients cancel and drop on purpose
            self.close_connection = True

    # --- plumbing -----------------------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status: int, code: str, message: str = "", retryable: bool = False,
             details: dict | None = None) -> None:
        self._send(status, envelope(code, message or code, retryable, details))

    def _read_body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj).encode())

    def _record(self) -> None:
        with self.state.lock:
            self.state.log.append({"method": self.command, "path": self.path.split("?")[0],
                                   "range": self.headers.get("Range"), "scenario": self.state.scenario})

    def _authed(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {self.state.token}":
            self._err(401, "unauthorized", "missing or invalid credential")
            return False
        return True

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        path = self.path.split("?")[0]
        body = self._read_body() if self.command == "POST" else b""
        if path.startswith("/__"):
            return self._control(path, body)
        self._record()
        if path == f"{PREFIX}/health":
            health = {"status": "ok", "service": "assetstudio-integration", "api_version": 1}
            return self._send(200, json.dumps(health).encode())
        if not path.startswith(PREFIX + "/") or not self._authed():
            return None if path.startswith(PREFIX + "/") else self._err(404, "asset_not_found")
        self._route(path[len(PREFIX):], body)
        return None

    # --- control ------------------------------------------------------------------------------------------
    def _control(self, path: str, body: bytes) -> None:
        if path == "/__scenario" and self.command == "POST":
            name = json.loads(body or b"{}").get("name")
            if name not in SCENARIOS:
                return self._err(400, "invalid_request", "unknown scenario")
            self.state.scenario = name
            return self._send(200, b'{"ok":true}')
        if path == "/__current" and self.command == "POST":
            self.state.current = json.loads(body or b"{}").get("version_id", self.state.current)
            return self._send(200, b'{"ok":true}')
        if path == "/__mutate" and self.command == "POST":
            req = json.loads(body or b"{}")
            self.state.mutate(req["version_id"], req.get("required_capabilities"), req.get("dependency_on"))
            return self._send(200, b'{"ok":true}')
        if path == "/__reset_manifests" and self.command == "POST":
            self.state.reset_manifests()
            return self._send(200, b'{"ok":true}')
        if path == "/__events" and self.command == "POST":
            self.state.events.extend(json.loads(body or b"{}").get("events", []))
            return self._send(200, b'{"ok":true}')
        if path == "/__publications":
            return self._send(200, json.dumps(self.state.published).encode())
        if path == "/__publish_reset" and self.command == "POST":
            self.state.reset_publications()
            return self._send(200, b'{"ok":true}')
        if path == "/__log":
            return self._send(200, json.dumps(self.state.log).encode())
        if path == "/__log/clear":
            self.state.log.clear()
            return self._send(200, b'{"ok":true}')
        return self._err(404, "invalid_request")

    # --- API ----------------------------------------------------------------------------------------------
    def _route(self, route: str, body: bytes) -> None:
        s = self.state
        if route == "/capabilities":
            sid = OTHER_SERVER_ID if s.scenario == "wrong_server_id" else SERVER_ID
            return self._send(200, json.dumps({"server_id": sid, "api_version": 1, "contract_version": 1,
                                               "representations": ["portable_glb_v1"],
                                               "granted": {"library_ids": [LIBRARY],
                                                           "scopes": ["assets:read", "assets:publish"]}}).encode())
        if s.scenario == "forbidden":
            return self._err(403, "forbidden", "token does not grant this access")
        parts = route.strip("/").split("/")
        if route == "/libraries":
            libs = {"libraries": [{"library_id": LIBRARY, "name": "Fake", "state": "available"}]}
            return self._send(200, json.dumps(libs).encode())
        if route == "/changes":
            events, s.events = s.events, []
            return self._send(200, json.dumps({"cursor": "Y3Vyc29y", "events": events, "reset_required": False}).encode())
        if len(parts) >= 3 and parts[0] == "libraries" and parts[1] == LIBRARY:
            return self._library_route(parts[2:], body)
        return self._err(403, "forbidden", "token does not grant this access")

    def _library_route(self, parts: list[str], body: bytes) -> None:
        s = self.state
        if parts == ["publications:preview"] and self.command == "POST":
            return self._preview(body)
        if parts == ["publications:commit"] and self.command == "POST":
            return self._commit(body)
        if len(parts) == 2 and parts[0] == "publication-operations" and self.command == "GET":
            return self._operation(parts[1])
        if parts == ["resolve"] and self.command == "POST":
            return self._resolve(json.loads(body))
        if parts == ["assets"]:
            item = {"asset_id": ASSET_ID, "display_name": "Fixture Crate", "category_id": "props", "tags": ["crate"],
                    "current_version_id": s.current, "display_version": s.current[-2:], "metadata_revision": 1,
                    "has_thumbnail": False}
            return self._send(200, json.dumps({"items": [item], "next_cursor": None}).encode())
        if parts == ["assets", ASSET_ID]:
            versions = [{"version_id": v, "display_version": v[-2:], "published_at": "2026-01-01T00:00:00Z"}
                        for v in s.versions]
            detail = {"library_id": LIBRARY, "asset_id": ASSET_ID, "display_name": "Fixture Crate",
                      "category_id": "props", "tags": ["crate"], "metadata_revision": 1,
                      "current_version_id": s.current, "versions": versions}
            return self._send(200, json.dumps(detail).encode())
        if len(parts) == 3 and parts[0] == "deliveries" and parts[2] == "manifest":
            raw = s.manifests.get(parts[1])
            if raw is None:
                return self._err(404, "asset_not_found")
            return self._send(200, raw, extra={"X-Content-SHA256": hashlib.sha256(raw).hexdigest()})
        if len(parts) == 3 and parts[0] == "artifacts" and parts[2] == "content":
            return self._content(parts[1])
        return self._err(404, "asset_not_found")

    def _resolve(self, req: dict) -> None:
        entries = []
        for ref in req["refs"]:
            ver = self.state.versions.get(ref["version_id"])
            base = {"asset_ref": ref, "descriptor_sha256": None, "descriptor_json": None, "deliveries": [],
                    "dependencies": []}
            key = _asset_key(ref)
            if ref["server_id"] != SERVER_ID:
                state, code = "server_identity_mismatch", "server_identity_mismatch"
            elif ref["asset_id"] != ASSET_ID:
                state, code = "not_found", "asset_not_found"
            elif ver is None:
                state, code = "not_found", "version_unavailable"
            else:
                state, code = "ready", None
                man = json.loads(ver["manifest"])
                base.update(descriptor_sha256=hashlib.sha256(ver["descriptor"]).hexdigest(),
                            descriptor_json=ver["descriptor"].decode(),
                            deliveries=[{"delivery_id": ver["delivery_id"], "representation": "portable_glb_v1",
                                         "profile_id": man["profile_id"], "profile_version": man["profile_version"],
                                         "manifest_sha256": hashlib.sha256(ver["manifest"]).hexdigest(),
                                         "total_bytes": ver["total"], "budget": {}}])
            base.update(asset_key=key, state=state, error={"code": code, "message": code} if code else None)
            if self.state.scenario != "legacy_resolve":
                base["representations"] = self._rep_map(req, state, code)
            entries.append(base)
        self._send(200, json.dumps({"entries": entries}).encode())

    def _rep_map(self, req: dict, state: str, code: str | None) -> dict:
        scenario = self.state.scenario
        out: dict = {}
        for rep in req.get("representations") or ["portable_glb_v1"]:
            if state != "ready":
                out[rep] = {"state": state, "error": {"code": code, "message": code}}
            elif rep == "portable_glb_v1" and scenario.startswith("rep_unsupported"):
                err = None if scenario == "rep_unsupported_no_error" else {
                    "code": "unsupported_representation", "message": "no preparer for this representation"}
                out[rep] = {"state": "unsupported", "error": err}
            elif rep == "portable_glb_v1" or scenario.startswith("rep_unsupported"):
                out[rep] = {"state": "ready", "error": None}
            else:
                out[rep] = {"state": "unsupported", "error": {"code": "unsupported_representation", "message": rep}}
        if scenario == "rep_missing_key":
            out = {"mobile_glb_v1": {"state": "ready", "error": None}}
        elif scenario.startswith("rep_unsupported") and state == "ready":
            out["mobile_glb_v1"] = {"state": "ready", "error": None}
        return out

    def _content(self, artifact_id: str) -> None:
        data = self.state.artifacts.get(artifact_id)
        if data is None:
            return self._err(404, "asset_not_found")
        scenario = self.state.scenario
        if scenario == "corrupt_content":
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        total = len(data)
        rng = self.headers.get("Range")
        start = 0
        status, extra = 200, {}
        if rng and scenario != "ignore_range":
            start = int(rng.split("=")[1].rstrip("-"))
            if start >= total:
                return self._send(416, envelope("invalid_request", "range"),
                                  extra={"Content-Range": f"bytes */{total}"})
            status, extra = 206, {"Content-Range": f"bytes {start}-{total - 1}/{total}"}
        chunk = data[start:]
        self.send_response(status)
        self.send_header("Content-Type", "model/gltf-binary")
        self.send_header("Content-Length", str(len(chunk)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if scenario == "drop_midway" and not rng:
            self.wfile.write(chunk[: len(chunk) // 2])
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(2)
            return
        if scenario == "slow":
            return self._slow_write(chunk)
        self.wfile.write(chunk)
        return None

    def _slow_write(self, chunk: bytes) -> None:
        self.wfile.write(chunk[: len(chunk) // 4])
        self.wfile.flush()
        for _ in range(100):  # up to ~10 s; a closed connection raises and ends the handler
            time.sleep(0.1)
            self.wfile.write(b"")
            self.wfile.flush()
            if self.connection.fileno() < 0:
                return
        self.wfile.write(chunk[len(chunk) // 4:])


def _asset_key(ref: dict) -> str:
    parts = [ref["server_id"], ref["library_id"], ref["asset_id"], ref["version_id"]]
    out = b"".join(len(p.encode()).to_bytes(4, "little") + p.encode() for p in parts)
    return hashlib.sha256(out).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", type=int, default=0)
    ap.add_argument("--token", default=os.environ.get("ASSETSTUDIO_FAKE_TOKEN", "fake-test-token"))
    ap.add_argument("--contracts-dir", type=Path, default=DEFAULT_CONTRACTS)
    args = ap.parse_args()
    Handler.state = State(args.contracts_dir, args.token)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    print(f"PORT {httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

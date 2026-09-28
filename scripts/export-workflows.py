#!/usr/bin/env python3
"""Generate Line A workflows from one Python definition, using a running ComfyUI's /object_info.

Writes per app:
  config/workflows/<slug>.api.json               API format (scripts/run_job.py, automation)
  config/workflows/<slug>.app.json               UI format with App Mode config (source of truth)
  comfyui/user/default/workflows/<Name>.app.json same, where ComfyUI's workflow sidebar finds it
"""
from __future__ import annotations

import argparse
import json
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCALAR_WIDGETS = {"INT", "FLOAT", "STRING", "BOOLEAN"}


@dataclass
class Node:
    id: int
    type: str
    title: str
    pos: tuple[int, int]
    values: dict[str, Any] = field(default_factory=dict)
    links: dict[str, tuple[int, int]] = field(default_factory=dict)  # input -> (src node, src slot)


@dataclass
class App:
    slug: str
    name: str
    nodes: list[Node]
    app_inputs: list[tuple[int, str]]
    app_outputs: list[int]
    labels: dict[tuple[int, str], str] = field(default_factory=dict)  # friendly App Mode field names


NEW_ASSET = App(
    slug="line_a_new_asset",
    name="Line A - 1 New Asset",
    nodes=[
        Node(1, "LineACreateJob", "create_job", (0, 0), {"prompt": "wooden medieval barrel", "asset_type": "small_prop",
             "target_triangles": 0, "candidate_count": 4, "seed_family": 0}),
        Node(2, "LineAEnhancePrompt", "enhance", (420, 0), links={"job_id": (1, 0)}),
        Node(3, "LineAConfirmPrompt", "confirm_prompt", (760, 0), {"final_prompt": ""}, {"job_id": (2, 0)}),
        Node(4, "UNETLoader", "unet", (0, 420), {"unet_name": "qwen_image_2512_fp8_e4m3fn.safetensors"}),
        Node(5, "CLIPLoader", "text_encoder", (0, 560), {"clip_name": "qwen_2.5_vl_7b_fp8_scaled.safetensors",
             "type": "qwen_image"}),
        Node(6, "VAELoader", "vae", (0, 700), {"vae_name": "qwen_image_vae.safetensors"}),
        Node(7, "ModelSamplingAuraFlow", "shift", (380, 420), {"shift": 3.1}, {"model": (4, 0)}),
        Node(8, "CLIPTextEncode", "positive", (760, 300), links={"clip": (5, 0), "text": (3, 1)}),
        Node(9, "CLIPTextEncode", "negative", (760, 520), links={"clip": (5, 0), "text": (3, 2)}),
        Node(10, "LineAGenerateCandidates", "generate", (1160, 200), {},
             {"job_id": (3, 0), "model": (7, 0), "vae": (6, 0), "positive": (8, 0), "negative": (9, 0)}),
        Node(11, "LineARunQA", "qa", (1560, 200), links={"job_id": (10, 1)}),
        Node(12, "LineAReviewGallery", "gallery", (1900, 200), links={"job_id": (11, 0)}),
    ],
    app_inputs=[(1, "prompt"), (1, "asset_type"), (1, "target_triangles"), (3, "final_prompt"),
                (10, "speed_preset"), (1, "lora_name"), (1, "lora_strength"), (1, "candidate_count"), (1, "seed_family")],
    app_outputs=[12],
    labels={(1, "prompt"): "Prompt", (1, "asset_type"): "Asset type",
            (1, "target_triangles"): "Target triangles (0 = none, hint only)",
            (3, "final_prompt"): "Edited prompt (optional, overrides enhanced)",
            (10, "speed_preset"): "Speed preset", (1, "lora_name"): "Style LoRA",
            (1, "lora_strength"): "LoRA strength", (1, "candidate_count"): "Variants",
            (1, "seed_family"): "Seed (0 = random)"},
)

REVIEW = App(
    slug="line_a_review_approve",
    name="Line A - 2 Review & Approve",
    nodes=[
        Node(1, "LineAReviewGallery", "gallery", (0, 0), {"job_id": "latest"}),
        Node(2, "LineAApprove", "approve", (400, 0), links={"job_id": (1, 0)}),
        Node(3, "LineACutout", "cutout", (760, 0), links={"job_id": (2, 0)}),
        Node(4, "LineATrellis3D", "trellis", (1100, 0), {}, {"job_id": (3, 0)}),
    ],
    app_inputs=[(1, "job_id"), (2, "approve_candidate"), (4, "pipeline_type"), (4, "texture_size"), (4, "model_seed")],
    app_outputs=[1, 4],
    labels={(1, "job_id"): "Job ('latest' = newest)", (2, "approve_candidate"): "Approve variant -> generate 3D",
            (4, "pipeline_type"): "3D quality", (4, "texture_size"): "Texture size", (4, "model_seed"): "3D seed"},
)
APPS = [NEW_ASSET, REVIEW]


def fetch_object_info(url: str) -> dict:
    with urllib.request.urlopen(f"{url}/object_info", timeout=30) as r:
        return json.loads(r.read())


def ordered_inputs(info: dict) -> list[tuple[str, list]]:
    spec = info["input"]
    return list(spec.get("required", {}).items()) + list(spec.get("optional", {}).items())


def is_widget(spec: list) -> bool:
    kind, opts = spec[0], (spec[1] if len(spec) > 1 else {})
    if isinstance(kind, list) or kind == "COMBO":
        return True
    return kind in SCALAR_WIDGETS and not opts.get("forceInput")


def default_of(spec: list) -> Any:
    kind, opts = spec[0], (spec[1] if len(spec) > 1 else {})
    if "default" in opts:
        return opts["default"]
    if isinstance(kind, list):
        return kind[0]
    if kind == "COMBO":
        return opts.get("options", [None])[0]
    return {"INT": 0, "FLOAT": 0.0, "STRING": "", "BOOLEAN": False}[kind]


def widget_values(node: Node, info: dict) -> dict[str, Any]:
    vals = {}
    for name, spec in ordered_inputs(info):
        if is_widget(spec):
            vals[name] = node.values.get(name, default_of(spec))
    unknown = set(node.values) - set(vals)
    if unknown:
        raise SystemExit(f"{node.type}: unknown widget(s) {unknown}")
    return vals


def to_api(app: App, oi: dict) -> dict:
    out = {}
    for n in app.nodes:
        inputs: dict[str, Any] = {k: v for k, v in widget_values(n, oi[n.type]).items() if k not in n.links}
        inputs.update({k: [str(src), slot] for k, (src, slot) in n.links.items()})
        out[str(n.id)] = {"class_type": n.type, "_meta": {"title": n.title}, "inputs": inputs}
    return out


def ui_inputs(n: Node, info: dict, in_link: dict[tuple[int, str], int], labels: dict[str, str]) -> list[dict]:
    """Frontend layout: sockets first, then every widget (linked or not). Link slots index this list."""
    optional = set(info["input"].get("optional", {}))
    specs = ordered_inputs(info)
    entries = []
    for name, spec in [x for x in specs if not is_widget(x[1])] + [x for x in specs if is_widget(x[1])]:
        entry: dict[str, Any] = {"localized_name": name, "name": name,
                                 "type": spec[0] if isinstance(spec[0], str) else "COMBO",
                                 "link": in_link.get((n.id, name))}
        if is_widget(spec):
            entry["widget"] = {"name": name}
        if name in optional:
            entry["shape"] = 7
        if name in labels:
            entry["label"] = labels[name]
        entries.append(entry)
    return entries


def to_ui(app: App, oi: dict) -> dict:
    wf_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"line-a/{app.slug}"))
    by_id = {n.id: n for n in app.nodes}
    links: list[list] = []
    out_links: dict[tuple[int, int], list[int]] = {}
    in_link: dict[tuple[int, str], int] = {}
    for n in app.nodes:
        for name, (src, slot) in n.links.items():
            lid = len(links) + 1
            in_link[(n.id, name)] = lid
            out_links.setdefault((src, slot), []).append(lid)
            links.append([lid, src, slot, n.id, None, oi[by_id[src].type]["output"][slot]])
    nodes = []
    for order, n in enumerate(app.nodes):
        info = oi[n.type]
        inputs = ui_inputs(n, info, in_link, {w: lbl for (nid, w), lbl in app.labels.items() if nid == n.id})
        for link in links:
            if link[3] == n.id:
                link[4] = next(i for i, e in enumerate(inputs) if e["link"] == link[0])
        outputs = [{"localized_name": oname, "name": oname, "type": otype, "links": out_links.get((n.id, i)) or None}
                   for i, (oname, otype) in enumerate(zip(info["output_name"], info["output"]))]
        nodes.append({"id": n.id, "type": n.type, "title": n.title, "pos": list(n.pos), "size": [340, 180],
                      "flags": {}, "order": order, "mode": 0, "inputs": inputs, "outputs": outputs,
                      "properties": {"Node name for S&R": n.type},
                      "widgets_values": list(widget_values(n, info).values())})
    return {
        "id": wf_id,
        "revision": 0,
        "last_node_id": max(n.id for n in app.nodes),
        "last_link_id": len(links),
        "nodes": nodes,
        "links": links,
        "groups": [],
        "config": {},
        "extra": {"linearMode": True, "linearData": {
            "inputs": [[f"{wf_id}:{nid}:{w}", w] for nid, w in app.app_inputs],
            "outputs": [str(nid) for nid in app.app_outputs]}},
        "version": 0.4,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8188")
    args = ap.parse_args()
    oi = fetch_object_info(args.url)
    wf_dir, user_dir = ROOT / "config" / "workflows", ROOT / "comfyui" / "user" / "default" / "workflows"
    user_dir.mkdir(parents=True, exist_ok=True)
    for app in APPS:
        for path, data in [(wf_dir / f"{app.slug}.api.json", to_api(app, oi)),
                           (wf_dir / f"{app.slug}.app.json", to_ui(app, oi)),
                           (user_dir / f"{app.name}.app.json", to_ui(app, oi))]:
            path.write_text(json.dumps(data, indent=2) + "\n")
            print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

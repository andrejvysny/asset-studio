import { type ReactNode, useEffect, useRef, useState } from "react";

import { artifactUrl, get } from "../lib/api";
import { ModelViewer } from "./ui";

/** role -> artifact id, as stored on build runs and published versions. */
export type Roles = Record<string, string>;

interface AtlasMeta { size: [number, number]; fps?: number; frames: { index: number; rect: [number, number, number, number] }[] }
interface SpriteMeta { pivot?: { mode: string; xy: [number, number] }; canvas?: [number, number];
  seam?: { ratio: number }; derived_maps?: string }

function useMeta<T>(project: string, id: string | undefined): T | null {
  const [meta, setMeta] = useState<T | null>(null);
  useEffect(() => {
    if (!id) return;
    let alive = true;
    void get<T>(artifactUrl(project, id)).then((m) => { if (alive) setMeta(m); }).catch(() => undefined);
    return () => { alive = false; };
  }, [project, id]);
  return id ? meta : null;
}

function Framed({ src, alt, height, children }: { src: string; alt: string; height: number; children?: ReactNode }) {
  return (
    <div className="checker" style={{ height, display: "flex", position: "relative" }}>
      <div style={{ margin: "auto", position: "relative", maxWidth: "100%", maxHeight: height }}>
        <img src={src} alt={alt} style={{ maxWidth: "100%", maxHeight: height, display: "block", objectFit: "contain" }} />
        {children}
      </div>
    </div>
  );
}

function SpriteView({ project, image, metaId, height }: { project: string; image: string; metaId: string; height: number }) {
  const meta = useMeta<SpriteMeta>(project, metaId);
  const piv = meta?.pivot;
  const c = meta?.canvas;
  return (
    <Framed src={artifactUrl(project, image)} alt="sprite" height={height}>
      {piv && c && <span title={`pivot ${piv.mode}`} aria-label="pivot" style={{ position: "absolute",
        left: `${(piv.xy[0] / c[0]) * 100}%`, top: `${(piv.xy[1] / c[1]) * 100}%`, width: 9, height: 9,
        transform: "translate(-50%,-50%)", border: "2px solid var(--warn)", borderRadius: "50%" }} />}
    </Framed>
  );
}

function IconView({ project, roles, height }: { project: string; roles: Roles; height: number }) {
  const icons = Object.entries(roles).filter(([r]) => r.startsWith("icon_"))
    .map(([r, art]) => ({ size: Number(r.slice(5)), art })).sort((a, b) => b.size - a.size);
  return (
    <div className="checker" style={{ minHeight: height, display: "flex", flexWrap: "wrap", gap: 16, padding: 16,
      alignItems: "flex-end", justifyContent: "center" }}>
      {icons.map(({ size: s, art }) => (
        <figure key={s} style={{ margin: 0, display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }}>
          <img src={artifactUrl(project, art)} alt={`${s}px`} width={Math.min(s, 160)}
            height={Math.min(s, 160)} style={{ imageRendering: s <= 64 ? "pixelated" : "auto" }} />
          <figcaption className="sub mono">{s}px</figcaption>
        </figure>
      ))}
    </div>
  );
}

function MaterialView({ project, roles, base, height }: { project: string; roles: Roles; base: string; height: number }) {
  const meta = useMeta<SpriteMeta>(project, roles.meta);
  const maps = ["base_color", "normal", "roughness", "metallic", "ao", "height"].filter((m) => roles[m]);
  const [shown, setShown] = useState("base_color");
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <div aria-label="tiled preview" style={{ height, backgroundImage: `url(${artifactUrl(project, roles[shown] ?? base)})`,
        backgroundSize: "33.34% auto", backgroundRepeat: "repeat" }} />
      <div className="row sub" style={{ padding: "6px 10px", gap: 6, flexWrap: "wrap" }}>
        <span>3×3 tiling ·</span>
        {maps.map((m) => <button key={m} className={`chip${m === shown ? " on" : ""}`} onClick={() => setShown(m)}>{m}</button>)}
        {meta?.seam && <span className="mono">seam ratio {meta.seam.ratio.toFixed(2)}</span>}
        {meta?.derived_maps && <span title={meta.derived_maps}>· no derived maps</span>}
      </div>
    </div>
  );
}

export function AtlasPlayer({ project, atlas, metaId, height }: { project: string; atlas: string; metaId: string;
  height: number }) {
  const meta = useMeta<AtlasMeta>(project, metaId);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [playing, setPlaying] = useState(true);
  const [frame, setFrame] = useState(0);
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  useEffect(() => {
    const im = new Image();
    im.onload = () => setImg(im);
    im.src = artifactUrl(project, atlas);
  }, [project, atlas]);
  const n = meta?.frames.length ?? 0;
  useEffect(() => {
    if (!playing || !n) return;
    const t = window.setInterval(() => setFrame((f) => (f + 1) % n), 1000 / (meta?.fps ?? 12));
    return () => window.clearInterval(t);
  }, [playing, n, meta?.fps]);
  useEffect(() => {
    const ctx = canvas.current?.getContext("2d");
    const r = meta?.frames[frame % Math.max(n, 1)]?.rect;
    if (!ctx || !img || !r) return;
    ctx.canvas.width = r[2];
    ctx.canvas.height = r[3];
    ctx.clearRect(0, 0, r[2], r[3]);
    ctx.drawImage(img, r[0], r[1], r[2], r[3], 0, 0, r[2], r[3]);
  }, [frame, img, meta, n]);
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <div className="checker" style={{ height, display: "flex" }}>
        <canvas ref={canvas} aria-label="frame player" style={{ margin: "auto", maxWidth: "90%", maxHeight: height - 16,
          height: height - 16, imageRendering: "pixelated", objectFit: "contain" }} />
      </div>
      <div className="row sub" style={{ padding: "6px 10px", gap: 8 }}>
        <button className="chip" onClick={() => setPlaying(!playing)}>{playing ? "Pause" : "Play"}</button>
        <input type="range" min={0} max={Math.max(n - 1, 0)} value={frame % Math.max(n, 1)} aria-label="frame"
          onChange={(e) => { setPlaying(false); setFrame(Number(e.target.value)); }} className="grow" />
        <span className="mono">{n ? `${(frame % n) + 1}/${n}` : "—"} · {meta?.fps ?? "?"} fps</span>
      </div>
    </div>
  );
}

/** Per-kind presentation of a build result or published version, chosen by the roles it carries. */
export function OutputView({ project, roles, height = 320, alt }: { project: string; roles: Roles; height?: number; alt: string }) {
  const { model, atlas, meta, image, base_color: base } = roles;
  if (model) return <ModelViewer src={artifactUrl(project, model)} height={height} />;
  if (atlas && meta) return <AtlasPlayer project={project} atlas={atlas} metaId={meta} height={height} />;
  if (Object.keys(roles).some((r) => r.startsWith("icon_"))) return <IconView project={project} roles={roles} height={height} />;
  if (base) return <MaterialView project={project} roles={roles} base={base} height={height} />;
  if (image && meta) return <SpriteView project={project} image={image} metaId={meta} height={height} />;
  const img = roles.image ?? roles.preview;
  return img ? <Framed src={artifactUrl(project, img)} alt={alt} height={height} />
    : <div className="stripes" style={{ height }} />;
}

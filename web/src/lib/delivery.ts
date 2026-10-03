import type { AssetVersion } from "./api";

export type DeliveryRepresentation = "portable_glb_v1" | "godot_static_source_v1";

export interface DeliveryInfo {
  representations: DeliveryRepresentation[];
  /** True when the version carries a Godot static source package (editable scenes, not only a baked GLB). */
  editableSource: boolean;
}

/** Mirrors the server's delivery rule (services/deliveries.py `_expected`) from the version's artifact roles; read-only. */
export function deliveryInfo(v: Pick<AssetVersion, "kind" | "artifacts">): DeliveryInfo | null {
  if (v.kind !== "model3d") return null;
  const has = (role: string) => role in v.artifacts;
  if (!has("descriptor")) return { representations: ["portable_glb_v1"], editableSource: false };
  const representations: DeliveryRepresentation[] = [];
  if (has("model")) representations.push("portable_glb_v1");
  if (has("godot_source")) representations.push("godot_static_source_v1");
  return { representations, editableSource: has("godot_source") };
}

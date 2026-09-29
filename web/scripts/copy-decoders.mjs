// Bundle glTF decoders locally so the 3D viewer never fetches from a CDN (offline requirement).
import { cpSync, mkdirSync } from "node:fs";

const libs = "node_modules/three/examples/jsm/libs";
for (const [from, to] of [[`${libs}/draco/gltf`, "public/decoders/draco"], [`${libs}/basis`, "public/decoders/basis"]]) {
  mkdirSync(to, { recursive: true });
  cpSync(from, to, { recursive: true, filter: (p) => !p.endsWith("README.md") && !p.includes("encoder") });
}

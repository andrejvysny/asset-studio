// ComfyUI (v1 job API) via the library-service proxy: load our workflow JSON, patch inputs by node title, queue it.

import { api, ApiError } from "./api";

type ApiWorkflow = Record<string, { class_type: string; _meta: { title: string }; inputs: Record<string, unknown> }>;
export type Patches = Record<string, Record<string, unknown>>;
export type WorkflowName = "line_a_enhance" | "line_a_generate" | "line_a_3d" | "line_a_reexport";

const clientId = crypto.randomUUID();

export async function queueWorkflow(name: WorkflowName, patches: Patches): Promise<{ promptId: string; wf: ApiWorkflow }> {
  const wf = await api<ApiWorkflow>(`/api/workflows/${name}`);
  const byTitle = new Map(Object.values(wf).map((n) => [n._meta.title, n]));
  for (const [title, values] of Object.entries(patches)) {
    const node = byTitle.get(title);
    if (!node) throw new Error(`workflow ${name} has no node titled ${title}`);
    Object.assign(node.inputs, values);
  }
  const res = await api<{ prompt_id: string; node_errors?: Record<string, unknown> }>("/comfy/prompt", {
    method: "POST", body: JSON.stringify({ prompt: wf, client_id: clientId }),
  });
  return { promptId: res.prompt_id, wf };
}

interface HistoryEntry {
  status: { status_str: string; completed: boolean; messages: [string, Record<string, unknown>][] };
  outputs: Record<string, Record<string, unknown[]>>;
}

/** Resolves with node outputs when the prompt finishes; rejects with ComfyUI's execution error message. */
export async function waitForPrompt(promptId: string, onTick?: () => void, timeoutMs = 3_600_000): Promise<HistoryEntry> {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const hist = await api<Record<string, HistoryEntry>>(`/comfy/history/${promptId}`);
    const entry = hist[promptId];
    if (entry?.status.status_str === "error") {
      const err = entry.status.messages.find((m) => m[0] === "execution_error")?.[1];
      throw new ApiError(500, String(err?.exception_message ?? "workflow failed").trim());
    }
    if (entry?.status.completed) return entry;
    onTick?.();
    await new Promise((r) => setTimeout(r, 1500));
  }
  throw new Error("timed out waiting for ComfyUI");
}

export function outputByTitle(entry: HistoryEntry, wf: ApiWorkflow, title: string, key = "text"): unknown[] {
  const id = Object.entries(wf).find(([, n]) => n._meta.title === title)?.[0];
  return (id && entry.outputs[id]?.[key]) || [];
}

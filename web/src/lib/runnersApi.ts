// Operator runner API (docs/modular/compute-runner.md R13). Types come from the generated OpenAPI schema
// (`make web-types`); response shapes are never re-declared by hand.

import { send } from "./api";
import type { components } from "./generated/openapi";

type S = components["schemas"];
export type RunnerSummary = S["RunnerSummary"];
export type RunnerDetail = S["RunnerDetail"];
export type RunnerList = S["RunnerList"];
export type GroupOut = S["GroupOut"];
export type GroupList = S["GroupList"];
export type GroupCreate = S["GroupCreate"];
export type TokenOut = S["TokenOut"];
export type AttemptSummary = S["AttemptSummary"];
export type DeviceSummary = S["DeviceSummary"];
export type SlotSummary = S["SlotSummary"];

const enc = encodeURIComponent;

export const runnersPath = "/api/v1/runners";
export const runnerPath = (id: string) => `${runnersPath}/${enc(id)}`;
export const groupsPath = "/api/v1/runner-groups";

export const createGroup = (body: GroupCreate) => send<GroupOut>("POST", groupsPath, body);
export const createRegistrationToken = (groupId: string, ttlS: number) =>
  send<TokenOut>("POST", `${groupsPath}/${enc(groupId)}/registration-tokens`, { ttl_s: ttlS });
export const revokeRunner = (id: string) => send<RunnerSummary>("POST", `${runnersPath}/${enc(id)}:revoke`);
export const setPushUrl = (id: string, url: string | null) =>
  send<RunnerSummary>("PUT", `${runnerPath(id)}/push-url`, { url });
export const declareLost = (attemptId: string) =>
  send<AttemptSummary>("POST", `/api/v1/attempts/${enc(attemptId)}:declare-lost`);

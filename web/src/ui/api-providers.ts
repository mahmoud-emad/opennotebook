// The AI providers and the studio's first-run setup: every call the setup
// tour and Settings › AI providers make. Like `api.ts`, the one place that
// knows these REST shapes.

import { call, enc } from "./api";
import type * as Rest from "@/client/types.gen";

export type Setup = Rest.SetupOut;
export type Preset = Rest.PresetOut;
export type Provider = Rest.ProviderOut;
export type Role = Rest.RoleOut;
export type KeyCheck = Rest.CheckOut;
export type Providers = Rest.ProvidersOut;
export type ProviderIn = { kind: string; key?: string; base_url?: string; label?: string };

/** Whether the studio can be used yet, and whether this person can set it up. */
export function setupLoad(): Promise<Setup> {
  return call<Setup>("GET", "/setup");
}

/** The presets, the connected providers, and the model each role uses. */
export function providersLoad(): Promise<Providers> {
  return call<Providers>("GET", "/ai/providers");
}

/** Test a key without keeping it. */
export function providerCheck(p: ProviderIn): Promise<KeyCheck> {
  return call<KeyCheck>("POST", "/ai/providers/check", p);
}

/** Test a key and, when it works, connect the provider. A key that does not
 * work is refused with the server's sentence saying why. */
export function providerAdd(p: ProviderIn): Promise<Provider> {
  return call<Provider>("POST", "/ai/providers", p);
}

export function providerRemove(id: string): Promise<void> {
  return call<void>("DELETE", `/ai/providers/${enc(id)}`);
}

/** Test a connected provider again. */
export function providerRecheck(id: string): Promise<KeyCheck> {
  return call<KeyCheck>("POST", `/ai/providers/${enc(id)}/check`);
}

/** The models a connected provider offers, as the settings name them. */
export async function providerModels(id: string): Promise<string[]> {
  return (await call<Rest.ModelsOut>("GET", `/ai/providers/${enc(id)}/models`)).models;
}

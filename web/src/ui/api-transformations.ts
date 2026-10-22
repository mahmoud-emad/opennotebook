// The transformations: the built-in ones and a person's own. Like `api.ts`,
// the one place that knows these REST shapes.

import { call, enc } from "./api";
import type * as Rest from "@/client/types.gen";

export type Transformation = Rest.TransformationOut;

/** The built-in transformations, then the person's own. */
export function transformationsLoad(): Promise<Transformation[]> {
  return call<Transformation[]>("GET", "/transformations");
}

export function transformationAdd(name: string, prompt: string): Promise<Transformation> {
  return call<Transformation>("POST", "/transformations", { name, prompt });
}

export function transformationChange(id: string, name: string, prompt: string): Promise<Transformation> {
  return call<Transformation>("PUT", `/transformations/${enc(id)}`, { name, prompt });
}

export function transformationRemove(id: string): Promise<void> {
  return call<void>("DELETE", `/transformations/${enc(id)}`);
}

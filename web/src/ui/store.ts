// A value any screen can read and set: the banner, the snackbar and the shared
// dialog are set from anywhere, even outside a component, and the shell draws
// them. A component that reads one redraws when it changes, and only then.

import { useSyncExternalStore } from "react";

export type Store<T> = {
  get: () => T;
  set: (v: T | ((prev: T) => T)) => void;
  subscribe: (fn: () => void) => () => void;
};

export function store<T>(initial: T): Store<T> {
  let value = initial;
  const subs = new Set<() => void>();
  return {
    get: () => value,
    set: (v) => {
      const next = typeof v === "function" ? (v as (p: T) => T)(value) : v;
      if (Object.is(next, value)) return;
      value = next;
      subs.forEach((f) => f());
    },
    subscribe: (fn) => {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
}

export function useStore<T>(s: Store<T>): T {
  return useSyncExternalStore(s.subscribe, s.get, s.get);
}

/** One fact read from a store, re-rendering only when that fact changes: a
 * count, a flag, a title. `pick` must answer a value that compares equal
 * when nothing it reads has changed (a number, a string, a boolean), not a
 * new array or object each time. */
export function useStoreSel<T, U>(s: Store<T>, pick: (v: T) => U): U {
  const read = () => pick(s.get());
  return useSyncExternalStore(s.subscribe, read, read);
}

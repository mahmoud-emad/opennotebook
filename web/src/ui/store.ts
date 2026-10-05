// A value any screen can read and set, the way the old app's context signals
// worked: the banner, the snackbar and the shared dialog are set from
// anywhere, even outside a component, and the shell draws them.

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

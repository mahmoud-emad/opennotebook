// The studio the suite runs against: the dev server by default (make dev),
// or any other, such as the compose stack, through E2E_URL. It ends in /ui/,
// so a test's page.goto("my-collections") lands on /ui/my-collections.
export const BASE = process.env.E2E_URL ?? "http://localhost:5173/ui/";

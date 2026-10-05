import { defineConfig } from "@hey-api/openapi-ts";

// The client is generated from the server's OpenAPI document (`pnpm api`) and
// committed, so a change on the server shows up as a type error here.
export default defineConfig({
  input: "openapi.json",
  output: "src/client",
  plugins: ["@hey-api/client-fetch", "@hey-api/typescript", "@hey-api/sdk"],
});

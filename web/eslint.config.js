import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "src/client"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  reactHooks.configs.flat.recommended,
  {
    files: ["scripts/**/*.mjs"],
    languageOptions: { globals: { URL: "readonly", console: "readonly", process: "readonly" } },
  },
);

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/theme.css";
import "./styles/style.css";
import { App } from "./ui/App";
import { SetupGate } from "./ui/providers";

createRoot(document.getElementById("main")!).render(
  <StrictMode>
    <SetupGate>
      <App />
    </SetupGate>
  </StrictMode>,
);

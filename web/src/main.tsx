import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles/theme.css";
import "./styles/style.css";
import { App } from "./ui/App";

createRoot(document.getElementById("main")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

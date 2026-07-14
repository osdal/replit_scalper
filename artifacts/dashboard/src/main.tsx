import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import Dashboard from "./Dashboard";
import { RoleProvider } from "./lib/useRole";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RoleProvider>
      <Dashboard />
    </RoleProvider>
  </StrictMode>
);

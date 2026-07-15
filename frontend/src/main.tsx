import { createRoot } from "react-dom/client";
import App from "./app/App";
import { AppAuthProvider } from "./app/auth";
import "./styles/index.css";

createRoot(document.getElementById("root")!).render(
  <AppAuthProvider>
    <App />
  </AppAuthProvider>,
);

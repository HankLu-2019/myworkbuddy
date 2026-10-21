import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider, createBrowserRouter } from "react-router-dom";
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "./api";
import { AuthProvider } from "./auth";
import App from "./App";
import { TitleProvider } from "./app/pageTitle";
import { applyTheme, readTheme } from "./app/theme";
import { allowUnload } from "./app/UnsavedGuard";
import { ToastProvider, TooltipProvider } from "./components/ui";
// Fonts are self-hosted: the CSP allows no third-party origins, and loading from Google would leak page views.
// latin + latin-ext covers English, Swedish and Polish; Chinese falls back to system fonts.
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-ext-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-ext-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "@fontsource/ibm-plex-sans/latin-ext-600.css";
import "@fontsource/ibm-plex-sans-condensed/latin-600.css";
import "@fontsource/ibm-plex-sans-condensed/latin-ext-600.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-ext-400.css";
import "./styles.css";

function onError(error: unknown) {
  // Session expired or revoked: reload into the login screen, then back here (07 §5.11). Unsent text is
  // already in localStorage (app/drafts.ts), so the page restores it after sign-in.
  if (error instanceof ApiError && error.status === 401 && location.pathname !== "/login") {
    const next = encodeURIComponent(location.pathname + location.search);
    allowUnload();
    location.assign(`/login?next=${next}&reason=expired`);
  }
}

applyTheme(readTheme());

const queryClient = new QueryClient({
  queryCache: new QueryCache({ onError }),
  mutationCache: new MutationCache({ onError }),
  defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
});

function Root() {
  return (
    <AuthProvider>
      <TitleProvider>
        <TooltipProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </TooltipProvider>
      </TitleProvider>
    </AuthProvider>
  );
}

// A data router, so screens can block navigation away from unsaved changes (07 §6.6).
const router = createBrowserRouter([{ path: "*", element: <Root /> }]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);

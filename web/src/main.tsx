import "@fontsource/geist-sans/400.css";
import "@fontsource/geist-sans/500.css";
import "@fontsource/geist-sans/600.css";
import "@fontsource/geist-mono/400.css";
import "@fontsource/geist-mono/500.css";
import "./theme.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider, useRouteError } from "react-router-dom";

import { AssetDetail } from "./screens/AssetDetail";
import { Attempts } from "./screens/Attempts";
import { Coverage } from "./screens/Coverage";
import { Jobs } from "./screens/Jobs";
import { Library } from "./screens/Library";
import { NewJob } from "./screens/NewJob";
import { Review } from "./screens/Review";
import { Runtime } from "./screens/Runtime";
import { Shell } from "./screens/Shell";

function RouteError() {
  const err = useRouteError() as Error | undefined;
  return <div className="page"><div className="h1">Something broke on this screen</div>
    <pre className="error">{err?.message ?? String(err)}</pre><a href="/jobs">Back to jobs</a></div>;
}

const router = createBrowserRouter([
  {
    path: "/",
    element: <Shell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/library" replace /> },
      { path: "library", element: <Library /> },
      { path: "library/:biome", element: <Library /> },
      { path: "slots/:slotId", element: <AssetDetail /> },
      { path: "coverage", element: <Coverage /> },
      { path: "jobs", element: <Jobs /> },
      { path: "new", element: <NewJob /> },
      { path: "new/:jobId", element: <NewJob /> },
      { path: "review", element: <Review /> },
      { path: "review/:jobId", element: <Review /> },
      { path: "attempts/:jobId", element: <Attempts /> },
      { path: "runtime", element: <Runtime /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);

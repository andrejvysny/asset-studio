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
import { Assets } from "./screens/Assets";
import { Batches } from "./screens/Batches";
import { BatchWorkspace } from "./screens/batch/BatchWorkspace";
import { Export } from "./screens/Export";
import { Home } from "./screens/Home";
import { NewBatch } from "./screens/NewBatch";
import { Pipelines } from "./screens/Pipelines";
import { QaRules } from "./screens/QaRules";
import { Runtime } from "./screens/Runtime";
import { Schema } from "./screens/Schema";
import { Shell } from "./screens/Shell";
import { ShotList } from "./screens/ShotList";
import { Storage } from "./screens/Storage";
import { Style } from "./screens/Style";

function RouteError() {
  const err = useRouteError() as Error | undefined;
  return <div className="content"><h1 className="h1">Something broke on this screen</h1>
    <pre className="error">{err?.message ?? String(err)}</pre><a href="/">Back to start</a></div>;
}

const router = createBrowserRouter([
  { path: "/", element: <Home />, errorElement: <RouteError /> },
  {
    path: "/p/:project",
    element: <Shell />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="assets" replace /> },
      { path: "assets", element: <Assets /> },
      { path: "assets/:assetId", element: <AssetDetail /> },
      { path: "shots", element: <ShotList /> },
      { path: "batches", element: <Batches /> },
      { path: "batches/new", element: <NewBatch /> },
      { path: "batches/:batchId", element: <BatchWorkspace /> },
      { path: "batches/:batchId/:tab", element: <BatchWorkspace /> },
      { path: "schema", element: <Schema /> },
      { path: "pipelines", element: <Pipelines /> },
      { path: "qa", element: <QaRules /> },
      { path: "style", element: <Style /> },
      { path: "storage", element: <Storage /> },
      { path: "export", element: <Export /> },
      { path: "runtime", element: <Runtime /> },
    ],
  },
  { path: "*", element: <Navigate to="/" replace /> },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);

import "@fontsource/geist-sans/400.css";
import "@fontsource/geist-sans/500.css";
import "@fontsource/geist-sans/600.css";
import "@fontsource/geist-mono/400.css";
import "@fontsource/geist-mono/500.css";
import "./theme.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider, useParams, useRouteError } from "react-router-dom";

import { AssetDetail } from "./screens/AssetDetail";
import { Assets } from "./screens/Assets";
import { CreateVariants } from "./screens/CreateVariants";
import { BatchDetail } from "./screens/batches/BatchDetail";
import { Batches } from "./screens/batches/Batches";
import { RunView } from "./screens/batches/RunView";
import { JobWorkspace } from "./screens/job/JobWorkspace";
import { Jobs } from "./screens/Jobs";
import { Export } from "./screens/Export";
import { Home } from "./screens/Home";
import { NewJob } from "./screens/NewJob";
import { Pipelines } from "./screens/Pipelines";
import { QaRules } from "./screens/QaRules";
import { Runtime } from "./screens/Runtime";
import { Schema } from "./screens/Schema";
import { Shell } from "./screens/Shell";
import { ShotList } from "./screens/ShotList";
import { Media } from "./screens/Media";
import { Storage } from "./screens/Storage";
import { Style } from "./screens/Style";

function RouteError() {
  const err = useRouteError() as Error | undefined;
  return <div className="content"><h1 className="h1">Something broke on this screen</h1>
    <pre className="error">{err?.message ?? String(err)}</pre><a href="/">Back to start</a></div>;
}

/** Old deep links used /batches/<id> for what is now a Job. Only a KNOWN Job id prefix (bat_/job_) redirects;
 * grouping Batches have their own bch_ ids, so nothing is guessed from an ambiguous path. */
function BatchOrLegacyJob() {
  const { project = "", batchId = "", tab } = useParams();
  if (/^(bat|job)_/.test(batchId)) return <Navigate to={`/p/${project}/jobs/${batchId}${tab ? `/${tab}` : ""}`} replace />;
  return <BatchDetail />;
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
      { path: "assets/:assetId/variants", element: <CreateVariants /> },
      { path: "shots", element: <ShotList /> },
      { path: "media", element: <Media /> },
      { path: "jobs", element: <Jobs /> },
      { path: "jobs/new", element: <NewJob /> },
      { path: "jobs/:jobId", element: <JobWorkspace /> },
      { path: "jobs/:jobId/:tab", element: <JobWorkspace /> },
      { path: "batches", element: <Batches /> },
      { path: "batches/new", element: <Navigate to="../jobs/new" replace /> },
      { path: "batches/:batchId", element: <BatchOrLegacyJob /> },
      { path: "batches/:batchId/:tab", element: <BatchOrLegacyJob /> },
      { path: "runs/:runId", element: <RunView /> },
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

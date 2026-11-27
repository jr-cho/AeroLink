import { createReadStream, existsSync, statSync } from "node:fs";
import { dirname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const BASEMAP_DIR = resolve(here, "../basemap");

/** Serve the repo's `../basemap` directory at `/basemap` (dev and preview), with HTTP Range
 *  support because PMTiles reads tiles with byte-range requests. Missing files fall through to a
 *  real 404 so the app can detect "no basemap yet" and draw overlays on a plain background. */
function basemapPlugin(): Plugin {
  const handler = (req: any, res: any, next: () => void) => {
    const url: string = (req.url ?? "").split("?")[0];
    if (!url.startsWith("/basemap/")) return next();
    const file = normalize(join(BASEMAP_DIR, decodeURIComponent(url.slice("/basemap/".length))));
    if (!file.startsWith(BASEMAP_DIR) || !existsSync(file) || !statSync(file).isFile()) {
      res.statusCode = 404;
      res.end("not found");
      return;
    }
    const size = statSync(file).size;
    const range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range ?? "");
    res.setHeader("Accept-Ranges", "bytes");
    if (range) {
      const start = range[1] ? Number(range[1]) : 0;
      const end = range[2] ? Math.min(Number(range[2]), size - 1) : size - 1;
      res.statusCode = 206;
      res.setHeader("Content-Range", `bytes ${start}-${end}/${size}`);
      res.setHeader("Content-Length", end - start + 1);
      createReadStream(file, { start, end }).pipe(res);
    } else {
      res.setHeader("Content-Length", size);
      createReadStream(file).pipe(res);
    }
  };
  return {
    name: "aerolink-basemap",
    configureServer: (s) => void s.middlewares.use(handler),
    configurePreviewServer: (s) => void s.middlewares.use(handler),
  };
}

const backend = "http://localhost:8000";
const proxy = {
  "/api": { target: backend, changeOrigin: true },
  "/healthz": { target: backend, changeOrigin: true },
  "/ws": { target: backend.replace("http", "ws"), ws: true },
};

export default defineConfig({
  plugins: [basemapPlugin()],
  server: { proxy },
  preview: { proxy },
  build: { chunkSizeWarningLimit: 1500 },
});

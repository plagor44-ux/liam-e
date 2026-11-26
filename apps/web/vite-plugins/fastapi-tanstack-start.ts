import type { Plugin } from "vite";

/** Every generated client path starts with it, and the FastAPI app serves everything under it. */
const API_PATH = "/api";

/**
 * Sends the dev server's `/api` requests, WebSocket upgrades included, to the FastAPI app, so the
 * web app calls the API same-origin under `vite dev` and `VITE_API_URL` can stay empty. When the
 * API is down, Vite's proxy answers 502.
 *
 * The target is IPv4 loopback, where the API's dev server listens (`apps/api/src/api/dev.py`), not
 * `localhost`: that name can resolve to `::1` first, where nothing listens.
 *
 * `server.proxy` alone never sees those requests. Nitro's dev handler is mounted in front of Vite's
 * proxy and renders the app, and so its "Page not found" page, for every path except one that
 * another plugin has mounted a middleware on. The pass-through below is that mount: Nitro steps
 * aside for `/api`, and Vite's proxy, further down the stack, answers.
 */
const fastapiProxy: Plugin = {
  name: "fastapi-tanstack-start",
  config: () => ({
    server: {
      proxy: {
        [API_PATH]: {
          target: "http://127.0.0.1:3000",
          changeOrigin: true,
          ws: true,
        },
      },
    },
  }),
  configureServer: (server) => {
    server.middlewares.use(API_PATH, (_req, _res, next) => {
      next();
    });
  },
};

export default fastapiProxy;

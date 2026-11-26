/**
 * The `/api` dev proxy, checked on the real dev server: this app's `vite.config.ts` with TanStack
 * Start and Nitro, on a free loopback port, its proxy pointed at a stub that stands in for FastAPI.
 * Only the API is a stand-in, because the bug this guards against lies between the real parts: Nitro's
 * dev handler answered `/api` with the app's "Page not found" page before Vite's proxy saw it.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { request } from "node:http";
import type { AddressInfo } from "node:net";
import { fileURLToPath } from "node:url";

import { createServer, resolveConfig } from "vite";
import type { ViteDevServer } from "vite";

const CONFIG_FILE = fileURLToPath(new URL("../vite.config.ts", import.meta.url));
const WEB_ROOT = fileURLToPath(new URL("../", import.meta.url));

/** Echoes what reached it, and greets a WebSocket with the path it was opened on. */
const api = Bun.serve<string>({
  hostname: "127.0.0.1",
  port: 0,
  fetch: async (req, server) => {
    const { pathname, search } = new URL(req.url);
    if (server.upgrade(req, { data: `${pathname}${search}` })) {
      return undefined;
    }
    return Response.json({
      body: await req.text(),
      method: req.method,
      path: `${pathname}${search}`,
    });
  },
  websocket: {
    open: (socket) => {
      socket.send(`stub api ${String(socket.data)}`);
    },
    message: () => {
      /* the stub only greets */
    },
  },
});

let web: ViteDevServer;
let webPort = 0;

beforeAll(async () => {
  web = await createServer({
    configFile: CONFIG_FILE,
    root: WEB_ROOT,
    logLevel: "silent",
    server: { host: "127.0.0.1", port: 0 },
    plugins: [
      {
        // Merged over the app plugin's config: same proxy entry, the stub as its target.
        name: "test-api-target",
        enforce: "post",
        config: () => ({
          server: { proxy: { "/api": { target: api.url.origin } } },
        }),
      },
    ],
  });
  await web.listen();
  webPort = (web.httpServer?.address() as AddressInfo).port;
});

afterAll(async () => {
  await web.close();
  await api.stop(true);
});

type Reply = { status: number; type: string; body: string };

/** `node:http` rather than `fetch`: it sends any `Host` header, and no test file can replace it. */
const send = (
  path: string,
  { method = "GET", host, body }: { method?: string; host?: string; body?: string } = {}
) =>
  new Promise<Reply>((resolve, reject) => {
    const req = request(
      {
        host: "127.0.0.1",
        port: webPort,
        path,
        method,
        headers: {
          ...(host === undefined ? {} : { host }),
          ...(body === undefined ? {} : { "content-type": "application/json" }),
        },
      },
      (res) => {
        let text = "";
        res.setEncoding("utf8");
        res.on("data", (chunk: string) => {
          text += chunk;
        });
        res.on("end", () => {
          resolve({
            status: res.statusCode ?? 0,
            type: res.headers["content-type"] ?? "",
            body: text,
          });
        });
      }
    );
    req.on("error", reject);
    req.end(body);
  });

describe("vite.config.ts", () => {
  test("proxies /api to the API on IPv4 loopback, WebSockets included", async () => {
    const config = await resolveConfig({ configFile: CONFIG_FILE, root: WEB_ROOT, logLevel: "silent" }, "serve");

    expect(config.server.proxy?.["/api"]).toMatchObject({
      target: "http://127.0.0.1:3000",
      ws: true,
    });
  });

  test("accepts Codespaces forwarded hosts on the dev server only", async () => {
    const config = await resolveConfig({ configFile: CONFIG_FILE, root: WEB_ROOT, logLevel: "silent" }, "serve");

    expect(config.server.allowedHosts).toEqual([".app.github.dev"]);
    expect(config.preview.allowedHosts).toEqual([]);
  });
});

describe("the dev server with the API up", () => {
  test("hands /api requests to the API", async () => {
    const reply = await send("/api/health?probe=1");

    expect(reply.status).toBe(200);
    expect(reply.type).toContain("application/json");
    expect(JSON.parse(reply.body)).toEqual({ body: "", method: "GET", path: "/api/health?probe=1" });
  });

  test("forwards the method and body of a write", async () => {
    const reply = await send("/api/v1/chat/proposals/p1/confirm", {
      method: "POST",
      body: '{"selection":["a"]}',
    });

    expect(JSON.parse(reply.body)).toEqual({
      body: '{"selection":["a"]}',
      method: "POST",
      path: "/api/v1/chat/proposals/p1/confirm",
    });
  });

  test("forwards a WebSocket opened under /api", async () => {
    const socket = new WebSocket(`ws://127.0.0.1:${webPort}/api/v1/audio/live/turn`);
    const greeting = await new Promise<string>((resolve, reject) => {
      socket.addEventListener("message", (event) => {
        resolve(String(event.data));
      });
      socket.addEventListener("error", () => {
        reject(new Error("the WebSocket failed"));
      });
    });
    socket.close();

    expect(greeting).toBe("stub api /api/v1/audio/live/turn");
  });

  test("still renders app routes itself", async () => {
    const reply = await send("/mail");

    expect(reply.status).toBe(200);
    expect(reply.type).toContain("text/html");
  });

  test("answers a Codespaces forwarded host", async () => {
    const reply = await send("/api/health", { host: "example-3001.app.github.dev" });

    expect(reply.status).toBe(200);
    expect(reply.type).toContain("application/json");
  });

  test("rejects any other host", async () => {
    const reply = await send("/api/health", { host: "rebind.example" });

    expect(reply.status).toBe(403);
  });
});

// Last, because it stops the stub for good.
describe("the dev server with the API down", () => {
  test("answers /api with a proxy error, not the app's page", async () => {
    await api.stop(true);
    const reply = await send("/api/health");

    expect(reply.status).toBe(502);
    expect(reply.type).not.toContain("text/html");
  });
});

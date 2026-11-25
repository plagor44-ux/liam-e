// Reproducible production-build PWA verification matrix using synthetic fixtures only.
// Usage:
//   bunx nx run web:build
//   NODE_PATH=/path/to/playwright/node_modules node tests/pwa_matrix.cjs
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { chromium } = require("playwright");

const WORKSPACE = path.resolve(__dirname, "..");
const OUTPUT_DIR = path.join(WORKSPACE, "apps/web/.output");

const IDS = {
  account1: "00000000-0000-4000-8000-000000000011",
  mail1: "00000000-0000-4000-8000-000000000101",
  conv1: "00000000-0000-4000-8000-000000000201",
  ctx1: "00000000-0000-4000-8000-000000000291",
  occ1: "00000000-0000-4000-8000-000000000301",
  calSource1: "00000000-0000-4000-8000-000000000311",
  task1: "00000000-0000-4000-8000-000000000401",
  prompt1: "00000000-0000-4000-8000-000000000501",
  pref1: "00000000-0000-4000-8000-000000000601",
};

const PRIVATE_SENTINEL = "Vertrauliche Synthetik-Abrechnung 2026";

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function waitForHttp(url) {
  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(url);
      if (r.status > 0) return;
    } catch {}
    await new Promise(r => setTimeout(r, 100));
  }
  throw new Error(`Timed out waiting for ${url}`);
}

function startNitro(serverEntry, port) {
  const child = spawn("node", [serverEntry], {
    cwd: path.dirname(path.dirname(serverEntry)),
    env: {
      ...process.env,
      NODE_ENV: "production",
      HOST: "127.0.0.1",
      PORT: String(port),
      APP_NAME: "liam-e",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let logs = "";
  child.stdout.on("data", d => { logs = (logs + d).slice(-4000); });
  child.stderr.on("data", d => { logs = (logs + d).slice(-4000); });
  return { child, logs: () => logs };
}

function createSyntheticApiHandler() {
  const now = "2026-11-21T09:00:00Z";
  return (req, res) => {
    const u = new URL(req.url, "http://127.0.0.1");
    const p = u.pathname;
    const json = (status, data) => {
      res.writeHead(status, {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
      });
      res.end(JSON.stringify(data));
    };

    if (p === "/api/health") return json(200, { status: "ok", deps: { database: { reachable: true }, mistral: { configured: false } } });
    if (p === "/api/v1/settings/ui") {
      return json(200, { locale: "browser", revision: 1, swipe_left: "archive", swipe_right: "toggle_read", updated_at: now });
    }
    if (p === "/api/v1/settings") {
      return json(200, { personalization: { use_preferences: false, suggest_preferences: false, version: 1 } });
    }
    if (p === "/api/v1/settings/chat") return json(200, { account_ids: [IDS.account1], version: 1 });
    if (p === "/api/v1/settings/calendar") return json(200, { default_source_id: IDS.calSource1, version: 1 });
    if (p === "/api/v1/mail/summary") {
      return json(200, {
        accounts: [{
          id: IDS.account1,
          display_name: "Privat",
          email_address: "privat@example.org",
          provider: "imap",
          connection: "imap",
          state: "ok",
          error_code: "",
          last_sync_at: now,
          unread: 1,
          color_index: 0,
        }],
        unread_total: 1,
        newsletter_senders: 1,
        last_sync_at: now,
        mail_scope_version: 1,
      });
    }
    if (p === "/api/v1/mail/sync") {
      if (u.searchParams.get("delay") === "1") {
        setTimeout(() => json(200, { queued: 0 }), 300);
        return;
      }
      return json(200, { queued: 0 });
    }
    if (p === "/api/v1/mail/accounts") {
      return json(200, [{
        id: IDS.account1,
        auth_type: "imap_password",
        chat_eligibility: "eligible",
        color_index: 0,
        connection: "imap",
        created_at: now,
        display_name: "Privat",
        email_address: "privat@example.org",
        error_code: "",
        is_active: true,
        last_sync_at: now,
        message_count: 1,
        provider: "imap",
        state: "ok",
        unread: 1,
        updated_at: now,
      }]);
    }
    if (p === "/api/v1/mail/accounts/options") {
      return json(200, { gmail_connector: false, google_oauth: false, microsoft_oauth: false, outlook_connector: false, imap_presets: [] });
    }
    if (p === "/api/v1/mail/connectors") return json(200, []);
    if (p === "/api/v1/mail/messages") {
      return json(200, {
        items: [{
          id: IDS.mail1,
          account_id: IDS.account1,
          color_index: 0,
          deadline: "2026-11-28",
          has_attachments: true,
          internal_date: now,
          is_flagged: true,
          is_read: false,
          needs_reply: true,
          sender_address: "absender@example.com",
          sender_name: "Test Absender",
          snippet: PRIVATE_SENTINEL,
          subject: PRIVATE_SENTINEL,
        }],
        next_cursor: null,
      });
    }
    if (p === `/api/v1/mail/messages/${IDS.mail1}`) {
      return json(200, {
        id: IDS.mail1,
        account_id: IDS.account1,
        attachments: [{ name: "Beleg.pdf", size: 12345, content_type: "application/pdf" }],
        body_text: `${PRIVATE_SENTINEL}\n\nDies ist eine rein synthetische Test-Mail fuer die PWA-Matrix.`,
        can_show_original: false,
        can_unsubscribe: false,
        cc: [],
        color_index: 0,
        deadline: "2026-11-28",
        has_attachments: true,
        internal_date: now,
        is_flagged: true,
        is_read: false,
        local_reference_only: false,
        needs_reply: true,
        reply_to: [],
        sender_address: "absender@example.com",
        sender_name: "Test Absender",
        snippet: PRIVATE_SENTINEL,
        subject: PRIVATE_SENTINEL,
        thread_id: "seed-thread-1",
        to: ["privat@example.org"],
      });
    }
    if (p === `/api/v1/mail/messages/${IDS.mail1}/insight`) {
      return json(200, {
        created_at: now,
        deadline: "2026-11-28",
        followups: ["Antwort entwerfen"],
        model: "mistral-small-latest",
        needs_reply: true,
        points: ["Synthetischer Pruefpunkt eins."],
      });
    }
    if (p === "/api/v1/chat/conversations") {
      return json(200, [{
        id: IDS.conv1,
        account_scope_json: JSON.stringify({ account_ids: [IDS.account1], selection: null }),
        created_at: now,
        model: "mistral-small-latest",
        prompt_revision_id: null,
        title: "Synthetische Unterhaltung mit einem sehr langen Titel zum Testen der Kopfzeile",
        updated_at: now,
        version: 1,
      }]);
    }
    if (p === "/api/v1/chat/history") {
      const context = {
        id: IDS.ctx1,
        account_scope_json: JSON.stringify({ account_ids: [IDS.account1], selection: null }),
        app_prompt_version: "8",
        calendar_scope_json: JSON.stringify([IDS.calSource1]),
        calendar_sources_replaced: false,
        mail_scope_version: 1,
        preference_revision_id: null,
        preference_revision_ids_json: "[]",
        preferences_enabled: false,
        prompt_hash: null,
        prompt_revision_id: null,
        revoked: false,
        task_scope_json: JSON.stringify({ source: "none" }),
      };
      return json(200, [
        {
          id: "00000000-0000-4000-8000-000000000211",
          conversation_id: IDS.conv1,
          role: "user",
          content: "Fasse meine Test-Mails zusammen.",
          created_at: now,
          context,
          extras: {},
          proposal: null,
          proposal_status: "",
          stopped: false,
          tool_name: "",
        },
        {
          id: "00000000-0000-4000-8000-000000000212",
          conversation_id: IDS.conv1,
          role: "assistant",
          content: "Hier ist deine synthetische Zusammenfassung fuer die PWA-Pruefung.",
          created_at: now,
          context,
          extras: { model: "mistral-small-latest", suggestions: ["Mehr Details zeigen"] },
          proposal: null,
          proposal_status: "",
          stopped: false,
          tool_name: "",
        },
      ]);
    }
    if (p === "/api/v1/chat/models") {
      return json(200, [{ id: "mistral-small-latest", label: "Mistral Small", description: "Schnell", is_default: true, recommended: true, availability: "available", group: "general" }]);
    }
    if (p === "/api/v1/prompts") {
      return json(200, [{ id: IDS.prompt1, name: "Kurzantwort", text: "Antworte kurz.", revision_id: "00000000-0000-4000-8000-000000000511", content_hash: "abc", version: 1, updated_at: now }]);
    }
    if (p === `/api/v1/prompts/${IDS.prompt1}`) {
      return json(200, { id: IDS.prompt1, name: "Kurzantwort", text: "Antworte kurz.", revision_id: "00000000-0000-4000-8000-000000000511", content_hash: "abc", version: 1, updated_at: now, revisions: [] });
    }
    if (p === "/api/v1/preferences") {
      return json(200, [{ id: IDS.pref1, key: "answer_length", scope: "*", value: "Kurz", status: "active", source: "manual", revision_id: "00000000-0000-4000-8000-000000000611", version: 1, confirmed_at: now }]);
    }
    if (p === "/api/v1/preferences/candidates") return json(200, []);
    if (p === "/api/v1/skills") return json(200, []);
    if (p === "/api/v1/audio/capabilities") return json(200, { stt: { status: "disabled", formats: [] }, tts: { status: "disabled", formats: [] } });
    if (p === "/api/v1/calendar/sources") {
      return json(200, [{
        id: IDS.calSource1,
        name: "Testkalender",
        provider: "google",
        is_active: true,
        capabilities: ["read"],
        chat_access: "read_only",
        coverage_start: "2026-10-01",
        coverage_end: "2027-05-01",
        incomplete: false,
        last_success_at: now,
        last_checked_at: now,
        last_error: null,
        data_revision: 1,
      }]);
    }
    if (p === "/api/v1/calendar/occurrences") {
      return json(200, {
        items: [{
          id: IDS.occ1,
          event_id: "evt-1",
          source_id: IDS.calSource1,
          title: "Synthetischer Termin",
          start_at: "2026-11-21T10:00:00Z",
          end_at: "2026-11-21T11:00:00Z",
          start_date: null,
          end_date: null,
          timezone: "Europe/Berlin",
          editable: false,
        }],
        next_cursor: null,
      });
    }
    if (p === `/api/v1/calendar/occurrences/${IDS.occ1}/live`) {
      return json(200, {
        occurrence_id: IDS.occ1,
        source_id: IDS.calSource1,
        title: "Synthetischer Termin",
        start_at: "2026-11-21T10:00:00Z",
        end_at: "2026-11-21T11:00:00Z",
        timezone: "Europe/Berlin",
        editable: false,
        reason: "calendar_not_writable",
      });
    }
    if (p === "/api/v1/tasks") {
      return json(200, {
        items: [{
          id: IDS.task1,
          title: "Synthetische Aufgabe",
          description: "Pruefaufgabe",
          archived: false,
          complete: true,
          version: 1,
          source_account_id: null,
          source_message_id: null,
          schedule: { timezone: "Europe/Berlin", rrule: null },
        }],
        next_cursor: null,
        window: { due_from: "2026-11-01", due_to: "2026-12-31" },
      });
    }
    if (p === "/api/v1/tasks/occurrences") {
      return json(200, {
        items: [{
          id: "00000000-0000-4000-8000-000000000411",
          series_id: IDS.task1,
          revision_id: "00000000-0000-4000-8000-000000000412",
          occurrence_key: "2026-11-21",
          due_local: "2026-11-21",
          due_at: null,
          status: "open",
          version: 1,
        }],
        next_cursor: null,
      });
    }
    if (p === `/api/v1/tasks/${IDS.task1}`) {
      return json(200, {
        series: {
          id: IDS.task1,
          title: "Synthetische Aufgabe",
          description: "Pruefaufgabe",
          archived: false,
          complete: true,
          version: 1,
          source_account_id: null,
          source_message_id: null,
          schedule: { timezone: "Europe/Berlin", rrule: null },
        },
        occurrences: {
          items: [{
            id: "00000000-0000-4000-8000-000000000411",
            series_id: IDS.task1,
            revision_id: "00000000-0000-4000-8000-000000000412",
            occurrence_key: "2026-11-21",
            due_local: "2026-11-21",
            due_at: null,
            status: "open",
            version: 1,
          }],
          next_cursor: null,
        },
        window: { due_from: "2026-11-01", due_to: "2026-12-31" },
      });
    }
    if (p === "/api/v1/tasks/reconcile") {
      return json(200, { series_id: IDS.task1, revision_id: "00000000-0000-4000-8000-000000000412", cursor: "done", target: "2026-12-31", complete: true });
    }
    return json(404, { detail: { code: "not_found" } });
  };
}

async function run() {
  assert.ok(fs.existsSync(path.join(OUTPUT_DIR, "server/index.mjs")), "apps/web/.output missing; run bunx nx run web:build first");

  const assetsDir = path.join(OUTPUT_DIR, "public/assets");
  const scripts = fs.readdirSync(assetsDir).filter(f => f.endsWith(".js"));
  const scriptText = new Map(scripts.map(f => [f, fs.readFileSync(path.join(assetsDir, f), "utf8")]));
  // The /tasks route chunk is the `tasks-<hash>.js` file loaded through a dynamic import. A shared
  // task module carries the same prefix but is imported statically.
  const lazyTasks = /import\(\s*[`'"]\.\/(tasks-[A-Za-z0-9_-]+\.js)[`'"]\s*\)/;
  const v1TasksChunk = scripts.map(f => scriptText.get(f).match(lazyTasks)?.[1]).find(Boolean);
  assert.ok(v1TasksChunk && scripts.includes(v1TasksChunk), "the build has no lazily imported tasks route chunk");
  // A real rebuild that changes a chunk also changes the hash of every chunk importing it, so v2
  // renames the route chunk and its transitive importers. Renaming only the route chunk would let
  // the browser reuse an immutable cached v1 importer that still names the removed file.
  const renamed = new Set([v1TasksChunk]);
  for (let grew = true; grew;) {
    grew = false;
    for (const f of scripts) {
      if (!renamed.has(f) && [...renamed].some(name => scriptText.get(f).includes(name))) {
        renamed.add(f);
        grew = true;
      }
    }
  }
  const v2Names = new Map([...renamed].map(f => [f, f.replace(/\.js$/, "V2.js")]));
  const v1Names = new Map([...v2Names].map(([v1, v2]) => [v2, v1]));
  const byLength = [...v2Names.keys()].sort((a, b) => b.length - a.length);
  const toV2 = text => byLength.reduce((out, name) => out.replaceAll(name, v2Names.get(name)), text);
  const v1SwText = fs.readFileSync(path.join(OUTPUT_DIR, "public/service-worker.js"), "utf8");

  const webPort = await freePort();
  const proxyPort = await freePort();
  let version = "v1";

  const nitro = startNitro(path.join(OUTPUT_DIR, "server/index.mjs"), webPort);
  const apiHandler = createSyntheticApiHandler();

  const proxy = http.createServer((req, res) => {
    if (req.url === "/api" || req.url.startsWith("/api/")) return apiHandler(req, res);

    if (version === "v2" && req.url === "/service-worker.js") {
      const body = v1SwText + "\n/* matrix-v2 */\n";
      res.writeHead(200, {
        "Content-Type": "text/javascript; charset=utf-8",
        "Cache-Control": "no-cache",
        "Content-Length": Buffer.byteLength(body),
      });
      res.end(body);
      return;
    }
    const assetName = req.url.startsWith("/assets/") ? req.url.slice("/assets/".length) : null;
    if (version === "v2" && assetName && v2Names.has(assetName)) {
      res.writeHead(404, { "Content-Type": "text/plain" });
      res.end("stale v1 chunk gone in v2");
      return;
    }

    let upstreamPath = req.url;
    if (version === "v2" && assetName && v1Names.has(assetName)) {
      upstreamPath = `/assets/${v1Names.get(assetName)}`;
    }

    const fwdHeaders = { ...req.headers };
    if (version === "v2") delete fwdHeaders["accept-encoding"];

    const upstream = http.request(
      { host: "127.0.0.1", port: webPort, method: req.method, path: upstreamPath, headers: fwdHeaders },
      incoming => {
        const ct = incoming.headers["content-type"] || "";
        if (version === "v2" && (ct.includes("text/html") || ct.includes("javascript"))) {
          const chunks = [];
          incoming.on("data", c => chunks.push(c));
          incoming.on("end", () => {
            const text = toV2(Buffer.concat(chunks).toString("utf8"));
            const headers = { ...incoming.headers, "content-length": String(Buffer.byteLength(text)) };
            delete headers["content-encoding"];
            res.writeHead(incoming.statusCode, headers);
            res.end(text);
          });
          return;
        }
        res.writeHead(incoming.statusCode, incoming.headers);
        incoming.pipe(res);
      }
    );
    upstream.on("error", err => res.destroy(err));
    req.pipe(upstream);
  });

  await new Promise(resolve => proxy.listen(proxyPort, "127.0.0.1", resolve));
  const origin = `http://localhost:${proxyPort}`;

  let browser;
  const pass = (name, detail = "") => console.log(`[PASS] ${name}${detail ? ` :: ${detail}` : ""}`);

  try {
    await waitForHttp(`http://127.0.0.1:${webPort}/`);

    // 1 + 2. Static PWA routes, headers, HTML tags, manifest
    const swHeadRes = await fetch(`${origin}/service-worker.js`);
    assert.equal(swHeadRes.status, 200);
    assert.ok((swHeadRes.headers.get("content-type") || "").includes("javascript"));
    assert.equal(swHeadRes.headers.get("cache-control"), "no-cache");

    const manifestRes = await fetch(`${origin}/manifest.webmanifest`);
    assert.equal(manifestRes.status, 200);
    assert.equal(manifestRes.headers.get("content-type"), "application/manifest+json");
    assert.equal(manifestRes.headers.get("cache-control"), "no-cache");
    const manifest = await manifestRes.json();
    assert.equal(manifest.display, "standalone");
    assert.equal(manifest.start_url, "/");
    assert.equal(manifest.scope, "/");
    assert.ok(manifest.icons.every(i => !i.purpose.includes(" ")));

    for (const icon of ["/app-180.png", "/app-192.png", "/app-512.png", "/favicon.svg"]) {
      const r = await fetch(`${origin}${icon}`);
      assert.equal(r.status, 200, icon);
    }
    const html = await (await fetch(`${origin}/`, { headers: { "Accept-Language": "de-DE,de;q=0.9" } })).text();
    assert.ok(html.includes("viewport-fit=cover"));
    assert.ok(html.includes("interactive-widget=resizes-content"));
    assert.ok(html.includes('name="apple-mobile-web-app-capable"'));
    assert.ok(html.includes('name="apple-mobile-web-app-title"'));
    assert.ok(html.includes('crossorigin="use-credentials"'));
    pass("1+2 Static PWA routes, headers & HTML head");

    // 2. Chromium installability & SW scope
    browser = await chromium.launch({ executablePath: process.env.PWA_MATRIX_CHROMIUM, headless: true });
    const context = await browser.newContext({
      locale: "de-DE",
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
      serviceWorkers: "allow",
    });
    const page = await context.newPage();
    page.setDefaultTimeout(10000);
    await page.goto(`${origin}/`, { waitUntil: "networkidle" });

    const cdp = await context.newCDPSession(page);
    const cdpManifest = await cdp.send("Page.getAppManifest");
    assert.deepEqual(cdpManifest.errors, []);
    const installability = await cdp.send("Page.getInstallabilityErrors").catch(() => ({ installabilityErrors: [] }));
    assert.deepEqual(installability.installabilityErrors, []);
    pass("1 Chromium installability", cdpManifest.url);

    const swReg = await page.evaluate(async () => {
      const reg = await navigator.serviceWorker.ready;
      return {
        scope: reg.scope,
        updateViaCache: reg.updateViaCache,
        scriptURL: reg.active?.scriptURL,
        controller: Boolean(navigator.serviceWorker.controller),
      };
    });
    assert.equal(swReg.scope, `${origin}/`);
    assert.equal(swReg.updateViaCache, "none");
    assert.equal(swReg.scriptURL, `${origin}/service-worker.js`);
    assert.equal(swReg.controller, true);
    pass("2 SW registration, scope & immediate claim");

    // 3. Home-screen start, theme before paint & locale
    await page.evaluate(() => localStorage.setItem("liam-e-theme", "dark"));
    await page.reload({ waitUntil: "networkidle" });
    const startInfo = await page.evaluate(() => ({
      dataTheme: document.documentElement.getAttribute("data-theme"),
      title: document.title,
      lang: document.documentElement.lang,
    }));
    assert.equal(startInfo.dataTheme, "dark");
    assert.equal(startInfo.title, "liam-e");
    assert.equal(startInfo.lang, "de");
    await page.evaluate(() => localStorage.removeItem("liam-e-theme"));
    pass("3 Standalone start_url, theme & locale");

    // 4. Deep links & return without browser chrome
    const deepLinks = [
      { url: `/mail/${IDS.mail1}`, expectText: PRIVATE_SENTINEL, backSelector: 'button[aria-label="Zurück zum Posteingang"]', backPath: "/mail" },
      { url: `/calendar/${IDS.occ1}`, expectText: "Synthetischer Termin", backSelector: "a.le-calendar-back", backPath: "/calendar" },
      { url: `/tasks/${IDS.task1}`, expectText: "Synthetische Aufgabe", backSelector: 'a[href="/tasks"]', backPath: "/tasks" },
      { url: `/settings/prompts/${IDS.prompt1}`, expectText: "Vorlage bearbeiten", backSelector: 'a[href="/settings/prompts"]', backPath: "/settings/prompts" },
      { url: `/settings/preferences/${IDS.pref1}`, expectText: "Bearbeiten", backSelector: 'a[href="/settings/preferences"]', backPath: "/settings/preferences" },
      { url: "/nicht-vorhanden", expectText: "Diese Seite gibt es nicht", backSelector: 'a[href="/"]', backPath: "/" },
    ];
    for (const dl of deepLinks) {
      await page.goto(`${origin}${dl.url}`, { waitUntil: "networkidle" });
      await page.getByText(dl.expectText, { exact: false }).first().waitFor();
      await page.locator(dl.backSelector).first().click();
      await page.waitForFunction(expected => location.pathname === expected, dl.backPath);
    }
    await page.goto(`${origin}/chat/${IDS.conv1}`, { waitUntil: "networkidle" });
    await page.locator(".le-composer textarea").waitFor();
    await page.getByRole("button", { name: "Menü öffnen" }).click();
    await page.getByRole("dialog", { name: "Menü" }).getByRole("link", { name: /Posteingang/ }).click();
    await page.waitForFunction(() => location.pathname === "/mail");
    pass("4 Deep links & standalone return paths");

    // 5. Portrait, compact, keyboard-open and landscape viewports + floating menu + touch targets
    for (const vp of [
      { width: 390, height: 844 },
      { width: 320, height: 568 },
      { width: 390, height: 420 },
      { width: 844, height: 390 },
    ]) {
      await page.setViewportSize(vp);
      for (const route of ["/", `/chat/${IDS.conv1}`, "/mail", "/calendar", "/tasks", "/accounts", "/settings/language"]) {
        await page.goto(`${origin}${route}`, { waitUntil: "networkidle" });
        const metrics = await page.evaluate(() => {
          const doc = document.documentElement;
          const composer = document.querySelector(".le-composer textarea")?.getBoundingClientRect();
          const send = document.querySelector(".le-send")?.getBoundingClientRect();
          const topbar = document.querySelector(".le-topbar")?.getBoundingClientRect();
          return {
            overflowX: doc.scrollWidth > window.innerWidth,
            composerClipped: composer ? composer.bottom > window.innerHeight + 1 || composer.right > window.innerWidth + 1 : false,
            sendClipped: send ? send.bottom > window.innerHeight + 1 || send.right > window.innerWidth + 1 : false,
            topbarHidden: topbar ? topbar.top < 0 : false,
          };
        });
        assert.equal(metrics.overflowX, false, `overflowX on ${route} ${vp.width}x${vp.height}`);
        assert.equal(metrics.composerClipped, false, `composerClipped on ${route} ${vp.width}x${vp.height}`);
        assert.equal(metrics.sendClipped, false, `sendClipped on ${route} ${vp.width}x${vp.height}`);
        assert.equal(metrics.topbarHidden, false, `topbarHidden on ${route} ${vp.width}x${vp.height}`);
      }
    }
    await page.setViewportSize({ width: 320, height: 568 });
    await page.goto(`${origin}/`, { waitUntil: "networkidle" });
    await page.locator(".le-model-control button").first().click();
    const menuRect = await page.locator(".le-menu").boundingBox();
    assert.ok(menuRect && menuRect.x >= 0 && menuRect.y >= 0 && menuRect.x + menuRect.width <= 320 && menuRect.y + menuRect.height <= 568);
    pass("5 Portrait, compact, keyboard-open & landscape viewports + menu bounds");

    // 6. Update v1 -> v2 across multiple open tabs (SW skipWaiting + no reload during typing/requests + stale chunk recovery)
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${origin}/`, { waitUntil: "networkidle" });
    const draftText = "Unvollendeter Entwurf waehrend SW-Update";
    await page.locator(".le-composer textarea").fill(draftText);

    const secondTab = await context.newPage();
    secondTab.setDefaultTimeout(10000);
    await secondTab.goto(`${origin}/mail`, { waitUntil: "networkidle" });
    await secondTab.getByRole("button", { name: "Suchen" }).click();
    await secondTab.locator(".le-inbox-search input").fill("Synthetik");
    await secondTab.waitForURL(url => url.searchParams.get("q") === "Synthetik");

    let tab1Loads = 0;
    let tab2Loads = 0;
    page.on("load", () => { tab1Loads++; });
    secondTab.on("load", () => { tab2Loads++; });

    await Promise.all([
      page.evaluate(() => {
        window.__stayAliveToken = "tab-1-alive";
        window.__swChanged = new Promise(resolve => {
          const timer = setTimeout(() => resolve(false), 4000);
          navigator.serviceWorker.addEventListener("controllerchange", () => {
            clearTimeout(timer);
            resolve(true);
          }, { once: true });
        });
      }),
      secondTab.evaluate(() => {
        window.__stayAliveToken = "tab-2-alive";
        window.__swChanged = new Promise(resolve => {
          const timer = setTimeout(() => resolve(false), 4000);
          navigator.serviceWorker.addEventListener("controllerchange", () => {
            clearTimeout(timer);
            resolve(true);
          }, { once: true });
        });
      }),
    ]);

    version = "v2";
    const inFlightSync = page.evaluate(() =>
      fetch("/api/v1/mail/sync?delay=1", { method: "POST" }).then(r => ({ ok: r.ok, status: r.status }))
    );
    await page.evaluate(() => navigator.serviceWorker.getRegistration().then(reg => reg?.update()));
    const [tab1Changed, tab2Changed, syncResult] = await Promise.all([
      page.evaluate(() => window.__swChanged),
      secondTab.evaluate(() => window.__swChanged),
      inFlightSync,
    ]);
    assert.equal(tab1Changed, true, "tab 1 should receive updated controller");
    assert.equal(tab2Changed, true, "tab 2 should receive updated controller");
    assert.deepEqual(syncResult, { ok: true, status: 200 });
    assert.equal(tab1Loads, 0, "tab 1 must not auto-reload while typing");
    assert.equal(tab2Loads, 0, "tab 2 must not auto-reload while searching");
    assert.equal(await page.evaluate(() => window.__stayAliveToken), "tab-1-alive");
    assert.equal(await secondTab.evaluate(() => window.__stayAliveToken), "tab-2-alive");
    assert.equal(await page.locator(".le-composer textarea").inputValue(), draftText);
    assert.equal(await secondTab.locator(".le-inbox-search input").inputValue(), "Synthetik");
    const regAfterUpdate = await page.evaluate(async () => {
      const reg = await navigator.serviceWorker.getRegistration();
      if (reg?.active && reg.active.state !== "activated") {
        await new Promise(resolve => {
          const timer = setTimeout(resolve, 2000);
          reg.active.addEventListener("statechange", () => {
            if (reg.active?.state === "activated") {
              clearTimeout(timer);
              resolve();
            }
          });
        });
      }
      return { waiting: reg?.waiting?.state ?? null, active: reg?.active?.state ?? null };
    });
    assert.equal(regAfterUpdate.waiting, null);
    assert.equal(regAfterUpdate.active, "activated");
    await secondTab.close();

    version = "v1";
    const ctxUpdate = await browser.newContext({ locale: "de-DE", viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, serviceWorkers: "allow" });
    const pUpdate = await ctxUpdate.newPage();
    await pUpdate.goto(`${origin}/`, { waitUntil: "networkidle" });
    version = "v2";
    await pUpdate.getByRole("button", { name: "Menü öffnen" }).click();
    await pUpdate.getByRole("dialog", { name: "Menü" }).getByRole("link", { name: "Aufgaben" }).click();
    await pUpdate.getByText("Synthetische Aufgabe", { exact: false }).waitFor({ timeout: 10000 });
    await ctxUpdate.close();
    pass("6 Update v1 -> v2 (multi-tab SW skipWaiting without auto-reload & stale chunk recovery)");

    // 7. Offline navigation, offline SPA lazy-chunk error, English locale & empty CacheStorage
    version = "v1";
    const offlineCtx = await browser.newContext({ locale: "de-DE", viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, serviceWorkers: "allow" });
    const offlinePage = await offlineCtx.newPage();
    await offlinePage.goto(`${origin}/mail/${IDS.mail1}`, { waitUntil: "networkidle" });
    await offlinePage.getByText(PRIVATE_SENTINEL, { exact: false }).first().waitFor();
    assert.deepEqual(await offlinePage.evaluate(() => caches.keys()), []);

    await offlineCtx.setOffline(true);
    let offlineStatus = 0;
    try {
      const resp = await offlinePage.goto(`${origin}/mail/${IDS.mail1}`, { waitUntil: "domcontentloaded" });
      offlineStatus = resp ? resp.status() : 0;
    } catch {}
    assert.equal(offlineStatus, 503);
    const offlineInfo = await offlinePage.evaluate(() => ({
      lang: document.documentElement.lang,
      viewport: document.querySelector('meta[name="viewport"]')?.getAttribute("content"),
      text: document.body.innerText,
    }));
    assert.equal(offlineInfo.lang, "de");
    assert.ok(offlineInfo.viewport?.includes("viewport-fit=cover"));
    assert.ok(offlineInfo.text.includes("Du bist offline"));
    assert.equal(offlineInfo.text.includes(PRIVATE_SENTINEL), false);

    const apiFail = await offlinePage.evaluate(async () => {
      try {
        await fetch("/api/v1/mail/summary");
        return false;
      } catch {
        return true;
      }
    });
    assert.equal(apiFail, true);

    await offlineCtx.setOffline(false);
    await offlinePage.locator('a[href=""]').click();
    await offlinePage.getByText(PRIVATE_SENTINEL, { exact: false }).first().waitFor();

    // Offline SPA navigation to an unloaded lazy route (/tasks) shows offline message without reloading.
    await offlinePage.goto(`${origin}/mail`, { waitUntil: "networkidle" });
    await offlinePage.evaluate(() => { window.__offlineStayAlive = "stay"; });
    let offlineSpaLoads = 0;
    offlinePage.on("load", () => { offlineSpaLoads++; });
    await offlineCtx.setOffline(true);
    await offlinePage.getByRole("button", { name: "Menü öffnen" }).click();
    await offlinePage.getByRole("dialog", { name: "Menü" }).getByRole("link", { name: "Aufgaben" }).click();
    await offlinePage.getByText("Du bist offline", { exact: true }).waitFor();
    assert.equal(await offlinePage.getByText("liam-e wurde aktualisiert").count(), 0);
    assert.equal(offlineSpaLoads, 0);
    assert.equal(await offlinePage.evaluate(() => window.__offlineStayAlive), "stay");

    // Coming back online and clicking Retry loads /tasks cleanly.
    await offlineCtx.setOffline(false);
    await offlinePage.getByRole("button", { name: "Erneut versuchen", exact: true }).click();
    await offlinePage.getByText("Synthetische Aufgabe", { exact: false }).waitFor({ timeout: 10000 });
    assert.deepEqual(await offlinePage.evaluate(() => caches.keys()), []);
    await offlineCtx.close();

    // Verify English offline fallback via LOCALE_COOKIE/Accept-Language
    const enCtx = await browser.newContext({ locale: "en-US", viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true, serviceWorkers: "allow" });
    const enPage = await enCtx.newPage();
    await enPage.goto(`${origin}/`, { waitUntil: "networkidle" });
    await enCtx.setOffline(true);
    try { await enPage.goto(`${origin}/`, { waitUntil: "domcontentloaded" }); } catch {}
    assert.equal(await enPage.evaluate(() => document.documentElement.lang), "en");
    assert.ok((await enPage.evaluate(() => document.body.innerText)).includes("You’re offline"));
    assert.deepEqual(await enPage.evaluate(() => caches.keys()), []);
    await enCtx.close();
    pass("7 Offline navigation, offline SPA lazy-chunk message, EN/DE locale & empty CacheStorage");
  } finally {
    await browser?.close().catch(() => {});
    proxy.closeAllConnections?.();
    await new Promise(r => { proxy.close(r); setTimeout(r, 500); });
    nitro.child.kill("SIGKILL");
  }
}

run().then(() => process.exit(0)).catch(err => {
  console.error(err);
  process.exit(1);
});

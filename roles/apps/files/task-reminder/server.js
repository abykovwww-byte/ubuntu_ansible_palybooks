import { createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import { createReadStream, existsSync, mkdirSync, promises as fs } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const publicDir = join(__dirname, "public");
const port = Number(process.env.PORT || 3000);
const dataFile = process.env.DATA_FILE || "/data/tasks.json";
const adminPassword = process.env.ADMIN_PASSWORD || randomBytes(24).toString("base64url");
const sessionSecret = process.env.SESSION_SECRET || randomBytes(32).toString("hex");

const mimeTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

function sign(value) {
  return createHmac("sha256", sessionSecret).update(value).digest("base64url");
}

function makeSession() {
  const value = `${Date.now()}.${randomBytes(24).toString("base64url")}`;
  return `${value}.${sign(value)}`;
}

function isValidSession(token = "") {
  const parts = token.split(".");
  if (parts.length !== 3) return false;
  const value = `${parts[0]}.${parts[1]}`;
  const expected = sign(value);
  const received = parts[2];
  if (Date.now() - Number(parts[0]) > 1000 * 60 * 60 * 24 * 30) return false;
  return safeEqual(expected, received);
}

function safeEqual(a, b) {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

function getCookie(req, name) {
  const cookies = Object.fromEntries(
    (req.headers.cookie || "")
      .split(";")
      .map((part) => part.trim().split("="))
      .filter(([key, value]) => key && value)
  );
  return cookies[name];
}

function isAdmin(req) {
  return isValidSession(getCookie(req, "task_reminder_session"));
}

async function ensureStore() {
  mkdirSync(resolve(dataFile, ".."), { recursive: true });
  if (!existsSync(dataFile)) {
    await fs.writeFile(
      dataFile,
      JSON.stringify(
        {
          tasks: [
            {
              id: randomBytes(8).toString("hex"),
              title: "Проверить первый деплой",
              notes: "После публикации открой task.abykov.site и создай свои задачи в админке.",
              triggerAt: "",
              enabled: true,
              completed: false,
              createdAt: new Date().toISOString(),
              updatedAt: new Date().toISOString(),
            },
          ],
        },
        null,
        2
      )
    );
  }
}

async function readStore() {
  await ensureStore();
  const raw = await fs.readFile(dataFile, "utf8");
  return JSON.parse(raw);
}

async function writeStore(store) {
  const tmpFile = `${dataFile}.tmp`;
  await fs.writeFile(tmpFile, JSON.stringify(store, null, 2));
  await fs.rename(tmpFile, dataFile);
}

function publicTask(task) {
  return {
    id: task.id,
    title: task.title,
    notes: task.notes || "",
    triggerAt: task.triggerAt || "",
    enabled: Boolean(task.enabled),
    completed: Boolean(task.completed),
    updatedAt: task.updatedAt,
  };
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (chunks.length === 0) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8").replace(/^\uFEFF/, ""));
}

function sendJson(res, status, body, headers = {}) {
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    ...headers,
  });
  res.end(JSON.stringify(body));
}

function sendError(res, status, message) {
  sendJson(res, status, { error: message });
}

function normalizeTask(input, existing = {}) {
  const title = String(input.title || "").trim();
  if (!title) throw new Error("Название задачи обязательно.");
  const now = new Date().toISOString();
  return {
    id: existing.id || randomBytes(8).toString("hex"),
    title,
    notes: String(input.notes || "").trim(),
    triggerAt: String(input.triggerAt || "").trim(),
    enabled: Boolean(input.enabled),
    completed: Boolean(input.completed),
    createdAt: existing.createdAt || now,
    updatedAt: now,
  };
}

async function handleApi(req, res, url) {
  if (url.pathname === "/api/health") {
    sendJson(res, 200, { ok: true });
    return;
  }

  if (url.pathname === "/api/tasks" && req.method === "GET") {
    const store = await readStore();
    sendJson(
      res,
      200,
      store.tasks
        .filter((task) => task.enabled && !task.completed)
        .map(publicTask)
        .sort((a, b) => (a.triggerAt || "9999").localeCompare(b.triggerAt || "9999"))
    );
    return;
  }

  if (url.pathname === "/api/admin/login" && req.method === "POST") {
    const body = await readJson(req);
    if (String(body.password || "") !== adminPassword) {
      sendError(res, 401, "Неверный пароль.");
      return;
    }
    const secure = req.headers["x-forwarded-proto"] === "https" ? "; Secure" : "";
    sendJson(res, 200, { ok: true }, {
      "set-cookie": `task_reminder_session=${makeSession()}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000${secure}`,
    });
    return;
  }

  if (url.pathname === "/api/admin/logout" && req.method === "POST") {
    sendJson(res, 200, { ok: true }, {
      "set-cookie": "task_reminder_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0",
    });
    return;
  }

  if (!url.pathname.startsWith("/api/admin/")) {
    sendError(res, 404, "API route not found.");
    return;
  }

  if (!isAdmin(req)) {
    sendError(res, 401, "Нужен вход в админку.");
    return;
  }

  if (url.pathname === "/api/admin/session" && req.method === "GET") {
    sendJson(res, 200, { ok: true });
    return;
  }

  if (url.pathname === "/api/admin/tasks" && req.method === "GET") {
    const store = await readStore();
    sendJson(res, 200, store.tasks);
    return;
  }

  if (url.pathname === "/api/admin/tasks" && req.method === "POST") {
    const store = await readStore();
    const task = normalizeTask(await readJson(req));
    store.tasks.push(task);
    await writeStore(store);
    sendJson(res, 201, task);
    return;
  }

  const taskMatch = url.pathname.match(/^\/api\/admin\/tasks\/([^/]+)$/);
  if (taskMatch && req.method === "PUT") {
    const store = await readStore();
    const index = store.tasks.findIndex((task) => task.id === taskMatch[1]);
    if (index === -1) {
      sendError(res, 404, "Задача не найдена.");
      return;
    }
    const task = normalizeTask(await readJson(req), store.tasks[index]);
    store.tasks[index] = task;
    await writeStore(store);
    sendJson(res, 200, task);
    return;
  }

  if (taskMatch && req.method === "DELETE") {
    const store = await readStore();
    store.tasks = store.tasks.filter((task) => task.id !== taskMatch[1]);
    await writeStore(store);
    sendJson(res, 200, { ok: true });
    return;
  }

  sendError(res, 404, "API route not found.");
}

function serveStatic(req, res, url) {
  const requested = url.pathname === "/" || url.pathname === "/admin"
    ? "/index.html"
    : decodeURIComponent(url.pathname);
  const filePath = normalize(join(publicDir, requested));
  if (!filePath.startsWith(publicDir)) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }
  const finalPath = existsSync(filePath) ? filePath : join(publicDir, "index.html");
  res.writeHead(200, {
    "content-type": mimeTypes[extname(finalPath)] || "application/octet-stream",
  });
  createReadStream(finalPath).pipe(res);
}

createServer(async (req, res) => {
  try {
    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    if (url.pathname.startsWith("/api/")) {
      await handleApi(req, res, url);
      return;
    }
    serveStatic(req, res, url);
  } catch (error) {
    sendError(res, 500, error.message || "Server error.");
  }
}).listen(port, () => {
  console.log(`Task Reminder listening on ${port}`);
});

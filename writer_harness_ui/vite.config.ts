import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const projectRoot = path.resolve(__dirname, "..");
let currentWriterRun: ChildProcessWithoutNullStreams[] = [];

type WriterMode = "vanilla" | "writer_harness" | "both";

type WriterRunPayload = {
  query: string;
  mode: WriterMode;
  autoExecute: boolean;
  executeOutputFormat?: string;
  ohRealRun: boolean;
  runtimeSettings?: Record<string, string>;
  directorHarnessEnabled?: boolean;
  sessionId?: string;
  turnId?: string;
  multiTurn?: boolean;
  resetSession?: boolean;
  deleteTurnId?: string;
};

const runtimeSettingEnvNames = {
  writerModel: "WRITER_MODEL",
  writerBaseUrl: "WRITER_BASE_URL",
  writerApiKey: "WRITER_API_KEY",
  actorModel: "ACTOR_MODEL",
  actorBaseUrl: "ACTOR_BASE_URL",
  actorApiKey: "ACTOR_API_KEY",
  actorApiFormat: "ACTOR_API_FORMAT",
  ohBin: "OH_BIN",
  openharnessSrc: "OPENHARNESS_SRC",
  directorLogPath: "DIRECTOR_LOG_PATH",
} as const;

function getRuntimeEnv(settings?: Record<string, string>, directorHarnessEnabled = false): NodeJS.ProcessEnv {
  const runtimeEnv = { ...process.env };
  for (const [key, envName] of Object.entries(runtimeSettingEnvNames)) {
    const value = settings?.[key];
    if (typeof value === "string" && value.trim()) runtimeEnv[envName] = value.trim();
  }
  if (runtimeEnv.OPENAI_API_BASE?.trim()) {
    runtimeEnv.WRITER_BASE_URL = runtimeEnv.OPENAI_API_BASE.trim();
    runtimeEnv.ACTOR_BASE_URL = runtimeEnv.OPENAI_API_BASE.trim();
  }
  if (runtimeEnv.OPENAI_API_KEY?.trim()) {
    runtimeEnv.WRITER_API_KEY = runtimeEnv.OPENAI_API_KEY.trim();
    runtimeEnv.ACTOR_API_KEY = runtimeEnv.OPENAI_API_KEY.trim();
  }
  const openharnessSrc = runtimeEnv.OPENHARNESS_SRC || path.join(projectRoot, "OpenHarness", "src");
  runtimeEnv.PYTHONPATH = [projectRoot, openharnessSrc, runtimeEnv.PYTHONPATH].filter(Boolean).join(path.delimiter);
  runtimeEnv.DIRECTOR_HARNESS_ENABLED = directorHarnessEnabled ? "true" : "false";
  runtimeEnv.DIRECTOR_MCP_CATALOG = path.join(projectRoot, "director_harness", "director_mcp_catalog.json");
  runtimeEnv.DIRECTOR_LOG_PATH = runtimeEnv.DIRECTOR_LOG_PATH || path.join(projectRoot, "logs", "director-events.jsonl");
  return runtimeEnv;
}

function previewText(value: string, limit = 1600): string {
  if (!value) return "";
  return value.length <= limit ? value : `${value.slice(0, limit)}\n...<truncated>`;
}

function collectMissingEnv(ohRealRun: boolean, runtimeEnv: NodeJS.ProcessEnv): string[] {
  const missing: string[] = [];
  if (!runtimeEnv.WRITER_MODEL) missing.push("WRITER_MODEL");
  if (!runtimeEnv.WRITER_BASE_URL) missing.push("WRITER_BASE_URL");
  if (!runtimeEnv.WRITER_API_KEY) missing.push("WRITER_API_KEY");
  if (!runtimeEnv.OH_BIN) missing.push("OH_BIN");
  if (!runtimeEnv.OPENHARNESS_SRC) missing.push("OPENHARNESS_SRC");
  if (ohRealRun || runtimeEnv.OH_REAL_RUN === "1") {
    if (!runtimeEnv.ACTOR_MODEL) missing.push("ACTOR_MODEL");
    if (!runtimeEnv.ACTOR_BASE_URL) missing.push("ACTOR_BASE_URL");
    if (!runtimeEnv.ACTOR_API_KEY) missing.push("ACTOR_API_KEY");
    if (!runtimeEnv.ACTOR_API_FORMAT) missing.push("ACTOR_API_FORMAT");
  }
  return missing;
}

function buildArgs(payload: WriterRunPayload, runtimeEnv: NodeJS.ProcessEnv): string[] {
  const args = payload.multiTurn && (payload.resetSession || payload.deleteTurnId)
    ? ["writer_excute_multiturn.py", "--mode", payload.mode, "--json"]
    : [
      payload.multiTurn ? "writer_excute_multiturn.py" : "writer_excute.py",
      "--query",
      payload.query,
      "--mode",
      payload.mode,
      "--writer-model",
      runtimeEnv.WRITER_MODEL || "",
      ...(payload.multiTurn ? [] : ["--print-preview", "0"]),
      "--json",
    ];
  if (payload.multiTurn && payload.resetSession) return [...args, "--session-id", payload.sessionId || "", "--reset-session"];
  if (payload.multiTurn && payload.deleteTurnId) return [...args, "--session-id", payload.sessionId || "", "--delete-turn-id", payload.deleteTurnId];
  if (runtimeEnv.WRITER_BASE_URL) args.push("--writer-base-url", runtimeEnv.WRITER_BASE_URL);
  if (runtimeEnv.WRITER_API_KEY) args.push("--writer-api-key", runtimeEnv.WRITER_API_KEY);
  args.push("--oh-bin", runtimeEnv.OH_BIN || "oh.exe");
  args.push("--openharness-src", runtimeEnv.OPENHARNESS_SRC || "./OpenHarness/src");
  if (payload.ohRealRun || runtimeEnv.OH_REAL_RUN === "1") args.push("--oh-real-run");
  if (runtimeEnv.ACTOR_MODEL) args.push("--actor-model", runtimeEnv.ACTOR_MODEL);
  if (runtimeEnv.ACTOR_BASE_URL) args.push("--actor-base-url", runtimeEnv.ACTOR_BASE_URL);
  if (runtimeEnv.ACTOR_API_KEY) args.push("--actor-api-key", runtimeEnv.ACTOR_API_KEY);
  if (runtimeEnv.ACTOR_API_FORMAT) args.push("--actor-api-format", runtimeEnv.ACTOR_API_FORMAT);
  if (payload.directorHarnessEnabled) args.push("--director-harness-enabled");
  if (runtimeEnv.DIRECTOR_LOG_PATH) args.push("--director-log-path", runtimeEnv.DIRECTOR_LOG_PATH);
  if (!payload.autoExecute) args.push("--skip-execute");
  if (payload.executeOutputFormat) args.push("--execute-output-format", payload.executeOutputFormat);
  if (payload.multiTurn && payload.sessionId) args.push("--session-id", payload.sessionId);
  if (payload.multiTurn && payload.turnId) args.push("--turn-id", payload.turnId);
  if (payload.resetSession) args.push("--reset-session");
  return args;
}

function runWriterMode(payload: WriterRunPayload): Promise<Record<string, unknown>> {
  return new Promise((resolve) => {
    const runtimeEnv = getRuntimeEnv(payload.runtimeSettings, payload.directorHarnessEnabled);
    const args = buildArgs(payload, runtimeEnv);
    const child = spawn(process.env.PYTHON || "python", args, {
      cwd: projectRoot,
      env: {
        ...runtimeEnv,
        PYTHONIOENCODING: "utf-8",
        PYTHONUTF8: "1",
      },
    });
    currentWriterRun.push(child);
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString();
    });
    child.on("close", (code) => {
      currentWriterRun = currentWriterRun.filter((item) => item !== child);
      const debug = {
        mode: payload.mode,
        autoExecute: payload.autoExecute,
        ohRealRun: payload.ohRealRun,
        command: [runtimeEnv.PYTHON || "python", ...args].map((value, index, command) => command[index - 1] === "--writer-api-key" || command[index - 1] === "--actor-api-key" ? "***" : value),
        cwd: projectRoot,
        stderrPreview: previewText(stderr),
        stderrFull: stderr,
        stdoutPreview: previewText(stdout),
        stdoutFull: stdout,
      };
      if (code !== 0) {
        resolve({
          mode: payload.mode,
          ok: false,
          return_code: code,
          stderr,
          stdout,
          error: `${payload.multiTurn ? "writer_excute_multiturn.py" : "writer_excute.py"} ${payload.mode} failed`,
          debug,
        });
        return;
      }
      try {
        const start = stdout.indexOf("{");
        const end = stdout.lastIndexOf("}");
        if (start < 0 || end < start) {
          resolve({
            mode: payload.mode,
            ok: false,
            return_code: code,
            stderr,
            stdout,
            error: "No JSON output found",
            debug,
          });
          return;
        }
        const data = JSON.parse(stdout.slice(start, end + 1)) as Record<string, unknown>;
        data.debug = {
          ...(typeof data.debug === "object" && data.debug !== null ? data.debug as Record<string, unknown> : {}),
          ...debug,
        };
        resolve(data);
      } catch (error) {
        resolve({
          mode: payload.mode,
          ok: false,
          return_code: code,
          stderr,
          stdout,
          error: error instanceof Error ? error.message : String(error),
          debug,
        });
      }
    });
  });
}

function writerHarnessApi(): Plugin {
  return {
    name: "writer-harness-api",
    configureServer(server) {
      server.middlewares.use("/api/writer-harness", (req, res) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.end(JSON.stringify({ error: "Method not allowed" }));
          return;
        }
        let body = "";
        req.setEncoding("utf8");
        req.on("data", (chunk) => {
          body += chunk;
        });
        req.on("end", async () => {
          try {
            const payload = JSON.parse(body || "{}");
            const query = String(payload.query || "").trim();
            if (!query) {
              res.statusCode = 400;
              res.end(JSON.stringify({ error: "query is required" }));
              return;
            }
            const ohRealRun = Boolean(payload.ohRealRun);
            const runtimeSettings = typeof payload.runtimeSettings === "object" && payload.runtimeSettings !== null ? payload.runtimeSettings as Record<string, string> : undefined;
            const missingEnv = collectMissingEnv(ohRealRun, getRuntimeEnv(runtimeSettings));
            if (missingEnv.length) {
              res.statusCode = 400;
              res.setHeader("Content-Type", "application/json; charset=utf-8");
              res.end(JSON.stringify({
                error: `缺少运行所需环境变量：${missingEnv.join(", ")}`,
                debug: {
                  missingEnv,
                  cwd: projectRoot,
                },
              }));
              return;
            }
            res.setHeader("Content-Type", "application/json; charset=utf-8");
            const autoExecute = payload.autoExecute !== false;
            const executeOutputFormat = typeof payload.executeOutputFormat === "string" ? payload.executeOutputFormat : "stream-json";
            const [vanilla, writerHarness] = await Promise.all([
              runWriterMode({ query, mode: "vanilla", autoExecute, executeOutputFormat, ohRealRun, runtimeSettings }),
              runWriterMode({ query, mode: "writer_harness", autoExecute, executeOutputFormat, ohRealRun, runtimeSettings }),
            ]);
            res.end(JSON.stringify({
              ok: Boolean(vanilla.ok) || Boolean(writerHarness.ok),
              query,
              vanilla,
              writer_harness: writerHarness,
            }));
          } catch (error) {
            res.statusCode = 500;
            res.setHeader("Content-Type", "application/json; charset=utf-8");
            res.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }));
          }
        });
      });

      server.middlewares.use("/api/writer-harness-stop", (req, res) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.end(JSON.stringify({ error: "Method not allowed" }));
          return;
        }
        const stopped = currentWriterRun.length > 0;
        currentWriterRun.forEach((child) => child.kill());
        currentWriterRun = [];
        res.setHeader("Content-Type", "application/json; charset=utf-8");
        res.end(JSON.stringify({ ok: true, stopped, message: stopped ? "已发送编导 Harness 终止信号" : "当前没有正在运行的任务" }));
      });

      server.middlewares.use("/api/writer-harness-multiturn", (req, res) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.end(JSON.stringify({ error: "Method not allowed" }));
          return;
        }
        let body = "";
        req.setEncoding("utf8");
        req.on("data", (chunk) => { body += chunk; });
        req.on("end", async () => {
          try {
            const payload = JSON.parse(body || "{}");
            const query = String(payload.query || "").trim();
            const sessionId = String(payload.sessionId || "").trim();
            const turnId = String(payload.turnId || "").trim();
            const mode: WriterMode = payload.mode === "writer_harness" ? "writer_harness" : "vanilla";
            if (!query || !sessionId || !turnId) {
              res.statusCode = 400;
              res.end(JSON.stringify({ error: "query、sessionId 和 turnId 均为必填项" }));
              return;
            }
            const runtimeSettings = typeof payload.runtimeSettings === "object" && payload.runtimeSettings !== null ? payload.runtimeSettings as Record<string, string> : undefined;
            const directorHarnessEnabled = payload.directorHarnessEnabled === true;
            const missingEnv = collectMissingEnv(true, getRuntimeEnv(runtimeSettings, directorHarnessEnabled));
            if (missingEnv.length) {
              res.statusCode = 400;
              res.end(JSON.stringify({ error: `缺少运行所需环境变量：${missingEnv.join(", ")}` }));
              return;
            }
            const result = await runWriterMode({ query, mode, autoExecute: true, ohRealRun: true, executeOutputFormat: typeof payload.executeOutputFormat === "string" ? payload.executeOutputFormat : "stream-json", runtimeSettings, directorHarnessEnabled, sessionId, turnId, multiTurn: true });
            res.setHeader("Content-Type", "application/json; charset=utf-8");
            res.end(JSON.stringify(result));
          } catch (error) {
            res.statusCode = 500;
            res.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }));
          }
        });
      });

      server.middlewares.use("/api/writer-harness-multiturn-reset", (req, res) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.end(JSON.stringify({ error: "Method not allowed" }));
          return;
        }
        let body = "";
        req.setEncoding("utf8");
        req.on("data", (chunk) => { body += chunk; });
        req.on("end", async () => {
          try {
            const payload = JSON.parse(body || "{}");
            const sessionId = String(payload.sessionId || "").trim();
            if (!sessionId) {
              res.statusCode = 400;
              res.end(JSON.stringify({ error: "sessionId 为必填项" }));
              return;
            }
            const runtimeSettings = typeof payload.runtimeSettings === "object" && payload.runtimeSettings !== null ? payload.runtimeSettings as Record<string, string> : undefined;
            const result = await runWriterMode({ query: "", mode: "both", autoExecute: true, ohRealRun: true, runtimeSettings, sessionId, multiTurn: true, resetSession: true });
            res.setHeader("Content-Type", "application/json; charset=utf-8");
            res.end(JSON.stringify(result));
          } catch (error) {
            res.statusCode = 500;
            res.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }));
          }
        });
      });

      server.middlewares.use("/api/writer-harness-multiturn-delete-turn", (req, res) => {
        if (req.method !== "POST") {
          res.statusCode = 405;
          res.end(JSON.stringify({ error: "Method not allowed" }));
          return;
        }
        let body = "";
        req.setEncoding("utf8");
        req.on("data", (chunk) => { body += chunk; });
        req.on("end", async () => {
          try {
            const payload = JSON.parse(body || "{}");
            const sessionId = String(payload.sessionId || "").trim();
            const turnId = String(payload.turnId || "").trim();
            if (!sessionId || !turnId) {
              res.statusCode = 400;
              res.end(JSON.stringify({ error: "sessionId 和 turnId 均为必填项" }));
              return;
            }
            const result = await runWriterMode({ query: "", mode: "both", autoExecute: true, ohRealRun: true, sessionId, turnId, multiTurn: true, deleteTurnId: turnId });
            res.setHeader("Content-Type", "application/json; charset=utf-8");
            res.end(JSON.stringify(result));
          } catch (error) {
            res.statusCode = 500;
            res.end(JSON.stringify({ error: error instanceof Error ? error.message : String(error) }));
          }
        });
      });
    },
  };
}

export default defineConfig(({ mode }) => {
  const rootEnv = loadEnv(mode, projectRoot, "");
  for (const name of ["OPENAI_API_BASE", "OPENAI_API_KEY"] as const) {
    if (rootEnv[name]?.trim()) process.env[name] = rootEnv[name].trim();
  }
  return {
    plugins: [react(), writerHarnessApi()],
    base: "./",
    server: {
      host: "127.0.0.1",
      port: 8090,
      strictPort: true,
    },
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
  };
});

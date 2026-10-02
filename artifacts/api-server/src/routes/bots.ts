import { Router } from "express";
import { db, botsTable } from "@workspace/db";
import { eq, getTableColumns } from "drizzle-orm";
import { spawn, exec, execFile, execSync, type ChildProcess } from "child_process";
import { promisify } from "util";
import path from "path";
import fs from "fs";
import yaml from "js-yaml";
import { fileURLToPath } from "url";
import {
  BOT_DIR,
  BOT_CONFIG_DIR,
  BOT_LOG_DIR,
  BOT_ENV,
  configPath,
  statePath,
  lockPrefix,
} from "../botPaths";

const execAsync = promisify(exec);
const execFileAsync = promisify(execFile);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const router = Router();
const botProcesses: Map<string, ChildProcess> = new Map();

// Символы, которые ОПЕРАТОР хочет держать запущенными (Start / стартовый
// авто-рестарт) — в отличие от ручного Stop/Kill. Периодический авто-рестарт
// (restartDesiredBots) поднимает только их: падение по сети/самостопу лечится
// автоматически, а осознанная остановка не отменяется.
const desiredRunning = new Set<string>();

/**
 * Обновляет config_<symbol>.yaml через отдельный Python-процесс.
 * Node.js не может импортировать .py файлы как модули — update_yaml_config()
 * живёт в bot/config.py и вызывается здесь через CLI-обёртку
 * bot/update_config_cli.py, которой параметры передаются как JSON через stdin
 * (тот же паттерн, что уже используется для backtest_runner.py).
 */
function updateYamlConfig(symbol: string, params: Record<string, unknown>): Promise<void> {
  return new Promise((resolve, reject) => {
    const proc = spawn("python", ["update_config_cli.py", symbol, BOT_CONFIG_DIR], { cwd: BOT_DIR, windowsHide: true });

    let output = "";
    let error = "";
    proc.stdout.on("data", (d) => (output += d));
    proc.stderr.on("data", (d) => (error += d));

    proc.on("close", () => {
      try {
        const parsed = JSON.parse(output.trim());
        if (parsed.error) {
          reject(new Error(parsed.error));
        } else {
          resolve();
        }
      } catch {
        reject(new Error(error || "update_config_cli.py produced no parseable output"));
      }
    });

    proc.on("error", (err) => reject(err));

    proc.stdin.write(JSON.stringify(params));
    proc.stdin.end();
  });
}

// Кэш перебора процессов: один PowerShell на пачку вызовов.
// Без него каждый findBotPid — отдельный O(все процессы) вызов PowerShell,
// из-за чего старт/авто-рестарт 40 ботов длился минуты.
let botPidCache: { at: number; map: Map<string, number> } | null = null;

// Резолвнутый интерпретатор python с зависимостями бота (кэш на процесс API).
let cachedPythonCmd: string | null = null;

async function getAllBotPidsCached(ttlMs = 3000): Promise<Map<string, number> | null> {
  const now = Date.now();
  if (botPidCache && now - botPidCache.at < ttlMs) return botPidCache.map;
  const map = await tryFindAllBotPids();
  if (map !== null) botPidCache = { at: now, map };
  return map;
}

// Найти PID процесса бота по имени конфига (Windows + Linux)
async function findBotPid(symbol: string): Promise<number | null> {
  const m = await getAllBotPidsCached();
  if (m === null) return null;
  return m.get(symbol.toUpperCase()) ?? null;
}

/**
 * Возвращает Map<symbol, pid> для всех запущенных ботов текущего окружения.
 * `null` означает, что список процессов получить НЕ удалось (в отличие от
 * пустой map = «ботов нет»). Это важно для reconcile: при сбое опроса мы не
 * должны ложно помечать живых ботов остановленными.
 */
export async function tryFindAllBotPids(): Promise<Map<string, number> | null> {
  const pidBySymbol = new Map<string, number>();
  const consider = (cmd: string, pid: number) => {
    if (!cmd.includes("main.py")) return;
    if (!cmd.includes(BOT_CONFIG_DIR)) return;
    const m = cmd.match(/config_([a-z0-9]+)\.yaml/);
    if (!m) return;
    if (!Number.isFinite(pid) || pid <= 0) return;
    pidBySymbol.set(m[1].toUpperCase() + "USDT", pid);
  };
  try {
    if (process.platform === "win32") {
      const { stdout } = await execAsync(
        `powershell -Command "Get-CimInstance -ClassName Win32_Process -Filter \\\"Name='python.exe'\\\" | Select-Object ProcessId,CommandLine | ConvertTo-Json"`,
        { windowsHide: true },
      );
      // Пустой stdout = процессов нет (валидная пустая map). Неразобранный
      // непустой stdout = сбой опроса: возвращаем null (unknown), чтобы
      // reconcile не пометил живых ботов остановленными.
      const text = stdout.trim();
      let raw: unknown = null;
      if (text) {
        try { raw = JSON.parse(text); } catch { return null; }
      }
      const processes = Array.isArray(raw) ? raw : raw ? [raw] : [];
      for (const proc of processes as Array<Record<string, unknown>>) {
        consider((proc.CommandLine as string) || "", parseInt(String(proc.ProcessId)));
      }
    } else {
      const { stdout } = await execAsync("ps -eo pid=,args=", { windowsHide: true });
      for (const line of stdout.split("\n")) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        const pid = parseInt(trimmed.split(/\s+/)[0]);
        consider(trimmed, pid);
      }
    }
    return pidBySymbol;
  } catch {
    return null;
  }
}

async function findAllBotPids(): Promise<Map<string, number>> {
  return (await tryFindAllBotPids()) ?? new Map<string, number>();
}

/**
 * Периодически сверяет is_running в БД с реально живыми процессами ботов.
 * Если процесс исчез (упал/самоостановился, а событие exit было потеряно —
 * например, при рестарте API), бот помечается остановленным и снимается
 * stop_requested, чтобы дашборд снова дал нажать Start. Источник правды —
 * список процессов: при неудачном опросе (`null`) ничего не меняем, чтобы не
 * пометить живых ботов остановленными ложно.
 *
 * ВАЖНО: наличие процесса важнее свежести heartbeat — при временной пропаже
 * данных (WS/REST) бот остаётся жив и не шлёт heartbeat, но останавливать его
 * в дашборде нельзя.
 */
export async function reconcileBotRunningStates(): Promise<string[]> {
  const stopped: string[] = [];
  const alive = await tryFindAllBotPids();
  if (alive === null) return stopped;
  try {
    const bots = await db.select().from(botsTable);
    for (const bot of bots) {
      if (!bot.is_running) {
        // Уже остановлен: снимаем залипший stop_requested, чтобы UI не показывал STOPPING.
        if (bot.stop_requested) {
          await db.update(botsTable)
            .set({ stop_requested: false, updated_at: new Date().toISOString() })
            .where(eq(botsTable.symbol, bot.symbol));
        }
        continue;
      }
      if (alive.has(bot.symbol)) continue;
      await db.update(botsTable)
        .set({
          is_running: false,
          stop_requested: false,
          stop_reason: bot.stop_reason || "process_exited",
          updated_at: new Date().toISOString(),
        })
        .where(eq(botsTable.symbol, bot.symbol));
      stopped.push(bot.symbol);
      console.log(`[reconcile] ${bot.symbol}: no live process — marked stopped`);
    }
  } catch (e) {
    console.warn(`[reconcile] failed: ${String(e)}`);
  }
  return stopped;
}

function autoRestartEnabled(): boolean {
  return String(process.env.AUTO_RESTART_BOTS ?? "true").toLowerCase() !== "false";
}

// Когда и сколько раз авто-поднимали символ (анти-цикл для вечно падающих).
const autoRestartHistory = new Map<string, number[]>();

/**
 * Периодический авто-рестарт: поднимает только символы из desiredRunning
 * (их оператор запускал и не останавливал), если процесс умер. Троттлинг на
 * символ (интервал + лимит попыток в окне), чтобы не было цикла рестартов.
 * Уважает AUTO_RESTART_BOTS (в live он false).
 */
export async function restartDesiredBots(): Promise<string[]> {
  const restarted: string[] = [];
  if (!autoRestartEnabled() || desiredRunning.size === 0) return restarted;
  const alive = await tryFindAllBotPids();
  if (alive === null) return restarted; // не смогли опросить процессы — не рискуем
  const now = Date.now();
  const cooldown = Number(process.env.AUTO_RESTART_DEAD_COOLDOWN_MS ?? 90_000);
  const windowMs = Number(process.env.AUTO_RESTART_WINDOW_MS ?? 1_800_000);
  const maxAttempts = Number(process.env.AUTO_RESTART_MAX_ATTEMPTS ?? 5);
  for (const symbol of Array.from(desiredRunning)) {
    try {
      if (alive.has(symbol)) continue;
      const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
      if (!bot) { desiredRunning.delete(symbol); continue; }
      if (!bot.armed || bot.stop_requested) continue;
      const hist = (autoRestartHistory.get(symbol) ?? []).filter(t => now - t < windowMs);
      if (hist.length && now - hist[hist.length - 1] < cooldown) {
        autoRestartHistory.set(symbol, hist);
        continue;
      }
      if (hist.length >= maxAttempts) {
        autoRestartHistory.set(symbol, hist);
        console.warn(`[auto-restart] ${symbol}: throttled (${hist.length} attempts in window)`);
        continue;
      }
      hist.push(now);
      autoRestartHistory.set(symbol, hist);
      const r = await startBotProcess(symbol);
      if (r.ok) {
        restarted.push(symbol);
        console.log(`[auto-restart] ${symbol}: dead → restarted (pid=${r.pid})`);
      } else {
        console.warn(`[auto-restart] ${symbol}: restart failed — ${r.message}`);
      }
    } catch (e) {
      console.warn(`[auto-restart] ${symbol}: error ${String(e)}`);
    }
  }
  return restarted;
}

// Убить процесс по PID
async function killPid(pid: number): Promise<void> {
  try {
    if (process.platform === "win32") {
      await execAsync(`taskkill /PID ${pid} /F`, { windowsHide: true });
    } else {
      process.kill(pid, "SIGKILL");
    }
  } catch (e) {
    // процесс уже завершён
  }
}

router.get("/", async (_req, res) => {
  try {
    const bots = await db.select().from(botsTable);
    res.json(bots.map(b => ({
      ...b,
      position: b.position ? JSON.parse(b.position as string) : null,
      llm_status: b.llm_status ? JSON.parse(b.llm_status as string) : null,
    })));
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.get("/:symbol", async (req, res) => {
  try {
    const [bot] = await db.select().from(botsTable)
      .where(eq(botsTable.symbol, req.params.symbol.toUpperCase()));
    if (!bot) return res.status(404).json({ error: "Bot not found" });
    res.json({
      ...bot,
      position: bot.position ? JSON.parse(bot.position as string) : null,
      llm_status: bot.llm_status ? JSON.parse(bot.llm_status as string) : null,
    });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.put("/:symbol/config", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const configUpdates = { ...req.body };
    delete configUpdates.updated_at;
    delete configUpdates.is_running;
    delete configUpdates.last_heartbeat;
    delete configUpdates.current_price;
    delete configUpdates.position;
    delete configUpdates.llm_status;
    delete configUpdates.symbol;

    // В БД пишем только реальные колонки: конфиг может содержать поля, которых
    // в таблице нет (они уходят в YAML через updateYamlConfig). Без фильтра
    // drizzle сгенерировал бы UPDATE с несуществующей колонкой.
    const tableCols = getTableColumns(botsTable) as Record<string, unknown>;
    const dbUpdates: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(req.body)) {
      if (k in tableCols) dbUpdates[k] = v;
    }

    const [updated] = await db.update(botsTable)
      .set({ ...dbUpdates, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol)).returning();
    if (!updated) return res.status(404).json({ error: "Bot not found" });

    // Синхронизируем изменения в config_<symbol>.yaml на диске — это то,
    // что реально читает Python-бот при запуске, БД для него не источник
    // правды. Если запись в YAML провалится — сообщаем об этом явно,
    // вместо того чтобы вернуть успех при несинхронизированном состоянии.
    try {
      await updateYamlConfig(symbol, configUpdates);
    } catch (yamlErr) {
      return res.status(500).json({
        error: `DB updated, but failed to write config_*.yaml: ${yamlErr}`,
        dbUpdated: updated,
      });
    }

    res.json(updated);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.patch("/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const body = { ...req.body };
    if (body.position && typeof body.position === "object") {
      body.position = JSON.stringify(body.position);
    }
    if (body.llm_status && typeof body.llm_status === "object") {
      body.llm_status = JSON.stringify(body.llm_status);
    }
    const [updated] = await db.update(botsTable)
      .set({ ...body, last_heartbeat: new Date().toISOString(), updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol)).returning();
    if (!updated) return res.status(404).json({ error: "Bot not found" });
    res.json(updated);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.post("/refresh", async (_req, res) => {
  try {
    await reloadConfigsFromYaml();
    for (const symbol of Array.from(botProcesses.keys())) {
      const proc = botProcesses.get(symbol);
      if (proc?.killed) botProcesses.delete(symbol);
    }
    const configs = fs.readdirSync(BOT_CONFIG_DIR).filter((f: string) => /^config_\w+\.yaml$/.test(f));
    for (const file of configs) {
      const symbol = (file.replace("config_", "").replace(".yaml", "").toUpperCase() + "USDT");
      const pid = await findBotPid(symbol);
      await db.update(botsTable).set({
        is_running: !!pid,
        last_heartbeat: pid ? new Date().toISOString() : null,
      }).where(eq(botsTable.symbol, symbol));
    }
    const bots = await db.select().from(botsTable);
    res.json({ refreshed: bots.length, bots: bots.map(b => ({ symbol: b.symbol, is_running: b.is_running })) });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

export type StartBotResult = {
  ok: boolean;
  message?: string;
  pid?: number;
  /** Если задано — роут обязан ответить 500 { error } (Python не найден и т.п.). */
  error?: string;
};

/**
 * Запускает Python-бота для символа. Процесс отвязывается от api-server
 * (`detached` + `unref`) и пишет stdio в файл `logs/api_<symbol>_stdout.log`,
 * чтобы рестарт/падение api-server не рвало pipe и не убивало бота.
 * Используется роутом POST /bots/:symbol/start и авто-рестартом на старте
 * (index.ts -> autoRestartBots).
 */
export async function startBotProcess(symbol: string): Promise<StartBotResult> {
  // Проверяем не запущен ли уже (через Map или через поиск PID).
  // ВАЖНО: exitCode !== null означает, что процесс уже завершился, даже если
  // событие exit не дошло до нас (например, при рестарте API). Такой «зомби»
  // в botProcesses не должен блокировать ручной Start.
  const existingProc = botProcesses.get(symbol);
  if (existingProc && !existingProc.killed && existingProc.exitCode === null) {
    return { ok: false, message: "Bot already running (dashboard)" };
  }
  if (existingProc) {
    botProcesses.delete(symbol);
  }
  const existingPid = await findBotPid(symbol);
  if (existingPid) {
    // Процесс уже жив, но API по какой-то причине считает бота остановленным.
    // Не отказываем во «Start»: синхронизируем БД и считаем запуск успешным,
    // чтобы кнопка в дашборде приводила к согласованному состоянию.
    await db.update(botsTable)
      .set({ is_running: true, stop_reason: null, stop_requested: false, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol));
    return { ok: true, message: `Bot ${symbol} already running (PID ${existingPid}) — adopted`, pid: existingPid };
  }

  // Find Python executable (результат кэшируется: проверка hasDeps — это
  // execSync с импортом pandas+binance, ~1с, и без кэша платили её на КАЖДЫЙ
  // старт бота, из-за чего массовый авто-рестарт занимал минуты).
  if (!cachedPythonCmd) {
    let pythonCmd = process.env.BOT_PYTHON || process.env.PYTHON || "";
    if (!pythonCmd) {
      if (process.platform === 'win32') {
        pythonCmd = 'python.exe';
      } else {
        pythonCmd = 'python3';
      }
    }
    // Verify the chosen python actually has the bot dependencies. If not, search
    // for one that does, because the system default may point to a pip-user/env
    // install without pandas.
    const hasDeps = (cand: string): boolean => {
      try {
        execSync(`"${cand}" -c "import pandas, binance"`, { stdio: 'pipe', windowsHide: true });
        return true;
      } catch {
        return false;
      }
    };
    if (!hasDeps(pythonCmd)) {
      // Try common explicit paths for the Python that has the deps installed.
      const candidates = [
        process.env.BOT_PYTHON,
        'C:/Users/osdal/AppData/Local/Programs/Python/Python311/python.exe',
        'C:/Python311/python.exe',
        'python',
        'python3',
      ].filter(Boolean) as string[];
      const found = candidates.find((c) => hasDeps(c));
      if (!found) {
        return { ok: false, error: "Python with bot dependencies (pandas, python-binance) not found. Check BOT_PYTHON in .env." };
      }
      pythonCmd = found;
    }
    cachedPythonCmd = pythonCmd;
  }
  const pythonCmd = cachedPythonCmd;

  const botTag = `[BOT ${symbol}]`;
  const debugLogPath = path.join(BOT_LOG_DIR, `api_${symbol.toLowerCase()}.log`);
  const debugWrite = (msg: string) => {
    console.log(msg);
    try {
      fs.appendFileSync(debugLogPath, `${new Date().toISOString()} ${msg}\n`);
    } catch { /* ignore */ }
  };

  // stdio ребёнка направляем в файл: если api-server умрёт, закрытый pipe
  // не сломает отвязанный процесс (нет EPIPE/BrokenPipe).
  fs.mkdirSync(BOT_LOG_DIR, { recursive: true });
  const outFd = fs.openSync(
    path.join(BOT_LOG_DIR, `api_${symbol.toLowerCase()}_stdout.log`),
    "a",
  );

  let proc: ChildProcess;
  try {
    proc = spawn(pythonCmd, ["main.py", configPath(symbol)], {
      cwd: BOT_DIR,
      detached: true,
      stdio: ["ignore", outFd, outFd],
      windowsHide: true,
      env: process.env,
    });
  } catch (err) {
    try { fs.closeSync(outFd); } catch { /* ignore */ }
    return { ok: false, error: `Failed to spawn bot process: ${String(err)}` };
  }
  botProcesses.set(symbol, proc);
  // Отвязываем дочерний процесс: он должен пережить рестарт api-server.
  proc.unref();

  proc.on("error", (err) => {
    const msg = `${botTag} spawn error: ${err.message}`;
    debugWrite(msg);
    try { fs.closeSync(outFd); } catch { /* ignore */ }
    botProcesses.delete(symbol);
    db.update(botsTable)
      .set({ is_running: false, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol))
      .catch(() => {});
  });
  proc.on("spawn", () => {
    debugWrite(`${botTag} process spawned (pid=${proc.pid})`);
  });
  // stdio больше не pipe, поэтому данные обработчики не срабатывают; оставлены
  // безвредными (optional chaining) на случай смены режима stdio. Дебаг-трейл
  // теперь идёт из spawn/error/exit.
  proc.stdout?.on("data", (d) => {
    const lines = d.toString().trim().split("\n");
    for (const line of lines) {
      if (line.trim()) debugWrite(`${botTag} [stdout] ${line}`);
    }
  });
  proc.stderr?.on("data", (d) => {
    const lines = d.toString().trim().split("\n");
    for (const line of lines) {
      if (line.trim()) debugWrite(`${botTag} ${line}`);
    }
  });
  proc.on("exit", async (code, signal) => {
    debugWrite(`${botTag} exited (code=${code} signal=${signal})`);
    try { fs.closeSync(outFd); } catch { /* ignore */ }
    botProcesses.delete(symbol);
    await db.update(botsTable)
      .set({ is_running: false, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol))
      .catch(() => {});
  });

  await db.update(botsTable)
    .set({ is_running: true, stop_reason: null, stop_requested: false, updated_at: new Date().toISOString() })
    .where(eq(botsTable.symbol, symbol));
  desiredRunning.add(symbol);

  return { ok: true, message: `Bot ${symbol} started`, pid: proc.pid };
}

router.post("/:symbol/start", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
    if (!bot) return res.status(404).json({ error: "Bot not found" });

    const result = await startBotProcess(symbol);
    if (!result.ok) {
      if (result.error) return res.status(500).json({ error: result.error });
      return res.json({ success: false, message: result.message });
    }
    return res.json({ success: true, message: result.message });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// Arm/disarm: разрешение боту открывать позиции (используется в live).
router.post("/:symbol/arm", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const [updated] = await db.update(botsTable)
      .set({ armed: true, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol)).returning();
    if (!updated) return res.status(404).json({ error: "Bot not found" });
    res.json({ success: true, symbol, armed: true });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.post("/:symbol/disarm", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const [updated] = await db.update(botsTable)
      .set({ armed: false, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol)).returning();
    if (!updated) return res.status(404).json({ error: "Bot not found" });
    res.json({ success: true, symbol, armed: false });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

/**
 * Останавливает ВСЕ боты текущего окружения: и запущенные нами (botProcesses),
 * и « stray»-процессы, найденные по командной строке. Кроссплатформенно:
 * taskkill на Windows, process.kill(SIGKILL) на Linux (в Docker ps-путей нет).
 *
 * Также чистит desiredRunning — иначе restartDesiredBots (раз в 30 с) поднимет
 * только что остановленных ботов обратно, и «Stop All» будет выглядеть как
 * мгновенный рестарт. Возвращает список остановленных символов.
 */
export async function stopAllBotProcesses(): Promise<string[]> {
  const stoppedBots: string[] = [];

  // 1. Kill bots we spawned and track (dashboard-managed).
  for (const [symbol, proc] of botProcesses) {
    if (!proc.killed) {
      proc.kill();
      stoppedBots.push(symbol);
    }
  }
  botProcesses.clear();
  desiredRunning.clear();

  // 2. Kill any stray bot processes (not tracked) by their config file,
  //    but ONLY python bot processes — never a global taskkill of all python
  //    (that can kill the API/dashboard dev chain on Windows).
  //    Один вызов PowerShell забирает PID всех ботов, дальше убиваем параллельно.
  const strayPids = await findAllBotPids();
  await Promise.all(Array.from(strayPids.entries()).map(async ([symbol, pid]) => {
    const existed = botProcesses.get(symbol);
    try {
      if (process.platform === "win32") {
        await execAsync(`taskkill /PID ${pid} /F`, { windowsHide: true });
      } else {
        process.kill(pid, "SIGKILL");
      }
      if (!existed && !stoppedBots.includes(symbol)) stoppedBots.push(symbol);
    } catch { /* already gone */ }
  }));

  // 3. Clear lock files (after stopping bots).
  const lockFiles = fs.readdirSync(BOT_DIR).filter(f => f.startsWith(lockPrefix()));
  for (const lockFile of lockFiles) {
    try { fs.unlinkSync(path.join(BOT_DIR, lockFile)); } catch { /* ignore */ }
  }

  return stoppedBots;
}

router.post("/stop-all", async (_req, res) => {  try {
    const stoppedBots = await stopAllBotProcesses();
    res.json({ success: true, message: `Stopped ${stoppedBots.length} bots`, bots: stoppedBots });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// Мягкая остановка: бот перестаёт открывать новые позиции, доводит текущую по
// своей логике (TP/SL/reverse) и сам выходит, когда станет флэт.
router.post("/:symbol/stop", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
    if (!bot) return res.status(404).json({ error: "Bot not found" });

    const proc = botProcesses.get(symbol);
    const pid = proc && !proc.killed ? proc.pid : await findBotPid(symbol);
    desiredRunning.delete(symbol);
    if (!pid) {
      await db.update(botsTable)
        .set({ is_running: false, stop_requested: false, position: null, updated_at: new Date().toISOString() })
        .where(eq(botsTable.symbol, symbol));
      return res.json({ success: false, message: "Bot not running" });
    }

    await db.update(botsTable)
      .set({ stop_requested: true, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol));
    res.json({
      success: true,
      message: `Graceful stop requested for ${symbol} — бот доведёт позицию и выйдет`,
    });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// Жёсткое убийство процесса (экстренная кнопка Kill). Позиция/ордера на бирже
// остаются как есть — закрывать их нужно через kill-switch / вручную.
router.post("/:symbol/kill", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();

    const proc = botProcesses.get(symbol);
    if (proc) {
      if (!proc.killed) {
        if (process.platform === "win32") {
          proc.kill();
        } else {
          proc.kill("SIGTERM");
        }
        await new Promise(resolve => setTimeout(resolve, 3000));
        if (!proc.killed) {
          if (process.platform === "win32") {
            proc.kill();
          } else {
            proc.kill("SIGKILL");
          }
        }
      }
      botProcesses.delete(symbol);
    }

    const pid = await findBotPid(symbol);
    if (pid) {
      await killPid(pid);
    }

    if (!proc && !pid) {
      return res.json({ success: false, message: "Bot not running" });
    }

    // State file is intentionally kept: a later Start restores the saved SL/TP
    // from state_<symbol>.json instead of recalculating from config.
    const stateFile = statePath(symbol);
    if (fs.existsSync(stateFile)) {
      console.log(`[KILL] Keeping state file for restore: ${stateFile}`);
    }

    desiredRunning.delete(symbol);
    await db.update(botsTable)
      .set({ is_running: false, position: null, stop_requested: false, updated_at: new Date().toISOString() })
      .where(eq(botsTable.symbol, symbol));

    res.json({ success: true, message: `Bot ${symbol} killed` });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// DELETE /bots/:symbol — удалить бота из БД
router.delete("/:symbol", async (req, res) => {
  try {
    const symbol = req.params.symbol.toUpperCase();
    const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
    if (!bot) return res.status(404).json({ error: "Bot not found" });
    
    // Stop if running
    const proc = botProcesses.get(symbol);
    if (proc && !proc.killed) { proc.kill(); }
    botProcesses.delete(symbol);
    desiredRunning.delete(symbol);
    
    await db.delete(botsTable).where(eq(botsTable.symbol, symbol));
    res.json({ success: true, message: `Bot ${symbol} deleted` });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

export async function reloadConfigsFromYaml(): Promise<void> {
  if (!fs.existsSync(BOT_CONFIG_DIR)) {
    throw new Error(`Bot config directory not found: ${BOT_CONFIG_DIR}`);
  }
  const configs = fs.readdirSync(BOT_CONFIG_DIR).filter((f: string) => /^config_\w+\.yaml$/.test(f) && f !== "config.yaml");
  
  for (const file of configs) {
    const raw = yaml.load(fs.readFileSync(path.join(BOT_CONFIG_DIR, file), "utf8")) as Record<string, unknown>;
    const symbol = (raw.symbol as string).toUpperCase();
    const [existing] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
    const values = {
      mode:             (raw.mode as string) || "live",
      timeframe:        raw.timeframe as string,
      leverage:         raw.leverage as number,
      risk_pct:         raw.risk_pct as number,
      sl_pct:           raw.sl_pct as number,
      tp1_pct:          raw.tp1_pct as number,
      tp1_close_pct:    raw.tp1_close_pct as number,
      tp2_pct:          raw.tp2_pct as number,
      ema_fast:         raw.ema_fast as number,
      ema_slow:         raw.ema_slow as number,
      volume_ma_period: raw.volume_ma_period as number,
      volume_multiplier: raw.volume_multiplier as number,
      htf_enabled:      (raw.htf_enabled as boolean) || false,
      htf_timeframe:    (raw.htf_timeframe as string) || null,
      htf_ema_fast:     (raw.htf_ema_fast as number) || null,
      htf_ema_slow:     (raw.htf_ema_slow as number) || null,
      htf2_enabled:     (raw.htf2_enabled as boolean) || false,
      htf2_timeframe:   (raw.htf2_timeframe as string) || null,
      htf2_ema_fast:    (raw.htf2_ema_fast as number) || null,
      htf2_ema_slow:    (raw.htf2_ema_slow as number) || null,
      auto_mode:        (raw.auto_mode as boolean) ?? true,
      paper_balance:    (raw.paper_balance as number) || 1000,
      log_file:         raw.log_file as string,
      trade_mode:       (raw.trade_mode as string) || "manual",
      position_size_usd: (raw.position_size_usd as number) || 0,
      position_size_pct: (raw.position_size_pct as number) ?? 1,
      reverse_chain_max: (raw.reverse_chain_max as number) ?? 10,
      max_position_notional_usd: (raw.max_position_notional_usd as number) || 0,
      max_position_pct_equity: (raw.max_position_pct_equity as number) || 0,
      updated_at:       new Date().toISOString(),
    };
    if (existing) {
      await db.update(botsTable).set(values).where(eq(botsTable.symbol, symbol));
    } else {
      await db.insert(botsTable).values({ symbol, is_running: false, position: null, armed: BOT_ENV !== "live", relay_only: BOT_ENV === "live", ...values });
    }
  }
}

/**
 * Авто-рестарт ботов, которые были помечены is_running=1, но чей процесс не
 * выжил (список собирает resetStaleRunningBots в index.ts). Вызывается один раз
 * на старте api-server, ДО app.listen. Ограничено: не более 3 попыток на символ,
 * каждая попытка логируется, функция никогда не бросает.
 * Выключается через AUTO_RESTART_BOTS=false (по умолчанию включено).
 */
export async function autoRestartBots(symbols: string[]): Promise<void> {
  const flag = (process.env.AUTO_RESTART_BOTS ?? "true").trim().toLowerCase();
  if (flag === "false") {
    if (symbols && symbols.length > 0) {
      console.log(`[auto-restart] AUTO_RESTART_BOTS=false — пропуск ${symbols.length} бот(ов): ${symbols.join(", ")}`);
    }
    return;
  }
  if (!symbols || symbols.length === 0) return;

  const MAX_ATTEMPTS = 3;
  for (const symbol of symbols) {
    // Не поднимаем бота, у которого взведена мягкая остановка: иначе рестарт
    // API молча отменяет Stop и бот снова начинает торговать.
    try {
      const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
      if (bot?.stop_requested) {
        console.log(`[auto-restart] ${symbol}: stop_requested=1 — пропуск (мягкая остановка)`);
        await db.update(botsTable)
          .set({ stop_requested: false, updated_at: new Date().toISOString() })
          .where(eq(botsTable.symbol, symbol));
        continue;
      }
    } catch (e) {
      console.warn(`[auto-restart] ${symbol}: stop_requested check failed — ${String(e)}`);
    }
    let started = false;
    for (let attempt = 1; attempt <= MAX_ATTEMPTS && !started; attempt++) {
      try {
        console.log(`[auto-restart] ${symbol}: attempt ${attempt}/${MAX_ATTEMPTS}`);
        const result = await startBotProcess(symbol);
        if (result.ok) {
          started = true;
          console.log(`[auto-restart] ${symbol}: started (pid=${result.pid})`);
        } else {
          console.warn(`[auto-restart] ${symbol}: not started — ${result.error || result.message}`);
        }
      } catch (e) {
        console.warn(`[auto-restart] ${symbol}: attempt ${attempt} threw — ${String(e)}`);
      }
      if (!started && attempt < MAX_ATTEMPTS) {
        await new Promise((r) => setTimeout(r, 2000));
      }
    }
    if (!started) {
      console.error(`[auto-restart] ${symbol}: gave up after ${MAX_ATTEMPTS} attempts`);
    }
  }
}

export async function stopAllBots(): Promise<void> {
  const symbols = Array.from(botProcesses.keys());
  // SIGTERM first (Windows: use kill() without signal)
  for (const symbol of symbols) {
    const proc = botProcesses.get(symbol);
    if (proc && !proc.killed) {
      if (process.platform === "win32") {
        proc.kill();
      } else {
        proc.kill("SIGTERM");
      }
    }
    botProcesses.delete(symbol);
  }
  // Wait for SIGTERM to take effect
  await new Promise(resolve => setTimeout(resolve, 3000));
  // Force kill any survivors and clean up state files
  for (const symbol of symbols) {
    const proc = botProcesses.get(symbol);
    if (proc && !proc.killed) {
      if (process.platform === "win32") {
        proc.kill();
      } else {
        proc.kill("SIGKILL");
      }
    }
    const stateFile = statePath(symbol);
    try { if (fs.existsSync(stateFile)) fs.unlinkSync(stateFile); } catch {}
  }
  const configFiles = fs.readdirSync(BOT_CONFIG_DIR).filter((f: string) => /^config_\w+\.yaml$/.test(f));
  for (const file of configFiles) {
    const symbol = file.replace("config_", "").replace(".yaml", "").toUpperCase() + "USDT";
    const pid = await findBotPid(symbol);
    if (pid) {
      try { if (process.platform === "win32") { await execAsync(`taskkill /PID ${pid} /F`, { windowsHide: true }); } else { process.kill(pid, "SIGKILL"); } } catch {}
    }
  }
  await db.update(botsTable).set({ is_running: false, position: null, updated_at: new Date().toISOString() });
}

export default router;

import { Router } from "express";
import { db, tradesTable, tradeLegsTable } from "@workspace/db";
import { and, eq, desc, sql, isNull } from "drizzle-orm";
import { botsTable } from "@workspace/db/schema";
import { chargeTradeCommission } from "../lib/clientAccount";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const router = Router();

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Корень проекта: src/api-server/src/routes/../../../../ = replit_scalper/
const PROJECT_ROOT = path.resolve(__dirname, "../../../..");
// Папка для отчётов Clear DB: bot/logs/analytics/ (вложена в logs).
const BACKUP_DIR = path.join(PROJECT_ROOT, "bot", "logs", "analytics");

// Заголовки CSV — все поля таблицы trades (для полного анализа).
const TRADE_CSV_COLS = [
  "id","symbol","direction","entry_price","exit_price","qty","sl_price","tp1_price","tp2_price",
  "pnl","exit_reason","entry_time","exit_time","is_open","ema_fast","ema_slow","volume","volume_ma",
  "mode","status","reject_reason","rsi","macd","macd_signal","macd_hist","bb_upper","bb_middle",
  "bb_lower","atr","preset","commission","quote_volume",
];

function csvEscape(v: unknown): string {
  if (v === null || v === undefined) return "";
  const s = String(v);
  if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function timestampDir(d: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}_${p(d.getHours())}-${p(d.getMinutes())}-${p(d.getSeconds())}`;
}

/** Строит компактный ANALYTICS.md из всех сделок (open/closed/rejected). */
function buildAnalytics(rows: Record<string, any>[]): string {
  const closes = rows.filter(r => r.status === "closed");
  const rejected = rows.filter(r => r.status === "rejected");
  const open = rows.filter(r => r.status === "open");

  const pnl = (r: any) => { const v = Number(r.pnl); return Number.isNaN(v) ? 0 : v; };
  const wins = closes.filter(r => pnl(r) > 0).length;
  const losses = closes.filter(r => pnl(r) < 0).length;
  const tp = closes.filter(r => (r.exit_reason || "").includes("TP")).length;
  const sl = closes.filter(r => (r.exit_reason || "").includes("SL")).length;
  const totalPnl = closes.reduce((s, r) => s + pnl(r), 0);
  const winRate = closes.length ? (wins / closes.length) * 100 : 0;

  // by reason
  const reasonMap = new Map<string, number>();
  for (const r of rejected) {
    const k = r.reject_reason || "rejected";
    reasonMap.set(k, (reasonMap.get(k) || 0) + 1);
  }

  // by preset (closed only)
  const presetMap = new Map<string, { n: number; pnl: number; wins: number; tp: number; sl: number }>();
  for (const r of closes) {
    const k = r.preset || "unknown";
    const e = presetMap.get(k) || { n: 0, pnl: 0, wins: 0, tp: 0, sl: 0 };
    e.n++;
    e.pnl += pnl(r);
    if (pnl(r) > 0) e.wins++;
    if ((r.exit_reason || "").includes("TP")) e.tp++;
    if ((r.exit_reason || "").includes("SL")) e.sl++;
    presetMap.set(k, e);
  }

  const lines: string[] = [];
  lines.push(`## Analysis Backup — ${new Date().toISOString()}`);
  lines.push("");
  lines.push("### Overall");
  lines.push("");
  lines.push("| Metric | Value |");
  lines.push("|---|---|");
  lines.push(`| Total Opens | ${rows.length} |`);
  lines.push(`| Open | ${open.length} |`);
  lines.push(`| Closed | ${closes.length} |`);
  lines.push(`| Rejected | ${rejected.length} |`);
  lines.push(`| Win Rate (closed) | ${winRate.toFixed(2)}% |`);
  lines.push(`| Total PnL (net closed) | ${totalPnl.toFixed(2)} |`);
  lines.push(`| TP Closes | ${tp} |`);
  lines.push(`| SL Closes | ${sl} |`);
  lines.push("");
  lines.push("### Rejected by reason");
  lines.push("");
  lines.push("| Reason | Count |");
  lines.push("|---|---|");
  for (const [k, v] of [...reasonMap.entries()].sort((a, b) => b[1] - a[1])) {
    lines.push(`| ${k} | ${v} |`);
  }
  lines.push("");
  lines.push("### Closed by preset");
  lines.push("");
  lines.push("| Preset | Trades | Wins | WR% | TP | SL | PnL |");
  lines.push("|---|---|---|---|---|---|---|");
  for (const [k, e] of [...presetMap.entries()].sort((a, b) => b[1].n - a[1].n)) {
    const wrPer = e.n ? (e.wins / e.n) * 100 : 0;
    lines.push(`| ${k} | ${e.n} | ${e.wins} | ${wrPer.toFixed(1)}% | ${e.tp} | ${e.sl} | ${e.pnl.toFixed(2)} |`);
  }
  lines.push("");
  lines.push("---");
  return lines.join("\n");
}

/** Все строки trades как плоские объекты (все колонки). */
async function loadAllTrades(): Promise<Record<string, any>[]> {
  const rows = await db.select().from(tradesTable);
  return rows as unknown as Record<string, any>[];
}

function toCsv(rows: Record<string, any>[]): string {
  const cols = Object.keys(rows[0] ?? {}).filter(c => TRADE_CSV_COLS.includes(c));
  const header = TRADE_CSV_COLS.join(",");
  const body = rows.map(r =>
    TRADE_CSV_COLS.map(c => csvEscape(r[c])).join(",")
  ).join("\n");
  return header + "\n" + body;
}

// DELETE /trades — удалить все сделки (предварительно сохраняя ANALYTICS.md + trades.csv в backup)
router.delete("/", async (_req, res) => {
  try {
    // 1. Считываем текущие сделки ДО удаления.
    const rows = await loadAllTrades();

    // 2. Формируем ANALYTICS и CSV.
    const analyticsContent = buildAnalytics(rows);
    const csvContent = toCsv(rows);

    // 3. Сохраняем во вложенную папку bot/logs/analytics/. В имени каждого файла —
    //    точная дата-время сохранения (YYYY-MM-DD_HH-MM-SS).
    fs.mkdirSync(BACKUP_DIR, { recursive: true });
    const stamp = timestampDir();
    const analyticsPath = path.join(BACKUP_DIR, `ANALYTICS_${stamp}.md`);
    const csvPath = path.join(BACKUP_DIR, `trades_${stamp}.csv`);
    const jsonPath = path.join(BACKUP_DIR, `trades_${stamp}.json`);
    fs.writeFileSync(analyticsPath, analyticsContent + "\n", "utf-8");
    fs.writeFileSync(csvPath, csvContent + "\n", "utf-8");
    fs.writeFileSync(jsonPath, JSON.stringify(rows, null, 2), "utf-8");

    // 4. Удаляем все сделки.
    try {
      await db.delete(tradesTable);
    } catch (e) { /* ignored — supported clients return undefined */ }
    res.json({
      deleted: rows.length,
      backup_dir: BACKUP_DIR,
      backup_stamp: stamp,
      backed_up: rows.length,
    });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// GET /trades/positions — история позиций с ногами. ?clientId=<id>&symbol=<SYM>&limit=50
// Группируем по trade_id: внутри — все ноги цикла, включая ещё открытую.
router.get("/positions", async (req, res) => {
  try {
    const clientId = req.query.clientId ? Number(req.query.clientId) : null;
    const symbol = req.query.symbol ? String(req.query.symbol).toUpperCase() : null;
    const limit = Math.min(Number(req.query.limit) || 50, 200);

    const conds: any[] = [isNull(tradesTable.reject_reason)];
    if (symbol) conds.push(eq(tradesTable.symbol, symbol));
    if (clientId && Number.isFinite(clientId)) {
      conds.push(sql`${tradesTable.bot_id} IN (SELECT id FROM bots WHERE client_id = ${clientId})`);
    }
    const trades = await db
      .select()
      .from(tradesTable)
      .where(and(...conds))
      .orderBy(desc(tradesTable.entry_time))
      .limit(limit);
    if (trades.length === 0) return res.json({ positions: [] });

    const ids = trades.map((t) => Number(t.id));
    const legs = await db
      .select()
      .from(tradeLegsTable)
      .where(sql`${tradeLegsTable.trade_id} IN ${ids}`)
      .orderBy(tradeLegsTable.trade_id, tradeLegsTable.leg_index);

    const byTrade = new Map<number, any[]>();
    for (const leg of legs) {
      const list = byTrade.get(Number(leg.trade_id)) || [];
      list.push({
        ...leg,
        entry_price: Number(leg.entry_price),
        exit_price: leg.exit_price == null ? null : Number(leg.exit_price),
        qty: Number(leg.qty),
        pnl: leg.pnl == null ? null : Number(leg.pnl),
        is_open: Boolean(Number(leg.is_open)),
      });
      byTrade.set(Number(leg.trade_id), list);
    }

    const positions = trades.map((t) => {
      let list = byTrade.get(Number(t.id)) || [];
      // Fallback для старых сделок до появления trade_legs: одна нога = сама сделка.
      if (list.length === 0) {
        list = [{
          id: 0,
          trade_id: Number(t.id),
          client_id: null,
          bot_id: t.bot_id,
          symbol: t.symbol,
          leg_index: 0,
          direction: t.direction,
          entry_price: Number(t.entry_price),
          exit_price: t.exit_price == null ? null : Number(t.exit_price),
          qty: Number(t.qty),
          sl_price: t.sl_price,
          tp1_price: t.tp1_price,
          pnl: t.pnl == null ? null : Number(t.pnl),
          exit_reason: t.exit_reason,
          entry_time: t.entry_time,
          exit_time: t.exit_time,
          is_open: Boolean(Number(t.is_open)),
          commission: t.commission,
        }];
      }
      return {
        id: Number(t.id),
        symbol: t.symbol,
        direction: t.direction,
        entry_price: Number(t.entry_price),
        qty: Number(t.qty),
        pnl: t.pnl == null ? null : Number(t.pnl),
        is_open: Boolean(Number(t.is_open)),
        status: t.status,
        exit_reason: t.exit_reason,
        entry_time: t.entry_time,
        exit_time: t.exit_time,
        cycle_pnl: t.cycle_pnl == null ? null : Number(t.cycle_pnl),
        chain_depth: t.chain_depth == null ? null : Number(t.chain_depth),
        legs: list,
      };
    });
    return res.json({ positions });
  } catch (e) {
    return res.status(500).json({ error: String(e) });
  }
});

// GET /trades/count-open — точное число открытых сделок (не зависит от limit/offset).
// Дашборд берёт отсюда open_trades, а не из страницы по 1000 записей, чтобы
// счёт не занижался при росте таблицы trades выше 1000 строк.
router.get("/count-open", async (_req, res) => {
  try {
    const [{ count }] = await db.select({ count: sql<number>`count(*)` })
      .from(tradesTable)
      .where(and(eq(tradesTable.is_open, true), isNull(tradesTable.reject_reason)));
    res.json({ count: Number(count) });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// DELETE /trades/clear-tp1 — удалить все TP1 записи (для миграции)
router.delete("/clear-tp1", async (_req, res) => {
  try {
    const result = await db.delete(tradesTable).where(eq(tradesTable.exit_reason, "TP1")).returning();
    res.json({ deleted: result.length });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.get("/", async (req, res) => {
  try {
    const symbol = req.query.symbol as string | undefined;
    const limit  = parseInt(req.query.limit as string) || 50;
    const offset = parseInt(req.query.offset as string) || 0;

    // Все условия — ОДНИМ where(and(...)), а не цепочкой where().
    // Проверено на боевой БД: два .where() подряд в drizzle-orm 0.45.2 теряют
    // ПЕРВОЕ условие. Запрос /trades?symbol=HBARUSDT&limit=10 возвращал сделки
    // всех символов, и каждый бот, спрашивая свои, получал чужие открытые сделки
    // и закрывал их ценой СВОЕГО символа (HBAR=0.09 <- RENDER=2.09, убыток -126).
    // Второе условие — isNull(reject_reason), а НЕ eq(col, null): последнее
    // генерирует SQL `= NULL`, а в SQL сравнение с NULL всегда ложно и фильтр
    // молча скрывал ВСЕ сделки (массив пустой при корректном total).
    const conditions: any[] = [isNull(tradesTable.reject_reason)];
    if (symbol) conditions.push(eq(tradesTable.symbol, symbol.toUpperCase()));

    const query = db.select().from(tradesTable)
      .where(and(...conditions))
      .orderBy(desc(tradesTable.entry_time))
      .limit(limit)
      .offset(offset);

    const trades = await query;
    const [{ total }] = await db.select({ total: sql<number>`count(*)` }).from(tradesTable);
    res.json({ trades, total });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// PATCH /trades/:id — обновить сделку (закрытие, синхронизация PnL)
router.patch("/:id", async (req, res) => {
  try {
    const id = parseInt(req.params.id);
    const updates: Record<string, unknown> = {};
    const allowed = ["direction", "exit_price", "pnl", "exit_reason", "qty", "is_open", "exit_time", "status", "reject_reason", "commission", "entry_price", "preset", "leg_pnl", "cycle_pnl", "chain_depth", "cycle_close_reason"];
    for (const key of allowed) {
      if (req.body[key] !== undefined) updates[key] = req.body[key];
    }
    // Сервисный флаг: reverse открыл новую ногу на той же строке trades.
    const newLeg = req.body.new_leg === true;
    if (Object.keys(updates).length === 0) {
      return res.status(400).json({ error: "No valid fields to update" });
    }
    // Строка ДО обновления: комиссию списываем только на переходе open → closed.
    // Повторные PATCH уже закрытой сделки (refinalize после residual flat,
    // ручной ретрай, синхронизация PnL) не должны списывать комиссию дважды:
    // защита — уникальный idempotency_key в ledger (fee:trade:<id>).
    const [prev] = await db.select({ id: tradesTable.id }).from(tradesTable).where(eq(tradesTable.id, id)).limit(1);
    if (!prev) {
      return res.status(404).json({ error: "Trade not found" });
    }

    const result = await db.update(tradesTable).set(updates).where(eq(tradesTable.id, id)).returning();
    if (result.length === 0) {
      return res.status(404).json({ error: "Trade not found" });
    }
    const updated = result[0] as any;

    // Reverse: закрываем предыдущую ногу и начинаем новую.
    if (newLeg) {
      try {
        const legs = await db
          .select()
          .from(tradeLegsTable)
          .where(eq(tradeLegsTable.trade_id, Number(updated.id)))
          .orderBy(desc(tradeLegsTable.id));
        const current = legs.find((l) => Boolean(Number(l.is_open)));
        if (current) {
          await db
            .update(tradeLegsTable)
            .set({
              exit_price: updated.entry_price != null ? Number(updated.entry_price) : null,
              pnl: updated.leg_pnl != null ? Number(updated.leg_pnl) : null,
              exit_reason: "reverse",
              exit_time: new Date().toISOString(),
              is_open: false,
            })
            .where(eq(tradeLegsTable.id, current.id));
        }
        const [bot] = updated.bot_id != null
          ? await db.select({ client_id: botsTable.client_id }).from(botsTable)
              .where(eq(botsTable.id, Number(updated.bot_id))).limit(1)
          : [];
        await db.insert(tradeLegsTable).values({
          trade_id: Number(updated.id),
          client_id: bot?.client_id ?? null,
          bot_id: updated.bot_id != null ? Number(updated.bot_id) : null,
          symbol: String(updated.symbol || "").toUpperCase(),
          leg_index: legs.length,
          direction: String(updated.direction || "LONG"),
          entry_price: Number(updated.entry_price || 0),
          qty: Number(updated.qty || 0),
          sl_price: updated.sl_price != null ? Number(updated.sl_price) : null,
          tp1_price: updated.tp1_price != null ? Number(updated.tp1_price) : null,
          entry_time: new Date().toISOString(),
          is_open: true,
        });
      } catch (e) {
        console.error(`[trade-legs] reverse leg for trade #${updated.id}:`, String(e));
      }
    }

    // Закрываем последнюю открытую ногу в истории цикла.
    try {
      const [openLeg] = await db
        .select({ id: tradeLegsTable.id })
        .from(tradeLegsTable)
        .where(and(
          eq(tradeLegsTable.trade_id, Number(updated.id)),
          eq(tradeLegsTable.is_open, true),
        ))
        .orderBy(desc(tradeLegsTable.id))
        .limit(1);
      if (openLeg) {
        await db
          .update(tradeLegsTable)
          .set({
            exit_price: updated.exit_price != null ? Number(updated.exit_price) : null,
            pnl: updated.pnl != null ? Number(updated.pnl) : null,
            exit_reason: updated.exit_reason ?? null,
            exit_time: updated.exit_time ?? new Date().toISOString(),
            commission: updated.commission != null ? Number(updated.commission) : null,
            is_open: false,
          })
          .where(eq(tradeLegsTable.id, openLeg.id));
      } else {
        // Сделка создана до появления trade_legs — создаём ногу задним числом
        // из данных самой сделки, чтобы история была полной.
        const [bot] = updated.bot_id != null
          ? await db.select({ client_id: botsTable.client_id }).from(botsTable)
              .where(eq(botsTable.id, Number(updated.bot_id))).limit(1)
          : [];
        await db.insert(tradeLegsTable).values({
          trade_id: Number(updated.id),
          client_id: bot?.client_id ?? null,
          bot_id: updated.bot_id != null ? Number(updated.bot_id) : null,
          symbol: String(updated.symbol || "").toUpperCase(),
          leg_index: 0,
          direction: String(updated.direction || "LONG"),
          entry_price: Number(updated.entry_price || 0),
          exit_price: updated.exit_price != null ? Number(updated.exit_price) : null,
          qty: Number(updated.qty || 0),
          sl_price: updated.sl_price != null ? Number(updated.sl_price) : null,
          tp1_price: updated.tp1_price != null ? Number(updated.tp1_price) : null,
          pnl: updated.pnl != null ? Number(updated.pnl) : null,
          exit_reason: updated.exit_reason ?? null,
          entry_time: String(updated.entry_time || new Date().toISOString()),
          exit_time: updated.exit_time ?? new Date().toISOString(),
          commission: updated.commission != null ? Number(updated.commission) : null,
          is_open: false,
        });
      }
    } catch (e) {
      console.error(`[trade-legs] close leg for trade #${updated.id}:`, String(e));
    }

    // Модель B: прибыль НЕ зачисляется на внутренний счёт. При закрытии
    // прибыльной сделки 10% её pnl списывается с предоплаченного баланса
    // клиента. Убыточная сделка баланс не меняет (но обновляет справочный
    // cum_pnl). Владелец находится по bot_id внутри clientAccount — символ
    // больше не уникален, поэтому выборка по символу опасна при 2 клиентах.
    const nowClosed = updated.status === "closed" && Number(updated.is_open) === 0;
    // Вызываем и при повторном PATCH уже закрытой сделки: бот сначала пишет
    // pnl<=0 (комиссия не списывается), а после ре-финализации по биржевым
    // филлам pnl становится >0 — в этом случае комиссия всё равно должна
    // быть списана. chargeTradeCommission идемпотентна по trade_id (ledger
    // idempotency_key), поэтому повторный вызов не спишет дважды.
    if (nowClosed && updated.status !== "rejected") {
      try {
        const resFee = await chargeTradeCommission(Number(updated.id));
        if (resFee?.charged) {
          console.log(
            `[client-account] fee trade #${updated.id}: -$${resFee.feeUsd.toFixed(4)} ` +
            `client=${resFee.clientId} balance $${resFee.balanceBeforeUsd.toFixed(4)} → $${resFee.balanceAfterUsd.toFixed(4)}`,
          );
        }
      } catch (e) {
        // Списание комиссии не должно ломать обновление сделки.
        console.error(`[client-account] fee failed for trade #${updated.id}:`, String(e));
      }
    }

    res.json(result[0]);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// DELETE /trades/:id — удалить конкретную сделку
router.delete("/:id", async (req, res) => {
  try {
    const id = parseInt(req.params.id);
    const result = await db.delete(tradesTable).where(eq(tradesTable.id, id)).returning();
    if (result.length === 0) {
      return res.status(404).json({ error: "Trade not found" });
    }
    res.json({ deleted: 1, id });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

router.get("/stats", async (_req, res) => {
  try {
    const stats = await db.run(sql`
      SELECT
        symbol,
        COUNT(*)                                                       AS total,
        COUNT(CASE WHEN pnl > 0 THEN 1 END)                           AS wins,
        COUNT(CASE WHEN pnl <= 0 THEN 1 END)                          AS losses,
        ROUND(COUNT(CASE WHEN pnl > 0 THEN 1 END)*100.0/MAX(COUNT(*),1),1) AS win_rate,
        ROUND(COALESCE(SUM(pnl),0),4)                                 AS total_pnl,
        ROUND(COALESCE(AVG(CASE WHEN pnl>0 THEN pnl END),0),4)        AS avg_win,
        ROUND(COALESCE(AVG(CASE WHEN pnl<=0 THEN pnl END),0),4)       AS avg_loss
      FROM trades WHERE is_open=0 AND status != 'rejected' GROUP BY symbol ORDER BY symbol
    `);
    res.json(stats.rows);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

router.get("/export", async (_req, res) => {
  try {
    const trades = await db
      .select()
      .from(tradesTable)
      .where(eq(tradesTable.is_open, false))
      .orderBy(desc(tradesTable.entry_time));

    const headers = [
      "ID", "Symbol", "Direction", "Entry Price", "Exit Price",
      "Quantity", "PnL", "Exit Reason", "Entry Time", "Exit Time", "Mode"
    ];

    const rows = trades.map(t => [
      t.id, t.symbol, t.direction, t.entry_price,
      t.exit_price || "", t.qty, t.pnl || "",
      t.exit_reason || "", t.entry_time, t.exit_time || "", t.mode
    ]);

    const csv = [
      headers.join(","),
      ...rows.map(row => row.map(cell =>
        typeof cell === 'string' && cell.includes(',') ? `"${cell}"` : cell
      ).join(","))
    ].join("\n");

    const timestamp = new Date().toISOString().split('T')[0];
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="trades-export-${timestamp}.csv"`);
    res.send(csv);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

router.post("/", async (req, res) => {
  try {
    const body: any = { ...req.body };
    // Атрибуция сделки инстансу бота: по bot_id считается комиссия и статистика
    // клиента. Символ больше не уникален, поэтому бот позже будет присылать
    // bot_id напрямую; пока — fallback по символу (клиент №1).
    if (body.bot_id == null && body.symbol) {
      const [bot] = await db
        .select({ id: botsTable.id })
        .from(botsTable)
        .where(eq(botsTable.symbol, String(body.symbol).toUpperCase()))
        .limit(1);
      if (bot) body.bot_id = Number(bot.id);
    }
    const [trade] = await db.insert(tradesTable).values(body).returning();

    // Первая нога цикла. При reverse бот обновляет ту же строку trades, но
    // новую ногу дописываем отдельной записью в trade_legs.
    try {
      const [bot] = body.bot_id != null
        ? await db.select({ client_id: botsTable.client_id }).from(botsTable)
            .where(eq(botsTable.id, Number(body.bot_id))).limit(1)
        : [];
      await db.insert(tradeLegsTable).values({
        trade_id: Number(trade.id),
        client_id: bot?.client_id ?? null,
        bot_id: body.bot_id != null ? Number(body.bot_id) : null,
        symbol: String(body.symbol || "").toUpperCase(),
        leg_index: 0,
        direction: String(body.direction || "LONG"),
        entry_price: Number(body.entry_price || 0),
        qty: Number(body.qty || 0),
        sl_price: body.sl_price != null ? Number(body.sl_price) : null,
        tp1_price: body.tp1_price != null ? Number(body.tp1_price) : null,
        entry_time: String(body.entry_time || new Date().toISOString()),
        is_open: true,
      });
    } catch (e) {
      console.error(`[trade-legs] insert leg for trade #${trade.id}:`, String(e));
    }

    res.status(201).json(trade);
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// POST /trades/sync-closed — синхронизировать закрытые позиции с биржей
router.post("/sync-closed", async (_req, res) => {
  try {
    const API_KEY = process.env.BINANCE_API_KEY || "";
    const API_SECRET = process.env.BINANCE_API_SECRET || "";
    
    if (!API_KEY || !API_SECRET) {
      return res.status(400).json({ error: "BINANCE_API_KEY and BINANCE_API_SECRET not configured" });
    }

    // Получаем открытые сделки из БД
    const openTrades = await db.select().from(tradesTable).where(eq(tradesTable.is_open, true));
    
    if (openTrades.length === 0) {
      return res.json({ synced: 0, message: "No open trades in DB" });
    }

    // Получаем текущие позиции с биржи
    const crypto = await import("crypto");
    const BASE_URL = "https://fapi.binance.com";
    
    function sign(params: Record<string, string | number>): string {
      const qs = Object.entries(params).map(([k, v]) => `${k}=${v}`).join("&");
      return crypto.createHmac("sha256", API_SECRET).update(qs).digest("hex");
    }

    function binanceGet(path: string, params: Record<string, string | number> = {}): Promise<any> {
      const ts = Date.now();
      const p = { ...params, timestamp: ts };
      const signature = sign(p);
      const qs = Object.entries(p).map(([k, v]) => `${k}=${v}`).join("&");
      const url = `${BASE_URL}${path}?${qs}&signature=${signature}`;
      return fetch(url, { headers: { "X-MBX-APIKEY": API_KEY } }).then(r => r.json());
    }

    const positions = await binanceGet("/fapi/v2/positionRisk");
    
    // Создаём карту открытых позиций на бирже
    const exchangePositions = new Map<string, number>();
    for (const pos of positions) {
      const amt = Math.abs(parseFloat(pos.positionAmt));
      if (amt > 0) {
        exchangePositions.set(pos.symbol, amt);
      }
    }

    // Закрываем сделки которых нет на бирже
    let closed = 0;
    const now = new Date().toISOString();
    
    for (const trade of openTrades) {
      if (!exchangePositions.has(trade.symbol)) {
        // Получаем PnL из userTrades
        let pnl = 0;
        let exitPrice = trade.entry_price;
        try {
          const userTrades = await binanceGet("/fapi/v1/userTrades", {
            symbol: trade.symbol,
            limit: 20,
          });
          // Ищем сделки которые закрыли нашу позицию
          for (const ut of userTrades) {
            const utTime = new Date(ut.time).toISOString();
            // Берём только сделки после открытия позиции
            if (utTime > trade.entry_time && ut.realizedPnl) {
              // ВАЖНО: Binance считает Realized PNL как реализованный pnl
              // МИНУС уплаченную комиссию по каждой сделке (мы это
              // подтвердили эмпирически в binance-sync.ts groupPositions).
              // realizedPnl сам по себе не включает комиссию — без вычитания
              // эта сумма систематически переоценивает прибыль (или
              // недооценивает убыток) на величину комиссии каждой сделки.
              const realizedPnl = parseFloat(ut.realizedPnl) || 0;
              const commission = parseFloat(ut.commission || "0");
              const commissionUsd = ut.commissionAsset === "USDT" ? commission : 0;
              pnl += realizedPnl - commissionUsd;
            }
          }
          // Рассчитываем exit_price из последней сделки
          if (userTrades.length > 0) {
            const lastTrade = userTrades[userTrades.length - 1];
            exitPrice = parseFloat(lastTrade.price) || trade.entry_price;
          }
        } catch (e) {
          // ignore
        }

        // Позиция закрыта на бирже — обновляем БД
        await db.update(tradesTable)
          .set({
            is_open: false,
            exit_time: now,
            exit_reason: "exchange_closed",
            exit_price: exitPrice,
            pnl: pnl,
            status: "closed",
          })
          .where(eq(tradesTable.id, trade.id));
        closed++;

        // ВАЖНО: recovery chain для убытка ЗДЕСЬ не создаём.
        // Цепочку создаёт сам бот при закрытии убыточной сделки через
        // recovery.report(pnl) (см. bot/main.py). Если бы мы создавали цепочку
        // и здесь, один и тот же убыток задваивался бы в две свободных
        // цепочки, каждая из которых запускала бы свой компенсатор и
        // приводила к открытию двух позиций по одной монете.
      }
    }

    res.json({ synced: closed, total: openTrades.length });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// GET /trades/:id — получить одну сделку по id (для корректного PnL восстановленных позиций)
router.get("/:id", async (req, res) => {
  try {
    const id = parseInt(req.params.id);
    const rows = await db.select().from(tradesTable).where(eq(tradesTable.id, id)).limit(1);
    if (rows.length === 0) return res.status(404).json({ error: "Trade not found" });
    res.json(rows[0]);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;

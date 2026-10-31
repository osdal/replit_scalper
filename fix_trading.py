import re

with open('/opt/trading-bot/artifacts/api-server/src/routes/trading.ts', 'r', encoding='utf-8') as f:
    content = f.read()

old = '''router.post("/check", async (req, res) => {
  try {
    const cfg = readConfig();
    const control = await getControlRow();
    const symbol = String(req.body?.symbol ?? "").trim().toUpperCase();
    // reserve=true — занять слот атомарно (вызывается ботом непосредственно перед
    // ордером). Без флага запрос "информационный" и ничего не занимает.
    const wantReserve = req.body?.reserve === true && symbol !== "";
    const { occupied: positions } = await slots.inspect(
      () => loadOccupiedSymbols(cfg.max_positions_ignore_after_hours),
    );
    const daily_loss = await getDailyLoss();

    // Автосброс устаревшей паузы/счётчика, если давно не было сделок.
    const reset = await autoResetStale(control, cfg.pause_timeout_minutes);
    if (reset) {
      control.loss_streak = 0;
      control.paused_remaining = 0;
    }

    // Дневной лимит убытков — жёсткая остановка на день.
    if (cfg.daily_loss_limit_usd > 0 && daily_loss >= cfg.daily_loss_limit_usd) {
      return res.json({
        allowed: false,
        reason: "daily_loss_limit",
        positions_open: positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    // Блок после серии убытков активен: при достижении loss_streak_trigger пауза
    // блокирует новые ВХОДЫ (реальные позиции), пока не будет прибыльной сделки.
    // Отклонённые (rejected) сделки по договорённости открываются как позиции и
    // могут снять паузу прибыльной симуляцией.
    if (control.paused_remaining > 0) {
      return res.json({
        allowed: false,
        reason: "loss_streak",
        positions_open: positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    // Лимит позиций: проверка и (при reserve) занятие слота — под одной блокировкой,
    // иначе синхронные сигналы многих ботов видят один и тот же счётчик (race).
    // Символ, который уже занимает слот (открытая позиция/резерв), лимитом не
    // блокируется — это добор reverse-ноги в уже открытую цепочку, а не новая позиция.
    let slot = await slots.check(
      symbol,
      cfg.max_positions,
      () => loadOccupiedSymbols(cfg.max_positions_ignore_after_hours),
      wantReserve,
    );
    // "Протухшая" позиция (старше max_positions_ignore_after_hours) исключена из
    // occupied и потому не опознаётся как own по БД. Из-за этого следующая
    // reverse-нога такого символа упиралась в max_positions, если слот занял
    // свежий символ, хотя на бирже позиция ЕСТЬ — это добор, а не новый вход.
    // Спрашиваем биржу. Для информационных запросов (reserve=false) — всегда.
    // Для резервирования (reserve=true) — тоже, если на бирже есть живая позиция
    // (реверс-нога в существующей цепочке не должна конкурировать за слот).
    if (!slot.allowed && symbol && !EXCHANGE_CHECK
        && await exchangeHasPosition(symbol)) {
      slot = { ...slot, allowed: true, own: true };
    }
    if (!slot.allowed) {
      return res.json({
        allowed: false,
        reason: "max_positions",
        positions_open: slot.occupied,
        positions_reserved: slot.reserved,
        max_positions: cfg.max_positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    return res.json({
      allowed: true,
      reason: null,
      positions_open: slot.occupied,
      positions_reserved: slot.reserved,
      max_positions: cfg.max_positions,
      own_slot: slot.own,
      reservation_id: slot.reservationId,
      loss_streak: control.loss_streak,
      paused_remaining: control.paused_remaining,
      daily_loss: Number(daily_loss.toFixed(2)),
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});'''

new = '''router.post("/check", async (req, res) => {
  try {
    const globalCfg = readConfig();
    const control = await getControlRow();
    const symbol = String(req.body?.symbol ?? "").trim().toUpperCase();

    // Read per-bot config from DB (falls back to global if not found)
    let botMaxPositions = globalCfg.max_positions;
    let botIgnoreAfterHours = globalCfg.max_positions_ignore_after_hours;
    if (symbol) {
      const [bot] = await db.select().from(botsTable).where(eq(botsTable.symbol, symbol));
      if (bot) {
        if (bot.max_positions != null && bot.max_positions > 0) botMaxPositions = bot.max_positions;
        if (bot.expired_after_minutes != null && bot.expired_after_minutes >= 0) {
          // Convert minutes to hours for server logic
          botIgnoreAfterHours = Math.max(0, Math.round(bot.expired_after_minutes / 60));
        }
      }
    }

    // reserve=true — занять слот атомарно (вызывается ботом непосредственно перед
    // ордером). Без флага запрос "информационный" и ничего не занимает.
    const wantReserve = req.body?.reserve === true && symbol !== "";
    const { occupied: positions } = await slots.inspect(
      () => loadOccupiedSymbols(botIgnoreAfterHours),
    );
    const daily_loss = await getDailyLoss();

    // Автосброс устаревшей паузы/счётчика, если давно не было сделок.
    const reset = await autoResetStale(control, globalCfg.pause_timeout_minutes);
    if (reset) {
      control.loss_streak = 0;
      control.paused_remaining = 0;
    }

    // Дневной лимит убытков — жёсткая остановка на день.
    if (globalCfg.daily_loss_limit_usd > 0 && daily_loss >= globalCfg.daily_loss_limit_usd) {
      return res.json({
        allowed: false,
        reason: "daily_loss_limit",
        positions_open: positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    // Блок после серии убытков активен: при достижении loss_streak_trigger пауза
    // блокирует новые ВХОДЫ (реальные позиции), пока не будет прибыльной сделки.
    // Отклонённые (rejected) сделки по договорённости открываются как позиции и
    // могут снять паузу прибыльной симуляцией.
    if (control.paused_remaining > 0) {
      return res.json({
        allowed: false,
        reason: "loss_streak",
        positions_open: positions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    // Лимит позиций: проверка и (при reserve) занятие слота — под одной блокировкой,
    // иначе синхронные сигналы многих ботов видят один и тот же счётчик (race).
    // Символ, который уже занимает слот (открытая позиция/резерв), лимитом не
    // блокируется — это добор reverse-ноги в уже открытую цепочку, а не новая позиция.
    let slot = await slots.check(
      symbol,
      botMaxPositions,
      () => loadOccupiedSymbols(botIgnoreAfterHours),
      wantReserve,
    );
    // "Протухшая" позиция (старше max_positions_ignore_after_hours) исключена из
    // occupied и потому не опознаётся как own по БД. Из-за этого следующая
    // reverse-нога такого символа упиралась в max_positions, если слот занял
    // свежий символ, хотя на бирже позиция ЕСТЬ — это добор, а не новый вход.
    // Спрашиваем биржу. Для информационных запросов (reserve=false) — всегда.
    // Для резервирования (reserve=true) — тоже, если на бирже есть живая позиция
    // (реверс-нога в существующей цепочке не должна конкурировать за слот).
    if (!slot.allowed && symbol && !EXCHANGE_CHECK
        && await exchangeHasPosition(symbol)) {
      slot = { ...slot, allowed: true, own: true };
    }
    if (!slot.allowed) {
      return res.json({
        allowed: false,
        reason: "max_positions",
        positions_open: slot.occupied,
        positions_reserved: slot.reserved,
        max_positions: botMaxPositions,
        loss_streak: control.loss_streak,
        paused_remaining: control.paused_remaining,
        daily_loss: Number(daily_loss.toFixed(2)),
      });
    }

    return res.json({
      allowed: true,
      reason: null,
      positions_open: slot.occupied,
      positions_reserved: slot.reserved,
      max_positions: botMaxPositions,
      own_slot: slot.own,
      reservation_id: slot.reservationId,
      loss_streak: control.loss_streak,
      paused_remaining: control.paused_remaining,
      daily_loss: Number(daily_loss.toFixed(2)),
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});'''

if old in content:
    content = content.replace(old, new)
    with open('/opt/trading-bot/artifacts/api-server/src/routes/trading.ts', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: Replaced trading/check endpoint")
else:
    print("FAILED: Old text not found")
    idx = content.find('router.post("/check"')
    if idx >= 0:
        print("Found at index:", idx)
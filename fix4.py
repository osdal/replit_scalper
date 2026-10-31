import re

with open('/opt/trading-bot/artifacts/api-server/src/routes/trading.ts', 'r', encoding='utf-8') as f:
    content = f.read()

# Use regex to find and replace the entire router.post("/check"...) block
pattern = r'router\.post\("/check", async \(req, res\) => \{(?:(?!\nrouter\.post\("/check").)*?\n\}\);'

# Since regex with DOTALL might be too greedy, let's find the exact boundaries
start = content.find('router.post("/check", async (req, res) => {')
if start == -1:
    print("ERROR: start not found")
    exit(1)

# Find the matching closing }); for this function
brace_count = 0
end = start
for i, ch in enumerate(content[start:], start):
    if ch == '{':
        brace_count += 1
    elif ch == '}':
        brace_count -= 1
        if brace_count == 0:
            end = i + 1
            break

if brace_count != 0:
    print("ERROR: Could not find matching brace")
    exit(1)

old_block = content[start:end]
print("OLD BLOCK LENGTH:", len(old_block))
print("OLD BLOCK START:", old_block[:100])

new_block = '''router.post("/check", async (req, res) => {
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

new_content = content[:start] + new_block + content[end:]
with open('/opt/trading-bot/artifacts/api-server/src/routes/trading.ts', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("SUCCESS: Replaced trading/check endpoint")
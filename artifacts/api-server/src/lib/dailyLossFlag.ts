/**
 * Глобальный выключатель дневного kill-switch.
 *
 * `DAILY_LOSS_ENABLED=false` (0/no/off) отключает:
 *  - срабатывание `checkDailyLossLimit` (остановка ботов + flatten);
 *  - блокировку входов по дневному лимиту в `POST /api/trading/check`;
 *  - автостарт ботов на смене дня в `checkDayRollover`.
 *
 * По умолчанию включён: пустое или неизвестное значение = true.
 * Вынесено отдельным модулем без зависимостей от БД/сети, чтобы флаг можно
 * было покрыть unit-тестом.
 */
export function dailyLossEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  const raw = String(env.DAILY_LOSS_ENABLED ?? "").trim().toLowerCase();
  return !["0", "false", "no", "off"].includes(raw);
}

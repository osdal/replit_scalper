// Запуск: cd artifacts/api-server && node --experimental-strip-types --test test/dailyLossFlag.test.ts
//
// DAILY_LOSS_ENABLED — аварийный выключатель дневного kill-switch. По умолчанию
// включён; false/0/no/off выключают и срабатывание лимита, и блокировку входов,
// и автостарт ботов на смене дня.
import { test } from "node:test";
import assert from "node:assert/strict";
import { dailyLossEnabled } from "../src/lib/dailyLossFlag.ts";

test("по умолчанию дневной лимит включён", () => {
  assert.equal(dailyLossEnabled({}), true);
  assert.equal(dailyLossEnabled({ DAILY_LOSS_ENABLED: "" }), true);
  assert.equal(dailyLossEnabled({ DAILY_LOSS_ENABLED: "true" }), true);
  assert.equal(dailyLossEnabled({ DAILY_LOSS_ENABLED: "1" }), true);
  assert.equal(dailyLossEnabled({ DAILY_LOSS_ENABLED: "yes" }), true);
  assert.equal(dailyLossEnabled({ DAILY_LOSS_ENABLED: "on" }), true);
});

test("false/0/no/off выключают дневной лимит", () => {
  for (const value of ["false", "FALSE", "0", "no", "off", " false "]) {
    assert.equal(dailyLossEnabled({ DAILY_LOSS_ENABLED: value }), false, value);
  }
});

// Запуск: cd artifacts/api-server && node --experimental-strip-types --test test/dayBoundary.test.ts
//
// Проверяем границу торгового дня по Киеву: полночь местного времени, а не UTC,
// включая переходы на летнее/зимнее время (иначе стоп срабатывал бы в 02:00–03:00).
import { test } from "node:test";
import assert from "node:assert/strict";
import { dayKey, dayStartUtcIso, tzOffsetMinutes } from "../src/lib/dayBoundary.ts";

test("зима (EET, UTC+2): день начинается в 22:00 UTC предыдущих суток", () => {
  const now = new Date("2026-01-15T12:00:00Z");
  assert.equal(dayKey(now), "2026-01-15");
  assert.equal(dayStartUtcIso(now), "2026-01-14T22:00:00.000Z");
  assert.equal(tzOffsetMinutes(now), 120);
});

test("лето (EEST, UTC+3): день начинается в 21:00 UTC предыдущих суток", () => {
  const now = new Date("2026-07-15T12:00:00Z");
  assert.equal(dayKey(now), "2026-07-15");
  assert.equal(dayStartUtcIso(now), "2026-07-14T21:00:00.000Z");
  assert.equal(tzOffsetMinutes(now), 180);
});

test("граница суток: 21:59Z ещё вчера, 22:00Z уже сегодня (зимой)", () => {
  assert.equal(dayKey(new Date("2026-01-14T21:59:59Z")), "2026-01-14");
  assert.equal(dayKey(new Date("2026-01-14T22:00:00Z")), "2026-01-15");
});

test("переход на летнее время (29.03.2026): день всё равно начинается в 22:00Z", () => {
  // 29.03.2026 03:00 EET -> 04:00 EEST.
  const now = new Date("2026-03-29T12:00:00Z");
  assert.equal(dayKey(now), "2026-03-29");
  assert.equal(dayStartUtcIso(now), "2026-03-28T22:00:00.000Z");
});

test("переход на зимнее время (25.10.2026): день начинается в 21:00Z", () => {
  // 25.10.2026 04:00 EEST -> 03:00 EET.
  const now = new Date("2026-10-25T12:00:00Z");
  assert.equal(dayKey(now), "2026-10-25");
  assert.equal(dayStartUtcIso(now), "2026-10-24T21:00:00.000Z");
});

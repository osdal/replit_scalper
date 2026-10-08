// Запуск: cd artifacts/api-server && node --experimental-strip-types --test test/
import { test } from "node:test";
import assert from "node:assert/strict";
import { SlotReservations, sanitizeMaxPositions } from "../src/lib/slotReservations.ts";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const symbols = (n: number) => Array.from({ length: n }, (_, i) => `COIN${i}USDT`);

test("35 одновременных сигналов при лимите 1 → ровно один allowed (гонка устранена)", async () => {
  const slots = new SlotReservations();
  // БД «медленная»: каждая проверка читает список с задержкой, как реальный запрос.
  const load = async () => {
    await sleep(5);
    return [] as string[];
  };
  const results = await Promise.all(symbols(35).map((s) => slots.check(s, 1, load, true)));
  assert.equal(results.filter((r) => r.allowed).length, 1);
  assert.equal(results.filter((r) => r.reservationId).length, 1);
});

test("лимит 3, 10 одновременных → 3 allowed", async () => {
  const slots = new SlotReservations();
  const load = async () => [] as string[];
  const results = await Promise.all(symbols(10).map((s) => slots.check(s, 3, load, true)));
  assert.equal(results.filter((r) => r.allowed).length, 3);
});

test("учитывает уже открытые в БД: открыто 2 из 3 → пройдёт один", async () => {
  const slots = new SlotReservations();
  const load = async () => ["AAAUSDT", "BBBUSDT"];
  const results = await Promise.all(symbols(6).map((s) => slots.check(s, 3, load, true)));
  assert.equal(results.filter((r) => r.allowed).length, 1);
});

test("reverse: символ с открытой позицией не блокируется лимитом (own slot)", async () => {
  const slots = new SlotReservations();
  const load = async () => ["AAAUSDT", "BBBUSDT", "CCCUSDT"]; // лимит 3 исчерпан
  const own = await slots.check("BBBUSDT", 3, load, false);
  assert.equal(own.allowed, true);
  assert.equal(own.own, true);
  assert.equal(own.reservationId, null);
  const other = await slots.check("ZZZUSDT", 3, load, true);
  assert.equal(other.allowed, false);
});

test("peek (reserve=false) ничего не занимает", async () => {
  const slots = new SlotReservations();
  const load = async () => [] as string[];
  for (const s of symbols(5)) {
    const r = await slots.check(s, 1, load, false);
    assert.equal(r.allowed, true);
    assert.equal(r.reservationId, null);
  }
  const first = await slots.check("AAAUSDT", 1, load, true);
  assert.equal(first.allowed, true);
});

test("резерв поглощается, когда символ появился в БД (двойного счёта нет)", async () => {
  const slots = new SlotReservations();
  let db: string[] = [];
  const load = async () => db;
  const a = await slots.check("AAAUSDT", 2, load, true);
  assert.equal(a.allowed, true);
  assert.equal((await slots.inspect(load)).reserved, 1);
  db = ["AAAUSDT"]; // бот записал строку в БД
  const st = await slots.inspect(load);
  assert.deepEqual(st, { open: 1, reserved: 0, occupied: 1 });
  // осталась ровно одна свободная позиция из двух
  assert.equal((await slots.check("BBBUSDT", 2, load, true)).allowed, true);
  assert.equal((await slots.check("CCCUSDT", 2, load, true)).allowed, false);
});

test("TTL: резерв упавшего бота освобождается сам", async () => {
  let t = 1_000_000;
  const slots = new SlotReservations({ ttlMs: 60_000, now: () => t });
  const load = async () => [] as string[];
  assert.equal((await slots.check("AAAUSDT", 1, load, true)).allowed, true);
  assert.equal((await slots.check("BBBUSDT", 1, load, true)).allowed, false);
  t += 59_000;
  assert.equal((await slots.check("BBBUSDT", 1, load, true)).allowed, false);
  t += 2_000; // 61 c
  assert.equal((await slots.check("BBBUSDT", 1, load, true)).allowed, true);
});

test("release освобождает слот; чужой id не освобождает", async () => {
  const slots = new SlotReservations();
  const load = async () => [] as string[];
  const a = await slots.check("AAAUSDT", 1, load, true);
  assert.equal(await slots.release("AAAUSDT", "wrong-id"), false);
  assert.equal((await slots.check("BBBUSDT", 1, load, true)).allowed, false);
  assert.equal(await slots.release("AAAUSDT", a.reservationId), true);
  assert.equal((await slots.check("BBBUSDT", 1, load, true)).allowed, true);
  assert.equal(await slots.release("NOPEUSDT"), false);
});

test("повторный reserve того же символа возвращает тот же id и не занимает второй слот", async () => {
  const slots = new SlotReservations();
  const load = async () => [] as string[];
  const a = await slots.check("AAAUSDT", 2, load, true);
  const b = await slots.check("AAAUSDT", 2, load, true);
  assert.equal(a.reservationId, b.reservationId);
  assert.equal(b.occupied, 1);
});

test("ошибка загрузки (БД недоступна) не ломает мьютекс для следующих вызовов", async () => {
  const slots = new SlotReservations();
  await assert.rejects(slots.check("AAAUSDT", 1, async () => { throw new Error("db down"); }, true));
  const ok = await slots.check("AAAUSDT", 1, async () => [], true);
  assert.equal(ok.allowed, true);
});

test("sanitizeMaxPositions: мусор/0/NaN не отключают лимит", () => {
  assert.equal(sanitizeMaxPositions("1", 10), 1);
  assert.equal(sanitizeMaxPositions(3, 10), 3);
  assert.equal(sanitizeMaxPositions(2.9, 10), 2);
  assert.equal(sanitizeMaxPositions("abc", 10), 10);
  assert.equal(sanitizeMaxPositions(0, 10), 10);
  assert.equal(sanitizeMaxPositions(undefined, 10), 10);
  assert.equal(sanitizeMaxPositions(-5, 10), 10);
});

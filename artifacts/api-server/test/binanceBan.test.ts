// Запуск: cd artifacts/api-server && node --experimental-strip-types --test test/binanceBan.test.ts
//
// Проверяем барьер бана: аггрегация ban_until_ms по gate-файлам, отсечение
// мусорных значений и разбор «banned until <ms>» из ответа Binance.
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { parseBanUntilMs, readBanFromDir } from "../src/lib/banFiles.ts";

const now = 1_800_000_000_000; // фиксированное «сейчас» (мс)

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "ban-"));
}

function writeGate(dir: string, name: string, banUntilMs: number): void {
  fs.writeFileSync(
    path.join(dir, name),
    JSON.stringify({ ban_until_ms: banUntilMs, used_weight_1m: 0, ts: now / 1000 - 3600 }),
  );
}

test("бан берётся из файла даже если он старый (срок задан ban_until_ms)", () => {
  const dir = tmpDir();
  writeGate(dir, "binance_gate_testnet.json.101", now + 60_000);
  const st = readBanFromDir(dir, "binance_gate_testnet.json", now);
  assert.equal(st.banUntilMs, now + 60_000);
  assert.equal(st.source, "binance_gate_testnet.json.101");
});

test("берётся максимальный срок среди нескольких процессов", () => {
  const dir = tmpDir();
  writeGate(dir, "binance_gate_testnet.json.101", now + 30_000);
  writeGate(dir, "binance_gate_testnet.json.102", now + 120_000);
  writeGate(dir, "binance_gate_testnet.json.103", now + 10_000);
  assert.equal(readBanFromDir(dir, "binance_gate_testnet.json", now).banUntilMs, now + 120_000);
});

test("чужой стек (live) не влияет на testnet-барьер", () => {
  const dir = tmpDir();
  writeGate(dir, "binance_gate_live.json.101", now + 600_000);
  writeGate(dir, "binance_gate_testnet.json.102", now + 15_000);
  assert.equal(readBanFromDir(dir, "binance_gate_testnet.json", now).banUntilMs, now + 15_000);
});

test("битый JSON и абсурдный срок (>24ч) игнорируются", () => {
  const dir = tmpDir();
  fs.writeFileSync(path.join(dir, "binance_gate_testnet.json.1"), "{oops");
  writeGate(dir, "binance_gate_testnet.json.2", now + 48 * 3600 * 1000);
  writeGate(dir, "binance_gate_testnet.json.3", now + 5_000);
  assert.equal(readBanFromDir(dir, "binance_gate_testnet.json", now).banUntilMs, now + 5_000);
});

test("нет файлов/каталога → 0 (fail-open, торговлю не блокируем)", () => {
  assert.equal(readBanFromDir(path.join(tmpDir(), "missing"), "binance_gate_testnet.json", now).banUntilMs, 0);
});

test("parseBanUntilMs понимает текст Binance", () => {
  const text = '{"code":-1003,"msg":"Way too many requests; IP(1.2.3.4) banned until 1794436677080."}';
  assert.equal(parseBanUntilMs(text), 1794436677080);
  assert.equal(parseBanUntilMs("no ban here"), 0);
});

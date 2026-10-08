import { test } from "node:test";
import assert from "node:assert/strict";
import { upsertYamlScalar } from "../src/lib/yamlUpsert.ts";

const SAMPLE = `# Общий конфиг
recovery_enabled: false     # текущее состояние
recovery_bonus_pct: 50
max_positions: 10             # лимит позиций
max_positions_ignore_after_hours: 0
daily_loss_limit_usd: 100000
`;

test("обновляет значение, сохраняя хвостовой комментарий и остальные ключи", () => {
  const out = upsertYamlScalar(SAMPLE, "recovery_enabled", true);
  assert.match(out, /^recovery_enabled: true {5}# текущее состояние$/m);
  assert.match(out, /^max_positions: 10 {13}# лимит позиций$/m);
  assert.match(out, /^max_positions_ignore_after_hours: 0$/m);
  assert.match(out, /^# Общий конфиг$/m);
});

test("не путает ключ с префиксом: max_positions vs max_positions_ignore_after_hours", () => {
  const out = upsertYamlScalar(SAMPLE, "max_positions", 7);
  assert.match(out, /^max_positions: 7 +# лимит позиций$/m);
  assert.match(out, /^max_positions_ignore_after_hours: 0$/m);
});

test("отсутствующий ключ дописывается в конец", () => {
  const out = upsertYamlScalar("a: 1\n", "recovery_max_pct", 50);
  assert.equal(out, "a: 1\nrecovery_max_pct: 50\n");
  assert.equal(upsertYamlScalar("a: 1", "b", 2), "a: 1\nb: 2\n");
});

test("CRLF-файл не портится", () => {
  const crlf = "recovery_enabled: false # x\r\nmax_positions: 5\r\n";
  const out = upsertYamlScalar(crlf, "recovery_enabled", true);
  assert.equal(out, "recovery_enabled: true # x\r\nmax_positions: 5\r\n");
});

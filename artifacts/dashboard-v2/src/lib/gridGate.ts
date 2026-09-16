export interface AdxTimeframeInfo {
  adx: number | null;
  gate: number;
  ok: boolean;
}

export interface AdxSymbolInfo extends AdxTimeframeInfo {
  tf5m?: AdxTimeframeInfo | null;
  tf1h?: AdxTimeframeInfo | null;
  tf4h?: AdxTimeframeInfo | null;
  tf12h?: AdxTimeframeInfo | null;
  tf1d?: AdxTimeframeInfo | null;
}

export type AdxStatusMap = Record<string, AdxSymbolInfo>;

type TfKey = "tf5m" | "tf1h" | "tf4h" | "tf12h" | "tf1d";

/**
 * Гейт показа сетки: сетка на таймфрейме `timeframe` рисуется ТОЛЬКО если
 * ADX ЭТОГО ЖЕ таймфрейма меньше `gate`.
 *
 * ⚠️ Контракт, который НЕЛЬЗЯ менять:
 *   - нельзя "хотя бы один таймфрейм" через `.some((v) => v < gate)`;
 *   - нельзя "первый не-null ADX" через `.find((v) => v != null)`.
 *
 * Почему: любой из этих вариантов снова сломает соответствие
 * "таймфрейм → своя сетка" — сетка, рассчитанная на 1h/4h, начнёт
 * показываться и на других таймфреймах. Этот баг уже дважды возвращался:
 *   1) `.find((v) => v != null)` — гейт фактически зависел только от 1h;
 *   2) `.some((v) => v != null && v < gate)` — сетка светилась на всех ТФ,
 *      если проходил хотя бы один.
 *
 * Правильно: у каждого таймфрейма свой гейт, включая 5m.
 */
export function getTimeframeAdx(
  adxStatus: AdxStatusMap,
  symbol: string | null | undefined,
  timeframe: string,
): number | null {
  if (!symbol) return null;
  const entry = adxStatus[symbol];
  if (!entry) return null;
  return entry[`tf${timeframe}` as TfKey]?.adx ?? null;
}

export function timeframePassesGridGate(
  adxStatus: AdxStatusMap,
  symbol: string | null | undefined,
  timeframe: string,
  gate: number,
): boolean {
  const tfAdx = getTimeframeAdx(adxStatus, symbol, timeframe);
  return tfAdx != null && tfAdx < gate;
}

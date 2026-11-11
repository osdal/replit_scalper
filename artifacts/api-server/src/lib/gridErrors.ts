/**
 * Ошибка с HTTP-статусом для grid/Binance-операций.
 *
 * Вынесена из grid-orders-lib, чтобы её могли использовать и сама библиотека,
 * и `lib/binanceBan.ts` (барьер бана) без циклического импорта.
 */
export class GridOrderError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "GridOrderError";
    this.status = status;
  }
}

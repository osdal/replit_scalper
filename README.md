# Запуск ботов после перезапуска компьютера

## Быстрый старт (3 терминала)

### Терминал 1: Инициализация БД
```powershell
cd C:\DATA\bots\replit_scalper
pnpm run init-db
```

### Терминал 2: API-сервер
```powershell
cd C:\DATA\bots\replit_scalper
pnpm run start:api
```

### Терминал 3: Дашборд
```powershell
cd C:\DATA\bots\replit_scalper
pnpm run start:dashboard
```

После этого открывай `http://localhost:5173` и нажимай **Start** на нужных ботах.

---

## Или одной командой (start-all.bat)

Запусти `start-all.bat` из корня проекта — он сам:
1. Инициализирует БД
2. Запустит API-сервер
3. Запустит дашборд
4. Откроет браузер на `http://localhost:5173`

---

## Остановка

Нажми `Ctrl+C` в каждом терминале. Для остановки всех ботов из дашборда — нажми **Stop** на каждом боте.

---

## Структура портов

| Сервис | Порт |
|--------|------|
| API-сервер | 5000 |
| Дашборд | 5173 |

---

## Первый запуск / после обновления кода

```powershell
cd C:\DATA\bots\replit_scalper
pnpm install
pnpm run init-db
pnpm run start:api
pnpm run start:dashboard
```

---

# Логика торговли (Механика совершения сделок)

Данный раздел описывает полный цикл жизни сделки — от получения рыночных данных до исполнения ордеров, управления позицией и логирования. Описание ориентировано на разработчика, который хочет понять архитектуру бота, внести изменения в стратегию или добавить новую биржу.

---

## 1. Архитектура и входные точки

### 1.1 Основные режимы работы
Бот поддерживает три режима (`Config.mode`):
- **live** — реальная торговля на Binance Futures (USDT-M)
- **paper** — бумажная торговля (симуляция без реальных ордеров)
- **backtest** — прогон на исторических данных

### 1.2 Главный цикл (`bot/main.py`)
Точка входа — `async def main()`:
1. Загружает конфиг (`config.yaml` + `.env` для API ключей)
2. Создаёт `AsyncClient` (python-binance) для Binance Futures
3. Инициализирует компоненты: `OrderManager`, `PositionTracker`, `SignalHandler`, `RecoveryClient`, `DbReporter`
4. Синхронизирует состояние с биржей при старте (`_sync_position_on_start`)
4. Загружает исторические свечи для прогрева индикаторов
5. Запускает поллинг свечей через `start_kline_polling` (REST каждые 10 сек)
6. На каждой закрытой свече вызывает `on_candle` — основной обработчик

---

## 2. Источники данных

### 2.1 Биржа и типы данных
- **Биржа**: Binance Futures (USDT-M perpetual)
- **Протокол**: REST API (python-binance `AsyncClient`) + WebSocket не используется (polling)
- **Данные**:
  - **Свечи (klines)**: OHLCV + volume, таймфреймы из конфига (`timeframe`, `htf_timeframe`)
  - **Позиции**: `futures_position_information` (размер, entry price, unrealized PnL)
  - **Баланс**: `futures_account_balance` (USDT available)
  - **Исторические сделки**: `futures_user_trades` (для PnL синхронизации)
  - **Стакан/тикеры**: не используются напрямую (только close price свечи)

### 2.2 Получение и обработка свечей (`bot/market_data.py`)
```python
# get_recent_klines — загрузка последних N свечей для прогрева
# get_historical_klines — загрузка диапазона для бэктеста
# start_kline_polling — REST polling каждые 10 сек
```
- **Polling**: каждые 10 сек запрашивает 2 последние свечи, определяет закрытую по `close_time`
- **dedup**: хранит `last_seen[interval]`, не отдаёт дубликаты
- **Конвертация**: `_klines_to_df` → pandas DataFrame с индексом `open_time` (datetime), колонки float

### 2.3 Higher Timeframe (HTF) фильтр
- Параллельно загружаются свечи старшего ТФ (`htf_timeframe`, по умолчанию 1h)
- Считаются EMA (`htf_ema_fast`=9, `htf_ema_slow`=21)
- Тренд: `LONG` если fast > slow, иначе `SHORT`
- Сигналы против тренда блокируются (см. `strategy.py:get_htf_trend`)

---

## 3. Индикаторы и генерация сигналов (`bot/strategy.py`)

### 3.1 Основные индикаторы (LTF)
| Индикатор | Формула | Параметры конфига |
|-----------|---------|-------------------|
| EMA fast | `ewm(span=ema_fast)` | `ema_fast` (default 12) |
| EMA slow | `ewm(span=ema_slow)` | `ema_slow` (default 26) |
| Volume MA | `rolling(volume_ma_period).mean()` | `volume_ma_period` (default 20) |

### 3.2 Условия входа (`get_signal`)
Сигнал генерируется на **закрытой** свече (`df.iloc[-2]` — предыдущая, `df.iloc[-1]` — текущая):

**LONG**:
```
prev.ema_fast <= prev.ema_slow  AND  curr.ema_fast > curr.ema_slow  (кросс вверх)
AND volume >= volume_ma * volume_multiplier
```

**SHORT**:
```
prev.ema_fast >= prev.ema_slow  AND  curr.ema_fast < curr.ema_slow  (кросс вниз)
AND volume >= volume_ma * volume_multiplier
```

### 3.3 Расчёт уровней (SL/TP)
```
sl_dist   = entry * sl_pct   / 100
tp1_dist  = entry * tp1_pct  / 100
tp2_dist  = entry * tp2_pct  / 100

LONG:  SL = entry - sl_dist,  TP1 = entry + tp1_dist,  TP2 = entry + tp2_dist
SHORT: SL = entry + sl_dist,  TP1 = entry - tp1_dist,  TP2 = entry - tp2_dist
```

### 3.4 Signal dataclass
```python
@dataclass
class Signal:
    direction: "LONG" | "SHORT"
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    timestamp: pd.Timestamp
    ema_fast, ema_slow, volume, volume_ma: float
```

### 3.5 HTF фильтр
Если `htf_enabled=True`, сигналы против тренда HTF отбрасываются в `on_candle` (стр. 468-472 main.py).

---

## 4. Подтверждение сигналов (`bot/signal_handler.py`)

### 4.1 Режимы подтверждения
| Режим | Поведение |
|-------|-----------|
| `auto_mode=True` | Всегда `True` |
| `backtest` | Всегда `True` |
| `paper` / `live` без `auto_mode` | Интерактивный prompt в stdin с таймаутом 60 сек |

### 4.2 SignalHandler.confirm()
- Логирует сигнал
- В auto/backtest возвращает `True` сразу
- В semi-auto ждёт `y/n` от пользователя 60 сек

---

## 5. Расчёт размера позиции (`bot/order_manager.py`)

### 5.1 Обычный расчёт (`calc_quantity`)
Формула риска (леверидж **не** влияет на qty, только на маржу):
```
risk_amount     = balance * risk_pct / 100
sl_distance     = entry_price * sl_pct / 100
quantity        = risk_amount / sl_distance
```

### 5.2 Recovery-расчёт (`calc_recovery_quantity`)
Для компенсирующих сделок (recovery mode):
```
target_profit = debt_amount * (1 + bonus_pct/100)
tp1_distance_after_fee = entry * tp1_pct/100 * (1 - 0.0004)
raw_qty = target_profit / tp1_distance_after_fee
qty = min(raw_qty, max_qty)  # если задан max_pct от депозита
```

### 5.3 Адаптация под фильтры символа (`_adjust_qty`, `_adjust_price`)
- Загружает `LOT_SIZE.stepSize` и `PRICE_FILTER.tickSize` через `futures_exchange_info`
- Округляет qty вниз до stepSize, цену — к ближайшему tickSize
- В paper/live режимах работает по-разному (live — реальные фильтры, paper — округление)

---

## 6. Исполнение ордеров (`bot/order_manager.py`)

### 6.1 Открытие позиции (`open_position`)
```python
# 1. Расчёт qty (обычный или recovery)
# 2. Адаптация qty под stepSize
# 3. Live: futures_create_order(type=MARKET, side, quantity)
# 4. Получение реальной цены входа (_get_fill_price через avgPrice/fills)
# 5. Размещение SL/TP:
#    - recovery: SL + TP1 (100% qty), SL = entry, TP1 = entry±tp1_pct
#    - обычный:  SL (100% qty) + TP1 (tp1_close_pct%) + TP2 (остаток)
```
**Типы ордеров**:
- Вход: `MARKET` (reduceOnly=False)
- SL: `STOP_MARKET` (reduceOnly=True, priceProtect=True)
- TP1/TP2: `LIMIT` (reduceOnly=True, timeInForce=GTC)

### 6.2 Управление SL/TP после TP1
При срабатывании TP1 (`tracker.apply_hit("TP1")`):
1. Перенос SL в breakeven (`move_sl_to_breakeven`):
   - Отмена всех открытых ордеров
   - Новый SL = entry_price (reduceOnly=True)
   - Перестановка TP2 (если остаток > 0)

### 6.3 Закрытие позиции
- `close_partial` / `close_full` — подтверждение исполнения через `_get_real_position_qty`
- `close_dust` — закрытие пыли (notional < $1) маркет-ордером reduceOnly

### 6.4 PnL синхронизация (`get_realized_pnl`)
Парсит `futures_user_trades` за период сделки:
- Первая сделка (entry): суммирует commission
- Выходная сделка: `realizedPnl - commission`
- Итоговый PnL = сумма exit_net - сумма entry_commission

---

## 7. Управление позицией и трекинг (`bot/position_tracker.py`)

### 7.1 Position dataclass
```python
@dataclass
class Position:
    direction: "LONG" | "SHORT"
    entry_price, sl_price, tp1_price, tp2_price: float
    total_qty, remaining_qty: float
    tp1_hit: bool = False
    closed: bool = False
    realized_pnl: float = 0.0
    entry_timestamp: pd.Timestamp
    is_recovery: bool = False
    recovery_chain_id: Optional[int]
    # + индикаторы на входе для логирования
```

### 7.2 Проверка хитов (`check`)
На каждой свече (`on_candle`) вызывает `tracker.check(current_price)`:
- **LONG**: price <= SL → "SL"; price >= TP1 (и не tp1_hit) → "TP1"; price >= TP2 (и tp1_hit) → "TP2"
- **SHORT**: зеркально

### 7.3 Применение хитов (`apply_hit` / `apply_hit_async`)
| Хит | Действие |
|-----|----------|
| **TP1** (обычный) | Закрывает `tp1_close_pct%` qty, переносит SL → entry, `tp1_hit=True`, сохраняет state |
| **TP1** (recovery) | Закрывает 100% позиции, полный PnL, `closed=True` |
| **TP2** | Закрывает остаток `remaining_qty`, полный PnL, `closed=True` |
| **SL** | Если `tp1_hit=True` → exit_reason="TP1" (безубыток), иначе "SL". Закрывает 100%, `closed=True` |

### 7.4 Сохранение состояния (`_save_state` / `load_state`)
JSON файл `state_{symbol}.json` с полным снапшотом позиции + `trade_id` из БД.
При старте бота `_sync_position_on_start` восстанавливает позицию с биржи и пересчитывает уровни из конфига.

### 7.5 PnL синхронизация с биржей
- `_sync_pnl_from_exchange` — после закрытия запрашивает `get_realized_pnl` и патчит запись в БД
- `sync_unrealized_pnl` — периодически (каждые 12 свечей) обновляет unrealized PnL в дашборде через `get_position_info`

---

## 8. Recovery Mode (Компенсация убытков) (`bot/recovery_client.py`, `main.py`)

### 8.1 Архитектура
- Центральный API сервер (порт 5000) хранит цепочки долгов (`chains`)
- Несколько ботов (отдельные процессы) координируются через HTTP
- Атомарный захват долга: `POST /recovery/claim` → возвращает `chainId`, `debtAmount`, `bonusPct`

### 8.2 Жизненный цикл
1. На сигнале `handler.confirm()` → `recovery.claim()` (стр. 479-506 main.py)
2. Если `chainId` получен → считает `recovery_qty` через `calc_recovery_quantity`
3. Открывает позицию с `recovery_qty`, флаг `is_recovery=True`, `recovery_chain_id=chainId`
4. При закрытии: `recovery.report(pnl, chain_id)` или `recovery.release(chain_id)` при ошибке
5. Сервер обновляет долг: при плюсе — долг уменьшается, при минусе — растёт

### 8.3 Конфиг recovery (`recovery_config.yaml`)
```yaml
recovery_enabled: true
recovery_bonus_pct: 10.0   # бонус к долгу при закрытии в плюсе
recovery_max_pct: 50.0     # макс. % депозита под recovery-сделку (0 = без лимита)
```

---

## 9. Бэктестинг и оптимизация

### 9.1 Бэктестер (`bot/backtester.py`)
- `run_backtest` — скачивает исторические свечи + HTF, прогоняет по свечам
- Использует тот же `get_signal`, `PositionTracker`, `calc_quantity`
- Упрощённый `PositionTracker` без БД/биржи (методы `open`/`apply_hit` синхронные)
- Статистика: `BacktestStats` (trades, win_rate, total_pnl, max_drawdown, avg_win/loss, return%)

### 9.2 Оптимизатор (`bot/optimizer.py`)
- **Optuna** (TPE sampler, seed=42)
- Параметры поиска:
  ```
  ema_fast: 5..20, ema_slow: ema_fast+3..55
  sl_pct: 0.2..1.5%, tp1_pct: 0.2..1.0%, tp2_pct: tp1+0.1..2.5%
  volume_multiplier: 1.0..2.5, tp1_close_pct: 30..70
  ```
- Скоринг: `profit_factor * sqrt(n_trades)`, мин. 10 сделок
- Данные скачиваются **один раз**, затем переиспользуются во всех триалах
- Результаты: консольный топ-10 + CSV в `logs/optimization_{timestamp}.csv`

---

## 10. Конфигурация

### 10.1 Основной конфиг (`config.yaml` + per-symbol overrides)
```yaml
symbol: "BTCUSDT"
timeframe: "5m"
leverage: 10
risk_pct: 2.0
sl_pct: 0.8
tp1_pct: 0.4
tp1_close_pct: 50
tp2_pct: 1.2
ema_fast: 12
ema_slow: 26
volume_ma_period: 20
volume_multiplier: 1.5
mode: "paper"          # live | paper | backtest
auto_mode: false       # true = без подтверждения
backtest_start: "2026-01-01"
backtest_end: "2026-06-01"
paper_balance: 10000
log_file: "logs/bot.log"
htf_enabled: true
htf_timeframe: "1h"
htf_ema_fast: 9
htf_ema_slow: 21
```

### 10.2 Переменные окружения (`.env`)
```env
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
DASHBOARD_API_URL=http://localhost:5000/api
RECOVERY_CONFIG_PATH=bot/recovery_config.yaml
```

---

## 11. Логирование и отчётность

### 11.1 Логгеры (`bot/logger.py`)
- **Основной** (`bot.log`): RotatingFileHandler 10MB×5, фильтр `TradeOnlyFilter` (пишет только ключевые события: открытие/закрытие, TP/SL, SL move, recovery)
- **Событийный** (`logs/events.log`): все INFO+ события для дашборда

### 11.2 DbReporter (`bot/db_reporter.py`)
- HTTP клиент к API серверу (`DASHBOARD_API_URL`)
- `report_trade` (open), `patch_trade` (close/pnl update), `report_position` (heartbeat), `report_heartbeat`
- Используется для дашборда (порт 5173)

---

## 12. Синхронизация при старте (`_sync_position_on_start`)

При запуске в live-режиме:
1. Запрашивает `futures_position_information`
2. Если позиция есть на бирже, а стейта нет — создаёт `Position` из биржевых данных, пересчитывает SL/TP из конфига
3. Если стейт есть, а биржи нет — считает позицию закрытой внешне, чистит стейт
4. Частичное закрытие / полное закрытие / пыль — авто-детект через сравнение `tracker.qty` vs `exchange_qty`
5. Переставляет SL/TP ордера под актуальные уровни

---

## 13. Файловая структура ключевых модулей

```
bot/
├── main.py              # Точка входа, главный цикл, on_candle
├── config.py            # Config dataclass + YAML load/validation
├── market_data.py       # Klines загрузка + REST polling
├── strategy.py          # EMA cross + volume + HTF filter + Signal
├── signal_handler.py    # Auto/semi-auto подтверждение
├── order_manager.py     # Qty calc, order placement, SL/TP mgmt, PnL sync
├── position_tracker.py  # Position state, hits, persistence, PnL sync
├── signal_handler.py    # Подтверждение сигналов
├── position_tracker.py  # Состояние позиции, хиты, стейт
├── recovery_client.py   # HTTP клиент для recovery API
├── backtester.py        # Backtest engine
├── optimizer.py         # Optuna оптимизация
├── backtest_runner.py   # CLI для API-сервера
├── logger.py            # Dual logger (trade-only + events)
├── db_reporter.py       # HTTP reporter к dashboard API
├── logger.py            # Логгеры
├── logger.py            # Логгер
└── recovery_config.yaml # Recovery параметры
```

---

## 14. Примеры ключевых точек расширения

| Задача | Куда смотреть |
|--------|---------------|
| Добавить новый индикатор | `strategy.py:calculate_indicators` + `get_signal` |
| Сменить условие входа | `strategy.py:get_signal` |
| Изменить формулу риска | `order_manager.py:calc_quantity` |
| Добавить новый тип ордера | `order_manager.py:_place_*` |
| Новый тип TP/SL логики | `position_tracker.py:apply_hit` |
| Новый recovery-алгоритм | `order_manager.py:calc_recovery_quantity` + `recovery_client.py` |
| Добавить биржу | Адаптеры под `AsyncClient` интерфейс в `market_data`, `order_manager` |

---

## 15. Запуск компонентов вручную

```bash
# Бэктест
cd bot && python -m backtester --config ../config.yaml

# Оптимизация
cd bot && python optimizer.py --trials 200 --symbol ETHUSDT --timeframe 15m

# Бэктест через API (используется дашбордом)
echo '{"symbol":"ETHUSDT","start":"2026-05-01","end":"2026-06-01","config":{"timeframe":"5m","leverage":10}}' | python backtest_runner.py

# Анализ пересечений сигналов
python signal_overlap.py config_eth.yaml config_sol.yaml config_btc.yaml
```

---

*Последнее обновление: 2026-07-21*
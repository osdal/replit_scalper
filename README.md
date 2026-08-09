# Trading Bot — документация

Краткое описание: автоматический торговый бот для Binance Futures (USDT-M) и KuCoin Futures, реализующий стратегию EMA-кроссов с фильтром объёма, HTF-трендом и ADX. Поддерживает три режима работы (live, paper, backtest), систему компенсации убытков (recovery), оптимизацию параметров и веб-дашборд.

---

## 1. Архитектура проекта

```
replit_scalper/
├── bot/                          # Основной код бота (Binance Futures)
│   ├── main.py                   # Точка входа, главный цикл, on_candle
│   ├── config.py                 # Config dataclass + YAML load/validation
│   ├── market_data.py            # Загрузка свечей (REST polling каждые 10с)
│   ├── strategy.py               # EMA cross + volume + HTF + ADX фильтры
│   ├── signal_handler.py         # Подтверждение сигналов (auto/semi-auto)
│   ├── order_manager.py          # Расчёт qty, размещение ордеров, SL/TP, PnL sync
│   ├── position_tracker.py       # Состояние позиции, хиты SL/TP, persistence
│   ├── recovery_client.py        # HTTP клиент к recovery API (aiohttp)
│   ├── backtester.py             # Движок бэктеста
│   ├── optimizer.py              # Optuna-оптимизация параметров
│   ├── logger.py                 # Двойной логгер (trade-only + events)
│   ├── db_reporter.py            # HTTP репортёр trades/heartbeat в API
│   ├── notifier.py               # Telegram уведомления (PNG-карточки)
│   ├── config_<symbol>.yaml      # Per-symbol конфиги
│   ├── recovery_config.yaml      # Recovery-параметры
│   ├── requirements.txt
│   └── logs/
├── kucoin/                       # KuCoin Futures (отдельный скрипт-тест)
│   ├── main_kucoin.py
│   ├── kucoin_client.py
│   ├── kucoin_config.py
│   └── .env
├── artifacts/
│   ├── api-server/               # Express API (порт 5000)
│   │   └── src/
│   │       ├── routes/           # bots, trades, backtest, recovery, optimizer
│   │       └── init-db.ts        # Инициализация БД из config_*.yaml
│   └── dashboard/                # React/Vite дашборд (порт 5173)
│       └── src/
│           ├── Dashboard.tsx
│           ├── hooks/useApi.ts
│           ├── OptimizerTab.tsx
│           ├── RecoveryTab.tsx
│           └── components/ui/
├── data/
│   └── bot.db                    # SQLite (bots, trades, recovery_chains)
├── config/
│   ├── binance/                  # Binance per-symbol YAML (альтернативная папка)
│   └── kucoin/                   # KuCoin per-symbol YAML
├── support-bot/                  # Telegram support-bot
├── .env.example                  # Пример переменных окружения
├── pyproject.toml                # Python зависимости
├── package.json                  # Node зависимости (dashboard/api)
├── start-all.ps1 / .bat / .sh    # Скрипты запуска
└── README.md
```

### Взаимодействие компонентов

1. `main.py` загружает `config_<symbol>.yaml`, создаёт `AsyncClient` (python-binance).
2. `market_data.start_kline_polling` REST-опрашивает Binance Futures каждые 10 секунд.
3. На закрытой свече вызывается `on_candle` → `strategy.get_signal` (EMA + volume + HTF + ADX).
4. `signal_handler.confirm` — подтверждение сигнала (auto/semi-auto).
5. `recovery_client.claim` — атомарный захват долга через API сервер.
6. `order_manager.open_position` — расчёт qty, MARKET-ордер, SL/TP.
7. `position_tracker` отслеживает hits (SL/TP1/TP2), обновляет состояние, пишет в БД через `db_reporter`.
8. `notifier` отправляет PNG-карточки в Telegram.
9. Dashboard (React) подключается к API серверу (Express) на порту 5000.

---

## 2. Ключевые торговые стратегии

### 2.1 Основной сигнал (EMA Cross + Volume)

Бот использует **EMA-кроссов** на закрытой свече с подтверждением объёмом.

**Условие LONG:**
```
prev.ema_fast <= prev.ema_slow  AND  curr.ema_fast > curr.ema_slow
AND curr.volume >= curr.volume_ma * volume_multiplier
```

**Условие SHORT:**
```
prev.ema_fast >= prev.ema_slow  AND  curr.ema_fast < curr.ema_slow
AND curr.volume >= curr.volume_ma * volume_multiplier
```

Параметры по умолчанию: `ema_fast=12`, `ema_slow=26`, `volume_ma_period=20`, `volume_multiplier=1.5`.

### 2.2 HTF-фильтр (Higher Timeframe)

При `htf_enabled=True` сигналы против тренда старшего ТФ блокируются.
- HTF EMA fast/slow (по умолчанию 9/21 на 1h)
- Тренд: `LONG` если `htf_ema_fast > htf_ema_slow`, иначе `SHORT`
- Сигнал допускается только если `signal.direction == htf_trend`

### 2.3 ADX-фильтр

При `adx_threshold > 0` сигналы допускаются только если `curr.adx >= adx_threshold` (по умолчанию 0 = отключён).

### 2.4 Расчёт уровней SL/TP

```
entry = curr["close"]
sl_dist  = entry * sl_pct  / 100
tp1_dist = entry * tp1_pct / 100
tp2_dist = entry * tp2_pct / 100

LONG:  SL = entry - sl_dist,  TP1 = entry + tp1_dist,  TP2 = entry + tp2_dist
SHORT: SL = entry + sl_dist,  TP1 = entry - tp1_dist,  TP2 = entry - tp2_dist
```

### 2.5 Recovery-режим

При `recovery_enabled=True` бот координируется через центральный API сервер:
- Перед входом `recovery.claim()` захватывает свободный долг (chain).
- Qty рассчитывается так, чтобы TP1 покрыл `debt * (1 + bonus_pct/100)`.
- При закрытии `recovery.report(pnl)` обновляет долг; при убытке долг растёт.
- При SL recovery-цепи бот освобождает chain и создаёт новый долг.

### 2.6 Резжимы работы

- **live** — реальная торговля на Binance Futures (USDT-M perpetual).
- **paper** — симуляция без реальных ордеров (используется `paper_balance`).
- **backtest** — прогон на исторических данных через `backtester.py`.

---

## 3. Управление рисками

### 3.1 Размер позиции

Бот поддерживает 4 режима расчёта qty (приоритет сверху вниз):

1. `margin_pct > 0` — % от депозита на маржу: `margin = round(balance * pct / 100, 1)`, `qty = margin * leverage / entry`
2. `fixed_notional_usd > 0` — фиксированная маржа в USD: `qty = fixed_notional_usd * leverage / entry`
3. `fixed_qty > 0` — фиксированное количество монет
4. `fixed_risk_usd > 0` — фиксированный риск в USD: `qty = fixed_risk_usd / (entry * sl_pct%)`
5. Иначе — `risk_pct%` от баланса: `risk_amount = balance * risk_pct / 100`, `qty = risk_amount / (entry * sl_pct/100)`

> **Важно:** leverage влияет только на маржу, не на размер позиции в монетах.

### 3.2 Stop-Loss и Take-Profit

- **SL** размещается как `STOP_MARKET` (reduceOnly=True, priceProtect=True).
- **TP1** — `LIMIT` (reduceOnly=True, GTC), закрывает `tp1_close_pct%` позиции (по умолчанию 50%).
- **TP2** — `LIMIT` (reduceOnly=True, GTC), закрывает остаток.
- При срабатывании TP1 SL автоматически переносится в breakeven (`entry_price`).

### 3.3 Recovery-ограничения

- `recovery_max_pct` — макс. % депозита под recovery-сделку.
- `max_positions` — глобальный лимит открытых позиций (через API).
- `loss_streak_trigger` — после N убытков подряд включается пауза на `loss_pause_signals` сигналов.
- `daily_loss_limit_usd` — дневной лимит убытков (USDT).
- `max_free_debt_usd` — потолок долга recovery (новые claim запрещены при превышении).

### 3.4 Синхронизация позиции

При старте бота (`_sync_position_on_start`):
1. Запрашивает позиции с биржи.
2. Если позиция есть, а стейта нет — восстанавливает `Position`, пересчитывает SL/TP из конфига.
3. Если стейт есть, а биржи нет — считает позицию закрытой внешне, чистит состояние.
4. Частичное закрытие / пылевая позиция — авто-детект.

### 3.5 Защита от дублирования

- Lock-файл `bot.lock.<symbol>` предотвращает запуск двух инстансов одного символа.
- Если при сигнале позиция уже открыта — новый сигнал пропускается.

---

## 4. Работа с API Binance

### 4.1 Клиент

Используется `python-binance` (`AsyncClient`) — асинхронный клиент Binance Futures.

### 4.2 Источники данных

| Тип данных | Endpoint / Метод | Назначение |
|------------|------------------|------------|
| Свечи (klines) | `futures_klines` / `futures_historical_klines` | OHLCV для стратегии и бэктеста |
| Позиции | `futures_position_information` | Контроль позиции, синхронизация |
| Баланс | `futures_account_balance` | Расчёт размера позиции (live) |
| Ордера | `futures_create_order`, `futures_cancel_all_open_orders` | Вход, SL/TP, выход |
| Информация о символе | `futures_exchange_info` | LOT_SIZE, PRICE_FILTER (шаги округления) |
| Тикер | `futures_symbol_ticker` | Текущая цена (для dust, syncing) |
| PnL | `futures_income_history` + `futures_account_trades` | Реальный PnL после закрытия |
| Algo-ордера | `futures_get_open_algo_orders`, `futures_cancel_algo_order` | Очистка algo-ордеров при SL move |

### 4.3 Обработка ошибок и реконнекты

- **Polling**: REST-запрос каждые 10 секунд, `last_seen` защита от дубликатов.
- **Ретраи**: `db_reporter` и `recovery_client` делают 3 попытки с `aiohttp.ClientTimeout(total=5)`.
- **SL placement retry**: 3 попытки с задержкой 1.5с при переносе SL в breakeven.
- **Позиция после входа**: 3 попытки по 1с verify позицию на бирже.
- **Graceful shutdown**: `SIGTERM`/`SIGINT` → `shutdown_event.set()` → очистка lock-файла, закрытие соединений.

### 4.4 Ограничения Binance

- `futures_income_history` хранит данные ~7 дней (fallback на `futures_account_trades` для старых сделок).
- priceProtect=True для STOP_MARKET защищает от всплесков цены.
- `reduceOnly=True` для всех защитных ордеров.

---

## 5. Переменные окружения (ENV)

Создайте файл `.env` в корне проекта на основе `.env.example`:

```env
# ============================================
# Binance API
# ============================================
BINANCE_API_KEY=YOUR_API_KEY_HERE
BINANCE_API_SECRET=YOUR_API_SECRET_HERE

# ============================================
# API Server (Express)
# ============================================
PORT=5000
BOT_DIR=./bot
DATABASE_PATH=./data/bot.db

# ============================================
# Dashboard / Recovery
# ============================================
DASHBOARD_API_URL=http://localhost:5000/api
RECOVERY_CONFIG_PATH=bot/recovery_config.yaml

# ============================================
# Telegram Notifications (опционально)
# ============================================
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TELEGRAM_CONNECT_URL=https://t.me/your_bot?start=connect
TELEGRAM_SUPPORT_USERNAME=your_support_username

# ============================================
# Support Bot (опционально)
# ============================================
SUPPORT_BOT_TOKEN=your_support_bot_token_from_BotFather
SUPPORT_CHAT_ID=your_chat_id
SUPPORT_BOT_MASTER_KEY=base64_32_bytes
SUPPORT_BOT_DB=./support-bot/data/support_bot.db
```

---

## 6. Установка и запуск

### 6.1 Python-зависимости

```powershell
cd C:\DATA\bots\replit_scalper
python -m venv venv
.\venv\Scripts\activate
pip install -r bot\requirements.txt
```

### 6.2 Node-зависимости (dashboard + API server)

```powershell
cd C:\DATA\bots\replit_scalper
pnpm install
```

### 6.3 Инициализация БД

```powershell
cd C:\DATA\bots\replit_scalper
pnpm --filter @workspace/api-server run init-db
```

### 6.4 Запуск (Windows PowerShell)

**Вариант A: Ручной запуск (3 консоли)**

```powershell
# Консоль 1: API сервер
cd C:\DATA\bots\replit_scalper
pnpm --filter @workspace/api-server run dev

# Консоль 2: Дашборд
cd C:\DATA\bots\replit_scalper
pnpm --filter @workspace/dashboard run dev

# Консоль 3: Бот (пример для BTC)
cd C:\DATA\bots\replit_scalper\bot
python main.py config_btc.yaml
```

**Вариант B: Автоматический запуск**

```powershell
cd C:\DATA\bots\replit_scalper
.\start-all.ps1
```

Откройте `http://localhost:5173` и нажмите **Start** на нужных ботах.

### 6.5 Запуск ботов (через дашборд)

После запуска API сервера и дашборда:
1. Откройте `http://localhost:5173`
2. Нажмите **Start** на карточке нужного бота.
3. API сервер запустит `python main.py config_<symbol>.yaml` как отдельный процесс.

### 6.6 Остановка

```powershell
# Остановить конкретного бота — кнопка Stop в дашборде
# Остановить все боты — кнопка Stop All & Reload Configs
# Или принудительно:
Stop-Process -Name python -ErrorAction SilentlyContinue
Stop-Process -Name node -ErrorAction SilentlyContinue
```

---

## 7. Логирование

### 7.1 Два логгера

1. **Trade-логи** (`bot/logs/<symbol>.log`):
   - Фильтр `TradeOnlyFilter` — пишет только ключевые события:
     - `Position opened`, `TP1 hit`, `TP2 hit`, `SL hit`
     - `SL moved to breakeven`
     - `[LIVE] Market order placed`, `[PAPER] Would open`
     - `[SYNC] Restored`, `[RECOVERY]`
   - Ротация: 10 MB × 5 файлов (`RotatingFileHandler`).

2. **Events-логи** (`bot/logs/events.log`):
   - Пишет все `INFO+` события для дашборда.
   - Формат: `%(asctime)s [%(symbol)s] %(message)s`.

### 7.2 Уровни детализации

- **Файл (trade)**: `DEBUG` (но фильтр оставляет только trade-события).
- **Консоль (stream)**: `INFO`.
- **Events**: `DEBUG` (все ключевые события).

### 7.3 Пример лога

```
2026-07-21 14:32:01 [LIVE XRPUSDT] INFO Position opened | LONG | entry=0.6234 SL=0.6167 TP1=0.6301 TP2=0.6368 qty=1500.0
2026-07-21 14:35:22 [LIVE XRPUSDT] INFO TP1 hit | price=0.6305 closed_qty=1050.0 remaining_qty=450.0 pnl=11.55 | SL moved to breakeven
2026-07-21 14:41:08 [LIVE XRPUSDT] WARNING SL hit (SL level) | price=0.6167 qty=450.0 pnl=-3.03 total_pnl=8.52
```

---

## 8. Известные проблемы / TODO

### 8.1 Критические

1. **`bot/main.py` строки 342-376 — частичное заполнение**  
   При `real_qty < qty * 0.9` qty корректируется, но `signal.entry_price` не обновляется. TP/SL уровни остаются на основе исходного сигнала, что может привести к расхождению с реальными уровнями на бирже.

2. **`bot/main.py` строки 339-342 — импорт requests внутри функции**  
   `import requests` выполняется внутри `_sync_position_on_start` при каждом вызове. Лучше вынести на уровень модуля.

3. **`bot/position_tracker.py` строка 299 — `_report_tp1` пустая**  
   Метод заглушка (pass), TP1 не репортится в БД. Это может искажать статистику в дашборде, если позиция не доходит до полного закрытия.

### 8.2 Средние

4. **`bot/main.py` строки 1241 — `asyncio.run(main())`**  
   При повторном запуске в том же процессе (например, в Jupyter/IDE) вызовет `RuntimeError: asyncio.run() cannot be called from a running event loop`.

5. **`bot/order_manager.py` строки 558-589 — fallback на userTrades**  
   Парсинг `futures_account_trades` для PnL может быть неточным при сложных сценариях (частичные закрытия,Funding). Рекомендуется полагаться на `futures_income_history` там, где возможно.

6. **`bot/market_data.py` строки 145-148 — `_kline_to_series` не используется**  
   Функция определена, но нигде не вызывается — можно удалить или использовать для WebSocket-адаптера.

7. **`bot/signal_handler.py` строки 35-39 — `asyncio.wait_for` + `run_in_executor`**  
   В Windows `input()` в executor может блокировать event loop при shutdown. Нужно добавить проверку `shutdown_event` внутри wait_for.

### 8.3 Долгосрочные

8. **Отсутствие WebSocket** — используется REST polling (10с). Для снижения задержки можно добавить WebSocket `kline` стрим.
9. **Жёсткая привязка к Binance** — нет абстрактного интерфейса биржи, KuCoin версия (`kucoin/main_kucoin.py`) полностью отдельная.
10. **`recovery_client.py` использует `aiohttp` как опциональную зависимость** — если библиотека не установлена, recovery молча отключается. Лучше явно указывать в requirements.

---

## 9. Зависимости

### 9.1 Python (`bot/requirements.txt`)

| Библиотека | Версия | Назначение |
|------------|--------|------------|
| `python-binance` | >=1.0.19 | Асинхронный клиент Binance Futures |
| `pandas` | >=2.0.0 | Обработка свечей, индикаторы |
| `pyyaml` | >=6.0 | Загрузка конфигов |
| `python-dotenv` | >=1.0.0 | Переменные окружения |
| `websockets` | >=12.0 | WebSocket клиент (задел) |
| `optuna` | >=3.6.0 | Оптимизация параметров стратегии |
| `python-telegram-bot` | >=21.0 | Telegram уведомления |

### 9.2 Node.js (`package.json`)

| Пакет | Назначение |
|--------|-----------|
| `express` | API сервер (порт 5000) |
| `drizzle-orm` + `better-sqlite3` | ORM + SQLite |
| `react` + `vite` + `tailwindcss` | Дашборд |
| `recharts` | Графики PnL |
| `lucide-react` | Иконки |
| `js-yaml` | Парсинг config_*.yaml при init-db |

### 9.3 Системные

- Python >= 3.11
- Node.js >= 18
- SQLite (встроен в Python, для API используется `better-sqlite3`)
- Windows: Arial шрифты для PNG-уведомлений (`C:/Windows/Fonts/arial.ttf`)

---

## 10. Конфигурация

### 10.1 Основной конфиг (`bot/config_<symbol>.yaml`)

```yaml
symbol: "BTCUSDT"
timeframe: "5m"
leverage: 10
risk_pct: 2.0
sl_pct: 0.8
tp1_pct: 0.4
tp1_close_pct: 50          # % позиции для закрытия на TP1
tp2_pct: 1.2
ema_fast: 12
ema_slow: 26
volume_ma_period: 20
volume_multiplier: 1.5
mode: "paper"               # live | paper | backtest
auto_mode: false            # true = без подтверждения
backtest_start: "2026-01-01"
backtest_end: "2026-06-01"
paper_balance: 10000.0
log_file: "logs/bot.log"
htf_enabled: true
htf_timeframe: "1h"
htf_ema_fast: 9
htf_ema_slow: 21
recovery_max_position_pct: 100.0
fixed_qty: 0.0
margin_pct: 0.0
fixed_notional_usd: 0.0
fixed_risk_usd: 0.0
adx_period: 14
adx_threshold: 0.0          # 0 = отключён
```

### 10.2 Recovery конфиг (`bot/recovery_config.yaml`)

```yaml
recovery_enabled: true
recovery_bonus_pct: 50
recovery_max_pct: 50
max_positions: 2
loss_streak_trigger: 2
loss_pause_signals: 3
pause_timeout_minutes: 120
max_free_debt_usd: 15.0
daily_loss_limit_usd: 8.0
```

---

## 11. Вопросы к автору для доработки

1. **Алгоритм выбора монет** — бот торгует одной парой на процесс. Как вы планируете масштабировать на N пар: отдельные процессы, asyncio-луп с несколькими символами, или через дашборд-оркестрацию?
2. **Частичное заполнение ордеров** — при `real_qty < qty * 0.9` бот корректирует qty, но не пересчитывает TP/SL. Как должен поступать бот в этом случае: пересчитывать уровни под реальный entry, или отменять сигнал?
3. **Funding fee / комиссии** — в `get_realized_pnl` учитывается только `REALIZED_PNL + COMMISSION + FUNDING_FEE`. Нужно ли дополнительно учитывать комиссии за открытие/закрытие в `calc_quantity` (сейчас риск считается от entry * sl_pct без комиссий)?
4. **Обработка ликвидаций** — при ликвидации `positionAmt` становится 0, но `get_realized_pnl` может вернуть `None` (если income history уже очищен). Как должен бот фиксировать такой исход: как SL, как отдельный `LIQUIDATION`, или игнорировать?
5. **Сетевые разрывы** — при долгом отсутствии связи биржа может закрыть позицию по SL/TP, но бот не узнает об этом до следующей синхронизации (каждые 12 свечей / 1 час). Нужно ли добавить WebSocket для мгновенного отслеживания ордеров?
6. **Глобальный risk-manager** — сейчас `recovery_client.can_open()` проверяет лимит позиций и паузу после убытков. Хотите ли вы расширить это до Portfolio-level VaR, максимального просадки за день, или ограничения по корреляции между парами?
7. **Дублирование кода KuCoin** — `kucoin/main_kucoin.py` полностью отделён от основного бота. Планируете ли вы унифицировать архитектуру под CCXT, чтобы добавлять новые биржи через адаптер, или оставить KuCoin как отдельный pet-проект?

---

## 12. Ежедневная автоматическая оптимизация

### 12.1 Скрипт `auto-optimize-daily.ps1`

Запускает walk-forward оптимизацию для всех 24 пар и применяет лучшие параметры к `config_*.yaml`.

- Период по умолчанию: 35 дней (`DaysBack=35`)
- Оптимизация: 200 trials × 2 folds через Optuna
- Batch: 2 пары параллельно, `jobs=1` (не грузить CPU живыми ботами)
- После оптимизации: `/api/refresh` → перезапуск ботов на новых параметрах
- Telegram-уведомления на английском: `COMPLETE`, `PARTIAL`, `FAILED`

### 12.2 Walk-forward логика

Период делится на 2 фолда:
- Fold 1: train `[start .. mid]`, test `[mid .. end]`
- Fold 2: train `[mid .. end]`, test `последний отрезок`

Из каждого фолда выбирается лучший набор параметров по `test_pnl`. Затем проверяется на тестовых данных через `run_backtest`. Параметры применяются через `apply_params_to_config`.

### 12.3 Мониторинг

Скрипт пишет лог в `bot/logs/auto-opt_<дата>.log` и статус в `bot/logs/optimization_status.txt`:
- `START` — начало
- `OPT_DONE` — оптимизация завершена
- `PARTIAL` — часть пар не оптимизирована
- `FAILED` — аварийный сбой
- `RESTART_OK` — боты перезапущены

### 12.4 Известные ограничения

- HBARUSDT/NEARUSDT часто падают по таймауту на этапе бэктеста (низкая ликвидность, Binance ограничивает запросы)
- Если daily-скрипт упадёт до финального статуса — проверь `optimization_status.txt` и логи
- Оптимизация не останавливает боты во время работы (они работают на старых параметрах)

---

## 13. Специальные механизмы

### 13.1 Пропуск исторических свечей при старте (`skip_catchup`)

В `bot/market_data.py` добавлен флаг `skip_catchup=True` (по умолчанию для live/paper режимов).

При старте бот:
1. Запрашивает последнюю закрытую свечу у Binance
2. Устанавливает `last_seen` на **последнюю** закрытую свечу (не предпоследнюю)
3. Пропускает все свечи между `last_seen` и текущим моментом
4. Начинает торговлю с **следующей** закрытой свечи

**Эффект:** бот стартует за ~47 секунд вместо 30 минут догоняющего цикла. Индикаторы прогреваются через `get_recent_klines` (200 свечей), поэтому пропуск не влияет на качество сигналов.

### 13.2 Лимит позиций по возрасту

В `artifacts/api-server/src/routes/trading.ts` функция `countOpenPositions()` теперь считает только позиции младше 2 часов:

```ts
const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
where(sql`is_open = 1 AND entry_time >= ${twoHoursAgo}`)
```

**Зачем:** если позиция висит дольше 2 часов — вероятно, рынок сменил тренд и сигналы от старой позиции блокируют новые. Бот начинает считать, что открыта 0 позиций, и может открыть новую.

### 13.3 Фильтр логов `TradeOnlyFilter`

В `bot/logger.py` включен фильтр, который пропускает в файл только сообщения, содержащие ключевые слова сделок:
- `Position opened`
- `TP1 hit`, `TP2 hit`, `SL hit`
- `[RECOVERY]`
- `[SYNC]`

Остальные служебные сообщения (старт, конфиг, warming up) записываются только в консоль. Для диагностики можно временно отключить фильтр.

---

## 14. Вспомогательные скрипты

### 14.1 `monitor-opt.ps1`
Мониторинг процесса оптимизации: проверяет, что `walk_forward_opt.py` запущен, и пишет статус.

### 14.2 `bot/apply_wf_results.py`
Применяет результаты walk-forward оптимизации к `config_*.yaml` вручную (если нужно обойти `auto-optimize-daily.ps1`).

### 14.3 `bot/apply_all_opt.py`
Массовое применение оптимизации ко всем парам.

### 14.4 `bot/apply_margin_pct.py`
Применяет `margin_pct` к конфигам.

### 14.5 `scripts/check_positions.py`
Проверяет открытые позиции на бирже и сравнивает с состоянием бота.

### 14.6 `scripts/check_signals_now.py`
Принудительно проверяет сигналы для всех пар прямо сейчас.

### 14.7 `scripts/fake_channel_messages.py`
Генерирует тестовые сообщения канала для проверки Telegram-уведомлений.

### 14.8 `scripts/test_binance_api.py`
Проверка подключения к Binance API.

### 14.9 `scripts/test_support_bot.py`
Тест support-bot.

---

## 15. Деплой

### 15.1 Windows (PowerShell)

```powershell
# Запуск всех ботов
.\start-all.ps1

# Ежедневная оптимизация (04:00 по расписанию)
.\auto-optimize-daily.ps1

# Мониторинг оптимизации
.\monitor-opt.ps1
```

### 15.2 Scheduled Task

Daily-скрипт регистрируется в планировщике задач Windows:
- Имя: `AutoOptimizeWalkForward`
- Время: ежедневно в 04:00
- Действие: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File "...\auto-optimize-daily.ps1"`

---

## 16. Changelog

### 2026-08-09
- Добавлен `skip_catchup=True` в `market_data.py` — бот пропускает исторические свечи при старте
- Добавлен 2-часовой лимит позиций в `countOpenPositions()` — позиции старше 2ч не учитываются в `max_positions`
- Добавлены Telegram-уведомления в `auto-optimize-daily.ps1` (COMPLETE/PARTIAL/FAILED)
- Исправлена кодировка Telegram-сообщений на UTF-8
- Исправлен `Get-PairsDoneToday` regex — теперь корректно считает завершённые пары
- Добавлены текстовые fallback-уведомления для TP1/TP2/SL в `main.py`
- Восстановлен `TradeOnlyFilter` после диагностики

### 2026-08-08
- Добавлен `auto-optimize-daily.ps1` с ежедневной walk-forward оптимизацией
- Добавлен scheduled task `AutoOptimizeWalkForward` (ежедневно в 04:00)
- Добавлен `monitor-opt.ps1`
- Добавлены вспомогательные скрипты в `scripts/`
- Добавлены `apply_wf_results.py`, `apply_all_opt.py`, `apply_margin_pct.py`

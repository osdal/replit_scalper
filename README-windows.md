# Binance Scalper Bot

Торговый бот для Binance Futures, реализующий стратегию на основе EMA-пересечений с фильтром тренда на старшем таймфрейме (HTF). Поддерживает live/paper/backtest режимы, частичное закрытие позиций (TP1/TP2), перенос стоп-лосса в безубыток, синхронизацию состояния с биржей и режим восстановления убытков (recovery) через внешнее API.

---

## 1. Архитектура проекта

### Структура папок
```
replit_scalper/
├── bot/                    # Основной код бота
│   ├── main.py            # Точка входа, главный цикл обработки свечей
│   ├── config.py          # Загрузка и валидация конфига (config.yaml)
│   ├── strategy.py        # Индикаторы (EMA, HTF) и генерация сигналов
│   ├── order_manager.py   # Работа с API Binance: ордера, баланс, позиции
│   ├── position_tracker.py# Отслеживание позиции, PnL, персистентность
│   ├── signal_handler.py  # Подтверждение сигналов (auto/semi-auto)
│   ├── recovery_client.py # Внешний API для recovery-режима
│   ├── notifier.py        # Отправка сигналов/событий в Telegram
│   ├── market_data.py     # Получение свечей (REST polling)
│   ├── logger.py          # Настройка логирования (файл + консоль)
│   ├── backtester.py      # Бэктестинг на исторических данных
│   ├── backtest_runner.py # CLI-раннер для бэктеста (stdin/stdout JSON)
│   ├── db_reporter.py     # Отчётность в БД (опционально)
│   └── *.yaml             # Конфиги под разные символы
├── config/                # Дополнительные конфиги (binance/kucoin)
├── kucoin/                # Отдельный бот для KuCoin (не используется основным ботом)
├── logs/                  # Логи (bot.log, events.log)
├── .env                   # Переменные окружения (не в git)
├── .env.example           # Пример переменных окружения
└── README-windows.md      # Этот файл
```

### Взаимодействие модулей
```
main.py
  ├─ AsyncClient (binance) ──► market_data.py (polling свечей)
  ├─ strategy.py ──► get_signal() ──► Signal
  ├─ signal_handler.py ──► confirm() ──► bool
  ├─ order_manager.py ──► open_position() / cancel_all_tp_sl() / move_sl_to_breakeven()
  ├─ position_tracker.py ──► open_async() / check() / apply_hit_async()
  ├─ recovery_client.py ──► claim() / report() / release()
  ├─ notifier.py ──► send_signal() / send_event() / send_message() → Telegram
  └─ logger.py / db_reporter.py ──► логи и БД
```

### Точки входа
- `python bot/main.py [config.yaml]` — основной запуск (live/paper/backtest)
- `python bot/backtest_runner.py` — запуск бэктеста через stdin JSON (используется API-сервером)
- `python bot/optimizer.py` — оптимизация параметров через Optuna (используется из дашборда или CLI)

---

## 2. Оптимизатор параметров

### Принцип работы (`bot/optimizer.py`)

Оптимизатор использует библиотеку **Optuna** для подбора наилучших параметров стратегии через бэктестинг на исторических данных.

### Процесс:
1. **Скачивание данных** — один раз загружаются исторические свечи с Binance за заданный период
2. **Запуск trials** — Optuna создаёт N комбинаций параметров через байесовский TPE Sampler
3. **Бэктест** — для каждой комбинации запускается `run_backtest_on_df()`
4. **Оценка (score)** — `profit_factor × sqrt(trades) × win_rate / (1 + max_drawdown%)`
5. **Результат** — вывод топ-10 и сохранение CSV с топ-100

### Параметры, которые подбирает оптимизатор:
| Параметр | Диапазон |
|----------|----------|
| `ema_fast` | 5–20 |
| `ema_slow` | fast+3 … 55 |
| `sl_pct` | 0.2–1.5% (шаг 0.05) |
| `tp1_pct` | 0.2–1.0% (шаг 0.05) |
| `tp2_pct` | tp1+0.1 … 2.4% (шаг 0.1) |
| `volume_multiplier` | 1.0–2.5× (шаг 0.1) |
| `tp1_close_pct` | 30–70% (шаг 10) |
| `risk_pct` | 1–10% (шаг 0.5) |
| `htf_ema_fast` | 5–15 |
| `htf_ema_slow` | fast+3 … 40 |

### Использование из CLI:
```bash
cd bot
python optimizer.py --symbol ETHUSDT --start 2026-07-01 --end 2026-07-24 --trials 200 --jobs 4
python optimizer.py --help
```

### Флаги:
| Флаг | Описание |
|------|----------|
| `--symbol` | Торговая пара (обязательно) |
| `--start` / `--end` | Период бэктеста (обязательно) |
| `--trials` | Число комбинаций (по умолч. 100) |
| `--jobs N` | Параллельные вычисления (1 = последовательно) |
| `--study-name NAME` | Сохранение результатов в SQLite (`data/optuna.db`) |
| `--timeframe` | Таймфрейм (по умолч. из конфига) |
| `--config` | Путь к конфигу (по умолч. `config.yaml`) |

### HTF-кеширование:
При повторении комбинации `htf_ema_fast`/`htf_ema_slow` HTF-индикаторы не пересчитываются — используется кэш в памяти. Это ускоряет оптимизацию при большом количестве trial'ов.

### SQLite-персистентность:
При указании `--study-name` результаты сохраняются в `data/optuna.db`. При повторном запуске с тем же `--study-name` Optuna продолжит с того же места, а не начнёт заново.

---

## 2b. Дашборд — вкладка Optimizer

Дашборд предоставляет UI для запуска и мониторинга оптимизации:
- **Symbol** — выбор пары из списка активных ботов
- **Start / End** — период для бэктеста
- **Trials** — количество комбинаций
- **Parallel Jobs** — количество одновременных вычислений
- **Progress bar** — реальный прогресс выполнения
- **Output** — live-вывод Python-процесса
- **Top Results** — таблица с результатами (Rank, Score, Trades, WR%, PnL, DD%, EMA F/S, SL%, TP1/TP2%, Vol×, TP1cl%, Risk%, HTF F/S)
- **✓ (Apply to Bot)** — сохраняет параметры в конфиг бота через `PUT /api/bots/:symbol/config`
- **➡ (Apply to Backtest)** — копирует параметры во вкладку Backtest

Результаты оптимизации также доступны по `GET /api/optimizer/results/:symbol`.

---

## 2c. Дашборд — вкладка Backtest

Вкладка позволяет запускать бэктест с произвольными параметрами и просматривать результаты:

### Параметры:
- Symbol, Timeframe, Start/End Date
- Leverage, Risk%, SL%, TP1%, TP2%, EMA Fast/Slow, Volume Multiplier
- **HTF Filter** — чекбокс включения/выключения фильтра по старшему таймфрейму
- **HTF EMA Fast/Slow** — настройка EMA для HTF (дизейблены если HTF выключен)

### Результаты:
- Total Trades, Win Rate (W/L)
- Total PnL, Max Drawdown
- Initial/Final Balance, Return%
- Avg Win / Avg Loss

### Кнопка "Load to Config":
Сохраняет текущие параметры бэктеста (включая HTF-настройки и risk_pct) в конфиг бота. После сохранения требуется перезагрузка ботов через "Stop All & Reload".

---

## 2. Ключевые торговые стратегии

### Основная логика входа (`strategy.py:get_signal`)
1. **EMA-пересечение** (быстрое `ema_fast=6`, медленное `ema_slow=39` на таймфрейме `5m`):
   - LONG: `ema_fast` пересекает `ema_slow` снизу вверх
   - SHORT: `ema_fast` пересекает `ema_slow` сверху вниз
2. **Объёмный фильтр**: текущий объём ≥ `volume_ma * volume_multiplier` (по умолчанию 20ср × 1.0)
3. **HTF-фильтр** (опционально, `htf_enabled=true`): тренд на `1h` (EMA 9/21) должен совпадать с направлением сигнала

### Параметры конфига (из `config.yaml`)
| Параметр | Значение по умолчанию | Описание |
|----------|----------------------|----------|
| `timeframe` | `5m` | Основной таймфрейм |
| `htf_timeframe` | `1h` | Старший таймфрейм для тренда |
| `ema_fast` / `ema_slow` | 6 / 39 | Периоды EMA на основном ТФ |
| `htf_ema_fast` / `htf_ema_slow` | 9 / 21 | Периоды EMA на HTF |
| `volume_ma_period` | 20 | Период среднего объёма |
| `volume_multiplier` | 1.0 | Множитель объёма |

### Выход из позиции
- **TP1** (`tp1_pct=0.2%`): закрытие `tp1_close_pct=60%` позиции, стоп переносится в безубыток (entry price)
- **TP2** (`tp2_pct=0.5%`): закрытие оставшихся 40%
- **SL** (`sl_pct=0.75%`): полное закрытие по стопу

> ⚠️ Для recovery-позиций TP1 закрывает **100%** позиции сразу (логика в `position_tracker.py:386-400`).

---

## 3. Управление рисками

### Расчёт размера позиции (`order_manager.py:calc_quantity`)
```python
risk_amount = balance * risk_pct / 100
sl_distance_pct = sl_pct / 100
quantity = risk_amount / (entry_price * sl_distance_pct)
```
- `leverage` **не влияет** на размер позиции (только на требуемую маржу)
- `risk_pct=1.0%` от баланса на сделку

### Стоп-лоссы и тейк-профиты
- Размещаются как **STOP_MARKET** (SL) и **LIMIT** (TP1, TP2) с `reduceOnly=true`
- При TP1: отмена всех ордеров, установка нового SL в entry price, перестановка TP2
- Пылевые позиции (notional < $1) закрываются маркет-ордером автоматически (`close_dust`)

### Recovery-режим (компенсация убытков)
- При убыточной сделке (`pnl < 0`) вызывается `recovery.report(pnl)` → создаётся "долг" на сервере
- При следующем сигнале `recovery.claim()` пытается захватить свободный долг
- Размер recovery-позиции рассчитывается так, чтобы прибыль на TP1 покрыла долг + бонус (`calc_recovery_quantity`)
- Ограничение: `recovery_max_pct` от баланса (по умолчанию 50%, настраивается в `recovery_config.yaml`)

---

## 4. Работа с API Binance

### Используемый клиент
- `binance.AsyncClient` (асинхронный, официальная библиотека `python-binance`)
- Создаётся в `main.py:266-269` с API ключами из `.env`

### Обработка ошибок и лимитов
- **Поллинг свечей** (`market_data.py`): REST `futures_klines` каждые 10 сек (`poll_seconds=10`)
- **Реконнекты**: нет встроенного вебсокет-переподключения — используется REST polling, устойчивый к разрывам
- **Ретраи**: в `order_manager.py:move_sl_to_breakeven` — 3 попытки с паузой 1.5с при установке SL
- **Ошибки API**: логируются, бот продолжает работу (try/except во всех критических местах)

### Основные эндпоинты
| Метод | Использование |
|-------|---------------|
| `futures_klines` | Получение свечей (polling) |
| `futures_historical_klines` | Исторические данные для бэктеста |
| `futures_create_order` | Маркет/лимит/стоп ордера |
| `futures_cancel_all_open_orders` | Отмена обычных ордеров |
| `futures_get_open_algo_orders` / `futures_cancel_algo_order` | Отмена стоп-ордеров (algo) |
| `futures_position_information` | Синхронизация позиции с биржей |
| `futures_account_balance` | Баланс USDT |
| `futures_user_trades` | Реальный PnL с комиссиями |
| `futures_change_leverage` | Установка плеча |

---

## 5. Переменные окружения (ENV)

### `.env.example`
```env
# Binance API (обязательно для live-режима)
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# Внешний API для recovery-режима (опционально)
DASHBOARD_API_URL=http://localhost:5000/api

# Путь к конфигу recovery (опционально, по умолчанию bot/recovery_config.yaml)
# RECOVERY_CONFIG_PATH=./bot/recovery_config.yaml

# Telegram-уведомления (опционально; если не заданы — уведомления игнорируются)
# TELEGRAM_BOT_TOKEN=<токен бота от @BotFather>
# TELEGRAM_CHAT_ID=<id канала/чата, обычно отрицательное число>
```

### `recovery_config.yaml` (в `bot/`)
```yaml
recovery_enabled: true
recovery_bonus_pct: 10.0      # Бонус к долгу при расчёте recovery-размера
recovery_max_pct: 50.0        # Макс. % баланса под recovery-позицию (0 = без лимита)
```

---

## 6. Инструкция по установке и запуску

### Требования
- Python 3.10+
- Windows / Linux / macOS

### Установка зависимостей
```bash
# Через pip
pip install -r requirements.txt

# Или через poetry (если pyproject.toml настроен)
poetry install
```

### Основные зависимости (`pyproject.toml` / `uv.lock`)
| Библиотека | Версия | Назначение |
|------------|--------|------------|
| `python-binance` | ≥1.0.0 | Async клиент Binance |
| `pandas` | ≥2.0 | Работа со свечами/индикаторами |
| `numpy` | ≥1.24 | Математика |
| `aiohttp` | ≥3.8 | HTTP-клиент для recovery API |
| `pyyaml` | ≥6.0 | Парсинг YAML конфигов |
| `python-dotenv` | ≥1.0 | Загрузка .env |
| `optuna` | ≥3.0 | Байесовская оптимизация параметров |
| `python-telegram-bot` | ≥21.0 | Отправка уведомлений в Telegram (async `telegram.Bot`) |
| `uvicorn` / `fastapi` | — | Для API-сервера (dashboard) |

### Настройка конфига
1. Скопируйте `bot/config.yaml` под нужный символ (например `bot/config_btc.yaml`)
2. Отредактируйте параметры: `symbol`, `timeframe`, `risk_pct`, `sl_pct`, `tp1_pct`, `tp2_pct`, `leverage`, `mode`, `htf_enabled`, `htf_ema_fast`, `htf_ema_slow`

### Запуск
```bash
# Live торговля (требует .env с ключами)
python bot/main.py bot/config_btc.yaml

# Paper trading (без реальных ордеров)
# В config.yaml: mode: paper

# Backtest
# В config.yaml: mode: backtest, backtest_start/end
python bot/main.py bot/config_btc.yaml

# Бэктест через CLI (JSON stdin) — используется API-сервером
echo '{"symbol":"BTCUSDT","start":"2026-05-01","end":"2026-06-01","config":{"timeframe":"5m","leverage":10}}' | python bot/backtest_runner.py

# Оптимизация параметров
python bot/optimizer.py --symbol ETHUSDT --start 2026-07-01 --end 2026-07-24 --trials 200 --jobs 4

# Через дашборд (см. раздел 2b): http://localhost:5173 → вкладка Optimizer
```

### Блокировка (lock-file)
- При запуске создаётся `bot/bot.lock.{symbol}` с PID процесса
- Предотвращает запуск двух экземпляров для одного символа
- При краше проверяется жив ли процесс — мёртвый lock удаляется автоматически

---

## 7. Логирование

### Настройка (`logger.py`)
- **Уровень**: `DEBUG` в файл, `INFO` в консоль
- **Ротация**: `RotatingFileHandler`, 10 МБ × 5 файлов
- **Фильтр в файл** (`TradeOnlyFilter`): пишутся только сообщения с ключевыми словами:
  `Position opened`, `TP1 hit`, `TP2 hit`, `SL hit`, `Partial close`, `Full close`, `SL moved to breakeven`, `[LIVE] Market order placed`, `[RECOVERY]`, `[SYNC]`, `[STATE]` и др.

### Новые ключевые слова для диагностики
| Ключевое слово | Описание |
|----------------|----------|
| `[TP1_START]` | Вход в обработку TP1 с position_id, current_price, qty_to_close, total_qty |
| `[TP1_RETURN]` | Результат возврата из TP1 (тип, pnl, exit_reason) |
| `[RECOVERY]` | Общая категория recovery-операций |
| `[RECOVERY_DEBUG]` | Детальная отладка recovery (is_recovery, chainId) |
| `[RECOVERY][CLAIM_ERROR]` | Ошибка при получении долга от сервера |
| `[RECOVERY][REPORT_ERROR]` | Ошибка при отчёте о сделке |
| `[RECOVERY][LIMIT_EXCEEDED]` | Превышен лимит recovery-позиции |
| `[RECOVERY][TP1_HIT_FULL_CLOSE]` | Полное закрытие recovery-позиции по TP1 |
| `[CRITICAL]` | Критические ошибки (гигантские позиции, отсутствие SL) |
| `[SYNC_WARNING]` | Расхождение между локальным и биржевым состоянием |
| `[SYNC_CHECK]` | Результат периодической проверки позиции |

### Файлы логов
| Файл | Содержание |
|------|------------|
| `logs/bot.log` | Основной лог (только торговые события + ошибки) |
| `logs/events.log` | Ключевые события в формате `timestamp [SYMBOL] message` (для дашборда) |
| `logs/backtest.log` | Лог бэктеста |

> ℹ️ **Telegram-уведомления не заменяют логирование.** Telegram (`bot/notifier.py`) — это лишь дублирование торговых сигналов и ключевых событий в мессенджер для быстрого уведомления. Полная диагностика и история всегда доступны в логах (`logs/*.log`) и на дашборде.

---

## 8. Известные проблемы / TODO

| Файл:строка | Проблема | Приоритет |
|-------------|----------|-----------|
| `order_manager.py:499-541` | `get_realized_pnl` парсит `futures_user_trades` с допущением о чередовании сторон. Может ошибиться при частичных исполнениях или нескольких входах в одну сторону. | 🔴 High |
| `market_data.py:62-161` | REST polling каждые 10 сек — задержка до 10 сек после закрытия свечи. Нет вебсокетов. | 🟡 Medium |
| `recovery_client.py:23-35` | `readRecoveryConfig` читает файл **при каждом вызове** (без кэша). Может стать бутылком при частых claim. | 🟡 Medium |
| `main.py:329-335` | `df_buffer = pd.concat([df_buffer, new_row]).tail(500)` — создаёт новый DataFrame каждую свечу. Утечка памяти при долгой работе. | 🟡 Medium |
| `config.py:51-56` | `load_config` игнорирует неизвестные поля в YAML молча. Опечатки в конфиге не выдают ошибку. | 🟡 Medium |
| `bot/*.yaml` | Много конфигов под разные символы — нет единого шаблона/генератора. | 🟢 Low |
| `kucoin/` | Отдельный бот для KuCoin дублирует логику. Нет общего ядра. | 🟢 Low |
| `optimizer.py` | `n_jobs=1` на Windows может не давать ускорения из-за GIL. Рекомендуется использовать `--study-name` для сохранения промежуточных результатов. | 🟡 Medium |

---

## 9. Расчёт прибыли

Для точного расчёта PnL используется эндпоинт `/income/{symbol}` вместо `futures_user_trades`. Это обеспечивает:

- **Точность**: PnL считается напрямую на сервере Binance с учётом всех комиссий
- **Надёжность**: Устранены ошибки из-за чередования сторон сделок
- **Сравнение**: Порог `pnl_tolerance` (по умолчанию 0.01) для сравнения с локальным расчётом

### Параметры конфигурации
```yaml
pnl_tolerance: 0.01  # Порог для предупреждения о расхождении PnL
recovery_max_position_pct: 10.0  # Максимальный % от баланса для recovery-позиции
```

---

## 10. Исправления и доработки (июль 2026)

### Верификация позиций (критические изменения логики)
- **После открытия маркет-ордера** — проверка `futures_position_information` (3 попытки × 1с). Если позиции нет → возврат `None`, SL/TP не ставятся. Частичные заполнения корректируют qty и entry_price
- **При закрытии SL/TP2** — ожидание до 10с подтверждения закрытия позиции на бирже. PnL берётся из Binance Income API, а не локально. Только после этого запись в БД
- **При рестарте бота** — сверка открытых трейдов в БД с биржей. Если позиция закрылась пока бот был выключен: запрос реального PnL, закрытие трейда, репорт в recovery

### Recovery (логика компенсации убытков)
- **Размер позиции:** `qty = target_profit / (entry × tp1_pct%)` — покрывает долг+бонус при TP1. Проверка маржи с плечом
- **TP2 не выставляется** для recovery-позиций, только SL + TP1
- **100% закрытие на TP1**
- **SL на recovery:** старая цепочка освобождается (`release`), новая создаётся только на свежий убыток
- **Восстановление контекста при рестарте:** проверка locked цепочек → `is_recovery=True`

### Очистка и синхронизация
- **Авто-очистка пыли** каждые 12 свечей — закрытие позиций с notional < $1, отмена stale ордеров
- **`close_dust`** — использует `max(real_qty, step_size)` для обхода minNotional
- **Stale трейды** — при старте бота закрываются с реальным PnL от Binance
- **Sync Binance** — читает символы из конфигов (24 пары), fuzzy-дедубликация по 1-минутному окну

### Блокировки и стабильность
- **`_acquire_lock`** — использует `os.kill(pid, 0)` вместо `tasklist`, кросс-платформенно
- **API start handler** — проверяет `.killed` перед блокировкой рестарта
- **`findBotPid`** — проверяет `main.py` в командной строке, чтобы не путать бота с оптимизатором

### Оптимизатор
- **`risk_pct` фиксирован на 3%** — не варьируется
- **HTF-параметры всегда генерируются** — независимо от `htf_enabled` в конфиге
- **SQLite-персистентность** (`--study-name`), **параллельные вычисления** (`--jobs N`)

### Дашборд
- **Backtest tab** — HTF Filter, поля HTF EMA Fast/Slow, «Load to Config»
- **Sync Binance** — все 24 пары, fuzzy-дедубликация
- **Delete bot** — иконка корзины на карточке, `DELETE /bots/:symbol`
- **RecoveryTab** — кнопка удаления цепочек

---

## 11. Исправления (август 2026)

### Исправление расхождения PnL между дашбордом и Binance
**Проблема:** Дашборд показывал gross PnL (REALIZED_PNL) без учёта комиссий, тогда как Binance Dashboard видит net PnL.

**Решение:** 
- **Файл:** `bot/order_manager.py:542`
- **Изменение:** `total_pnl = g["REALIZED_PNL"] + g["COMMISSION"] + g["FUNDING_FEE"]`
- **Пояснение:** В API Binance поле `COMMISSION` и `FUNDING_FEE` хранится как отрицательные значения (списания), поэтому их нужно **прибавлять** к gross PnL для получения net PnL.
- **До исправления:** `total_pnl = g["REALIZED_PNL"] - g["COMMISSION"] - g["FUNDING_FEE"]` (неверно, так как комиссия уже отрицательна)

### Исправление qty=0 в дашборде при TP1_full_close + TP2
**Проблема:** После полного закрытия TP1 (100%) позиция переходит в TP2, но qty в дашборде становилось 0.

**Решение:**
- **Файл:** `bot/position_tracker.py:465-466, 494-498`
- **Изменение:** Добавлен `total_qty_before = p.total_qty` и логика `qty_to_report = remaining_before if remaining_before > 0.0 else total_qty_before`
- **Пояснение:** При полном TP1-закрытии (remaining=0) и последующем TP2 сохраняем исходный объём позиции, чтобы дашборд отображал реальный traded qty.

### Восстановление освобождённых recovery-цепочек при рестарте
**Проблема:** При рестарте бота замоканная recovery-цепочка (chain_id=49) оставалась locked, бот не мог зайти в recovery.

**Решение:**
- **Файл:** `bot/position_tracker.py`, `bot/main.py`
- **Изменение:** При рестарте проверяем locked цепочки → если свободны, позволяем зайти в recovery с `is_recovery=True`
- **Результат:** Цепочки теперь автоматически освобождаются при рестарте бота

---

## 12. Уведомления в Telegram + восстановление recovery (август 2026)

### Уведомления в Telegram (`bot/notifier.py`)

Новый модуль `Notifier` дублирует торговые сигналы и события в Telegram-канал. Реализован на асинхронной библиотеке `python-telegram-bot` (не блокирует основной asyncio-цикл).

**Настройка (`.env`):**
```
TELEGRAM_BOT_TOKEN=<токен бота от @BotFather>
TELEGRAM_CHAT_ID=<id канала/чата>
```
Если переменные не заданы — `Notifier` логирует `WARNING` и все отправки игнорируются (не влияет на торговлю).

**Функциональность:**
- `send_signal(signal_data)` — красивое сообщение о новом сигнале (Entry/SL/TP1/TP2/EMA/Volume/Leverage)
- `send_event(event_type, details)` — события:
  - `position_opened` → `✅ Position opened | {symbol} {direction} qty={qty} entry={price}`
  - `tp1_hit` / `tp2_hit` → `🎯 TP1/TP2 hit | {symbol} qty={qty} pnl={pnl}`
  - `sl_hit` → `❌ SL hit | {symbol} qty={qty} pnl={pnl}`
  - `recovery` → `🔄 Recovery | {symbol} chainId={chainId} debt={debt}`
- `send_message(text)` — произвольное сообщение
- Автоматический повтор при ошибках (до 3 попыток с паузой 1с) через `_send_with_retry`
- Интеграция в `bot/main.py`: вызовы `notifier.send_signal(...)` после `get_signal`, `notifier.send_event(...)` в местах открытия позиции и TP1/TP2/SL

> Файлы: `bot/notifier.py` (новый), `bot/main.py` (интеграция), `bot/position_tracker.py` (параметр `notifier`).

### Защита от открытия второй позиции по той же монете
**Проблема:** Бот мог открыть новую позицию, пока предыдущая по тому же символу ещё отслеживалась открытой (например, после частичного TP1). На бирже (one-way mode) вторая открывающая сделка сливалась в одну позицию, а в дашборде появлялись две записи (кейс «двойной SUI»).

**Решение (`bot/main.py`):**
- Guard в `on_candle`: если после обработки TP/SL `tracker.has_open_position()` всё ещё True — новую позицию не открываем (`log.debug(...); return`).
- Проверка реальной позиции на бирже **после `recovery.claim()`** перед открытием компенсатора: если по символу уже есть позиция — цепочка освобождается (`release`) и открытие пропускается.

### Устранение дублирования recovery-цепочек
**Проблема:** Один убыток создавал **две** свободные цепочки — бот через `recovery.report(pnl)` (без chainId) и дашборд через `POST /trades/sync-closed` (`trades.ts`).

**Решение (`artifacts/api-server/src/routes/trades.ts`):** из `/sync-closed` удалено создание recovery-цепочек. Цепочки создаёт только сам бот через `report()` при закрытии убыточной сделки. Две цепочки = два компенсатора = риск двух позиций по одной монете.

### Восстановление «зависших» locked recovery-цепочек

**Проблема:** Цепочка переходит в `locked` при `POST /claim`. Если бот упал между `claim` и открытием позиции (или во время открытия), цепочка оставалась `locked` навсегда: `claim` берёт только `free`-цепочки → застрявший долг выпадал из ротации.

**Решение — 3 уровня защиты:**

1. **Сервер (`artifacts/api-server/src/routes/recovery.ts` + `index.ts`)**
   - `recoverStaleChains()` освобождает `locked`-цепочку в `free`, если: процесс бота-владельца мёртв, **или** с момента блокировки прошло больше `RECOVERY_LOCK_TTL_MINUTES` (по умолчанию 360 = 6ч), **или** `locked_by` пуст.
   - Запуск при старте сервера и периодически каждые 30 минут.
   - Ручной запуск: `POST /api/recovery/recover-stale`.

2. **Бот (`bot/recovery_client.py` + `main.py`)**
   - Новый метод `RecoveryClient.release_all_for_symbol()` освобождает все `locked`-цепочки символа.
   - Вызывается в `_sync_position_on_start` в ветках, где биржа показывает «позиции нет» — закрывает случай, когда бот упал между `claim` и открытием.

3. **Ручная очистка через API** — `DELETE /api/recovery/chains` (все) или `DELETE /api/recovery/chains/:id` (одна).

### Прочая очистка
- Из `bot/trades.ts` убран неиспользуемый импорт `recoveryChainsTable` после удаления дублирующего создания цепочек.
- `.env.example` дополнен переменными `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
- `bot/requirements.txt` и `pyproject.toml` дополнены зависимостью `python-telegram-bot>=21.0`.

---

## 13. Глобальное управление рисками (август 2026)

Все боты — отдельные процессы, делящие **один USDT-баланс**. Для согласованного управления рисками по всему портфелю введена единая точка контроля — API-сервер + SQLite. Реализовано 4 механизма + фиксация отклонённых сигналов.

### Конфигурация (`bot/recovery_config.yaml`)
```yaml
# Потолок одновременно открытых позиций по всему портфелю
max_positions: 2
# Пауза: после N убытков подряд (общий счётчик) пропускаем M сигналов
loss_streak_trigger: 2
loss_pause_signals: 3
# Потолок накопленного долга recovery (free+locked). Выше — claim() отказывает.
max_free_debt_usd: 15.0
# Дневной лимит убытков (USDT). При достижении боты останавливаются до конца дня.
daily_loss_limit_usd: 8.0
```

### Механизм 1 — Лимит позиций (`max_positions`)
- Эндпоинт `POST /api/trading/check` считает открытые позиции из таблицы `trades` (`is_open=1`).
- Если `positions_open >= max_positions` → блокирует новый вход (`reason: "max_positions"`).

### Механизм 2 — Пауза после серии убытков
- Общий счётчик `loss_streak` (по всему портфелю, таблица `trading_control`, одна строка id=1).
- Бот сообщает результат закрытой сделки через `POST /api/trading/result {pnl}`.
- После `loss_streak_trigger` (2) убытков подряд включается `paused_remaining = loss_pause_signals` (3). Каждый `check` в паузе возвращает `allowed:false` и декрементирует счётчик.

### Механизм 3 — Потолок долга recovery (`max_free_debt_usd`)
- В `POST /api/recovery/claim`: перед выдачей free-цепочки считается сумма всех `free+locked` долгов.
- Если `total_debt >= max_free_debt_usd` → `{chainId: null, reason: "debt_limit"}`.
- Бот при `reason="debt_limit"` **пропускает сигнал целиком** (не открывает даже обычную позицию).

### Механизм 4 — Дневной лимит убытков (`daily_loss_limit_usd`)
- `POST /api/trading/check` считает сумму `pnl < 0` по закрытым сделкам за текущий UTC-день.
- Если `daily_loss >= daily_loss_limit_usd` → `{allowed: false, reason: "daily_loss_limit"}` — боты не входят до конца дня.

### Фиксация отклонённых сигналов (для статистики)
- В таблицу `trades` добавлены колонки `status` (`open`/`closed`/`rejected`) и `reject_reason`.
- При блокировке сигнала риск-контролем (`pause`/`max_positions`/`daily_loss_limit`/`debt_limit`) бот записывает сделку `status='rejected'`, `pnl=0`, `is_open=false` через `POST /api/trades`.
- Отклонённые сделки **исключаются** из торговой статистики `GET /api/trades/stats` (фильтр `status != 'rejected'`), но видны в списке сделок — для анализа, сколько сигналов выкинуто защитами.

### Файлы
| Файл | Что |
|------|-----|
| `bot/recovery_config.yaml` | пороги всех механизмов |
| `bot/recovery_client.py` | `can_open()`, `report_result()` |
| `bot/main.py` | вызовы `can_open` перед входом, `report_result` при закрытии, запись отклонённых |
| `bot/db_reporter.py` | `report_rejected()` |
| `artifacts/api-server/src/routes/trading.ts` | `/trading/check`, `/trading/result`, `/trading/status` |
| `artifacts/api-server/src/routes/recovery.ts` | потолок долга в `/claim` |
| `lib/db/src/schema/trading.ts` + `trades.ts` | таблицы `trading_control`, колонки `status`/`reject_reason` |

### Диагностика
`GET /api/trading/status` возвращает текущее состояние всех механизмов:
```
{ positions_open, max_positions, loss_streak, loss_streak_trigger,
  paused_remaining, loss_pause_signals, max_free_debt_usd, free_debt,
  daily_loss_limit_usd, daily_loss }
```

### ADX-фильтр силы тренда (дополнительно)
- `bot/strategy.py`: расчёт ADX(14) в `calculate_indicators`; в `get_signal` сигнал пропускается, если `adx < adx_threshold`.
- `bot/config.py`: поля `adx_period: int = 14`, `adx_threshold: float = 0.0` (0 = отключён).
- Во всех `config_*.yaml`: `adx_threshold: 20.0`.
- ✓ Анализ 2 августа: ADX на входах был 20–47 — фильтр не отсекает вчерашние убытки (они были не из-за боковика), но защищает от будущего чопа.

---

## 14. Новинки (август 2026): Telegram-картинки, симуляция отклонённых, margin_pct

### Margin_Pct — размер позиции как % от депозита
Новое поле конфигурации `margin_pct` определяет, **какой процент от текущего баланса USDT брать в обеспечение маржи** при открытии позиции (ранее использовался фиксированный `fixed_notional_usd`).

- `bot/config.py`: поле `margin_pct: float = 0.0` (0 = отключено).
- `bot/order_manager.py`: маржа = `round(balance * margin_pct / 100, 1)` (до 1 знака), позиция = `маржа * leverage`.
- Пример: баланс **$20.3**, `margin_pct: 10` → маржа **$2.0**, при плече 75 → позиция **~$150**.
- Во всех `config_*.yaml`: `margin_pct: 10`, `fixed_notional_usd: 0`.

**Приоритет размера позиции:** Recovery (под долг) → `margin_pct` → `fixed_notional_usd` → `fixed_qty` → `fixed_risk_usd` → `risk_pct`.

### Telegram-уведомления в виде PNG-карточек
- `bot/notifier.py`: сигналы и события отдаются как **изображения** (Pillow), а не текст.
- Из сообщений убраны объём/EMA/qty — только цены.
- **PnL** в событиях TP1/TP2/SL показывается как ценовой %: **крупный зелёный** при прибыли, **маленький красный** при убытке.
- Зависимость: `python-telegram-bot` + `Pillow`.

### Что НЕ транслируется в Telegram
Канал предназначен для привлечения пользователей, поэтому из него убраны:
- ~~`⛔ Signal rejected`~~ — уведомления об отклонённых сигналах.
- ~~`✅ Position opened`~~ — подтверждение открытия сделки на бирже.

В канал идут только: **🔔 сигнал** (Entry/SL/TP1/TP2/Leverage) и **результаты** 🎯 TP1/TP2 / ❌ SL (с ценой входа/выхода и % PnL).

### Кнопка «ПОДКЛЮЧИТЬ БОТ» в Telegram
- `bot/notifier.py`: все сообщения теперь отправляются с инлайн-кнопкой **ПОДКЛЮЧИТЬ БОТ**.
- URL берётся из переменной окружения `TELEGRAM_CONNECT_URL` (`.env`).
- Сейчас кнопка ведёт на личный диалог/канал. В будущем туда можно подключить отдельного бота для обработки запросов.
- Если `TELEGRAM_CONNECT_URL` не задан — кнопка не добавляется, поведение не меняется.

```env
TELEGRAM_CONNECT_URL=https://t.me/your_username
```

### Симуляция исхода отклонённых сигналов
Отклонённая сделка фактически не открывалась (позиции не было), поэтому её результат **симулируется** для статистики в дашборде:
- `bot/db_reporter.py`: `report_rejected()` возвращает `trade_id` созданной записи.
- `bot/main.py`: при отклонении сигнала (риск-контроль / debt_limit) бот сохраняет `{trade_id, direction, entry, sl, tp1}` и на каждой свече проверяет цену:
  - дошла до **SL** → записывается `exit_reason="SL"`, `exit_price`;
  - дошла до **TP1** → записывается `exit_reason="TP1"`, `exit_price`;
  - через 24 свечи (≈2ч) без касания — удаляется без исхода.
- В дашборде (таблица сделок) отклонённая позиция помечается бейджем **`⛔ REJECTED (причина)`**, а при наличии симуляции — бейджем **`sim: TP1`/`sim: SL`** + цена выхода.
- `artifacts/dashboard/src/Dashboard.tsx`: бейдж REJECTED + sim-результат; отклонённые **исключены из графика PnL** и из торговой статистики `/trades/stats` (фильтр `status != 'rejected'`).

### Автосброс паузы по времени (против «зависания» в тишине)
- `bot/recovery_config.yaml`: `pause_timeout_minutes: 120`.
- `artifacts/api-server/src/routes/trading.ts`: функция `autoResetStale()` вызывается на `/trading/check` и `/trading/status`. Если с последнего события (убытка/включения паузы) прошло ≥ `pause_timeout_minutes` без новых сделок — пауза и счётчик подряд убытков сбрасываются.
- `/trading/status` дополнительно возвращает `pause_timeout_minutes`.

### Прочее
- **Безопасный рестарт ботов**: `scripts_restart_all.ps1` + `scripts_start_bots_parallel.py` — параллельный запуск 24 ботов, без `stop-all` (который валил API).
- `artifacts/api-server/src/routes/trades.ts`: добавлен `GET /trades/:id`.
- `bot/position_tracker.py`: для восстановленных рестартом позиций (без `entry_timestamp`) время входа берётся из записи БД при расчёте реального PnL с биржи.
- **Фикс задвоения сделок в дашборде** (например AVAX): при рестарте бот переиспользует существующую открытую сделку вместо создания дубликата.

## 15. Исправления синхронизации позиций (4 августа 2026)

### entry_timestamp при восстановлении позиции
**Проблема:** При рестарте бота, если на бирже была открыта позиция, а локального состояния нет, `entry_timestamp` устанавливался в `datetime.utcnow()`. Из-за этого при закрытии позиции запрос реального PnL через `get_realized_pnl` использовал неверный период (от текущего времени, а не от реального входа), и реальный PnL не находился.

**Решение:**
- **Файл:** `bot/main.py:356-401`
- **Изменение:** `entry_timestamp` теперь берётся из поля `entryTime` ответа `futures_position_information` (Binance возвращает timestamp в миллисекундах). Если `entryTime` отсутствует — используется `datetime.utcnow()` как fallback.

### Периодическая проверка позиции (`periodic_position_check`)
**Проблема:** Фоновая задача каждые 1 час обнаруживала внешнее закрытие позиции (биржа закрыла позицию без участия бота), очищала локальное состояние, но **не закрывала запись в БД** и **не обрабатывала recovery**. Это приводило к «зависшим» сделкам в дашборде и зависшим recovery-цепочкам.

**Решение:**
- **Файл:** `bot/main.py:1003-1049`
- **Изменение:** При обнаружении полного внешнего закрытия (`exchange_qty < 0.000001`):
  - вызывается `tracker.apply_hit_async(hit_type, price, candle_time_ms)` для корректного закрытия сделки в БД;
  - отменяются оставшиеся ордера (`cancel_all_tp_sl`);
  - освобождается recovery-цепочка (`release`) и отправляется результат (`report`, `report_result`).

### Уточнение определения hit_type при внешнем закрытии
**Проблема:** Если позиция была закрыта внешне после частичного TP1 (остаток — пыль), бот определял закрытие как TP2 (если цена ушла в плюс) или SL (если ушла в минус), что искажало статистику.

**Решение:**
- **Файл:** `bot/main.py:711-723` и `bot/main.py:1033-1045`
- **Изменение:** Перед определением `hit_type` проверяется `pos.tp1_hit`. Если `tp1_hit == True` — остаток позиции закрывается как `TP1` (безубыток), а не как `TP2` или `SL`.

### Симуляция отклонённых сигналов (`scripts/simulate_rejected.py`)
Отклонённые сделки (`status='rejected'`) не открывались на бирже, но их исход можно симулировать по историческим данным Binance:
- **Файл:** `scripts/simulate_rejected.py`
- **Запуск:** `python scripts/simulate_rejected.py`
- **Логика:** для каждой отклонённой сделки с `entry_time` скачиваются 5m-свечи с момента входа, проверяется, какой уровень (`SL` или `TP1`) был достигнут первым;
- **Результат:** в `trades` записываются `exit_reason`, `exit_price`, `pnl`, `exit_time`. Статус остаётся `rejected`, поэтому в дашборде отображается бейдж `⛔ REJECTED` + `sim: TP1/SL`.
- Повторный запуск скрипта пропускает уже симулированные записи (`exit_reason IS NOT NULL`).

---

## Вопросы к автору для доработки

1. **Какой алгоритм выбора монет?** Сейчас символ жестко задан в `config.yaml` (`BTCUSDT`). Планируется мульти-символьный запуск или ручной выбор?

2. **Как обрабатывается частичное заполнение маркет-ордера при входе?** В `order_manager.py:_get_fill_price` берётся `avgPrice` или средневзвешенное по fills. Но если ордер заполнился частями на разных ценах — entry_price в трекере будет средним, а SL/TP рассчитаны от сигнальной цены. Это намеренно?

3. **Какой механизм выбора recovery-цепочки?** `recovery.claim()` возвращает `chainId`, но логика приоритета (старший долг, наибольший убыток, случайный) скрыта на сервере. Нужно ли боту знать/контролировать выбор?

4. **Что происходит при разрыве связи с Binance во время открытой позиции?** Polling продолжает попытки, но SL/TP уже стоят на бирже. Есть ли сценарий "force close" или ручное управление в таком случае?

5. **Планируется ли вебсокет-стриминг свечей вместо REST polling?** Текущая задержка до 10 сек может критична для скальпинга на 5m.

6. **Как часто бот проверяет, что recovery-долг действительно погашен на сервере, и есть ли повторные попытки при ошибке release?**

---

*Файл создан автоматически на основе анализа кодовой базы. Для уточнения деталей см. исходные файлы в `bot/`.*
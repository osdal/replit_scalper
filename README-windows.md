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
  └─ logger.py / db_reporter.py ──► логи и БД
```

### Точки входа
- `python bot/main.py [config.yaml]` — основной запуск (live/paper/backtest)
- `python bot/backtest_runner.py` — запуск бэктеста через stdin JSON (используется API-сервером)

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
| `uvicorn` / `fastapi` | — | Для API-сервера (dashboard) |

### Настройка конфига
1. Скопируйте `bot/config.yaml` под нужный символ (например `bot/config_btc.yaml`)
2. Отредактируйте параметры: `symbol`, `timeframe`, `risk_pct`, `sl_pct`, `tp1_pct`, `tp2_pct`, `leverage`, `mode`

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

---

## 8. Известные проблемы / TODO

| Файл:строка | Проблема | Приоритет |
|-------------|----------|-----------|
| `order_manager.py:499-541` | `get_realized_pnl` парсит `futures_user_trades` с допущением о чередовании сторон. Может ошибиться при частичных исполнениях или нескольких входах в одну сторону. | 🔴 High |
| `market_data.py:62-161` | REST polling каждые 10 сек — задержка до 10 сек после закрытия свечи. Нет вебсокетов. | 🟡 Medium |
| `recovery_client.py:23-35` | `readRecoveryConfig` читает файл **при каждом вызове** (без кэша). Может стать бутылком при частых claim. | 🟡 Medium |
| `main.py:329-335` | `df_buffer = pd.concat([df_buffer, new_row]).tail(500)` — создаёт новый DataFrame каждую свечу. Утечка памяти при долгой работе. | 🟡 Medium |
| `config.py:51-56` | `load_config` игнорирует неизвестные поля в YAML молча (`filtered = {k:v for k,v in data.items() if k in valid_fields}`). Опечатки в конфиге не выдают ошибку. | 🟡 Medium |
| `bot/*.yaml` | Много конфигов под разные символы (`config_btc.yaml`, `config_eth.yaml` и т.д.) — нет единого шаблона/генератора. | 🟢 Low |
| `kucoin/` | Отдельный бот для KuCoin дублирует логику. Нет общего ядра. | 🟢 Low |

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

## 10. Вопросы к автору для доработки

1. **Какой алгоритм выбора монет?** Сейчас символ жестко задан в `config.yaml` (`BTCUSDT`). Планируется мульти-символьный запуск или ручной выбор?

2. **Как обрабатывается частичное заполнение маркет-ордера при входе?** В `order_manager.py:_get_fill_price` берётся `avgPrice` или средневзвешенное по fills. Но если ордер заполнился частями на разных ценах — entry_price в трекере будет средним, а SL/TP рассчитаны от сигнальной цены. Это намеренно?

3. **Какой механизм выбора recovery-цепочки?** `recovery.claim()` возвращает `chainId`, но логика приоритета (старший долг, наибольший убыток, случайный) скрыта на сервере. Нужно ли боту знать/контролировать выбор?

4. **Что происходит при разрыве связи с Binance во время открытой позиции?** Polling продолжает попытки, но SL/TP уже стоят на бирже. Есть ли сценарий "force close" или ручное управление в таком случае?

5. **Почему для recovery-позиций TP1 закрывает 100% позиции?** (см. `position_tracker.py:386-400`). Это осознанное решение (одна сделка = одно закрытие) или баг копипаста?

6. **Планируется ли вебсокет-стриминг свечей вместо REST polling?** Текущая задержка до 10 сек может критична для скальпинга на 5m.

7. **Как часто бот проверяет, что recovery-долг действительно погашен на сервере, и есть ли повторные попытки при ошибке release?**

---

*Файл создан автоматически на основе анализа кодовой базы. Для уточнения деталей см. исходные файлы в `bot/`.*
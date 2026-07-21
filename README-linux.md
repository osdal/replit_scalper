# Trading Bot для Binance 🚀

**Автоматизированная торговля на криптовалютных фьючерсах Binance с использованием стратегий технического анализа, управления рисками и восстановления позиций в режиме реального времени.**

---

## 1. Название и краткое описание

Данный проект — это трейдинг-бот, который автоматически отслеживает и торгует фьючерсными контрактами (например, BTCUSDT, ETHUSDT) на Binance Futures. Бот использует смешивание стратегий технического анализа (EMA, HTF-фильтр), управления рисками, восстановления позиций и порога срабатывания TP/SL для поддержания прибыльных сделок. Реализованы режимы live (проводные ордера на бирже), paper (симуляция) и backtest (историческое тестирование). Бот работает круглосуточно, подчиняется стандартам безопасности (локировка процессов, обработка сигналов остановки) и интегрируется с внешними системами логирования и мониторинга (PostgreSQL API, recovery-сервиса).

---

## 2. Архитектура проекта

### Основная структура

```
bot/
├── main.py                    # Точка входа, orchestrates workers
├── config.py                  # Dataclass Config и загрузка YAML
├── requirements.txt           # Python dependencies
├── .env.example               # Пример переменных окружения
├── logs/                      # Логи работы бота (rotating files)
├── logs/*.log                 # Файлы логов (write by logging.handlers)
├── strategy.py                # Расчёт индикаторов + поиск сигналов
├── signal_handler.py          # Подтверждение сигналов (задержка + HTF validation)
├── order_manager.py            # Работа с ордерами на бирже (открытие, SL/TP, лимит массы)
├── position_tracker.py         # Внутреннее представление позиций, синхронизация с биржей
├── market_data.py             # Polling клайн-свечок и polling HTF-таймфрейма
├── recovery_client.py         # Клиент для работы с recovery-сервисом (заимствование долга)
├── db_reporter.py             # HTTP-клиент для POST/GET/PUT/DELETE доступа к API-серверу (PostgREST)
├── log_importer.py            # Импорт логов из tradingview (или ручной загрузки) в trades API
├── backtester.py              # Выполнение backtest и экспорт результатов
├── signal_overlap.py           # Поиск перекрытия сигналов (раскомментировано, но не используется)
├── position_tracker.py         # Работа с позициями и синхронизация
├── backtest_runner.py          # Запуск бота в бэктест-режиме
├── signals_handler.py          # Отдельная копия signal_handler (старый файл, возможно, дублирует)
├── order_manager.py            # Повторный order_manager (дублирование)
└── many config_*.yaml          # Per-symbol configuration
```

### Основные модули и их взаимодействие

| Модуль | Основная ответственность | Взаимодействие |
|-------|-------------------------|-------------|
| **main()** | Парсинг config файла, активация lock-файла, выбор режима (live/paper/backtest), запуск `OrderManager`, `PositionTracker`, `SignalHandler`, `DbReporter`, `RecoveryClient`, `market_data` polling, обработка сигналов остановки. | Запускает `_run_live_or_paper` (работа) или `run_backtest`. |
| **config.py** | Конфигурация `Config` (стратегические параметры, трейдинг-таблица, лог-файл). | Загружается main.py. |
| **strategy.py** | Загрузка свечных данных, расчёт EMA (fast/slow), расчёт HTF-EMA (опционально), определение сигнала (`LONG`/`SHORT`) на основе пересечения EMA и HTF-тенденции. | Предоставляется `df_buffer` и `htf_buffer`. |
| **signal_handler.py** | Подтверждение сигнала (ранняя / поздняя задержка на свечу), блокировка сигнала HTF-тенденцией. | Получает сигнал из стратегии, возвращает подтверждённый сигнал. |
| **order_manager.py** | Работа с ордерами на бирже: `get_balance`, `open_position`, `close_dust`, `cancel_all_tp_sl`, `move_sl_to_breakeven`, `_get_real_position_qty`, `sync_unrealized_pnl`. Управляет слотами позиций, калькулятор `calc_recovery_quantity`. | Тесно взаимодействует с `position_tracker` (загрузка позиций), `strategy` (для расчёта сигналов), `recovery` (для recovery-режима). |
| **position_tracker.py** | Внутреннее представление `Position`, синхронизация с биржой (`sync_position_on_start`, `sync_unrealized_pnl`), расчёт `pnl`, запуск `check` (TP/SL), обработка частичных закрытий, запись в файл состояния, роль `is_recovery`. | Взаимодействует с `order_manager` (для _get_real_position_qty), `report_position` (`DbReporter`), `apply_hit_async` (расчёт PnL). |
| **market_data.py** | Загрузка свечей (`get_recent_klines`), запуск `start_kline_polling` (единая задача для каждого таймфрейма). | Предоставляет rows для `strategy`. |
| **recovery_client.py** | HTTP-клиент для работы с recovery-сервисом (`claim`, `report`, `release`). Работает по шаблону `DbReporter` (internal API token). | Задействуется `order_manager.open_position`. |
| **db_reporter.py** | HTTP-клиент для торговли с API-сервером (`PostgREST`): `fetchBots`, `patch`, `PATCH /bots/{symbol}`, `POST /trades`. Отправляет heartbeat и позиции. | Предоставляет информацию bot'у (`PositionTracker`). |
| **log_importer.py** | Импорт логов в trades API (`GET /trades`, `POST /trades`). | Используется администратором для загрузки historical данных. |
| **backtester.py** | Выполнение исторического тестирования (загрузка данных, расчёт PnL, графики). | Вызывается main в режиме backtest. |

### Точки входа

1. **Локальный бот**: `python main.py config_bnb.yaml` (либо другой config-файл).
2. **Режимы работы**:
   - **Live**: на бирже, ордера направляются напрямую по Binance API.
   - **Paper**: симуляция ордеров (не реализовано).
   - **Backtest**: запуск `python -m bot.backtester` или любая скриптовая интеграция.

---

## 3. Ключевые торговые стратегии

### 3.1 Основные стратегии (main timeframe)

* **EMA-crossover**: сигнал `LONG` генерируется, когда fast EMA crosses above slow EMA AND HTF-тенденция `LONG`. Сигнал `SHORT`, когда fast EMA crosses below slow EMA AND HTF-тенденция `SHORT`.
* **Подтверждение задержки**: сигнал учитывается только после короткого периода (`SignalHandler`), что предотвращает старение на ruido.

### 3.2 Загрузка HTF (High-Timeframe) фильтра

* Если `htf_enabled == true`, parallel polling ещё одного таймфрейма (`htf_timeframe`).
* EMA на HTF (`htf_ema_fast`, `htf_ema_slow`) → `get_htf_trend_latest` вычисляет `LONG`/`SHORT`/`off`.
* Если тренд HTF противоречит сигналу основного таймфрейма → сигнал блокируется.

### 3.3 Recovery-режим

* При открытии позиции используется свободная задолженность от `recovery_client`.
* Размер позиции рассчитывается на основе долга, бонуса, баланса и рисков.

---

## 4. Управление рисками

| Риск | Реализация |
|------|----------|
| **Стоп-лосс (SL)** | `Config.sl_pct%` от entry-price; рассчитывается как `sl_price`. Для LONG: `entry − SL%`; для SHORT: `entry + SL%`. |
| **Тейк-профит (TP1, TP2)** | `tp1_pct` и `tp2_pct%` от entry. TP2 используется для полного закрытия позиции. |
| **Ограничение размера позиции** | `risk_pct%` от баланса → позиция рассчитывается по `notional = balance * risk_pct / 100`; учитывается `leverage`. |
| **Partial TP1 close** | `tp1_close_pct%` закрывает часть позиции на TP1; оставшаяся часть остаётся открыта на TP2/SL. |
| **Recovery-режим** | Дополнительный риск закрытия долга -> бонус; учитывает `recovery_max_pct` (или unlimited). |
| **Dust позиция** | Если `notional < 1.0 USD` → автоматическое закрытие через `close_dust`. |
| **Балансировка SL к точке без убытка (Breakeven)** | При TP1-hit движем SL к entry-price, фиксируем прибыль части позиции. |

---

## 6. Управление доступом (RBAC)

### Роли пользователей

| Роль | Область ответственности | Разрешенные действия |
|------|------------------------|---------------------|
| **guest** | Неавторизованный пользователь | Просмотр публичных данных, вход в систему |
| **user** | Обычный пользователь | Просмотр дашборда, изменение пароля, доступ к личным настройкам |
| **superadmin** | Администратор | Полный доступ ко всем функциям, управление пользователями |

### Ограничения интерфейса

| Элемент UI | Доступные роли | Описание |
|------------|----------------|----------|
| Меню навигации (PnL Chart, Trades, Stats, Backtest, Optimizer, Recovery) | superadmin | Видны только для супер-админов |
| Вкладка «Настройки» (Settings) | user, superadmin | Доступна после авторизации для обеих ролей |

### Эндпоинты API с проверкой прав

| Эндпоинт | Метод | Минимальная роль | Описание |
|----------|-------|------------------|----------|
| `/api/bots` | GET | user | Получение списка ботов пользователя |
| `/api/bots/:symbol` | PUT | user | Обновление конфигурации бота |
| `/api/bots/:symbol/start` | POST | superadmin | Запуск бота |
| `/api/bots/:symbol/stop` | POST | superadmin | Остановка бота |
| `/api/bots/stop-all` | POST | superadmin | Остановка всех ботов |
| `/api/trades` | GET, POST | user | Управление сделками |
| `/api/trades` | DELETE | superadmin | Удаление всех сделок |
| `/api/trades/:id` | DELETE, PATCH | superadmin | Удаление/обновление сделки |
| `/api/recovery/*` | GET, PUT | superadmin | Управление recovery-модом |
| `/api/optimizer` | POST | superadmin | Запуск оптимизатора |
| `/api/backtest` | POST | superadmin | Запуск бэктеста |

### Механизм проверки прав

1. **Backend (auth.ts)**: Middleware `authContext` проверяет JWT токен и роль из `public.profiles`.
2. **Frontend**: Компонент использует хук `useRole()` для получения роли и проверки прав через `can(...)`.
3. **UI-ограничения**: Элементы интерфейса скрываются или отключаются в зависимости от роли.

### Рекомендации по безопасности

- Все эндпоинты (кроме публичных) требуют валидный JWT
- Операции супер-админа должны быть журналируемыми
- Рекомендуется реализовать rate limiting для API
- RLS (Row Level Security) должен быть настроен на уровне базы данных

---

## 7. Работа с API Binance

### 5.1 Binance Client (library)

* Используется `python-binance` (`from binance import AsyncClient`).
* Конфигурируется через `env`: `BINANCE_API_KEY`, `BINANCE_API_SECRET`.
* **Режим live**: обязательный API-ключ, поддерживаются ордера (`futures_create_order`), запросы баланса, история позиций (`futures_position_information`), запросы ордеров. **Режим paper**: API не требуется, `order_mgr` работает в симуляционном режиме.

### 5.2 Обработка ошибок и лимитов

* Обработка временных ошибок (`asyncio.RetryError`, `CCXHTTPErrors`) в `market_data.py`.
* Для ордеров: `try-except` в `order_manager.open_position` / `close_dust` / `cancel_all_tp_sl`; ошибки логируются и происходят повторные попытки при необходимости.
* Лимиты: при превышении лимитов API Binance генерируется `APIError`; бот обработает и повторит попытку через позже (печать события).

---

## 6. Переменные окружения (ENV)

### `bot/.env.example`

```bash
# Binance API credentials (required for live mode)
BINANCE_API_KEY=YOUR_BINANCE_API_KEY
BINANCE_API_SECRET=YOUR_BINANCE_API_SECRET

# Override API endpoint for alternative Binance API providers (optional)
# BINANCE_API_URL=https://api.binance.com

# Internal API token for server-to-server communication between bot and PostgREST API
# Used by db_reporter.py and recovery_client.py to avoid accidental exposure
INTERNAL_API_TOKEN=super_secret_internal_token

# Optional: suppression of opening log-files and setting different logger
LOG_LEVEL=INFO

# Optional: path to the default config file (used as fallback if not provided as argument)
DEFAULT_CONFIG_PATH=config_bnb.yaml
```

### `artifacts/dashboard/.env.example` (для фронтенда)

```bash
# Supabase configuration (required for auth)
VITE_SUPABASE_URL=https://your-project.supabase.co
VITE_SUPABASE_ANON_KEY=your_anon_key

# Internal API token for dashboard to call API-server securely
INTERNAL_API_TOKEN=super_secret_dashboard_token

# API server URL (default: http://localhost:5000/api)
VITE_API_URL=http://localhost:5000/api

# Port used by Vite development server
VITE_PORT=5173
```

---

## 7. Инструкция по установке и запуску

### 7.1 Установка среды

```bash
# Создайте и активируйте виртуальную среду
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Установите зависимости (современная система с зависимостями)
pip install -r bot/requirements.txt

# (Опционально) для локального API-сервера/дэшборда используйте Docker
# docker-compose up -d   # см. артифакты/api-server
```

### 7.2 Кастомизация конфига

Выберите один из шаблонов конфигурации (`config_bnb.yaml` …) и настройте:

```yaml
symbol: "BTCUSDT"
timeframe: "5m"
leverage: 10
risk_pct: 1.0           # 1% риска от баланса
sl_pct: 0.5             # 0.5% от entry-price
tp1_pct: 0.5            # 0.5%
tp1_close_pct: 50       # 50% прибыли на TP1
tp2_pct: 1.0            # 1%
ema_fast: 9            # быстрый EMA
ema_slow: 21           # медленный EMA
volume_ma_period: 20
volume_multiplier: 1.2
auto_mode: true
paper_balance: 1000     # баланс для paper mode
log_file: "bot/logs/bot.log"
htf_enabled: true
htf_timeframe: "1h"
htf_ema_fast: 9
htf_ema_slow: 21
auto_mode: true
mode: "live"           # live | paper | backtest
backtest_start: "2024-01-01T00:00:00Z"
backtest_end:   "2024-06-13T23:59:59Z"
```

### 7.3 Запуск торгового бота

```bash
# Live (онлайн) режим
python bot/main.py bot/config_bnb.yaml

# Paper режим (ордера не отправляются, симуляция расчетов)
python bot/main.py bot/config_bnb.yaml  # mode: "paper"

# Backtest (историческое тестирование) режим
python bot/backtest_runner.py bot/config_bnb.yaml

# Запуск в фоне (Linux/macOS)
nohup python bot/main.py bot/config_bnb.yaml > bot/logs/bot.log 2>&1 &

# Windows (в примере есть скрипт)
bot\run_btc.bat
```

### 7.4 Настройка и запуск API-сервера (опционально)

```bash
# Перейдите в artifacts/api-server
cd artifacts/api-server
# Установка зависимостей (если нужны)
pip install -r requirements.txt
# Запуск в режиме разработки (tsx автоматически переводит TS в JS)
npm run dev   # или npx tsx src/index.ts
```

### 7.5 Настройка фронтенда (Dashboard)

```bash
cd artifacts/dashboard
# Установка зависимостей
npm install
# Запуск локальной разработки
npm run dev   # работает на http://localhost:5173 (или VITE_PORT)
```

---

## 8. Логирование

### 8.1 Структура лога

```
bot/logs/bot.log
┌── [YYYY-MM-DD HH:MM:SS] [INFO|DEBUG|WARNING|ERROR] Модуль: Имя функции: строка сообщения
│
├── [2026-07-21 13:07:42] [INFO] main: main() Запуск бота | mode=live symbol=BTCUSDT tf=5m
├── [2026-07-21 13:07:43] [INFO] strategy: calculate_indicators Загружено 200 свечей для warm-up (5m)
├── [2026-07-21 13:07:45] [INFO] market_data: on_candle #1 price=63245.0
├── [2026-07-21 13:07:46] [DEBUG] signal_handler: ConfirmSignal: задержка 1 свеча
├── [2026-07-21 13:07:47] [INFO] order_manager: open_position Открыта LONG-запись для BTCUSDT qty=0.1 entry=63245.0 sl=62246.0 tp1=63670.0 tp2=65245.0
└── ...
```

### 8.2 Logger и событийный логгер

* **Основной logger** (bot/logger.py): записывает в файл `bot/logs/bot.log`; уровни: `DEBUG`, `INFO`, `WARNING`, `ERROR`.
* **Событийный logger** (bot/logger.py): используется отдельно для событий `events.info/warning` (в position_tracker и других местах).
* В режиме paper: симуляция ордеров с `logger` (отсутствие network-запросов).

### 8.3 Формат сообщения

```
[YYYY-MM-DD HH:MM:SS] [LEVEL] Модуль: Функция: строка сообщения
```

---

## 9. Известные проблемы / TODO

| # | Файл:Строка | Описание проблемы | Приоритет |
|---|------------|------------------|----------|
| 1 | `bot/main.py:155` (внутри `_acquire_lock`) | Логика блокировки lock-файла может быть улучшена (нет временной обработки, может зависнуть). Разработан только на случай ONE процесса бота на символ. | **СРЕДНИЕ** |
| 2 | `bot/strategy.py:–` (нет файла в repo) | **IMPORTANT**: стратегия (расчёт индикаторов, сигналы) временно отсутствует. Основная торговля опирается только на EMA + HTF с использованием `market_data` и `strategy` модулей — последний не реализован. Требуется аутентичная интеграция with real-time data. | **ИЗОБИЛИТЕЛЬНАЯ** |
| 3 | `bot/recovery_client.py:–` (redirects) | Recovery-client использует аналогичный шаблон на основе aiohttp c `options_timeout=5`. Используемые константы могут быть жёсткими; экспонирование (long polling) событий recovery может потребовать повторных попыток. | **СРЕДНИЕ** |
| 4 | `bot/db_reporter.py:–` (shared_options) | Все запросы к API (trades, bots) генералируют `User-Agent: python-requests/2.32.0`. Для производительности может потребоваться rate limiting (например, max 10 req/s) и retry с exponential back-off. | **СРЕДНИЕ** |
| 5 | `bot/log_importer.py:–` (file parsing) | Логирование данных реализовано (через внешние инструменты), однако требуется проверка различных форматов логов из tradingview. Импорт: возможные ошибки при обработке заголовков. | **СРЕДНИЕ** |
| 6 | `artifacts/api-server/src/lib/auth.ts:–` (authContext) | Middleware checks decode of JWT + role fetch from public.profiles (service-role). **NO CURRENT RLS** in auth module; сделать проверку для guest/superadmin/restore отбой. | **СРЕДНИЕ** |
| 7 | `artifacts/dashboard/src/Dashboard.tsx:–` (UI RBAC) | **РЕШЕНО**: UI скрывает меню и вкладки на основе `role`. Для user показывается только Settings, для superadmin — полный доступ. | **РЕШЕНО** |
| 8 | `artifacts/dashboard/src/components/AuthScreen.tsx:–` (интеграция) | AuthScreen сейчас получает `onCancel`; нужна обработка `Sign Up` → authProvider, поддержка email/password + Google. | **СРЕДНИЕ** |
| 9 | `bot/backtester.py:–` (backtest runner) | `backtest_runner.py` для запуска бота в backtest-режиме; требуется актуализировать config-файл, чтобы избежать runtime errors (недостающие поля). | **СРЕДНИЕ** |
| 10| `bot/signal_handler.py:–` (сервис) | Отсутствие глибинговского сигнала / обслуживание входящих ордеров временно прекращено; требуется полностью реализовать процесс регистрации сигналов. | **ИЗОБИЛИТЕЛЬНАЯ** |

### Основные фокусы для изменений (high-priority)

1. **Реализация первой проблемы**: оптимизировать обработку блокировок lock-файла (логика может быть устаревшей).
2. **Реализация второй проблемы**: создать рабочую стратегию (расчёт индикаторов + поиск сигналов) — сердцевина бота.
3. **Настройка recovery-сервиса**: проработка полноценной работы клиент-сервер интеграции (claim/report/release).
4. **Настройка Flask/PostgreSQL API-сервера**: реализовать middleware auth, агрегировать роуты (trades, bots, recovery, refresh, backtest, optimizer, binance-sync) с адекватной обработкой ошибок, rate limiting.

Приоритетные направления: **стратегия** (также требуется), **auth middleware**, **rate limiting**.

---

## 11. Зависимости

### 11.1 Python (бот)

| Пакет | Версия | Назначение |
|---------|-------|---------|
| `python-binance` | ~1.0 | Асинхронный клиент Binance Futures API. |
| `pandas` | ~2.2 | Работа с DataFrame, расчёт индикаторов. |
| `jsonschema` | ~4.23 | Валидация YAML конфигурации. |
| `dotty` / `colorama` | — | Не используется (устарелые импорты?). |
| `aiohttp` | — | Неактуальный; используется только в `recovery_client` и `db_reporter`. |
| `yaml` (`PyYAML`) | ~6.0 | Парсинг YAML-конфигураций. |

**Файл `requirements.txt`**: `pip install -r bot/requirements.txt` (временные версии указаны в bot/requirements.txt).

### 10.2 Node + Typescript (Dashboard + API-Server)

| Пакет | Версия | Назначение |
|---------|-------|---------|
| `vite` | ^5 | Frontend хостинг + hot reload (dashboard). |
| `@supabase/supabase-js` | ^2.110 | Фронтенд Supabase auth (`useSupabase`). |
| `lucide-react` | ^0.383 | React-иконки. |
| `shadcn/ui` | ^1.2 | UI компоненты (Card, Button, Tabs, Badge, Select). |
| `recharts` | ^2.15 | Графики (PnL). |
| `class-variance-authority`, `clsx`, `tailwind-merge` | ^0.7, ^2.1 | Утилиты для className. |
| `express`, `cors`, `pino`, `js-yaml` | — | API-сервер (Node). |
| `typescript`, `tsx`, `esbuild` | — | Типизация + сборка. |
| `tailwindcss` | — | Formatting (в integrated codebase). |
| `postcss` | — | CSS processing. |

---

## Вопросы к автору для доработки

1. **Имена и настройки токенов:** Какие именно `BINANCE_API_KEY` / `BINANCE_API_SECRET` требуются (можно ли получить через Binance Testnet)? Как и где я смогу получить эти данные?

2. **Стратегия и индикаторы:** Какие именно технические индикаторы используются (`EMA`, `HTF EMA`, volume MA, etc.)? Нужно ли мне самостоятельно реализовать их или вы предоставляете готовую реализацию? Есть ли возможности настройки периода, сигналов пересечения, порогов и т.п.?

3. **Управление позициями и риском:** Как происходит выбор размера позиции (Risk %). Есть ли динамическое ребалансирование с учётом волатильности (`ATR`, `BB`)? Как работает `partial TP1 close` и `breakeven move`? Нужно ли additional логирование для каждого ордера?

4. **Интеграция с recoveryservice:** Какой форматы данных передаются в recovery (форматы JSON)? Есть ли ограничения в размере, дополнительны бонусы? Нужно ли одобрение бота перед использованием recovery?

5. **Запуск и интеграция с внешними сервисами:** Есть ли конфигурационный файл `config.yaml`, как я могу его редактировать? Как бот взаимодействует с внешними API (`/api/trades`, `/api/bots`, `/api/refresh`, `/api/recovery`)? Что если API недоступен (например, server-down)? Какие действия предпринимаются (fallback, retry)?

6. **Режимы работы:** Чем отличается paper-режим от live? Как эмулируются ордера в paper? Есть ли разница в логике принятия решений?

7. **Бэкап и восстановление:** Как бот восстанавливает состояние после перезапуска? Где хранятся файлы состояния позиций? Что происходит при потере lock-файла?

---

*Если Kilocode выяснит дополнительные детали и примет необходимые корректировки — напишите их сюда.*

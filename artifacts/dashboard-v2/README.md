# Trading Bot Dashboard

Веб-дашборд для управления торговыми ботами.

## Запуск

### 1. Установить зависимости
```powershell
cd artifacts\dashboard
npm install
```

### 2. Запустить дашборд
```powershell
npm run dev
```

Открыть в браузере: http://localhost:3000

### 3. Убедиться что API сервер запущен
Дашборд подключается к `http://localhost:5000/api`.
Если API не запущен — бот-карточки будут пустыми.

## Запуск API сервера
```powershell
cd artifacts\api-server
npm run dev
```

## Server engine cutover (Phase 3)

По умолчанию `VITE_GRID_ENGINE` не задан → дашборд работает как раньше (browser engine,
сетки считаются и исполняются во вкладке браузера).

Порядок переключения на серверный движок:

1. `artifacts/dashboard-v2/.env`: `VITE_GRID_ENGINE=server` (см. `.env.example`).
2. `artifacts/api-server/.env`: `GRID_ENGINE_ENABLED=true`
   (опционально `GRID_AUTO_ENABLED=true` — серверный авто-режим).
3. Перезапустить оба процесса: dashboard-v2 и api-server.
4. Открыть вкладку дашборда. При первом запуске она один раз мигрирует сетки из
   `localStorage['gridsim.state.v1']` в БД (`POST /api/grids/import`), перенесёт ключ в
   `gridsim.state.v1.migrated` (данные сохраняются) и станет viewer'ом: `GET /api/grids`
   каждые ~5 с. После миграции вкладку можно закрыть — торговлю продолжит api-server.

Откат: убрать `VITE_GRID_ENGINE` (или выставить `browser`) и перезапустить дашборд.

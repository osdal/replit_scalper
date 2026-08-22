# API Server

Express сервер для дашборда. Использует SQLite — никакой внешней БД не нужно.

## Первый запуск

```powershell
cd artifacts\api-server
npm install

# Инициализировать БД и добавить ботов из yaml конфигов
$env:BOT_DIR="C:\DATA\bots\replit_scalper\bot"
npm run init-db

# Запустить сервер
npm run dev
```

Сервер запустится на http://localhost:5000

## Переменные окружения

Основные переменные загружаются из корневого `.env` проекта. Дополнительно можно задать в `artifacts/api-server/.env`:

```
BOT_DIR=C:\DATA\bots\replit_scalper\bot
DATABASE_PATH=C:\DATA\bots\replit_scalper\data\bot.db
PORT=5000
BOT_PYTHON=C:\Users\osdal\AppData\Local\Programs\Python\Python311\python.exe
```

`BOT_PYTHON` — полный путь к Python, в котором установлены зависимости бота (`pandas`, `python-binance` и т.д.). Используется при запуске ботов из дашборда. Если не задан, сервер попытается найти подходящий `python` в PATH.

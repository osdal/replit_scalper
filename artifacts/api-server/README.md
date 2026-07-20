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

Создай `.env` в папке `artifacts/api-server/`:
```
BOT_DIR=C:\DATA\bots\replit_scalper\bot
DATABASE_PATH=C:\DATA\bots\replit_scalper\data\bot.db
PORT=5000
```

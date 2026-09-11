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

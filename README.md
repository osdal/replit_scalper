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

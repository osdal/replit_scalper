# Переезд на Oracle Cloud Free Tier

Инструкция для `replit_scalper`: перенос стеков testnet и live с Windows-машины
на виртуальную машину в Oracle Cloud (Always Free).

Сделано 02.10.2026. Актуально для состояния репозитория на эту дату.

---

## 0. Что важно знать ДО начала

| Тема | Что нужно |
|---|---|
| **Free tier урезан** | С 15 июня 2026 Always Free A1 — это **2 OCPU / 12 ГБ RAM** (было 4/24). Инстансы больше этого лимита Oracle отключает. Считайте под 2/12. |
| **Home region выбирается навсегда** | Always Free Compute можно создать только в home region. Навсегда выбрать другую страну/регион нельзя. Выбирайте регион, ближайший к бирже (Frankfurt/Amsterdam) — меньше задержка. |
| **Простой = остановка** | Инстанс считается idle за 7 дней, если CPU (95-й перцентиль) < 20%, сеть < 20% и память < 20%. Бот простаивает (свечи 5 мин) → нужен keepalive, см. §7. |
| **A1 hard to get** | «Out of host capacity» — самая частая проблема. Берите A1 2/12; если нет — E2.1.Micro (1 ГБ) только под live-стек, см. §8. |
| **IP для Binance** | В `.env.live` ключи заведены с IP-whitelist. Вам нужен **Reserved public IP**, иначе после каждой остановки инстанса IP меняется и Binance отклоняет ключи (-2014). |
| **Не открывайте порты наружу** | 5001/5176 — это live-контур с реальными деньгами и кнопкой Close All. Наружу отдавайте только SSH, доступ к UI — через SSH-туннель. См. §6. |
| **30 дней триала** | $300 кредитов на 30 дней. Если их потратить на платные ресурсы, их заберут. Держитесь Always Free-ресурсов. |

---

## 1. Создание VM в консоли Oracle

1. Войти в <https://cloud.oracle.com> → **Compute → Instances → Create instance**.
2. **Name**: `trading-bot`.
3. **Image**: Ubuntu 24.04 (или 22.04). Своей версии Python на хосте не нужно —
   всё работает в контейнерах (`node:20-slim`, там же Debian + Python 3.11).
4. **Shape**: `VM.Standard.A1.Flex`, **2 OCPU / 12 GB RAM** (в сумме по tenancy,
   не больше). Если A1 недоступна — см. §8.
5. **Networking**: создать новый VCN (кнопка Create VNIC) либо взять дефолтный.
   Подсеть public — нужен публичный IP наружу.
6. **SSH keys**: **Generate key pair for me** и обязательно **Download Private Key**
   (`ssh-key-ocid...key`). Этот файл больше нигде не получить. Либо вставьте свой
   ключ (`ssh-keygen -t ed25519`).
7. **Boot volume**: увеличьте до **100 ГБ** (по умолчанию ~47 ГБ). Docker-образы
   занимают 4-8 ГБ, логи ботов растут быстро.
8. Create.

---

## 2. Reserved public IP (обязательно)

Ephemeral IP меняется при каждой остановке/старте инстанса. Для Binance и для
keepalive нужен постоянный.

**Compute → Instance → trading-bot → Attached VNIC → Private IP** →
**«Add resilience (reserved) IP»** либо Networking → Reserved IPs → Create
(instance + VNIC).

Запишите адрес — он понадобится в §5 (whitelist Binance).

---

## 3. Подготовка VM

```bash
ssh -i ~/Downloads/ssh-key-ocid-xxxx.key ubuntu@<RESERVED_IP>
```

На VM:

```bash
sudo apt update && sudo apt upgrade -y
git clone https://github.com/osdal/replit_scalper.git /opt/trading-bot
cd /opt/trading-bot
sudo bash deploy/oci/bootstrap-ubuntu.sh
```

Скрипт поставит Docker, создаст swap 2 ГБ, ограничит рост docker-логов
(иначе диск забьётся), создаст `/opt/trading-bot/data` и `logs`.

---

## 4. Перенос данных

### 4.1. Сначала закоммитить и запушить изменения

Вся работа с Docker лежит **локально и не закоммичена**. Проверьте:

```powershell
cd C:\DATA\bots\replit_scalper
git status
git add .
git commit -m "docker: dual stack testnet/live, init-db fix, OCI deploy scripts"
git push
```

`.env`, `.env.live`, `data/`, `logs/` в `.gitignore` — через git они **не**
передаются, это правильно (ключи не должны попадать в репозиторий).

### 4.2. Остановить ботов на Windows (обязательно)

SQLite нельзя копировать, пока его пишут. На Windows:

```powershell
curl.exe -X POST http://localhost:5001/api/bots/stop-all
curl.exe -X POST http://localhost:5000/api/bots/stop-all
docker-compose down
```

### 4.3. Скопировать секреты и данные

> **Commit + push — это только код. Для переезда этого недостаточно.**
> Три критичных файла закрыты `.gitignore` и через git не передаются:
>
> | Файл | Правило .gitignore | Что потеряем |
> |---|---|---|
> | `.env.live` | `.env`, `.env.*` | реальные ключи Binance — бот не подключится |
> | `bot/state_live_<символ>.json` | `state_*.json` (строка 17) | **открытую позицию**: направление, вход, SL/TP, `backstop_algo_id`, шаг reverse-цепочки |
> | `data/bot_live.db` | `*.db`, `data/` | историю сделок, `armed`, позиции в БД |
>
> Без `state_*.json` новый бот стартует «пустым»: позиция на бирже есть, но бот её
> не видит — не поставит TP, не будет доводить SL/reverse и не восстановит учёт.
>
> **Код коммитить и пушить при открытой позиции безопасно** — работающие процессы
> это не затрагивает. Но перенос состояния делается отдельно, в момент передачи.

Из PowerShell (scp идёт по SSH-ключу):

```powershell
scp -i $env:USERPROFILE\Downloads\ssh-key-ocid-xxxx.key `
    C:\DATA\bots\replit_scalper\.env `
    C:\DATA\bots\replit_scalper\.env.live `
    ubuntu@<RESERVED_IP>:/opt/trading-bot/

# Открытые позиции: state-файлы (обязательно, если позиция открыта)
scp -i $env:USERPROFILE\Downloads\ssh-key-ocid-xxxx.key `
    C:\DATA\bots\replit_scalper\bot\state_live_1000pepeusdt.json `
    ubuntu@<RESERVED_IP>:/opt/trading-bot/bot/

# БД (около 87 МБ вместе с backups; bot_live.db всего ~0.7 МБ)
scp -r -i $env:USERPROFILE\Downloads\ssh-key-ocid-xxxx.key `
    C:\DATA\bots\replit_scalper\data `
    ubuntu@<RESERVED_IP>:/opt/trading-bot/

# Логи нужны только для истории; можно пропустить (210 МБ)
scp -r -i $env:USERPROFILE\Downloads\ssh-key-ocid-xxxx.key `
    C:\DATA\bots\replit_scalper\logs `
    ubuntu@<RESERVED_IP>:/opt/trading-bot/
```

> **Не запускайте один и тот же символ на двух машинах.** Один аккаунт, один
> символ, два процесса = удвоение позиции. На Windows бот должен быть остановлен
> (§4.2) **до** старта на Oracle.

На VM:

```bash
cd /opt/trading-bot
chmod 600 .env .env.live
mkdir -p data logs
```

**Что переносится и что нет:**

| Что | Где | Комментарий |
|---|---|---|
| Код | git clone | Dockerfile, docker-compose.yml, deploy/ |
| `.env` | scp | тестнет-ключи, порт 5000 |
| `.env.live` | scp | **реальные** ключи Binance, порт 5001 |
| `data/bot.db` | scp | БД testnet |
| `data/bot_live.db` | scp | БД live |
| `bot/state_*.json` | git/копия | состояние открытых позиций (в `.gitignore` — копировать вручную если есть открытая позиция!) |
| `bot/logs/` | scp или не копировать | логи |

> **Важно про `bot/state_*.json`:** файл `bot/state_live_<символ>.json` — это
> состояние открытой позиции. Он в `.gitignore`, через git не придёт. Если на
> момент переезда есть открытая live-позиция — скопируйте этот файл вручную,
> иначе бот при старте не увидит позицию (хотя на бирже она есть) и не
> поставит защиту. Либо закройте все позиции перед переездом — это чище.

---

## 5. Binance: IP-whitelist

Если на ключе `.env.live` включён IP-whitelist, добавьте **Reserved public IP**
из §2: <https://www.binance.com> → My API Key → Edit → IP access.

Пока IP не добавлен, бот будет получать `-2014` и не сможет торговать live.

---

## 6. Firewall: наружу только SSH

В консоли: **Networking → VCN → Security Lists → Add Ingress Rules**.

| Порт | Источник | Зачем |
|---|---|---|
| 22 | ваш IP (или 0.0.0.0/0) | SSH |
| 5000, 5001, 5173, 5176 | **не открывать** | доступ через SSH-туннель |

Дашборды и API слушают `0.0.0.0`, поэтому правило на уровне VCN — единственная
защита. SSH-туннель с Windows:

```powershell
ssh -i $env:USERPROFILE\Downloads\ssh-key-ocid-xxxx.key `
    -L 5000:localhost:5000 -L 5001:localhost:5001 `
    -L 5173:localhost:5173 -L 5176:localhost:5176 `
    ubuntu@<RESERVED_IP>
```

После этого на Windows работают: <http://localhost:5173> (testnet) и
<http://localhost:5176> (live).

> Если нужен доступ с телефона/извне — лучше поставить Tailscale или Cloudflare
> Tunnel, а не открывать 5176 на `0.0.0.0/0`: live-дашборд умеет закрывать все
> позиции по рынку.

---

## 7. Первый запуск на VM

```bash
cd /opt/trading-bot
chmod +x stack.sh deploy/oci/*.sh
./stack.sh
```

Поднимет оба стека и сделает Arm+Start для `1000PEPEUSDT`. Проверка:

```bash
docker compose ps
curl -s localhost:5001/api/bots/1000PEPEUSDT | head -c 400
```

### Автостарт после ребута инстанса

```bash
echo "LIVE_SYMBOLS=1000PEPEUSDT" | sudo tee /etc/default/trading-bot
sudo cp deploy/oci/trading-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trading-bot.service
```

### Защита от остановки за простой

```bash
crontab -e
*/10 * * * * /opt/trading-bot/deploy/oci/keepalive.sh >> /var/log/keepalive.log 2>&1
```

Без этого Oracle через 7 дней может остановить инстанс (idle: CPU/сеть/память
< 20%).

---

## 8. Если A1 недоступна (out of capacity)

Вариант `VM.Standard.E2.1.Micro` (1 ГБ RAM) — это слишком мало для полного
стека: там Node API + vite dev-сервер + боты.

Что можно урезать:

1. **Только live-стек**: `docker compose up -d api-live dashboard-live`
   (testnet на этой машине не запускать). Один бот + API + vite ≈ 1.5–2 ГБ.
2. **Swap обязателен** (bootstrap его уже создаёт, 2 ГБ).
3. **Не запускать vite dev-сервер** — отдать статику. Соберите дашборд заранее
   и раздавайте через `nginx`/Node. Это убирает ~300–500 МБ памяти.
4. Честно: 1 ГБ — впритык. 2/12 (A1) комфортнее для 40 ботов + live.

Размер памяти — не только наш расчёт: A1 12 ГБ, ботов 40, и при включённом
`htf`/pandas каждый бот ест 100–200 МБ. Считайте, что 40 testnet-ботов + live
— это 6–9 ГБ. Впритык, но работает.

---

## 9. Обновление кода на VM

```bash
cd /opt/trading-bot
git pull
docker compose build
./stack.sh
```

---

## 10. Откат

Ничего не удаляйте на Windows, пока не убедитесь, что VM работает. Если что-то
пошло не так:

```powershell
cd C:\DATA\bots\replit_scalper
docker-compose up -d      # поднимает стеки обратно
```

БД — это обычные файлы `data/*.db`, копия на Windows остаётся актуальной
(пока не началась торговля на VM).

---

## 11. Чек-лист перед переездом

- [ ] `git status` чистый, всё запушено
- [ ] Боты остановлены на Windows (`stop-all` + `docker-compose down`)
- [ ] `bot/state_*.json` скопированы (или позиции закрыты)
- [ ] `.env` и `.env.live` на VM, `chmod 600`
- [ ] `data/bot.db` и `data/bot_live.db` на VM
- [ ] Reserved public IP назначен и записан
- [ ] IP добавлен в whitelist Binance
- [ ] В VCN открыт только 22
- [ ] SSH-туннель работает, `:5173` и `:5176` отдают страницы
- [ ] `./stack.sh` поднял стеки, live-бот `is_running=true`, heartbeat свежий
- [ ] `systemd` unit и cron keepalive установлены

---

## 12. Что делать не нужно

- **Не переносите `bot/lock*` и старые state-файлы тестнета**, если меняете
  машину во время работы — лучше закрыть позиции и начать с чистого state.
- **Не открывайте 5176 на весь интернет** — это live-контур с реальными деньгами.
- **Не держите `AUTO_RESTART_BOTS=true` в `.env.live`** — сейчас там `false`
  намеренно: после падения API live-боты не должны молча возобновить реальную
  торговлю.
- **Не храните `.env.live` в git.** Он в `.gitignore` — так и должно остаться.
- **Не ставьте `latest`-образы на прод** — у нас свои теги `replit_scalper-api`,
  `replit_scalper-dashboard`, собираются из репозитория.

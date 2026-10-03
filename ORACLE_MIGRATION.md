# Переезд на Oracle Cloud Free Tier

Полная инструкция по переносу стеков **testnet** и **live** с Windows-машины на
виртуальную машину в Oracle Cloud (Always Free).

Документ дополняется по мере реального переезда. Последнее обновление: **03.10.2026**.

---

## 0. Текущий статус

| Шаг | Состояние |
|---|---|
| VCN `trading-bot` (10.0.0.0/16), Internet Gateway, route `0.0.0.0/0`, subnet `public-subnet` (10.0.0.0/24, Public) | ✅ создано |
| Инстанс `trading-bot`: A1.Flex **2 OCPU / 12 GB**, Ubuntu 24.04, boot volume 100 GB / VPU 50 | ✅ Running |
| Публичный IP `92.5.180.72` (eu-frankfurt-1) | ⚠️ **Ephemeral — нужно закрепить Reserved** |
| SSH-доступ, Docker 29.1.3 (arm64), Compose v2.40.3, swap 2 ГБ | ✅ работает |
| Репозиторий в `/opt/trading-bot`, `.env`/`.env.live` (600), БД перенесены | ✅ |
| Образы собраны на ARM, все Python-зависимости проверены | ✅ |
| Стек (`:5000`, `:5001`, `:5173`, `:5176`) поднимается и отвечает 200 | ✅ проверено, **остановлен** |
| Reserved public IP | ❌ не сделано |
| IP в whitelist ключа Binance | ❌ **не сделано — live не запускать** |
| systemd-юнит + keepalive cron | ❌ не настроено |
| Перенос открытой позиции и запуск live на Oracle | ❌ не сделано |

> **Live на Oracle не запускать**, пока не выполнены два пункта выше: Reserved IP и
> whitelist в Binance. Без whitelist ключ возвращает `-2014` и торговля невозможна.

---

## 1. Что важно знать ДО начала

| Тема | Что нужно |
|---|---|
| **Free tier урезан** | С 15 июня 2026 Always Free A1 — это **2 OCPU / 12 ГБ RAM** (было 4/24). Инстансы сверх лимита Oracle отключает. |
| **Home region навсегда** | Always Free Compute создаётся только в home region. Выбирайте регион ближе к бирже — **eu-frankfurt-1** проверен и выбран. |
| **Простой = остановка** | Инстанс считается idle за 7 дней, если CPU (95-й перцентиль) < 20%, сеть < 20%, память < 20%. Бот простаивает (свечи 5 мин) → обязателен keepalive, §8. |
| **A1 hard to get** | «Out of host capacity» — самая частая проблема. Берите A1 2/12; при нехватке мощностей повторите позже или с меньшим размером. |
| **Ephemeral IP меняется** | После каждой остановки инстанса адрес меняется. Для ключа Binance с whitelist нужен **Reserved IP**, §3. |
| **Не открывайте порты наружу** | 5001/5176 — live-контур с реальными деньгами и кнопкой Close All. Наружу только SSH (22), доступ к UI — через SSH-туннель, §9. |
| **Хост-дистрибутив не важен** | Python/Node работают в контейнерах (`node:20-slim` = Debian 12 + Python 3.11). Хосту нужен только Docker. Но Ubuntu удобнее: на Oracle Linux 9 включён SELinux, который конфликтует с bind-mount'ами. |
| **30 дней триала** | $300 кредитов. Always Free-ресурсы после триала остаются; потраченные кредиты на платные ресурсы заберут. |

---

## 2. Создание инстанса — пошагово

### 2.1. VCN — создаём ОТДЕЛЬНОЙ СТРАНИЦЕЙ

Мастер «Create instance» не умеет создавать публичную подсеть с интернетом (сам
Oracle об этом предупреждает). Поэтому VCN и подсеть делаем через **Networking**.

1. **Networking → Virtual Cloud Networks → Create VCN** (идти по адресу
   `https://cloud.oracle.com/networking/vcns`, раздел **Networking** — соседний с
   Compute, а не внутри него).
2. **Name**: `trading-bot`, Compartment: `osdal (root)`.
3. **Add IPv4 CIDR Blocks** → `10.0.0.0/16`. IPv6/BYOIPv6/ULA — пусто.
4. **DNS Label**: `trading-bot` (только латиница в нижнем регистре, цифры, дефис).
5. Create.

> Грабли: кнопки в консоли не копируются вместе с текстом. Если поля «Add» не
> видно — уменьшите масштаб браузера (`Ctrl` + `-`).

### 2.2. Internet Gateway

Открыть VCN `trading-bot` (клик по имени в списке) → сайдбар или раздел **Resources**
→ **Internet Gateways** → **Create**:
- Name: `trading-bot` (имя не важно, главное — другое IGW в этом VCN не создавать)
- VCN: `trading-bot`

### 2.3. Маршрут в интернет

В том же VCN → **Route Tables** → **Default Route Table for trading-bot** → **Edit**
→ **Add Route Rules**:
- Target Type: **Internet Gateway**
- Target: созданный шлюз
- Destination: `0.0.0.0/0`

### 2.4. Публичная подсеть

В том же VCN → **Subnets** → **Create Subnet**:
- Name: `public-subnet`
- IPv4 CIDR block: `10.0.0.0/24`
- **Public access** → **Specify Internet Gateway** → выбрать шлюз
- Route table: `Default Route Table for trading-bot`
- Security list: `Default Security List for trading-bot` (в нём по умолчанию открыт
  **только SSH** — это ровно то, что нужно)
- **Use DNS hostnames in this Subnet** — включить, DNS Label: `public-subnet`
- Prohibit public IP / Prohibit VCN egress — снять

### 2.5. Инстанс

**Compute → Instances → Create instance**:

| Параметр | Значение |
|---|---|
| Name | `trading-bot` |
| Image | **Canonical Ubuntu 24.04** |
| Shape series | **Ampere** (не AMD! E5.Flex — платная) |
| Shape | `VM.Standard.A1.Flex`, **2 OCPU / 12 GB** |
| Networking | **Select existing** VCN `trading-bot` + subnet `public-subnet` |
| Private IPv4 | Automatically assign |
| **Public IPv4** | **Automatically assign** ← иначе не подключитесь по SSH |
| SSH keys | **Paste public key** (см. ниже) |
| Boot volume | **Create new**, **100 GB**, **VPU 50** |
| Shielded / Confidential computing | не включать |

**Про VPU:** консоль спрашивает число, а не название. Соответствие:
`10` = Low Performance (медленно для Docker-сборки и SQLite), **`50` = Balanced**
(бесплатно в рамках 200 GB), `125` = High Performance (платно).

**Про SSH-ключ.** Свой ключ надёжнее, чем сгенерированный OCI (приватный от OCI
скачивается один раз). В PowerShell:

```powershell
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub | Set-Clipboard
```

Вставить в **Paste public key**. Ошибка «Invalid input» = кириллица или лишний
символ; ключ должен быть одной строкой (`ssh-ed25519 AAAA...`).

**Грабли, на которые напороли:**
- Поле «Operating system» не меняется, пока не отщёлкните до конца (иногда нужен
  жёсткий перезагруз страницы, Ctrl+Shift+R).
- «Create new subnet» в мастере инстанса даёт подсеть **без** Internet Gateway →
  публичный адрес не предлагается. Поэтому §2.1–2.4 отдельными страницами.
- В форме инстанса в поле SSH keys может остаться **текст команды** вместо ключа.
  Проверяйте, что строка начинается с `ssh-ed25519 AAAA`.

---

## 3. Reserved public IP (обязательно)

Ephemeral IP меняется при каждой остановке инстанса.

**Instance → Attached VNIC → Private IP → Add resilience (reserved) IP**, либо
`https://cloud.oracle.com/networking/reserved-ips` → **Create reserved IP**.

Адрес при этом остаётся прежним (`92.5.180.72`), просто перестаёт быть ephemeral.

---

## 4. Подготовка VM

```powershell
ssh -i $env:USERPROFILE\.ssh\id_ed25519 ubuntu@92.5.180.72
```

На VM:

```bash
sudo bash deploy/oci/bootstrap-ubuntu.sh   # из распакованного репозитория
```

Скрипт ставит Docker + compose-плагин, создаёт swap 2 ГБ, ограничивает рост
docker-логов (20 МБ × 5 на контейнер) и создаёт `/opt/trading-bot`.

> **Важно:** после установки Docker текущая SSH-сессия остаётся без группы
> `docker`, поэтому первое время `docker` работает только через `sudo`. Либо
> переподключитесь, либо `sudo usermod -aG docker ubuntu`.

---

## 5. Перенос кода и данных

### 5.1. Что передаёт git, а что — нет

| Файл | Правило `.gitignore` | Что потеряем |
|---|---|---|
| `.env`, `.env.live` | `.env`, `.env.*` | ключи Binance |
| `bot/state_*.json` | `state_*.json` (строка 17) | **открытые позиции**, SL/TP, `backstop_algo_id`, шаг reverse-цепочки |
| `data/*.db` | `*.db`, `data/` | историю сделок, `armed`, позиции в БД |
| `bot/logs/`, `logs/` | — | логи |

> Commit + push — это **только код**. Для переезда этого недостаточно.

### 5.2. Код

Если изменения закоммичены и запушены:

```bash
git clone https://github.com/osdal/replit_scalper.git /opt/trading-bot
```

Если нет (в рабочем дереве есть незакоммиченное) — копируем архивом с хоста:

```powershell
tar -czf repo.tar.gz --exclude=.git --exclude=node_modules --exclude=venv `
    --exclude=support-bot --exclude=.kilo --exclude=data --exclude=logs `
    --exclude=__pycache__ --exclude=dist -C C:\DATA\bots\replit_scalper .
scp -i $env:USERPROFILE\.ssh\id_ed25519 repo.tar.gz ubuntu@92.5.180.72:/tmp/
```
```bash
sudo mkdir -p /opt/trading-bot && sudo tar -xzf /tmp/repo.tar.gz -C /opt/trading-bot \
  && sudo chown -R ubuntu:ubuntu /opt/trading-bot
```

### 5.3. Секреты и БД

```powershell
scp -i $env:USERPROFILE\.ssh\id_ed25519 .env .env.live ubuntu@92.5.180.72:/opt/trading-bot/
scp -i $env:USERPROFILE\.ssh\id_ed25519 data/bot.db data/bot_live.db `
    ubuntu@92.5.180.72:/opt/trading-bot/data/
# открытые позиции — обязательно, если они есть
scp -i $env:USERPROFILE\.ssh\id_ed25519 bot/state_live_<символ>.json `
    ubuntu@92.5.180.72:/opt/trading-bot/bot/
```
```bash
cd /opt/trading-bot && chmod 600 .env .env.live
```

> **Никогда не запускайте один символ на двух машинах.** Один аккаунт, два
> процесса — позиция удвоится. Сначала останавливаем ботов на Windows, потом
> запускаем на Oracle.

---

## 6. Binance: IP-whitelist

Если на ключе включён whitelist IP — добавить **Reserved IP** из §3:
<https://www.binance.com> → My API Key → Edit → IP access.

Без этого бот получает `-2014` и не может торговать live. Если whitelist нет и
не планируется — проверить, что у ключа **нет** прав на вывод (withdrawal).

---

## 7. Запуск стеков

```bash
cd /opt/trading-bot
sudo docker compose build      # один раз, ~5-10 мин на ARM
./stack.sh                     # поднимает оба стека + Arm/Start live-ботов
```

Либо вручную:

```bash
sudo docker compose up -d
curl -s localhost:5001/api/bots | head -c 200
```

Проверка: `:5000` и `:5001` отдают JSON, `:5173` и `:5176` отдают HTML.

> **Грабли, уже исправленные в репозитории:**
> - Оба api-сервиса используют один образ `replit_scalper-api`. Если указать
>   `build:` у обоих, BuildKit падает `image ... already exists`. В `docker-compose.yml`
>   `build` есть только у testnet-сервисов.
> - `docker compose build` на свежей машине требует `sudo` (группа docker ещё не
>   применена в текущей сессии).
> - На ARM все Python-пакеты встают штатно (проверено): pandas 3.0.6, numpy 2.4.6,
>   binance 1.0.37, matplotlib 3.11.2, Pillow 12.3.0.

---

## 8. Автостарт и защита от остановки

### 8.1. systemd — стеки и live-боты после ребута

```bash
echo "LIVE_SYMBOLS=1000PEPEUSDT" | sudo tee /etc/default/trading-bot
sudo cp deploy/oci/trading-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable trading-bot.service
```

Юнит поднимает контейнеры (у всех `restart: unless-stopped`) и делает Arm+Start
live-ботов. Без него после ребута контейнеры поднимутся, а live-боты — нет:
в `.env.live` `AUTO_RESTART_BOTS=false` намеренно (реальные деньги не должны поехать
торговать без явного Start).

### 8.2. keepalive — против idle-reclamation

```bash
crontab -e
*/10 * * * * /opt/trading-bot/deploy/oci/keepalive.sh >> /var/log/keepalive.log 2>&1
```

---

## 9. Доступ к дашбордам (SSH-туннель)

Наружу открыт только порт 22. На Windows:

```powershell
ssh -i $env:USERPROFILE\.ssh\id_ed25519 `
    -L 5000:localhost:5000 -L 5001:localhost:5001 `
    -L 5173:localhost:5173 -L 5176:localhost:5176 `
    ubuntu@92.5.180.72
```

После этого в браузере: <http://localhost:5173> (testnet) и <http://localhost:5176> (live).

> Live-дашборд умеет закрывать все позиции по рынку — не выставляйте порт 5176
> на `0.0.0.0/0`. Если нужен доступ с телефона — Tailscale или Cloudflare Tunnel.

---

## 10. Передача открытой позиции

Порядок (иначе позиция останется без трекинга или удвоится):

1. Добавить Reserved IP в whitelist Binance (§3, §6).
2. Остановить live-ботов на Windows: `POST http://localhost:5001/api/bots/stop-all`.
   Биржевые SL/TP-ордера при этом остаются — позиция защищена.
3. Перекопировать **свежие** `data/bot_live.db` и `bot/state_live_<символ>.json`
   на Oracle (копия, сделанная заранее, протухает: SL/TP и шаг цепочки меняются).
4. На Oracle: `./stack.sh`.
5. Проверить, что в карточке бота та же позиция, те же SL/TP, и на бирже
   подхватился существующий backstop-ордер.

Проверить реальное состояние на бирже (без запуска ботов) можно готовым скриптом
`scripts/check_exchange_state.py` — он читает ключи из `.env.live` и печатает
открытые позиции и биржевые SL/TP-алгоритмы:

```powershell
docker cp scripts/check_exchange_state.py replit_scalper-api-live:/tmp/
docker exec -w /app/bot replit_scalper-api-live python3 /tmp/check_exchange_state.py
```

Ожидаемый вывод, если всё в порядке, — список открытых позиций и их защиты:

```
BINANCE_TESTNET=false
  INJUSDT        SHORT qty=1.4           entry=7.578 uPNL=0.037
  Биржевые ордера INJUSDT:
    algoId=3000002229585392 side=BUY qty=0.0 trigger=7.784 type=STOP_MARKET
```

---

## 11. Обновление кода на VM

```bash
cd /opt/trading-bot
git pull
sudo docker compose build
./stack.sh
```

> На VM репозиторий разложен архивом, а не склонирован (если изменения не были
> запушены). Чтобы `git pull` заработал, после первого пуша проще переклонить:
> `sudo rm -rf /opt/trading-bot && git clone ... /opt/trading-bot`, затем заново
> положить `.env`, `.env.live` и `data/` (они в `.gitignore`).

---

## 12. Откат

На Windows ничего не удаляйте, пока не убедитесь, что VM работает:

```powershell
cd C:\DATA\bots\replit_scalper
docker-compose up -d
```

---

## 13. Чек-лист

- [ ] VCN + Internet Gateway + route `0.0.0.0/0` + public subnet
- [ ] Инстанс A1 2/12, Ubuntu 24.04, boot volume 100 GB VPU 50
- [ ] **Reserved public IP** создан
- [ ] IP добавлен в whitelist ключа Binance
- [ ] `bootstrap-ubuntu.sh` выполнен, `docker compose version` работает
- [ ] Репозиторий в `/opt/trading-bot`, `.env`/`.env.live` с правами 600
- [ ] `data/bot.db`, `data/bot_live.db` перенесены
- [ ] `bot/state_live_*.json` перенесены **свежими** (если есть открытая позиция)
- [ ] Windows-боты остановлены **до** старта на Oracle
- [ ] `sudo docker compose ps` — 4 контейнера Up
- [ ] `:5001` отдаёт JSON, `:5176` отдаёт HTML
- [ ] SSH-туннель открывает оба дашборда
- [ ] `systemd` юнит включён, cron с keepalive настроен

---

## 14. Что делать не нужно

- **Не запускать live на Oracle** без Reserved IP и whitelist — не заработает.
- **Не запускать один символ на двух машинах** — позиция удвоится.
- **Не открывать 5176/5001 наружу** — live-контур с реальными деньгами.
- **Не хранить `.env.live` в git** — он в `.gitignore`, так и должно остаться.
- **Не ставить `AUTO_RESTART_BOTS=true` в `.env.live`** — сейчас там `false`
  намеренно.
- **Не переносить `bot/lock*`** — lock-файлы не нужны и могут помешать старту.
- **Не удалять `state-*.json` при переносе** — это состояние открытой позиции.
"""Публичная страница пользователя: /u/<slug>."""
import json
import os


def _render_keys_block(profile) -> str:
    """Форма ключей Binance на странице клиента."""
    cid = profile.get("client_id")
    if not cid:
        return ""
    key = profile.get("key") or {}
    mask = key.get("mask")
    current = (
        "<div class='current'>Текущий ключ: <code>" + str(mask) + "</code>"
        + (" · " + str(key.get("label")) if key.get("label") else "")
        + "</div>"
    ) if mask else "<div class='current'>Ключ не задан — бот использует общий ключ платформы.</div>"
    if not key.get("masterKeyReady", True):
        current += "<p class='msg err'>Шифрование не настроено на сервере — сохранение недоступно.</p>"
    return (
        "<div class='keys' id='keys-block'>\n"
        "  <h2>Ключи Binance для вашего бота</h2>\n"
        "  <p>Введите API key и secret вашего аккаунта Binance (права Futures, без вывода средств). "
        "Секрет шифруется на сервере и не показывается в открытом виде. "
        "Рекомендуется привязать ключ к IP сервера.</p>\n"
        "  " + current + "\n"
        "  <input type='hidden' id='key-client-id' value='" + str(cid) + "'>\n"
        "  <input type='text' id='key-api' placeholder='API key' autocomplete='off'>\n"
        "  <input type='password' id='key-secret' placeholder='API secret' autocomplete='new-password'>\n"
        "  <input type='text' id='key-label' placeholder='Название (необязательно)'>\n"
        "  <p id='key-msg' class='msg'></p>\n"
        "  <button type='button' id='key-save'>Сохранить ключ</button>\n"
        "  <button type='button' id='key-show' class='ghost' style='color:#2aabee;border-color:#bae6fd'>Показать сохранённый ключ</button>\n"
        "  <button type='button' id='key-delete' class='ghost'>Удалить ключ</button>\n"
        "</div>"
    )


def _render_positions_block(positions) -> str:
    if not positions:
        return ""
    rows = []
    for p in positions:
        legs = "".join(
            "<div class='leg'>"
            "<b>Нога " + str(l["leg_index"] + 1) + "</b> · "
            + str(l["direction"]) + " · вход " + str(l["entry_price"])
            + " · выход " + (str(l["exit_price"]) if l["exit_price"] is not None else "—")
            + " · qty " + str(l["qty"])
            + " · " + ("<span style='color:#0284c7'>открыта</span>" if l["is_open"]
                       else "PnL " + str(l["pnl"]) + " (" + str(l["exit_reason"] or "—") + ")")
            + "</div>"
            for l in p["legs"]
        )
        pnl = p.get("cycle_pnl") if p.get("cycle_pnl") is not None else p.get("pnl")
        rows.append(
            "<div class='pos'>"
            "<div class='pos-head' onclick='this.nextElementSibling.classList.toggle(\"open\")'>"
            "<span><b>" + str(p["symbol"]) + "</b> " + str(p["direction"])
            + (" <span style='color:#0284c7'>• открыта</span>" if p["is_open"] else "")
            + "</span><span>PnL: " + str(pnl) + " · ног: " + str(len(p["legs"]))
            + " · " + str(p["entry_time"])[:16] + "</span></div>"
            "<div class='pos-legs'>" + legs + "</div></div>"
        )
    return (
        "<div class='keys' id='positions-block'>\n"
        "  <h2>История позиций</h2>\n"
        "  <p>Нажмите на позицию, чтобы раскрыть все ноги цикла, включая ещё не закрытые.</p>\n"
        + "".join(rows) + "\n"
        "</div>"
    )


_KEYS_SCRIPT = """
<script>
(function () {
  var block = document.getElementById('keys-block');
  if (!block) return;
  var cid = document.getElementById('key-client-id').value;
  var msg = document.getElementById('key-msg');
  function out(text, err) {
    msg.textContent = text;
    msg.className = 'msg' + (err ? ' err' : '');
  }
  function tgId() {
    // telegram_id доступен из данных профиля (вставлены сервером ниже)
    return window.__OSDAL_TG_ID__ || 0;
  }
  document.getElementById('key-save').onclick = function () {
    var apiKey = document.getElementById('key-api').value.trim();
    var apiSecret = document.getElementById('key-secret').value.trim();
    if (!apiKey || !apiSecret) { out('Введите API key и secret', true); return; }
    fetch('/bot-api/registration/keys', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        client_id: Number(cid),
        telegram_id: tgId(),
        apiKey: apiKey,
        apiSecret: apiSecret,
        label: document.getElementById('key-label').value.trim(),
        ipWhitelisted: true
      })
    }).then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.d.error || 'Ошибка');
        out('Ключ сохранён: ' + res.d.mask);
        document.getElementById('key-api').value = '';
        document.getElementById('key-secret').value = '';
        setTimeout(function () { location.reload(); }, 800);
      })
      .catch(function (e) { out(e.message, true); });
  };
  document.getElementById('key-show').onclick = function () {
    fetch('/bot-api/registration/keys?slug=' + encodeURIComponent(window.__OSDAL_SLUG__ || '') + '&tg=' + tgId())
      .then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.d.error || 'Ошибка');
        var k = res.d.key;
        if (!k) { out('Ключ не задан', true); return; }
        out('API key: ' + (k.apiKey || '—') + ' | Secret: ' + (k.apiSecret || '—'));
      })
      .catch(function (e) { out(e.message, true); });
  };
  document.getElementById('key-delete').onclick = function () {
    if (!confirm('Удалить ключ Binance? Бот вернётся к общему ключу.')) return;
    fetch('/bot-api/registration/keys', {
      method: 'DELETE',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({client_id: Number(cid), telegram_id: tgId()})
    }).then(function (r) { return r.json().then(function (d) { return {ok: r.ok, d: d}; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.d.error || 'Ошибка');
        out('Ключ удалён');
        setTimeout(function () { location.reload(); }, 800);
      })
      .catch(function (e) { out(e.message, true); });
  };
})();
</script>
"""


def get_profile(slug: str):
    """Публичные данные пользователя по slug (username / tg<id> / id)."""
    import sqlite3
    slug = (slug or "").strip().strip("/")
    if not slug:
        return None
    DB_PATH = "/opt/trading-bot/data/bot_live.db"  # overridden below
    import os
    db_path = os.environ.get("REGISTRATION_DB", "/opt/trading-bot/data/bot_live.db")

    def connect():
        c = sqlite3.connect(db_path, timeout=15.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 15000")
        return c

    with connect() as conn:
        row = None
        row = conn.execute(
            """SELECT u.id, u.telegram_id, u.telegram_username, u.first_name, u.last_name, u.photo_url,
                      u.status, u.created_at, u.consent_messages,
                      COALESCE(a.balance_usd, 0) AS balance_usd,
                      COALESCE(a.high_water_mark_usd, 0) AS hwm_usd,
                      (SELECT COUNT(*) FROM bots b WHERE b.user_id = u.id) AS bots_count
               FROM users u LEFT JOIN user_internal_accounts a ON a.user_id = u.id
               WHERE u.telegram_username = ?""",
            (slug,),
        ).fetchone()
        if row is None and slug.lower().startswith("tg"):
            try:
                tid = int(slug[2:])
                row = conn.execute(
                    """SELECT u.id, u.telegram_id, u.telegram_username, u.first_name, u.last_name, u.photo_url,
                              u.status, u.created_at, u.consent_messages,
                              COALESCE(a.balance_usd, 0) AS balance_usd,
                              COALESCE(a.high_water_mark_usd, 0) AS hwm_usd,
                              (SELECT COUNT(*) FROM bots b WHERE b.user_id = u.id) AS bots_count
                       FROM users u LEFT JOIN user_internal_accounts a ON a.user_id = u.id
                       WHERE u.telegram_id = ?""",
                    (tid,),
                ).fetchone()
            except ValueError:
                pass
        if row is None:
            try:
                uid = int(slug)
                row = conn.execute(
                    """SELECT u.id, u.telegram_id, u.telegram_username, u.first_name, u.last_name, u.photo_url,
                              u.status, u.created_at, u.consent_messages,
                              COALESCE(a.balance_usd, 0) AS balance_usd,
                              COALESCE(a.high_water_mark_usd, 0) AS hwm_usd,
                              (SELECT COUNT(*) FROM bots b WHERE b.user_id = u.id) AS bots_count
                       FROM users u LEFT JOIN user_internal_accounts a ON a.user_id = u.id
                       WHERE u.id = ?""",
                    (uid,),
                ).fetchone()
            except ValueError:
                pass
    if row is None:
        return None
    name = " ".join(x for x in (row["first_name"], row["last_name"]) if x) or "Пользователь"
    return {
        "id": row["id"],
        "telegram_id": row["telegram_id"],
        "slug": (row["telegram_username"] or ("tg" + str(row["id"]))),
        "name": name,
        "username": row["telegram_username"],
        "photo_url": row["photo_url"],
        "status": row["status"],
        "balance_usd": round(float(row["balance_usd"] or 0), 4),
        "hwm_usd": round(float(row["hwm_usd"] or 0), 4),
        "bots_count": int(row["bots_count"] or 0),
        "consent_messages": bool(row["consent_messages"]),
        "created_at": row["created_at"],
        "profile_slug": (row["telegram_username"] or ("tg" + str(row["id"]))),
        "profile_url": "/u/" + (row["telegram_username"] or ("tg" + str(row["id"]))),
    }


def get_positions(client_id: int, limit: int = 50):
    """История позиций клиента с ногами (из trades + trade_legs)."""
    import sqlite3
    db_path = os.environ.get("REGISTRATION_DB", "/opt/trading-bot/data/bot_live.db")
    try:
        conn = sqlite3.connect(db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT t.* FROM trades t
               WHERE t.bot_id IN (SELECT id FROM bots WHERE client_id=?)
                 AND t.reject_reason IS NULL
               ORDER BY t.entry_time DESC LIMIT ?""",
            (client_id, limit),
        ).fetchall()
        out = []
        for r in rows:
            legs = conn.execute(
                "SELECT * FROM trade_legs WHERE trade_id=? ORDER BY leg_index, id", (r["id"],)
            ).fetchall()
            leg_list = []
            for l in legs:
                leg_list.append({
                    "id": l["id"], "leg_index": l["leg_index"], "direction": l["direction"],
                    "entry_price": round(float(l["entry_price"] or 0), 8),
                    "exit_price": None if l["exit_price"] is None else round(float(l["exit_price"]), 8),
                    "qty": round(float(l["qty"] or 0), 6),
                    "pnl": None if l["pnl"] is None else round(float(l["pnl"]), 4),
                    "is_open": bool(l["is_open"]), "exit_reason": l["exit_reason"],
                })
            if not leg_list:
                leg_list = [{
                    "id": 0, "leg_index": 0, "direction": r["direction"],
                    "entry_price": round(float(r["entry_price"] or 0), 8),
                    "exit_price": r["exit_price"], "qty": round(float(r["qty"] or 0), 6),
                    "pnl": r["pnl"], "is_open": bool(r["is_open"]), "exit_reason": r["exit_reason"],
                }]
            out.append({
                "id": r["id"], "symbol": r["symbol"], "direction": r["direction"],
                "entry_price": round(float(r["entry_price"] or 0), 8),
                "pnl": None if r["pnl"] is None else round(float(r["pnl"]), 4),
                "cycle_pnl": None if r["cycle_pnl"] is None else round(float(r["cycle_pnl"]), 4),
                "chain_depth": r["chain_depth"], "is_open": bool(r["is_open"]),
                "entry_time": r["entry_time"], "legs": leg_list,
            })
        return out
    except Exception as exc:
        print(f"[WARN] get_positions: {exc}", flush=True)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def render_profile_html(profile, slug):
    if profile is None:
        return ("<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Пользователь не найден — OSDAL Scalper</title>"
                "<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#eef3f8;"
                "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;color:#1f2937;}"
                ".card{background:#fff;border-radius:16px;padding:40px;text-align:center;"
                "box-shadow:0 10px 40px rgba(15,23,42,.08);max-width:420px;}"
                "h1{font-size:22px;margin:0 0 8px;}p{color:#64748b;margin:0 0 20px;}"
                "a{color:#2aabee;text-decoration:none;font-weight:600;}</style></head>"
                "<body><div class='card'><h1>Пользователь не найден</h1>"
                "<p>Страница <b>/" + slug + "</b> не существует.</p>"
                "<a href='/scalper-register/'>Регистрация</a></div></body></html>")
    name = profile["name"]
    username = ("@" + profile["username"]) if profile["username"] else ""
    photo = ('<img class="avatar" src="' + profile["photo_url"] + '" alt="">'
             if profile["photo_url"] else
             '<div class="avatar ph">' + name[:2].upper() + '</div>')
    status = "активен" if profile["status"] == "active" else profile["status"]
    consent = "подписан" if profile["consent_messages"] else "отписан"
    created = (profile["created_at"] or "")[:10]
    return (
        "<!DOCTYPE html>\n<html lang='ru'>\n<head>\n"
        "<meta charset='utf-8'>\n"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>\n"
        "<title>" + name + " — OSDAL Scalper</title>\n"
        "<style>\n"
        "  html,body{margin:0;padding:0;min-height:100vh;background:#eef3f8;\n"
        "    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1f2937;}\n"
        "  .wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;}\n"
        "  .card{background:#fff;border:1px solid #e2e8f0;border-radius:16px;padding:32px 28px;\n"
        "    box-shadow:0 10px 40px rgba(15,23,42,.08);max-width:460px;width:100%;text-align:center;}\n"
        "  .avatar{width:88px;height:88px;border-radius:50%;object-fit:cover;border:3px solid #e2e8f0;margin:0 auto 14px;display:block;}\n"
        "  .avatar.ph{background:#e2e8f0;color:#64748b;display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:700;}\n"
        "  h1{font-size:22px;margin:0 0 4px;color:#1f2937;}\n"
        "  .uname{color:#64748b;font-size:15px;margin:0 0 18px;}\n"
        "  .badge{display:inline-block;font-size:12px;padding:4px 10px;border-radius:999px;\n"
        "    background:rgba(42,171,238,.1);border:1px solid rgba(42,171,238,.3);color:#2aabee;margin-bottom:18px;}\n"
        "  .stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:18px 0;text-align:left;}\n"
        "  .stat{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:12px 14px;}\n"
        "  .stat .lbl{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em;}\n"
        "  .stat .val{font-size:18px;font-weight:700;color:#1f2937;margin-top:4px;}\n"
        "  .foot{margin-top:18px;font-size:12px;color:#94a3b8;}\n"
        "  a{color:#2aabee;text-decoration:none;font-weight:600;}\n"
        "  .keys{margin-top:20px;padding-top:18px;border-top:1px solid #e2e8f0;text-align:left;}\n"
        "  .keys h2{font-size:15px;margin:0 0 8px;color:#1f2937;}\n"
        "  .keys p{font-size:12px;color:#64748b;line-height:1.5;margin:0 0 12px;}\n"
        "  .keys input{width:100%;box-sizing:border-box;margin-bottom:8px;padding:10px 12px;\n"
        "    border:1px solid #cbd5e1;border-radius:10px;font-size:14px;background:#fff;}\n"
        "  .keys input:focus{outline:none;border-color:#2aabee;}\n"
        "  .keys button{width:100%;padding:11px 14px;border:0;border-radius:10px;background:#2aabee;\n"
        "    color:#04121c;font-weight:700;font-size:14px;cursor:pointer;}\n"
        "  .keys button.ghost{background:transparent;color:#dc2626;border:1px solid #fecaca;margin-top:8px;}\n"
        "  .keys .msg{font-size:12px;margin:8px 0 0;color:#059669;}\n"
        "  .keys .msg.err{color:#dc2626;}\n"
        "  .keys .current{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:10px 12px;\n"
        "    font-size:13px;margin-bottom:10px;}\n"
        "  .keys .current code{font-size:12px;color:#1f2937;}\n"
        "  .pos{border:1px solid #e2e8f0;border-radius:10px;margin-bottom:8px;overflow:hidden;}\n"
        "  .pos-head{display:flex;justify-content:space-between;gap:8px;padding:10px 12px;background:#f8fafc;\n"
        "    cursor:pointer;font-size:12px;color:#1f2937;}\n"
        "  .pos-head:hover{background:#eef6fd;}\n"
        "  .pos-legs{display:none;padding:6px 12px;background:#fff;}\n"
        "  .pos-legs.open{display:block;}\n"
        "  .leg{font-size:11px;color:#475569;padding:6px 0;border-bottom:1px dashed #e2e8f0;}\n"
        "</style>\n</head>\n<body>\n"
        "<div class='wrap'>\n  <div class='card'>\n"
        "    " + photo + "\n"
        "    <h1>" + name + "</h1>\n"
        "    <p class='uname'>" + username + "</p>\n"
        "    <span class='badge'>&#10003; " + status + "</span>\n"
        "    <div class='stats'>\n"
        "      <div class='stat'><div class='lbl'>Баланс</div><div class='val'>$" + str(profile["balance_usd"]) + "</div></div>\n"
        "      <div class='stat'><div class='lbl'>Ботов</div><div class='val'>" + str(profile["bots_count"]) + "</div></div>\n"
        "      <div class='stat'><div class='lbl'>HWM</div><div class='val'>$" + str(profile["hwm_usd"]) + "</div></div>\n"
        "      <div class='stat'><div class='lbl'>Уведомления</div><div class='val' style='font-size:14px;'>" + consent + "</div></div>\n"
        "    </div>\n"
        "    <p class='foot'>Зарегистрирован: " + created + "</p>\n"
        "    " + _render_keys_block(profile) + "\n"
        "    " + _render_positions_block(profile.get("positions") or []) + "\n"
        "    <p class='foot'><a href='https://osdal.duckdns.org/scalper-register/'>Регистрация</a></p>\n"
        "  </div>\n</div>\n"
        "<script>window.__OSDAL_TG_ID__=" + str(profile.get("telegram_id") or 0)
        + ";window.__OSDAL_SLUG__=" + json.dumps(str(profile.get("slug") or "")) + ";</script>\n"
        + _KEYS_SCRIPT +
        "</body>\n</html>"
    )

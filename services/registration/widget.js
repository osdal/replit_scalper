/*!
 * OSDAL Scalper — страница регистрации через Telegram.
 * Отдаётся сервисом регистрации: /bot-api/registration/widget.js
 * Встраивается в WordPress-страницу /scalper-register/
 */
(function () {
  "use strict";

  var API = "/bot-api/registration";
  var root = document.getElementById("osd-register-root");
  if (!root) return;
  if (root.getAttribute("data-osd-ready") === "1") return;
  root.setAttribute("data-osd-ready", "1");

  var cfg = { ready: false, bot_username: "", bot_name: "", platform: "OSDAL Scalper" };

  // ───────────────────────── стили ─────────────────────────
  var CSS = ""
    + "#osd-register-root{--osd-bg:#0f1115;--osd-card:#171a21;--osd-line:#262b36;--osd-text:#e8ebf0;--osd-muted:#9aa3b2;--osd-accent:#2aabee;--osd-ok:#22c55e;--osd-err:#ef4444;"
    + "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:var(--osd-text);max-width:620px;margin:0 auto;}"
    + "#osd-register-root *{box-sizing:border-box;}"
    + "#osd-register-root .osd-card{background:var(--osd-card);border:1px solid var(--osd-line);border-radius:16px;padding:28px;box-shadow:0 18px 40px rgba(0,0,0,.35);}"
    + "#osd-register-root .osd-badge{display:inline-block;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--osd-accent);border:1px solid rgba(42,171,238,.35);background:rgba(42,171,238,.08);padding:5px 10px;border-radius:999px;margin-bottom:14px;}"
    + "#osd-register-root h1{font-size:26px;line-height:1.25;margin:0 0 10px;color:var(--osd-text);font-weight:700;}"
    + "#osd-register-root .osd-sub{color:var(--osd-muted);font-size:15px;line-height:1.55;margin:0 0 20px;}"
    + "#osd-register-root .osd-list{list-style:none;padding:0;margin:0 0 24px;}"
    + "#osd-register-root .osd-list li{position:relative;padding-left:26px;margin:9px 0;color:var(--osd-muted);font-size:14px;line-height:1.5;}"
    + "#osd-register-root .osd-list li:before{content:'';position:absolute;left:6px;top:7px;width:8px;height:8px;border-radius:50%;background:var(--osd-accent);opacity:.85;}"
    + "#osd-register-root .osd-tg-wrap{display:flex;flex-direction:column;align-items:flex-start;gap:10px;padding:18px;border:1px dashed var(--osd-line);border-radius:12px;background:rgba(255,255,255,.015);}"
    + "#osd-register-root .osd-hint{font-size:12.5px;color:var(--osd-muted);line-height:1.5;margin:0;}"
    + "#osd-register-root .osd-divider{height:1px;background:var(--osd-line);margin:22px 0;}"
    + "#osd-register-root .osd-spinner{width:18px;height:18px;border:2px solid rgba(255,255,255,.18);border-top-color:var(--osd-accent);border-radius:50%;display:inline-block;animation:osd-spin .8s linear infinite;vertical-align:-3px;margin-right:8px;}"
    + "@keyframes osd-spin{to{transform:rotate(360deg);}}"
    + "#osd-register-root .osd-loading{color:var(--osd-muted);font-size:14px;}"
    + "#osd-register-root .osd-alert{border-radius:12px;padding:14px 16px;font-size:14px;line-height:1.5;margin:0 0 16px;}"
    + "#osd-register-root .osd-alert.err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.35);color:#fca5a5;}"
    + "#osd-register-root .osd-alert.warn{background:rgba(234,179,8,.1);border:1px solid rgba(234,179,8,.35);color:#fde68a;}"
    + "#osd-register-root .osd-alert.ok{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.35);color:#86efac;}"
    + "#osd-register-root .osd-profile{display:flex;align-items:center;gap:14px;margin-bottom:18px;}"
    + "#osd-register-root .osd-avatar{width:56px;height:56px;border-radius:50%;object-fit:cover;border:2px solid var(--osd-line);background:#0b0d11;}"
    + "#osd-register-root .osd-name{font-size:17px;font-weight:600;line-height:1.3;}"
    + "#osd-register-root .osd-uname{font-size:14px;color:var(--osd-muted);}"
    + "#osd-register-root .osd-status{display:inline-flex;align-items:center;gap:6px;font-size:13px;padding:5px 10px;border-radius:999px;background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.4);color:#86efac;}"
    + "#osd-register-root .osd-btn{display:inline-flex;align-items:center;gap:8px;background:var(--osd-accent);color:#04121c;border:0;border-radius:10px;padding:11px 18px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;}"
    + "#osd-register-root .osd-btn:hover{filter:brightness(1.07);color:#04121c;}"
    + "#osd-register-root .osd-btn.ghost{background:transparent;color:var(--osd-text);border:1px solid var(--osd-line);}"
    + "#osd-register-root .osd-foot{color:var(--osd-muted);font-size:12px;line-height:1.55;margin:14px 0 0;}"
    + "#osd-register-root .osd-hide{display:none !important;}";

  function injectCSS() {
    if (document.getElementById("osd-register-css")) return;
    var st = document.createElement("style");
    st.id = "osd-register-css";
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  // ───────────────────────── разметка ─────────────────────────
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function shell(inner) {
    root.innerHTML =
      '<div class="osd-card">' +
      '  <span class="osd-badge">' + esc(cfg.platform) + "</span>" +
      "  <h1>Регистрация доступа к торговому боту</h1>" +
      '  <p class="osd-sub">Аккаунт создаётся через Telegram — без паролей и подтверждений по почте. ' +
      "Достаточно нажать кнопку и подтвердить вход в Telegram.</p>" +
      inner +
      '  <p class="osd-foot">Мы получаем только ваше имя, @username и фото профиля из Telegram. ' +
      "Пароль и доступ к переписке не передаются.</p>" +
      "</div>";
  }

  function showLoading() {
    shell('<div class="osd-loading"><span class="osd-spinner"></span>Проверяем настройки регистрации…</div>');
  }

  function showNotReady() {
    shell(
      '<div class="osd-alert warn">Регистрация пока не открыта: Telegram-бот ещё не подключён. ' +
      "Загляните позже.</div>"
    );
  }

  function showForm() {
    shell(
      '<ul class="osd-list">' +
      "  <li>Доступ к торговым сигналам и статистике</li>" +
      "  <li>Личный кабинет с историей сделок и балансом</li>" +
      "  <li>Уведомления о входе в сделку и закрытии по TP</li>" +
      "</ul>" +
      '<div class="osd-tg-wrap">' +
      '  <div id="osd-tg-slot"></div>' +
      '  <p class="osd-hint" id="osd-tg-hint">Нажмите «Войти через Telegram» и подтвердите вход в открывшемся окне.</p>' +
      "</div>" +
      '<div class="osd-divider"></div>' +
      '<div id="osd-tg-fallback" class="osd-hide">' +
      '  <p class="osd-hint">Кнопка не появилась? Значит домен ещё не привязан к боту. ' +
      "Напишите нам в Telegram — зарегистрируем вручную.</p>" +
      "</div>"
    );
    mountWidget();
  }

  function showError(msg) {
    shell(
      '<div class="osd-alert err">' + esc(msg) + "</div>" +
      '<button type="button" class="osd-btn ghost" id="osd-retry">Попробовать снова</button>'
    );
    var btn = document.getElementById("osd-retry");
    if (btn) btn.addEventListener("click", showForm);
  }

  function showSuccess(user, already) {
    var name = [user.first_name, user.last_name].filter(Boolean).join(" ") || "Пользователь Telegram";
    var uname = user.telegram_username ? "@" + user.telegram_username : "без username";
    var avatar = user.photo_url
      ? '<img class="osd-avatar" src="' + esc(user.photo_url) + '" alt="">'
      : '<div class="osd-avatar"></div>';
    var statusText = user.status === "blocked" ? "Доступ заблокирован" : "Аккаунт активен";

    shell(
      '<div class="osd-alert ok">' +
      (already ? "Вы уже зарегистрированы — вход выполнен." : "Готово! Регистрация завершена.") +
      "</div>" +
      '<div class="osd-profile">' + avatar +
      "  <div>" +
      '    <div class="osd-name">' + esc(name) + "</div>" +
      '    <div class="osd-uname">' + esc(uname) + "</div>" +
      "  </div>" +
      "</div>" +
      '<p class="osd-status">✓ ' + esc(statusText) + "</p>" +
      '<div class="osd-divider"></div>' +
      '<p class="osd-hint" style="margin-bottom:12px">Чтобы получать уведомления о сделках, откройте бота и нажмите <b>Start</b>.</p>' +
      (cfg.bot_username
        ? '<a class="osd-btn" href="https://t.me/' + esc(cfg.bot_username) + '" target="_blank" rel="noopener">Открыть бота в Telegram</a>'
        : "")
    );
  }

  function mountWidget() {
    var slot = document.getElementById("osd-tg-slot");
    if (!slot) return;
    if (!cfg.bot_username) {
      showNotReady();
      return;
    }
    slot.innerHTML = "";
    var s = document.createElement("script");
    s.async = true;
    s.src = "https://telegram.org/js/telegram-widget.js?22";
    s.setAttribute("data-telegram-login", cfg.bot_username);
    s.setAttribute("data-size", "large");
    s.setAttribute("data-radius", "10");
    s.setAttribute("data-userpic", "true");
    s.setAttribute("data-request-access", "write");
    s.setAttribute("data-lang", "ru");
    s.setAttribute("data-onauth", "osdOnTelegramAuth(user)");
    slot.appendChild(s);

    // если виджет не отрисовался — показываем подсказку про /setdomain
    setTimeout(function () {
      var iframe = slot.querySelector("iframe");
      var fallback = document.getElementById("osd-tg-fallback");
      if (!iframe && fallback) fallback.classList.remove("osd-hide");
    }, 6000);
  }

  // ───────────────────── колбэк Telegram ─────────────────────
  window.osdOnTelegramAuth = function (user) {
    if (!user) return;
    var payload = {
      id: user.id,
      first_name: user.first_name,
      last_name: user.last_name,
      username: user.username,
      photo_url: user.photo_url,
      auth_date: user.auth_date,
      hash: user.hash
    };
    var slot = document.getElementById("osd-tg-slot");
    if (slot) slot.innerHTML = '<div class="osd-loading"><span class="osd-spinner"></span>Проверяем подпись Telegram…</div>';

    fetch(API + "/telegram", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().then(function (d) { return { ok: r.ok, data: d }; });
      })
      .then(function (res) {
        if (!res.ok) throw new Error(res.data && res.data.error ? res.data.error : "Не удалось завершить регистрацию");
        showSuccess(res.data.user, res.data.already_registered);
      })
      .catch(function (e) {
        showError(e && e.message ? e.message : "Ошибка сети. Попробуйте ещё раз.");
      });
  };

  // ───────────────────────── старт ─────────────────────────
  injectCSS();
  showLoading();

  fetch(API + "/config")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      cfg = data || cfg;
      if (!cfg.ready) return showNotReady();
      showForm();
    })
    .catch(function () {
      showError("Сервис регистрации недоступен. Попробуйте позже.");
    });
})();

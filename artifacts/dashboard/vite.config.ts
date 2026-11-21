import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig(({ mode }) => {
  // Адрес API для прокси. Раньше он был зашит как http://localhost:5000, а
  // клиент при этом ходил напрямую на VITE_API_URL (http://localhost:5001),
  // минуя прокси. Из-за этого дашборд, открытый через SSH-туннель на
  // нестандартном порту, показывал данные ЧУЖОГО стека (тот, что слушал
  // этот порт локально). Теперь клиент ходит на /api (относительный URL) →
  // запрос всегда уходит в свой же vite-сервер и проксируется в нужный API,
  // независимо от того, на каком порту открыт дашборд.
  const env = loadEnv(mode, __dirname, "");
  // Дефолт по mode: live-дашборд ходит на :5001, остальные — на :5000.
  // В Docker адрес переопределяется через DASHBOARD_API_TARGET (внутри
  // контейнера localhost — это сам дашборд, нужен адрес API-сервиса).
  const apiTarget =
    process.env.DASHBOARD_API_TARGET ||
    env.DASHBOARD_API_TARGET ||
    (mode === "live" ? "http://localhost:5001" : "http://localhost:5000");

  // Сервис регистрации пользователей через Telegram (см. services/registration).
  // Снаружи он доступен как /bot-api/* через Caddy; этот прокси нужен только
  // при прямом доступе к vite-серверу (без домена).
  const regTarget =
    process.env.DASHBOARD_REG_TARGET || env.DASHBOARD_REG_TARGET || "http://127.0.0.1:5100";

  // Админ-токен для изменения баланса держим на стороне vite-сервера:
  // браузер не должен видеть секрет в JS. Заголовок добавляется только на
  // money-роуты, остальные /api-запросы не трогаются.
  const adminToken =
    process.env.INTERNAL_ACCOUNT_ADMIN_TOKEN || env.INTERNAL_ACCOUNT_ADMIN_TOKEN || "";

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { "@": path.resolve(__dirname, "src") },
    },
    server: {
      port: 5173,
      // Доступ через внешний домен (Caddy → 127.0.0.1:517x). Без этого Vite
      // отвечает 403 "Blocked request" на любой Host, кроме localhost.
      allowedHosts: [".duckdns.org", ".sslip.io"],
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
          configure: (proxy) => {
            if (!adminToken) return;
            proxy.on("proxyReq", (proxyReq, req) => {
              if (req.url?.startsWith("/api/internal-account/topup")
                || req.url?.startsWith("/api/internal-account/clients/")) {
                proxyReq.setHeader("x-admin-token", adminToken);
              }
            });
          },
        },
        "/bot-api": {
          target: regTarget,
          changeOrigin: true,
          secure: false,
        },
      },
    },
  };
});

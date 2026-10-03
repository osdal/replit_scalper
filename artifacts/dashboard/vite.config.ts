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

  return {
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: { "@": path.resolve(__dirname, "src") },
    },
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
        },
      },
    },
  };
});

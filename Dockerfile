FROM node:20-slim AS base
# procps нужен и api-серверу (перечисление PID ботов через `ps`), и боту
# (проверка «жив ли процесс, держущий lock-файл»).
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip sqlite3 procps && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python3 /usr/bin/python
WORKDIR /app

# Полная установка workspace-зависимостей ВНУТРИ образа (Linux-бинарники).
# .dockerignore исключает node_modules, поэтому в образ попадает чистое дерево
# исходников. Благодаря этому контейнеры НЕ трогают node_modules на хосте:
# монтируем только bot/, data/, logs/ и .env* — без node_modules.
FROM base AS deps
COPY . .
# store-dir на cache-mount: пересборки не качают пакеты заново.
RUN --mount=type=cache,id=pnpm-store,target=/pnpm/store \
    npm install -g pnpm && pnpm install --frozen-lockfile --store-dir=/pnpm/store

# dev-образ: образ deps + Python-зависимости ботов. Они нужны api-серверу,
# потому что он сам спавнит ботов (кнопка Start в дашборде) для обоих
# окружений — testnet и live.
FROM deps AS dev
RUN pip install --break-system-packages --no-cache-dir -r bot/requirements.txt
# testnet API/dashboard + live API/dashboard
EXPOSE 5000 5001 5173 5176

# Production builders
FROM base AS api-builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm install -g pnpm && pnpm --filter @workspace/api-server run build

FROM base AS dashboard-builder
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm install -g pnpm && pnpm --filter @workspace/dashboard run build

# Runtime
FROM base AS runtime
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY --from=api-builder /app/artifacts/api-server/dist ./artifacts/api-server/dist
COPY --from=dashboard-builder /app/artifacts/dashboard/dist ./artifacts/dashboard/dist
COPY . .
# testnet API/dashboard + live API/dashboard
EXPOSE 5000 5001 5173 5176

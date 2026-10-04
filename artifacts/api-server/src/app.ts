import express, { type Express } from "express";
import cors from "cors";
import pinoHttp from "pino-http";
import router from "./routes";
import { logger } from "./lib/logger";
import { isAllowedOrigin } from "./middlewares/notifyAuth";

const app: Express = express();

app.use(
  pinoHttp({
    logger,
    // По умолчанию pino-http пишет только «request completed» без метода и URL
    // (quietReqLogger=true), из-за чего по логу невозможно понять, кто инициировал
    // торговую операцию — например, закрыл ли позицию kill-switch, рестарт или
    // внешний скрипт. Включаем req и добавляем ip/user-agent: по ним запрос
    // атрибутируется однозначно.
    quietReqLogger: false,
    serializers: {
      req(req) {
        return {
          id: req.id,
          method: req.method,
          url: req.url?.split("?")[0],
          ip: req.remoteAddress,
          ua: req.headers?.["user-agent"],
        };
      },
      res(res) {
        return {
          statusCode: res.statusCode,
        };
      },
    },
  }),
);
app.use(
  cors({
    origin(origin, cb) {
      cb(null, isAllowedOrigin(origin));
    },
    methods: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allowedHeaders: ["Content-Type", "x-notify-token"],
  }),
);
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

app.use("/api", router);

export default app;

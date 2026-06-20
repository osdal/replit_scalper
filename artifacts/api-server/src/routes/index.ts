import { Router } from "express";
import botsRouter      from "./bots";
import tradesRouter    from "./trades";
import backtestRouter  from "./backtest";
import optimizerRouter  from "./optimizer";
import binanceSyncRouter from "./binance-sync";
import recoveryRouter     from "./recovery";

const router = Router();

router.get("/healthz", (_req, res) => res.json({ status: "ok" }));
router.use("/bots",      botsRouter);
router.use("/trades",    tradesRouter);
router.use("/backtest",  backtestRouter);
router.use("/optimizer",     optimizerRouter);
router.use("/binance-sync", binanceSyncRouter);
router.use("/recovery",     recoveryRouter);

export default router;

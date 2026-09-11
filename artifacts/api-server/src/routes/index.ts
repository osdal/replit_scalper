import { Router } from "express";
import botsRouter      from "./bots";
import tradesRouter    from "./trades";
import backtestRouter  from "./backtest";
import optimizerRouter  from "./optimizer";
import binanceSyncRouter from "./binance-sync";
import recoveryRouter     from "./recovery";
import refreshRouter      from "./refresh";
import tradingRouter      from "./trading";
import pairsRouter        from "./pairs";
import historyRouter      from "./history";
import tickerRouter       from "./ticker";
import adxRouter          from "./adx";

const router = Router();

router.get("/healthz", (_req, res) => res.json({ status: "ok" }));
router.use("/bots",      botsRouter);
router.use("/trades",    tradesRouter);
router.use("/backtest",  backtestRouter);
router.use("/optimizer",     optimizerRouter);
router.use("/binance-sync", binanceSyncRouter);
router.use("/recovery",     recoveryRouter);
router.use("/refresh",      refreshRouter);
router.use("/trading",    tradingRouter);
router.use("/pairs",      pairsRouter);
router.use("/history",    historyRouter);
router.use("/ticker",     tickerRouter);
router.use("/adx",        adxRouter);

export default router;

import { Router } from "express";
import botsRouter      from "./bots";
import tradesRouter    from "./trades";
import backtestRouter  from "./backtest";
import optimizerRouter from "./optimizer";

const router = Router();

router.get("/healthz", (_req, res) => res.json({ status: "ok" }));
router.use("/bots",      botsRouter);
router.use("/trades",    tradesRouter);
router.use("/backtest",  backtestRouter);
router.use("/optimizer", optimizerRouter);

export default router;

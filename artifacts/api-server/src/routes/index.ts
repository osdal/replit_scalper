import { Router } from "express";
import healthRouter   from "./health";
import botsRouter     from "./bots";
import tradesRouter   from "./trades";
import backtestRouter from "./backtest";

const router = Router();

router.use("/health",   healthRouter);
router.use("/bots",     botsRouter);
router.use("/trades",   tradesRouter);
router.use("/backtest", backtestRouter);

export default router;

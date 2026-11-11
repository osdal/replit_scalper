const fs = require('fs');
const path = 'artifacts/api-server/src/routes/trading.ts';
let content = fs.readFileSync(path, 'utf8');
const endpoint = `
/**
 * POST /api/trading/global-limits
 * Обновляет глобальные лимиты позиций (max_positions, max_positions_with_expired).
 * Сохраняет в файл data/global_limits_<env>.json — применяется сразу без рестарта.
 * body: { max_positions: number, max_positions_with_expired: number }
 */
router.post('/global-limits', async (req, res) => {
  try {
    const max_positions = Number(req.body?.max_positions);
    const max_positions_with_expired = Number(req.body?.max_positions_with_expired);
    if (!Number.isFinite(max_positions) || max_positions < 0 || max_positions > 50) {
      return res.status(400).json({ error: 'max_positions: 0-50' });
    }
    if (!Number.isFinite(max_positions_with_expired) || max_positions_with_expired < 0 || max_positions_with_expired > 50) {
      return res.status(400).json({ error: 'max_positions_with_expired: 0-50' });
    }
    const limits = { max_positions: Math.floor(max_positions), max_positions_with_expired: Math.floor(max_positions_with_expired) };
    writeGlobalLimits(limits);
    res.json({ success: true, ...limits });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

export default router;
`;
content = content.replace(/export default router;/, endpoint);
fs.writeFileSync(path, content);
console.log('Done');
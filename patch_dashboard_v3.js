const fs = require('fs');
const filePath = 'artifacts/dashboard/src/Dashboard.tsx';
let c = fs.readFileSync(filePath, 'utf8');

// 1. Update import
c = c.replace(
  'import { fetchBots, fetchTrades, fetchStats, startBot, stopBot, syncBinance, runBacktest } from "./hooks/useApi";',
  'import { fetchBots, fetchTrades, fetchStats, startBot, stopBot, syncBinance, runBacktest, updateConfig } from "./hooks/useApi";'
);

// 2. Add state after btResetKey
c = c.replace(
  'const [btResetKey, setBtResetKey] = useState(0);',
  'const [btResetKey, setBtResetKey] = useState(0);\n  const [showApplyDialog, setShowApplyDialog] = useState(false);\n  const [applying, setApplying] = useState(false);'
);

// 3. Add handleApplyConfig before handleRunBacktest
const handleRunBacktestIdx = c.indexOf('  const handleRunBacktest');
const handlerCode = `  const handleApplyConfig = async (shouldStopBot: boolean) => {
    if (!btConfig) return;
    setApplying(true);
    try {
      if (shouldStopBot) {
        await stopBot(btSymbol.toUpperCase());
      }
      await updateConfig(btSymbol.toUpperCase(), btConfig as unknown as Record<string, unknown>);
      setShowApplyDialog(false);
      alert("Конфиг обновлён" + (shouldStopBot ? " (бот остановлен)" : ""));
      await load();
    } catch (e) {
      alert("Ошибка обновления конфига");
    } finally {
      setApplying(false);
    }
  };

`;
c = c.slice(0, handleRunBacktestIdx) + handlerCode + c.slice(handleRunBacktestIdx);

// 4. Add button after btResult block (before </div> closing the backtest section)
// Find the btResult closing: </> followed by </div> followed by </TabsContent>
const btResultBlockEnd = c.indexOf('                }\n              </div>\n            </TabsContent>');
if (btResultBlockEnd === -1) {
  console.log('ERROR: Could not find btResult block end');
  process.exit(1);
}
const buttonCode = `                )}

                {btResult && (
                  <div className="mt-4">
                    <Button
                      onClick={() => setShowApplyDialog(true)}
                      disabled={applying}
                      className="flex items-center gap-2 bg-green-600 hover:bg-green-700 text-white"
                    >
                      {applying ? <><RefreshCw className="w-4 h-4 mr-2 animate-spin" />Применение...</> : "Применить в конфиг"}
                    </Button>
                    <p className="text-xs text-zinc-500 mt-2">
                      Обновит параметры {btSymbol.toUpperCase()} в config_{btSymbol.toLowerCase()}.yaml
                    </p>
                  </div>
                )}
              </div>
            </TabsContent>`;
c = c.slice(0, btResultBlockEnd) + buttonCode + c.slice(btResultBlockEnd + '                }\n              </div>\n            </TabsContent>'.length);

// 5. Add modal dialog before the final </div>
const finalDivIdx = c.lastIndexOf('    </div>\n  );\n}');
if (finalDivIdx === -1) {
  console.log('ERROR: Could not find final </div>');
  process.exit(1);
}
const modalCode = `      {showApplyDialog && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <Card className="bg-zinc-900 border border-zinc-700 p-6 max-w-md w-full mx-4">
            <CardHeader><CardTitle className="text-white">Применить конфиг?</CardTitle></CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-zinc-400">
                Обновить параметры <span className="text-white font-semibold">{btSymbol.toUpperCase()}</span> в config_{btSymbol.toLowerCase()}.yaml?
              </p>
              <div className="text-xs text-zinc-500 space-y-1">
                <p>Будут обновлены:</p>
                <ul className="list-disc list-inside pl-2">
                  <li>EMA Fast: {btConfig.ema_fast}</li>
                  <li>EMA Slow: {btConfig.ema_slow}</li>
                  <li>SL: {btConfig.sl_pct}%</li>
                  <li>TP1: {btConfig.tp1_pct}%</li>
                  <li>TP2: {btConfig.tp2_pct}%</li>
                  <li>Volume Multiplier: {btConfig.volume_multiplier}</li>
                  <li>TP1 Close: {btConfig.tp1_close_pct}%</li>
                </ul>
              </div>
              <div className="flex gap-3 pt-2">
                <Button onClick={() => handleApplyConfig(true)} disabled={applying}
                  className="flex-1 bg-red-600 hover:bg-red-700 text-white">
                  {applying ? "..." : "Да, остановить бота"}
                </Button>
                <Button onClick={() => handleApplyConfig(false)} disabled={applying}
                  className="flex-1 bg-green-600 hover:bg-green-700 text-white">
                  {applying ? "..." : "Только конфиг"}
                </Button>
                <Button onClick={() => setShowApplyDialog(false)} variant="outline"
                  className="flex-1 border-zinc-700 text-zinc-300">
                  Отмена
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      )}

    </div>
  );
}`;
c = c.slice(0, finalDivIdx) + modalCode + c.slice(finalDivIdx + '    </div>\n  );\n}'.length);

fs.writeFileSync(filePath, c);
console.log('Done!');
console.log('handleApplyConfig:', c.includes('handleApplyConfig'));
console.log('Применить в конфиг:', c.includes('Применить в конфиг'));
console.log('Только конфиг:', c.includes('Только конфиг'));
console.log('showApplyDialog:', c.includes('showApplyDialog'));

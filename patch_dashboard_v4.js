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
  'const [btResetKey, setBtResetKey] = useState(0);\r\n  const [showApplyDialog, setShowApplyDialog] = useState(false);\r\n  const [applying, setApplying] = useState(false);'
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

// 4. Add button after btResult block
// Find: </> followed by )} followed by </div> followed by </TabsContent>
const btResultEndMarker = '</>\r\n                )}\r\n              </div>\r\n            </TabsContent>';
const btResultEndIdx = c.indexOf(btResultEndMarker);
if (btResultEndIdx === -1) {
  console.log('ERROR: Could not find btResult block end');
  process.exit(1);
}
const buttonCode = `</>\r\n                )}\r\n\r\n                {btResult && (\r\n                  <div className="mt-4">\r\n                    <Button\r\n                      onClick={() => setShowApplyDialog(true)}\r\n                      disabled={applying}\r\n                      className="flex items-center gap-2 bg-green-600 hover:bg-green-700 text-white"\r\n                    >\r\n                      {applying ? <><RefreshCw className="w-4 h-4 mr-2 animate-spin" />Применение...</> : "Применить в конфиг"}\r\n                    </Button>\r\n                    <p className="text-xs text-zinc-500 mt-2">\r\n                      Обновит параметры {btSymbol.toUpperCase()} в config_{btSymbol.toLowerCase()}.yaml\r\n                    </p>\r\n                  </div>\r\n                )}\r\n              </div>\r\n            </TabsContent>`;
c = c.slice(0, btResultEndIdx) + buttonCode + c.slice(btResultEndIdx + btResultEndMarker.length);

// 5. Add modal dialog before the final </div>
const finalMarker = '    </div>\r\n  );\r\n}';
const finalIdx = c.lastIndexOf(finalMarker);
if (finalIdx === -1) {
  console.log('ERROR: Could not find final </div>');
  process.exit(1);
}
const modalCode = `      {showApplyDialog && (\r\n        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">\r\n          <Card className="bg-zinc-900 border border-zinc-700 p-6 max-w-md w-full mx-4">\r\n            <CardHeader><CardTitle className="text-white">Применить конфиг?</CardTitle></CardHeader>\r\n            <CardContent className="space-y-4">\r\n              <p className="text-sm text-zinc-400">\r\n                Обновить параметры <span className="text-white font-semibold">{btSymbol.toUpperCase()}</span>?\r\n              </p>\r\n              <div className="flex gap-3 pt-2">\r\n                <Button onClick={() => handleApplyConfig(true)} disabled={applying}\r\n                  className="flex-1 bg-red-600 hover:bg-red-700 text-white">\r\n                  {applying ? "..." : "Да, остановить бота"}\r\n                </Button>\r\n                <Button onClick={() => handleApplyConfig(false)} disabled={applying}\r\n                  className="flex-1 bg-green-600 hover:bg-green-700 text-white">\r\n                  {applying ? "..." : "Только конфиг"}\r\n                </Button>\r\n                <Button onClick={() => setShowApplyDialog(false)} variant="outline"\r\n                  className="flex-1 border-zinc-700 text-zinc-300">\r\n                  Отмена\r\n                </Button>\r\n              </div>\r\n            </CardContent>\r\n          </Card>\r\n        </div>\r\n      )}\r\n\r\n    </div>\r\n  );\r\n}`;
c = c.slice(0, finalIdx) + modalCode + c.slice(finalIdx + finalMarker.length);

fs.writeFileSync(filePath, c);
console.log('Done!');
console.log('handleApplyConfig:', c.includes('handleApplyConfig'));
console.log('Применить в конфиг:', c.includes('Применить в конфиг'));
console.log('Только конфиг:', c.includes('Только конфиг'));

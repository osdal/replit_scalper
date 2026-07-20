const fs = require('fs');
const filePath = 'artifacts/dashboard/src/Dashboard.tsx';
let c = fs.readFileSync(filePath, 'utf8');

// 1. Update import to include updateConfig
c = c.replace(
  'import { fetchBots, fetchTrades, fetchStats, startBot, stopBot, syncBinance, runBacktest } from "./hooks/useApi";',
  'import { fetchBots, fetchTrades, fetchStats, startBot, stopBot, syncBinance, runBacktest, updateConfig } from "./hooks/useApi";'
);

// 2. Add showApplyDialog and applying state after btResetKey
c = c.replace(
  'const [btResetKey, setBtResetKey] = useState(0);',
  'const [btResetKey, setBtResetKey] = useState(0);\n  const [showApplyDialog, setShowApplyDialog] = useState(false);\n  const [applying, setApplying] = useState(false);'
);

// 3. Add handleApplyConfig handler after handleApplyToBacktest
const marker3 = 'setBtResetKey(k => k + 1);\r\n  };\r\n\r\n  const handleRunBacktest';
const replacement3 = 'setBtResetKey(k => k + 1);\r\n  };\r\n\r\n  const handleApplyConfig = async (stopBot) => {\r\n    if (!btConfig) return;\r\n    setApplying(true);\r\n    try {\r\n      if (stopBot) {\r\n        await stopBot(btSymbol);\r\n      }\r\n      await updateConfig(btSymbol, btConfig);\r\n      setShowApplyDialog(false);\r\n      alert(\'Конфиг успешно применён\' + (stopBot ? \' (бот остановлен)\' : \'\'));\r\n      await load();\r\n    } catch (e) {\r\n      alert(\'Ошибка применения конфига\');\r\n    } finally {\r\n      setApplying(false);\r\n    }\r\n  };\r\n\r\n  const handleRunBacktest';
c = c.replace(marker3, replacement3);

// 4. Add "Применить в конфиг" button after backtest results
const marker4 = '                    </div>\r\n                  </>\r\n                )}\r\n              </div>\r\n            </TabsContent>';
const replacement4 = '                    </div>\r\n                  </>\r\n                )}\r\n\r\n                {btResult && (\r\n                  <div className="mt-4">\r\n                    <Button\r\n                      onClick={() => setShowApplyDialog(true)}\r\n                      disabled={applying}\r\n                      variant="outline"\r\n                      size="sm"\r\n                      className="border-zinc-700 text-zinc-300 hover:bg-zinc-800"\r\n                    >\r\n                      {applying ? <><RefreshCw className="w-4 h-4 mr-2 animate-spin" />Применение...</> : \'Применить в конфиг\'}\r\n                    </Button>\r\n                  </div>\r\n                )}\r\n              </div>\r\n            </TabsContent>';
c = c.replace(marker4, replacement4);

// 5. Add modal dialog before the closing </div> of the main component
const marker5 = '    </div>\r\n  );\r\n}';
const replacement5 = '      {showApplyDialog && (\r\n        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">\r\n          <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 max-w-md w-full mx-4 shadow-xl">\r\n            <h3 className="text-lg font-semibold text-white mb-2">Применить конфиг</h3>\r\n            <p className="text-sm text-zinc-400 mb-6">\r\n              Применить текущие настройки бектеста к конфигу бота <span className="font-mono text-white">{btSymbol}</span>?\r\n            </p>\r\n            <div className="flex flex-col gap-2">\r\n              <Button\r\n                onClick={() => handleApplyConfig(true)}\r\n                disabled={applying}\r\n                variant="destructive"\r\n                className="w-full"\r\n              >\r\n                {applying ? <><RefreshCw className="w-4 h-4 mr-2 animate-spin" />Применение...</> : \'Да, остановить бота\'}\r\n              </Button>\r\n              <Button\r\n                onClick={() => handleApplyConfig(false)}\r\n                disabled={applying}\r\n                variant="outline"\r\n                className="w-full border-zinc-700 text-zinc-300 hover:bg-zinc-800"\r\n              >\r\n                Только конфиг\r\n              </Button>\r\n              <Button\r\n                onClick={() => setShowApplyDialog(false)}\r\n                disabled={applying}\r\n                variant="ghost"\r\n                className="w-full text-zinc-400 hover:text-white hover:bg-zinc-800"\r\n              >\r\n                Отмена\r\n              </Button>\r\n            </div>\r\n          </div>\r\n        </div>\r\n      )}\r\n\r\n    </div>\r\n  );\r\n}';
c = c.replace(marker5, replacement5);

fs.writeFileSync(filePath, c);
console.log('handleApplyConfig:', c.includes('handleApplyConfig'));
console.log('Применить в конфиг:', c.includes('Применить в конфиг'));
console.log('Только конфиг:', c.includes('Только конфиг'));

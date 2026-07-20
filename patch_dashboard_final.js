const fs = require('fs');
const filePath = 'artifacts/dashboard/src/Dashboard.tsx';
let c = fs.readFileSync(filePath, 'utf8');

// 1. Update import to include updateConfig
const oldImport = 'import { fetchBots, fetchTrades, fetchStats, startBot, stopBot, syncBinance, runBacktest } from "./hooks/useApi";';
const newImport = 'import { fetchBots, fetchTrades, fetchStats, startBot, stopBot, syncBinance, runBacktest, updateConfig } from "./hooks/useApi";';
c = c.replace(oldImport, newImport);

// 2. Add showApplyDialog and applying state after btResetKey
const btResetKeyLine = 'const [btResetKey, setBtResetKey] = useState(0);';
const afterBtResetKey = btResetKeyLine + '\n  const [showApplyDialog, setShowApplyDialog] = useState(false);\n  const [applying, setApplying] = useState(false);';
c = c.replace(btResetKeyLine, afterBtResetKey);

// 3. Add handleApplyConfig handler after handleApplyToBacktest
const handleApplyToBacktestEnd = `setBtSymbol(params.symbol);
    setBtConfig(newConfig);
    setBtResetKey(k => k + 1);
  };`;

const handleApplyConfig = `
  const handleApplyConfig = async (stopBot) => {
    if (!btConfig) return;
    setApplying(true);
    try {
      if (stopBot) {
        await stopBot(btSymbol);
      }
      await updateConfig(btSymbol, btConfig);
      setShowApplyDialog(false);
      alert('Конфиг успешно применён' + (stopBot ? ' (бот остановлен)' : ''));
      await load();
    } catch (e) {
      alert('Ошибка применения конфига');
    } finally {
      setApplying(false);
    }
  };`;

c = c.replace(handleApplyToBacktestEnd, handleApplyToBacktestEnd + handleApplyConfig);

// 4. Add "Применить в конфиг" button after backtest results
const btResultClosing = `                    </div>
                  </>
                )}`;

const buttonBlock = `                    </div>
                    <div className="mt-4">
                      <Button
                        onClick={() => setShowApplyDialog(true)}
                        disabled={applying}
                        variant="outline"
                        size="sm"
                        className="border-zinc-700 text-zinc-300 hover:bg-zinc-800"
                      >
                        {applying ? <><RefreshCw className="w-4 h-4 mr-2 animate-spin" />Применение...</> : 'Применить в конфиг'}
                      </Button>
                    </div>
                  </>
                )}`;

c = c.replace(btResultClosing, buttonBlock);

// 5. Add modal dialog before the closing </div> of the main component
const lastDivBeforeReturn = `    </div>
  );
}`;

const modalDialog = `      {/* Apply Config Modal */}
      {showApplyDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="bg-zinc-900 border border-zinc-700 rounded-lg p-6 max-w-md w-full mx-4 shadow-xl">
            <h3 className="text-lg font-semibold text-white mb-2">Применить конфиг</h3>
            <p className="text-sm text-zinc-400 mb-6">
              Применить текущие настройки бектеста к конфигу бота <span className="font-mono text-white">{btSymbol}</span>?
            </p>
            <div className="flex flex-col gap-2">
              <Button
                onClick={() => handleApplyConfig(true)}
                disabled={applying}
                variant="destructive"
                className="w-full"
              >
                {applying ? <><RefreshCw className="w-4 h-4 mr-2 animate-spin" />Применение...</> : 'Да, остановить бота'}
              </Button>
              <Button
                onClick={() => handleApplyConfig(false)}
                disabled={applying}
                variant="outline"
                className="w-full border-zinc-700 text-zinc-300 hover:bg-zinc-800"
              >
                Только конфиг
              </Button>
              <Button
                onClick={() => setShowApplyDialog(false)}
                disabled={applying}
                variant="ghost"
                className="w-full text-zinc-400 hover:text-white hover:bg-zinc-800"
              >
                Отмена
              </Button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}`;

c = c.replace(lastDivBeforeReturn, modalDialog);

fs.writeFileSync(filePath, c);
console.log('Dashboard.tsx patched successfully');
console.log('handleApplyConfig:', c.includes('handleApplyConfig'));
console.log('Применить в конфиг:', c.includes('Применить в конфиг'));
console.log('Только конфиг:', c.includes('Только конфиг'));

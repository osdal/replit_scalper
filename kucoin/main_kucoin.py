import ccxt
import logging
import os
import sys
from dotenv import load_dotenv
from kucoin_futures.client import User

env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)

KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "") or ""
TRIAL_FUNDS_FALLBACK = float(os.getenv("TRIAL_FUNDS_FALLBACK", "0"))

def main():
    symbol_input = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    if '/' in symbol_input:
        symbol = symbol_input
    else:
        base = symbol_input.replace('USDT', '').replace('USDC', '')
        symbol = f"{base}/USDT:USDT"

    log = logging.getLogger("kucoin")
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)

    user_client = None
    try:
        client = ccxt.kucoinfutures({
            'apiKey': KUCOIN_API_KEY,
            'secret': KUCOIN_API_SECRET,
            'password': KUCOIN_API_PASSPHRASE,
            'enableRateLimit': True,
        })

        now = client.fetch_time()
        client.options['timeDifference'] = now - int(client.milliseconds())

        ticker = client.fetch_ticker(symbol)
        price = float(ticker['last'])
        log.info(f"[KCOIN] Price for {symbol}: {price}")

        user_client = User(
            key=KUCOIN_API_KEY,
            secret=KUCOIN_API_SECRET,
            passphrase=KUCOIN_API_PASSPHRASE,
            url='https://api-futures.kucoin.com',
            is_v1api=True
        )

        try:
            print("--- ТЕСТ ВАУЧЕРОВ ---")
            print(user_client._request('GET', '/api/v1/vouchers', params={'currency': 'USDT'}))
        except Exception as e:
            print("Ошибка ваучеров:", e)

        try:
            print("--- ТЕСТ BULLET ---")
            print(user_client._request('GET', '/api/v1/bullet-user'))
        except Exception as e:
            print("Ошибка bullet:", e)

        account_info = user_client.get_account_overview(currency='USDT')
        available_balance = float(account_info.get('availableBalance', 0))

        trial_balance = 0.0
        if TRIAL_FUNDS_FALLBACK > 0 and available_balance < 1:
            trial_balance = TRIAL_FUNDS_FALLBACK

        final_balance = trial_balance if trial_balance > 0 else available_balance
        log.info(f"[KCOIN] USDT-M balance: {final_balance}")

        client.close()
        log.info(f"[KCOIN] Test OK - API connected successfully")

    except Exception as e:
        log.error(f"[KCOIN] Error: {e}")
        import traceback
        traceback.print_exc()

    finally:
        if user_client and user_client.session:
            user_client.session.close()

if __name__ == "__main__":
    main()
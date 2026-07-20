import os
import time
from dotenv import load_dotenv
from kucoin_futures.client import User, Trade

env_path = os.path.join(os.path.dirname(__file__), "kucoin", ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)

KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "") or ""

SYMBOL = "DOGEUSDTM"  # дешевый контракт для минимального лота (фьючерсный тикер с M)

def dump_balance(user_client, label):
    print(f"\n===== {label} =====")
    try:
        overview = user_client.get_account_overview(currency="USDT")
        print("get_account_overview(USDT):")
        print(overview)
    except Exception as e:
        print("Overview error:", e)

def dump_position(trade_client):
    print("\n===== OPEN POSITIONS =====")
    try:
        positions = trade_client.get_position_details(SYMBOL)
        print(positions)
    except Exception as e:
        print("Position error:", e)

def send_order(trade_client, side, size=1, lever=1, reduce_only=False):
    """Отправляет маркет-ордер с перебором marginMode CROSS -> ISOLATED."""
    last_err = None
    for margin_mode in ("CROSS", "ISOLATED"):
        try:
            print(f"  -> попытка marginMode={margin_mode}")
            order = trade_client.create_market_order(
                symbol=SYMBOL,
                side=side,
                lever=lever,
                size=size,
                marginMode=margin_mode,
                reduceOnly=reduce_only,
            )
            print("Order response:")
            print(order)
            return order
        except Exception as e:
            print(f"  -> marginMode={margin_mode} не прошёл:", e)
            last_err = e
    return None

def main():
    user_client = User(
        key=KUCOIN_API_KEY,
        secret=KUCOIN_API_SECRET,
        passphrase=KUCOIN_API_PASSPHRASE,
        url="https://api-futures.kucoin.com",
        is_v1api=True,
    )
    trade_client = Trade(
        key=KUCOIN_API_KEY,
        secret=KUCOIN_API_SECRET,
        passphrase=KUCOIN_API_PASSPHRASE,
        url="https://api-futures.kucoin.com",
        is_v1api=True,
    )

    print("=== ШАГ 1: БАЛАНС ДО СДЕЛКИ ===")
    dump_balance(user_client, "БАЛАНС ДО")

    input("\nНажми Enter для отправки тестового ордера...")

    print("\n=== ШАГ 2: ОТПРАВКА МИНИМАЛЬНОГО МАРКЕТНОГО ОРДЕРА (1 лот DOGEUSDTM) ===")
    try:
        order = send_order(trade_client, side="buy", size=1, lever=1)
        if order is None:
            print("Order не прошёл ни в CROSS, ни в ISOLATED.")
            dump_balance(user_client, "БАЛАНС ПОСЛЕ ОШИБКИ")
            return
    except Exception as e:
        print("Order error:", e)
        dump_balance(user_client, "БАЛАНС ПОСЛЕ ОШИБКИ")
        return

    print("\n=== ШАГ 3: ОЖИДАНИЕ 2 СЕК И ПОВТОРНЫЙ ДАМП ===")
    time.sleep(2)
    dump_balance(user_client, "БАЛАНС ПОСЛЕ")
    dump_position(trade_client)

    confirm = input("\nЗакрыть позицию встречным ордером reduceOnly? (y/n): ")
    if confirm.strip().lower() == "y":
        print("\n=== ШАГ 4: ЗАКРЫТИЕ ПОЗИЦИИ (reduceOnly sell) ===")
        try:
            close = send_order(trade_client, side="sell", size=1, lever=1, reduce_only=True)
            if close is not None:
                # принудительно ставим reduceOnly для закрытия
                print("Close response:")
                print(close)
        except Exception as e:
            print("Close error:", e)
        time.sleep(2)
        dump_balance(user_client, "БАЛАНС ПОСЛЕ ЗАКРЫТИЯ")
    else:
        print("Позиция НЕ закрыта — закрой вручную в кабинете KuCoin!")

if __name__ == "__main__":
    main()
"""
CLI-обёртка над config.update_yaml_config — позволяет API серверу
(Node.js/TypeScript) обновлять config_*.yaml файлы ботов через
subprocess, а не прямым импортом Python-модуля (это невозможно —
Node.js не умеет исполнять .py файлы как JS-модули).

Вызов:
    python update_config_cli.py <symbol> <bot_dir>

Параметры для обновления передаются через stdin как JSON, например:
    {"risk_pct": 1.5, "sl_pct": 0.8}

Вывод (stdout, JSON): {"success": true} или {"error": "..."}
"""
import json
import sys

from config import update_yaml_config


def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: update_config_cli.py <symbol> <bot_dir>"}))
        sys.exit(1)

    symbol = sys.argv[1]
    bot_dir = sys.argv[2]

    raw = sys.stdin.read()
    try:
        params = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON input: {e}"}))
        sys.exit(1)

    try:
        update_yaml_config(symbol, params, bot_dir)
        print(json.dumps({"success": True}))
    except FileNotFoundError as e:
        print(json.dumps({"error": f"Config file not found: {e}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()

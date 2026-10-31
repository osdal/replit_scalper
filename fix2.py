with open('/opt/trading-bot/artifacts/api-server/src/routes/trading.ts', 'r', encoding='utf-8') as f:
    content = f.read()

idx = content.find('router.post("/check"')
print("Found at:", idx)
print("Context:")
print(content[idx:idx+100])
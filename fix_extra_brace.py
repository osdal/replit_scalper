with open('/opt/trading-bot/artifacts/api-server/src/routes/trading.ts', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the extra });
content = content.replace('}););', '});')

with open('/opt/trading-bot/artifacts/api-server/src/routes/trading.ts', 'w', encoding='utf-8') as f:
    f.write(content)

print("Fixed extra });")
import os

bot_dir = r'C:\DATA\bots\replit_scalper\bot'
configs = [f for f in os.listdir(bot_dir) if f.startswith('config_') and f.endswith('.yaml') and f != 'config.yaml' and 'recovery' not in f]

added = 0
for cfg_name in sorted(configs):
    path = os.path.join(bot_dir, cfg_name)
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    if 'llm_enabled' in content:
        continue
    
    llm_block = (
        "\n# LLM filter (optional)\n"
        "llm_enabled: false\n"
        "llm_mock: false\n"
        "llm_api_key: \"\"\n"
        "llm_model: \"llama-3.1-70b-versatile\"\n"
        "llm_fallback_models: \"\"\n"
        "llm_confidence_threshold: 0.7\n"
        "llm_calls_per_min: 20\n"
        "llm_per_symbol_cooldown_min: 5\n"
        "llm_backoff_sec: 60.0\n"
        "llm_short_backoff_sec: 5.0\n"
        "llm_provider_retry_delay_sec: 1.0\n"
        "gemini_api_key: \"\"\n"
        "gemini_model: \"gemini-2.0-flash-exp\"\n"
        "groq_api_key: \"\"\n"
        "groq_model: \"groq/compound-mini\"\n"
    )
    content = content.rstrip() + llm_block
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    added += 1

print(f'Added LLM config to {added} files')

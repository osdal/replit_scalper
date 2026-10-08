/**
 * Точечное обновление скалярных ключей верхнего уровня в YAML-тексте без потери
 * остальных ключей и КОММЕНТАРИЕВ (yaml.dump комментарии выбрасывает).
 * Если ключа нет — дописывается в конец файла.
 */
function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function upsertYamlScalar(text: string, key: string, value: string | number | boolean): string {
  const rendered = typeof value === "string" ? value : String(value);
  const re = new RegExp(`^(${escapeRe(key)}:[ \\t]*)([^#\\r\\n]*?)([ \\t]*(?:#[^\\r\\n]*)?)(?=\\r?$)`, "m");
  if (re.test(text)) {
    return text.replace(re, (_m, p1: string, _old: string, p3: string) => `${p1}${rendered}${p3}`);
  }
  const sep = text.length === 0 || text.endsWith("\n") ? "" : "\n";
  return `${text}${sep}${key}: ${rendered}\n`;
}

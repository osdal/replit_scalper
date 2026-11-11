const fs = require('fs');
const path = 'artifacts/api-server/src/routes/trading.ts';
let content = fs.readFileSync(path, 'utf8');

// Remove the duplicate GlobalLimits interface and readGlobalLimits/writeGlobalLimits functions (second occurrence)
// Pattern: from "interface GlobalLimits {" to "}" before "function readConfig"
const pattern = /interface GlobalLimits \{[\s\S]*?function writeGlobalLimits\(limits: GlobalLimits\): void \{[\s\S]*?\n\}\n\nfunction readConfig\(\)/;
const replacement = 'function readConfig()';
content = content.replace(pattern, replacement);

fs.writeFileSync(path, content);
console.log('Done');
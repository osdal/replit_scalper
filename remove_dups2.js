const fs = require('fs');
const path = 'artifacts/api-server/src/routes/trading.ts';
let content = fs.readFileSync(path, 'utf8');

// Split into lines
const lines = content.split('\n');

// Find the duplicate block: starts with "interface GlobalLimits {" at line ~136
// and ends before "function readConfig"
let startIdx = -1;
let endIdx = -1;
for (let i = 0; i < lines.length; i++) {
  if (lines[i].trim() === 'interface GlobalLimits {' && i > 50) {
    startIdx = i;
  }
  if (startIdx !== -1 && lines[i].trim().startsWith('function readConfig()')) {
    endIdx = i;
    break;
  }
}

if (startIdx !== -1 && endIdx !== -1) {
  // Remove lines from startIdx to endIdx-1
  lines.splice(startIdx, endIdx - startIdx);
  content = lines.join('\n');
  fs.writeFileSync(path, content);
  console.log('Removed duplicate block from line', startIdx + 1, 'to', endIdx);
} else {
  console.log('Could not find duplicate block');
  console.log('startIdx:', startIdx, 'endIdx:', endIdx);
}
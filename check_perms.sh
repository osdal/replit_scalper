#!/bin/bash
docker exec replit_scalper-api-testnet sh -c 'ls -la /app/data/'
docker exec replit_scalper-api-testnet sh -c 'touch /app/data/test_write.txt && echo "write ok" || echo "write failed"'
docker exec replit_scalper-api-testnet sh -c 'ls -la /app/data/test_write.txt 2>/dev/null || echo "no test file"'
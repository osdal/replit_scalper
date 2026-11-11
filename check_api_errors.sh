#!/bin/bash
docker logs replit_scalper-api-testnet --since 10m 2>&1 | grep -iE "stop|exit|crash|error|exception" | tail -20
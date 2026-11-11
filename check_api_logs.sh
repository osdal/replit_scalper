#!/bin/bash
docker logs replit_scalper-api-testnet 2>&1 | grep -iE "bot|spawn|start" | tail -30
#!/bin/bash
docker exec replit_scalper-dashboard-live curl -s http://localhost:5176/ | grep -o "Глобальные лимиты" || echo "not in index.html"
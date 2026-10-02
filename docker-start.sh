#!/bin/bash
# Запуск обоих стеков (testnet + live) в Docker.
# Соответствует docker-compose.yml: testnet 5000/5173, live 5001/5176.
set -e

echo "=== Starting both stacks in Docker ==="

# Check if docker is running
if ! docker info > /dev/null 2>&1; then
    echo "Error: Docker is not running. Please start Docker Desktop first."
    exit 1
fi

docker-compose up -d "$@"

echo "Waiting for services to start..."
sleep 15

echo ""
echo "=== Service Status ==="
docker-compose ps

echo ""
echo "=== URLs ==="
echo "TESTNET API:       http://localhost:5000"
echo "TESTNET dashboard: http://localhost:5173"
echo "LIVE API:          http://localhost:5001"
echo "LIVE dashboard:    http://localhost:5176"
echo ""
echo "Bots start with the Start button in the matching dashboard."
echo "In live press Arm first (REQUIRE_ARM=true)."
echo ""
echo "To view logs: docker-compose logs -f api-live"
echo "To stop:      docker-compose down"

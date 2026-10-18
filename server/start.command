#!/bin/bash
cd "$(dirname "$0")"
while true; do
  python3 server.py
  [ $? -eq 3 ] || break
  echo "Перезапуск после обновления..."
done

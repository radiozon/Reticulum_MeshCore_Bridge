#!/bin/bash
export PATH="$HOME/.local/bin:$PATH"
cd /home/orangepi/RNS_MeshCore

# Очищаем экран и показываем информацию
clear
echo "=========================================="
echo "RNS MeshCore Bridge - Interactive Mode"
echo "=========================================="
echo "Запуск в: $(date)"
echo "Используется Python: /usr/local/bin/python3.11"
echo "=========================================="
echo ""

# Запускаем с правильной версией Python 3.11
/usr/local/bin/python3.11 RMesh_final.py 2>&1

echo ""
echo "=========================================="
echo "Программа завершена"
echo "Нажмите Enter для закрытия окна"
read

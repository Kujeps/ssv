"""Запуск всех проверок: python tests/run_all.py (из корня проекта)."""
import glob
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
failed = []
for path in sorted(glob.glob(os.path.join(here, "e2e_*.py"))):
    print(f"\n=== {os.path.basename(path)} ===", flush=True)
    if subprocess.run([sys.executable, path]).returncode != 0:
        failed.append(os.path.basename(path))
print("\n" + ("ПРОВАЛЕНЫ: " + ", ".join(failed) if failed else "ВСЕ ПРОВЕРКИ ПРОШЛИ ✅"))
sys.exit(1 if failed else 0)

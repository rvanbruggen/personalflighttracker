"""Run every test module in a clean subprocess (each needs its own DB env)."""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ["test_provider.py", "test_e2e.py", "test_alerts_and_quota.py"]

failed = []
for module in MODULES:
    print(f"\n{'=' * 62}\n  {module}\n{'=' * 62}")
    result = subprocess.run([sys.executable, os.path.join(HERE, module)])
    if result.returncode != 0:
        failed.append(module)

print(f"\n{'=' * 62}")
if failed:
    print(f"❌ FAILED: {', '.join(failed)}")
    sys.exit(1)
print("✅ all test modules passed")

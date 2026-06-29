"""Make the repo root importable so `import bulk` works under pytest from anywhere."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

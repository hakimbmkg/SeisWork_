#!/usr/bin/env python3
"""
Backward-compatible launcher — delegates to seiswork.cli.main().
Use this if the package is not installed (pip install -e .).
After install, just run:  seiswork <command>
"""
import sys
import os

# Make project root importable when run as a plain script
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from seiswork.cli import main

if __name__ == "__main__":
    main()

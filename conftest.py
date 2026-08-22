"""Root pytest conftest: put the repo root on sys.path so tests can
`import womens_nonprofits_pipeline` (and other root-level modules) regardless
of the directory pytest is invoked from -- mirrors the sys.path.insert(0, ...)
pattern already used by build_index.py etc.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

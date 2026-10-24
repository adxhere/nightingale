"""Shared test setup: never touch the real history file or the real GPU."""
import os

os.environ.setdefault("NIGHTINGALE_DB", ":memory:")
os.environ.setdefault("LOCAL_GPU", "0")

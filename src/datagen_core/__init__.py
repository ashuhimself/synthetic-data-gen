"""datagen_core — generic, YAML-driven synthetic data generation runtime.

The executor and primitives here are deliberately schema-agnostic (C-3):
every behavior is driven by validated ``TableSchema`` YAML. AI-authored
scripts (C-2) import these primitives and add domain flavor via overrides —
they do not reimplement generation mechanics.
"""

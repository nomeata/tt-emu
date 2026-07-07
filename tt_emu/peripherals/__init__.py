"""Peripheral models, one module per hardware-component doc.

Real models for this build stage: :mod:`syscon` (chip-ID/clock gate),
:mod:`intc` (interrupt controller + timer1), :mod:`gpio`, :mod:`battery`, and
:mod:`zc90b` (anti-clone auth). :mod:`stubs` holds the constant-returning models
for the boot-constants checklist (NAND/ECC/DMA/USB/audio-clock) whose full
implementations are later tasks.
"""

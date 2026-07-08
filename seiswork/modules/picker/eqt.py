#!/usr/bin/env python3
"""
SeisWork — EQTransformer picker module (seisbench backend)
Author : HakimBMKG

Thin subclass of PhaseNetPicker that reads from cfg["pick"]["eqt"] and
uses seisbench EQTransformer model by default.  All pipeline logic
(GPU annotation, amplitude measurement, SDS/file/FDSN sources) is
inherited from PhaseNetPicker.
"""

from seiswork.modules.picker.phasenet import PhaseNetPicker


class EQTPicker(PhaseNetPicker):
    """EQTransformer phase picker via seisbench (same pipeline as PhaseNet)."""

    def __init__(self, cfg: dict, base_dir: str):
        # Inject eqt config block under the "phasenet" key so the parent
        # __init__ picks it up without modification.  The original cfg is
        # not mutated — we shadow only the phasenet slot for this instance.
        _cfg = dict(cfg)
        _pick = dict(cfg.get("pick", {}))
        _eqt  = dict(_pick.get("eqt", {}))
        # Default model for EQT block
        _eqt.setdefault("model",      "EQTransformer")
        _eqt.setdefault("pretrained", "original")
        _pick["phasenet"] = _eqt
        _cfg["pick"] = _pick
        super().__init__(_cfg, base_dir)
        # Override method tag so picks.csv shows method=seisbench_eqtransformer
        self.model_name = _eqt.get("model", "EQTransformer")

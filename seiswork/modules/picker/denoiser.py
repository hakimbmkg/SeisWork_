#!/usr/bin/env python3
"""
SeisWork — DeepDenoiser pre-processing module
Author : HakimBMKG

Applies SeisBench's DeepDenoiser to an ObsPy Stream before phase picking.
DeepDenoiser uses a U-Net on STFT spectrograms to separate seismic signal
from noise (Zhu et al. 2019, doi:10.1029/2019JB018501).

Pretrained models available via SeisBench:
  'original' — trained on NCEDC broadband data (default)
  'urban'    — trained on urban noise environments

Usage from PhaseNet picker:
    from seiswork.modules.picker.denoiser import denoise_stream
    st_clean = denoise_stream(st, pretrained='original')
"""

import logging

logger = logging.getLogger(__name__)

_DENOISER_CACHE: dict = {}   # pretrained_name → loaded model (avoid reload per chunk)


def _load_model(pretrained: str = "original"):
    """Load (or return cached) SeisBench DeepDenoiser model."""
    if pretrained in _DENOISER_CACHE:
        return _DENOISER_CACHE[pretrained]

    try:
        import seisbench.models as sbm
    except ImportError:
        raise RuntimeError(
            "SeisBench not installed. Run: pip install seisbench"
        )

    import torch

    logger.info(f"[DeepDenoiser] Loading pretrained='{pretrained}' ...")
    print(f"[DeepDenoiser] Loading pretrained='{pretrained}' ...", flush=True)
    try:
        model = sbm.DeepDenoiser.from_pretrained(pretrained)
    except Exception as e:
        raise RuntimeError(
            f"Failed to load DeepDenoiser '{pretrained}': {e}\n"
            "Ensure internet access on first run, or copy ~/.seisbench cache."
        )

    if torch.cuda.is_available():
        model = model.cuda()
        print(f"[DeepDenoiser] GPU: {torch.cuda.get_device_name(0)}", flush=True)
    else:
        print("[DeepDenoiser] CPU mode", flush=True)

    model.eval()
    _DENOISER_CACHE[pretrained] = model
    logger.info(f"[DeepDenoiser] Model loaded: {pretrained}")
    return model


def denoise_stream(st, pretrained: str = "original", batch_size: int = 1):
    """
    Denoise an ObsPy Stream using SeisBench DeepDenoiser.

    DeepDenoiser works on 30-second windows at 100 Hz. SeisBench handles
    resampling, windowing, and stitching internally via model.annotate() +
    model.classify() — but for denoising we use the stream_denoiser approach:
    annotate returns the denoised waveform directly.

    Args:
        st         : ObsPy Stream (any sampling rate, 3-component or single)
        pretrained : SeisBench pretrained model name ('original' or 'urban')
        batch_size : GPU batch size (default 1 for memory safety)

    Returns:
        ObsPy Stream with denoised waveforms (same metadata as input)
    """
    import numpy as np
    import torch
    from obspy import Stream, Trace

    if len(st) == 0:
        return st

    model = _load_model(pretrained)

    try:
        with torch.no_grad():
            st_denoised = model.annotate(st, batch_size=batch_size)
    except Exception as e:
        logger.warning(f"[DeepDenoiser] annotate failed, returning original stream: {e}")
        return st

    # model.annotate() for DeepDenoiser returns traces named like
    # NET.STA.LOC.CHA__DeepDenoiser_signal — extract only the signal traces
    # and reconstruct a clean stream with original channel names.
    signal_traces = []
    for tr in st_denoised:
        if "__DeepDenoiser_signal" in tr.stats.channel:
            # restore original channel name (strip suffix added by annotate)
            orig_ch = tr.stats.channel.split("__")[0]
            tr_out = tr.copy()
            tr_out.stats.channel = orig_ch
            signal_traces.append(tr_out)

    if not signal_traces:
        # Fallback: annotate() didn't return signal component — return original
        logger.warning("[DeepDenoiser] no signal traces found in annotate output, returning original")
        return st

    return Stream(signal_traces)

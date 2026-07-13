# PhaseNet (AI4EPS, TensorFlow) inference image for SeisWork — by HakimBMKG
# Mirrors the proven conda `phasenet` env (TF 2.11) so the H100 (sm_90) runs the
# same PTX-JIT path. PhaseNet code + model + SDS are mounted at runtime (-v),
# keeping the image small and the engine decoupled from the host conda envs.
FROM tensorflow/tensorflow:2.12.0-gpu

# predict.py runtime deps (TF/numpy already in base)
RUN pip install --no-cache-dir \
        "numpy<1.24" obspy pandas h5py scipy tqdm \
    && rm -rf /root/.cache/pip

# Default workdir = mounted PhaseNet repo
WORKDIR /opt/PhaseNet

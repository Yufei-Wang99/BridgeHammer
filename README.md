# BridgeHammer

Artifact for *BridgeHammer: DNN Backdoor Attack via CPU-iGPU Shared-memory Rowhammer*.

[Project website](https://noisyor.github.io/ROBBIN-Rowhammer-Based-Backdoor-Injection-during-Inference/#bridgehammer) | [Paper (PDF)](https://arxiv.org/pdf/2611.34684)
## Overview

Heterogeneous SoCs share physical memory between the CPU and the iGPU, so CPU-side Rowhammer can corrupt a DNN that the iGPU uses for inference. BridgeHammer is a backdoor attack that places the target weights before zero-copy GPU bind and flips them before inference; later GPU inferences then consume the corrupted model.

## Prerequisites

- Linux, Intel CPU + iGPU
- OpenVINO **2025.1**
- Python 3.10+

```bash
python3 -m venv .venv_ov251
.venv_ov251/bin/pip install openvino==2025.1.0 numpy
make
make hammer
```

### Required files

**Option A (default).** Use the ResNet-20 INT8 files in `artifacts/resnet20_int8/`:

- model and weights: `model.onnx`, `combined_weights.bin`, `combined_manifest.json`
- trigger and eval: `trigger_pattern.npy`, `eval/cifar10_test256.npz`
- online map: `online_targets.json`
- offline record: `offline/dram_profile.json`, `offline/blacksmith_fuzz_summary.json`, `offline/ResNet20_INT8_pagemap.txt`, `attack/attack_report.txt`

**Option B.** To run on your own machine, produce a Blacksmith profile ([code](https://github.com/comsec-group/blacksmith)) and a ROBBIN backdoor record ([code](https://github.com/noisyor/ROBBIN-Rowhammer-Based-Backdoor-Injection-during-Inference/tree/main)), then:

```bash
python3 scripts/build_online_targets.py \
  --profile /path/to/your_dram_profile.json \
  --report /path/to/attack_report.txt \
  --pagemap /path/to/model_pagemap.txt \
  --blacksmith-json /path/to/blacksmith_fuzz_summary.json
```

Recorded PFNs are DIMM-specific. A different machine needs its own profile.

## Quick Start

```bash
./run_attack.sh
```

## Repository Structure

```
victim.py                 # OpenVINO iGPU victim
framework/                # F+R, evict + PCP plant, Blacksmith replay
eviction/
blacksmith/
artifacts/resnet20_int8/  # ResNet-20 INT8 weights, map, and profile
scripts/build_online_targets.py
```

## Notes

Rowhammer results are DIMM-specific. For academic use on machines you own or are authorized to test.

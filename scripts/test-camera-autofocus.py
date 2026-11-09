#!/usr/bin/env python3
"""Test the production autofocus with a simulated lens and scene.

Since libcamera-gts9u patch 0009 the autofocus is SoftAf
(src/ipa/simple/soft_af.{h,cpp}); tests/test_soft_af.py builds it from the
patch with tests/soft_af_sim.cpp and checks smoothness, accuracy, stillness
in a still scene, refocusing, shake, pan, flat scenes and AfModeAuto.  This
entry point runs that suite.  The source-tree argument the old state-machine
test took is accepted and ignored.

This does not certify optical sharpness or the real actuator's settling.
"""
from pathlib import Path
import subprocess
import sys

repo = Path(__file__).resolve().parents[1]
sys.exit(subprocess.run([sys.executable, "-m", "unittest", "-v", "tests.test_soft_af"],
                        cwd=repo).returncode)

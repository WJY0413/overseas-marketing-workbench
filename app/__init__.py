"""Local BD email automation workbench."""

import os

# Bound the numerical runtime before Excel/report modules import NumPy.
# Workbench is an IO-bound service, not a parallel matrix-compute worker.
os.environ["OPENBLAS_NUM_THREADS"] = "1"

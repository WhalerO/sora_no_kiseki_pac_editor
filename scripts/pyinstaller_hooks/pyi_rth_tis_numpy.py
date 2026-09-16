"""Keep the bundled Conda MKL subset deterministic and portable."""

import os


os.environ["MKL_THREADING_LAYER"] = "SEQUENTIAL"
os.environ["MKL_ENABLE_INSTRUCTIONS"] = "SSE4_2"

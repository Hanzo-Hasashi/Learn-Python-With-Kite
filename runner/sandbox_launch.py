from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from sandbox import set_execution_limits

uid = int(sys.argv[1])
gid = int(sys.argv[2])
bootstrap = sys.argv[3]
code = sys.argv[4]
set_execution_limits(uid, gid)
os.execv(sys.executable, [sys.executable, "-I", "-S", bootstrap, code])

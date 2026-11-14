from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from sandbox import install_execution_filter

sys.dont_write_bytecode = True
install_execution_filter()
source = base64.b64decode(sys.argv[1], validate=True).decode("utf-8")
namespace = {"__name__": "__main__", "__file__": "<kite>"}
exec(compile(source, "<kite>", "exec"), namespace, namespace)

# Local stub overlay: NVIDIA's generated pxr stubs mistype many returns as None
# (boost-python stubgen artifacts), producing false errors. Treat pxr as untyped.
from typing import Any

def __getattr__(name: str) -> Any: ...

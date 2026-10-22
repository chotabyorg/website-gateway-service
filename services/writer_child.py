"""Trusted gateway child bootstrap; no provider sender descendants are allowed."""
import runpy
from services.writer_attestor import deny_sender_subprocesses

deny_sender_subprocesses()
runpy.run_module("main", run_name="__main__")

"""Run meaningful regressions without touching production data."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

root=Path(__file__).resolve().parent
sys.path.insert(0,str(root))
temporary=root/'.test-tmp'
temporary.mkdir(exist_ok=True)
tempfile.tempdir=str(temporary)
os.environ['APPSTORE_DISABLE_WORKER']='1'
suite=unittest.defaultTestLoader.discover(str(root/'tests'))
result=unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(not result.wasSuccessful())

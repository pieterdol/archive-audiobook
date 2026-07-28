"""Put the script itself on the path.

Without this the tests import librivox only when pytest is run from inside the repo, and
`pytest ~/Code/librivox` from anywhere else fails at the import — which is how it was found.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

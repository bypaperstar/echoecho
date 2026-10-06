"""A bounded, allocation-light meter; no PCM is retained or logged."""
import array
import math
import sys


def pcm_level(data):
    if not data or len(data) % 2:
        return 0.0
    samples = array.array('h')
    samples.frombytes(data)
    if sys.byteorder != 'little':
        samples.byteswap()
    # Show quiet laptop speech as well as loud signals on a useful 0..1 scale.
    rms = math.sqrt(sum(x*x for x in samples) / len(samples)) / 32768
    return min(1.0, math.sqrt(rms)*3)

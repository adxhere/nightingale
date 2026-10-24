"""A tiny numpy stand-in for the parts of PyTorch the self-tests use, so their logic runs in CI without a GPU.

It is not PyTorch: it only checks that app/selftest.py calls things in a sensible order and that its
arithmetic and reporting work. FAKE_TORCH_FLIP=N flips N words during the memory test, to show errors are caught.
"""
import os

import numpy as np

__version__ = "0.0-fake"
float64, float32, float16, int32 = np.float64, np.float32, np.float16, np.int32
_FLIP = {"left": int(os.getenv("FAKE_TORCH_FLIP", "0"))}


class version:  # noqa: N801
    cuda = None


class _Mat:
    allow_tf32 = True


class backends:  # noqa: N801
    class cuda:  # noqa: N801
        matmul = _Mat()


class cuda:  # noqa: N801
    @staticmethod
    def is_available():
        return False

    @staticmethod
    def synchronize():
        pass

    @staticmethod
    def empty_cache():
        pass


class Generator:
    def __init__(self, device="cpu"):
        self.rng = np.random.default_rng(0)

    def manual_seed(self, seed):
        self.rng = np.random.default_rng(seed)
        return self


class Tensor:
    def __init__(self, a):
        self.a = np.asarray(a)

    # conversions
    def to(self, dev=None, dtype=None):
        return Tensor(self.a.astype(dtype) if dtype is not None else self.a.copy())

    def float(self):
        return Tensor(self.a.astype(np.float32))

    def double(self):
        return Tensor(self.a.astype(np.float64))

    def half(self):
        return Tensor(self.a.astype(np.float16))

    def cpu(self):
        return Tensor(self.a.copy())

    def item(self):
        return self.a.item()

    def numel(self):
        return self.a.size

    # maths
    def __matmul__(self, o):
        return Tensor(self.a.astype(np.float32) @ o.a.astype(np.float32) if self.a.dtype == np.float16 else self.a @ o.a)

    def __sub__(self, o):
        return Tensor(self.a - o.a)

    def __truediv__(self, o):
        return Tensor(self.a / (o.a if isinstance(o, Tensor) else o))

    def __ne__(self, o):
        return Tensor(self.a != (o.a if isinstance(o, Tensor) else np.int32(o)))

    def __getitem__(self, k):
        return Tensor(self.a[k])

    def abs(self):
        return Tensor(np.abs(self.a))

    def max(self):
        return Tensor(self.a.max())

    def sum(self):
        return Tensor(self.a.sum())

    def all(self):
        return bool(self.a.all())

    # in place
    def copy_(self, o):
        self.a[...] = o.a
        return self

    def fill_(self, v):
        self.a[...] = np.int32(v) if self.a.dtype == np.int32 else v
        if _FLIP["left"] and self.a.dtype == np.int32 and self.a.size > 10:
            self.a[5] ^= 1
            _FLIP["left"] -= 1
        return self

    def random_(self, lo, hi, generator=None):
        rng = generator.rng if generator is not None else np.random.default_rng()
        self.a[...] = rng.integers(lo, hi, size=self.a.shape, dtype=np.int64).astype(self.a.dtype)
        return self


def randn(*shape, generator=None, dtype=np.float32, device="cpu"):
    rng = generator.rng if generator is not None else np.random.default_rng(1)
    return Tensor(rng.standard_normal(shape).astype(dtype))


def empty(n, dtype=np.float32, device="cpu"):
    return Tensor(np.zeros(n, dtype=dtype))


def empty_like(t):
    return Tensor(np.zeros_like(t.a))


def arange(n, device="cpu", dtype=np.int32):
    return Tensor(np.arange(n, dtype=dtype))


def isfinite(t):
    return Tensor(np.isfinite(t.a))


def allclose(a, b, rtol=1e-5, atol=1e-8):
    return bool(np.allclose(a.a, b.a, rtol=rtol, atol=atol))

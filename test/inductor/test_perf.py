# Owner(s): ["module: inductor"]
import torch._dynamo
import torch._inductor.config as config
from torch._dynamo.optimizations.backends import register_backend
from torch._inductor import metrics
from torch._inductor.compile_fx import compile_fx, count_bytes_inner
from torch.testing._internal.common_utils import (
    TEST_WITH_ROCM,
    TestCase as TorchTestCase,
)
from torch.testing._internal.inductor_utils import HAS_CPU, HAS_CUDA

aten = torch.ops.aten


@register_backend
def count_bytes_inductor(gm, example_inputs):
    return compile_fx(gm, example_inputs, inner_compile=count_bytes_inner)


@torch._dynamo.optimize("count_bytes_inductor")
def f(x):
    return torch.cat([x, x.cos()])


def count_numel(f, *args):
    """
    Assumes all inputs are fp32
    """
    metrics.reset()
    torch._dynamo.optimize("count_bytes_inductor")(f)(*args)
    print(metrics.nodes_num_elem)
    return str(metrics.num_bytes_accessed // 4)


def T(*size, dtype=torch.float32, device="cuda"):
    return torch.randn(size, dtype=dtype, device=device)


class TestCase(TorchTestCase):
    def assertExpectedInt(self, actual, expected):
        return self.assertExpectedInline(actual, str(expected), skip=1)


class NumBytesMetricTests(TestCase):
    """
    Primarily used for testing that the num_bytes_accessed metrics is correct.
    """

    def test_pointwise(self):
        def f(x):
            return x.cos()

        inp = (T(10),)
        self.assertExpectedInline(count_numel(f, *inp), """20""")

        def f(x, y):
            return x + y

        inp = (T(10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """30""")

        def f(x, y):
            return x + y

        inp = (T(10, 10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """210""")

        def f(x):
            return x + x

        inp = (T(10),)
        self.assertExpectedInline(count_numel(f, *inp), """20""")

        def f(x):
            return x + x.t()

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """200""")

        def f(a, b, c):
            return a.cos(), b.sin() + c.sin()

        inp = (T(10), T(10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """50""")

    def test_reduction(self):
        def f(x):
            return x.sum(dim=1)

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """110""")

        def f(x):
            return x.sum(dim=0)

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """110""")

    def test_extern(self):
        def f(x):
            return torch.mm(x, x)

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """200""")

        def f(a, b):
            return torch.mm(a, b)

        inp = (T(10, 10), T(10, 10))
        self.assertExpectedInline(count_numel(f, *inp), """300""")

        def f(x):
            x = x.cos()
            x = torch.mm(x, x)
            x = x.cos()
            return x

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """600""")

        def f(x):
            a = x.cos()
            b = x.sin()
            x = torch.mm(a, b)
            return x

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """600""")

    def test_cat(self):
        def f(a, b):
            return torch.cat([a.sin(), b.sin()])

        inp = (T(10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """40""")

        def f(a, b):
            return torch.cat([a, b])

        inp = (T(10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """40""")

        def f(a, b):
            return torch.cat([a.cos(), b])

        inp = (T(10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """40""")

        def f(a):
            return torch.cat([a.cos(), a.sin()])

        inp = (T(10),)
        self.assertExpectedInline(count_numel(f, *inp), """30""")


class FusionTests(TorchTestCase):
    device = """cuda"""

    def test_horizontal_reduction_pointwise(self):
        def f(a):
            b = a.sum(dim=1)
            c = a.cos()
            return b, c

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """210""")

    def test_horizontal_reduction_reduction(self):
        def f(a):
            b = a.sum(dim=1)
            c = a.amax(dim=1)
            return b, c

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """120""")

    def test_horizontal_reduction_pointwise2(self):
        def f(a, b):
            c = a.sum(dim=1)
            b = b.cos()
            return b + c

        inp = (T(10, 10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """120""")

    def test_horizontal_reduction_outer_pointwise(self):
        def f(a, b):
            c = a.sum(dim=0)
            b = b.cos()
            return b + c

        inp = (T(10, 10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """120""")

    def test_horizontal_sum_pw_broadcast(self):
        def f(a, b):
            a = a.sum(dim=1, keepdim=True)
            b = b.cos()
            return a * b

        inp = (T(10, 10), T(10))
        self.assertExpectedInline(count_numel(f, *inp), """210""")

    def test_vertical_sum_pw(self):
        def f(a):
            a = a.cos()
            a = a.sum(dim=1)
            return a.cos()

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """110""")

    def test_norm_chain(self):
        def f(a):
            b = a.sum(dim=1, keepdim=True)
            a = a * b
            b = a.sum(dim=1, keepdim=True)
            a = a * b
            b = a.sum(dim=1, keepdim=True)
            a = a * b
            return a

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """200""")

    def test_softmax_inner(self):
        def f(a):
            return torch.softmax(a, dim=1)

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """200""")

    def test_layer_norm(self):
        # TODO: Suboptimal! We shouldn't need to save normalization stats.
        mod = torch.nn.LayerNorm(10, device=self.device)

        def f(x):
            return mod(x)

        inp = (T(10, 10),)
        with torch.no_grad():
            self.assertExpectedInline(count_numel(f, *inp), """220""")

    def test_double_softmax(self):
        def f(x):
            x = torch.softmax(x, dim=1)
            x = torch.softmax(x, dim=1)
            return x

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """200""")

    def test_softmax_backward(self):
        def f(grad_out, out):
            return aten._softmax_backward_data(grad_out, out, 1, torch.float32)

        inp = (T(10, 10), T(10, 10))
        self.assertExpectedInline(count_numel(f, *inp), """300""")

    def test_neighbor(self):
        def f(a, b):
            return ((a - b) ** 2).sum(dim=-1).amax(dim=1)

        inp = (T(10, 1, 4), T(1, 10, 4))
        self.assertExpectedInline(count_numel(f, *inp), """90""")


class SchedulerFusionTests(TorchTestCase):
    """
    Testing the fusion group creation heuristic.
    Disables inductor rematerialization for easier reasoning of tests.
    """

    def setUp(self):
        super().setUp()
        config.realize_bytes_threshold = 0

    def test_fusion_choice1(self):
        # Doesn't matter where we break fusion group here
        def f(a):
            c = a.cos()
            d = torch.mm(c, c)
            e = c.cos()
            return d + e

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """700""")

    def test_fusion_choice2(self):
        # We should materialize e (it's smaller!)
        # [c, e]: 210, [f]: 210, [d]: 200
        def f(a):
            c = a.cos()
            d = torch.mm(c, c)
            e = c.sum(dim=1)
            f = d + e
            return f

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """620""")

    def test_fusion_choice3(self):
        # We should materialize e.
        # [c, e]: 300, [f]: 300, [d]: 200
        def f(a):
            c = a.cos()
            d = torch.mm(c, c)
            e = c + a
            f = d + e
            return f, e

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """800""")


# Test cases where we don't do the right thing yet.
class WouldBeNiceIfItWorked:
    def test_horizontal(self):
        def f(a):
            b = a.sum(dim=0)
            c = a.cos()
            return b, c

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """210""")

    # TODO: We aren't fusing outer dim softmaxes
    def test_softmax_outer(self):
        def f(a):
            return torch.softmax(a, dim=1)

        inp = (T(10, 10),)
        self.assertExpectedInline(count_numel(f, *inp), """200""")

    # TODO: We materialize the intermediate if we don't unroll the reduction
    def test_neighbor(self):
        def f(a, b):
            return ((a - b) ** 2).sum(dim=-1).amax(dim=1)

        inp = (T(10, 1, 8), T(1, 10, 8))
        self.assertExpectedInline(count_numel(f, *inp), """170""")

    # TODO: We end up with 1050 not 1000 due to greedy fusion.
    def test_fusion_choice4(self):
        def f(a, b, b2):
            c = a + b
            d = torch.mm(c, c)
            e = c + b + b2
            f = d + e + b2
            return f, e

        inp = (T(10, 10), T(10, 10, dtype=torch.float16), T(10, 10))
        self.assertExpectedInline(count_numel(f, *inp), """1000""")


if __name__ == "__main__":
    from torch._dynamo.test_case import run_tests

    if (HAS_CPU or HAS_CUDA) and not TEST_WITH_ROCM:
        run_tests(needs="filelock")
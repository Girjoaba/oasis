import random
from typing import Optional
from coyote_test import fpga_stream

from libstf_utils.configured_test_case import ConfiguredTestCase
from libstf_utils.fpga_configuration import StreamConfig
from oasis_utils.fpga_configuration import FilterConfig, INT32_MIN, INT32_MAX, INT64_MIN, INT64_MAX

INT32 = fpga_stream.StreamType.SIGNED_INT_32
INT64 = fpga_stream.StreamType.SIGNED_INT_64


class FilterCompareTestCase(ConfiguredTestCase):
    """
    Tests the FilterCompare: stream 0 returns the values unchanged and stream 1 one element per
    value, 1 if the value passes the interval and 0 otherwise.
    """

    alternative_vfpga_top_file = "vfpga-tops/filter_compare_test.sv"
    debug_mode = True

    def configure(self):
        # The bounds are written before the stream type, which holds back the values till it arrives
        filter_config = self.config.get_config(FilterConfig)
        if self.bounds is None:
            filter_config.enqueue(0, compare=False)
        else:
            filter_config.enqueue(0, *self.bounds, compare=True, negate=self.negate)

        self.config.get_config(StreamConfig).enqueue_stream_config(0, self.stream_type)

        self.set_stream_input(0, fpga_stream.Stream(self.stream_type, self.values))
        self.set_expected_output(0, fpga_stream.Stream(self.stream_type, self.values))
        self.set_expected_output(1, fpga_stream.Stream(self.stream_type, self.mask))

    def run_compare(
        self,
        stream_type: fpga_stream.StreamType,
        values: list[int],
        bounds: Optional[tuple[int, int]],
        negate: bool = False,
    ):
        """
        Streams the values through the FilterCompare with the bounds (None disables the compare)
        and asserts the values and the pass bits.
        """
        self.stream_type = stream_type
        self.values = values
        self.bounds = bounds
        self.negate = negate

        if bounds is None:
            self.mask = [1] * len(values)
        else:
            lo, hi = bounds
            self.mask = [int((lo <= value <= hi) != negate) for value in values]

        self.simulate_fpga()
        self.assert_simulation_output()

    # -- INT32 ------------------------------------------------------------------------------------
    def test_int32_between_signed(self):
        # Negative bounds over the full value range: an unsigned compare fails on the signs
        rng = random.Random(1)
        values = [rng.randint(INT32_MIN, INT32_MAX) for _ in range(1001)]
        self.run_compare(INT32, values, (-pow(2, 30), pow(2, 29)))

    def test_int32_equal(self):
        # TPC-H Q2: p_size = 15, sizes 1 to 50
        rng = random.Random(2)
        values = [rng.randint(1, 50) for _ in range(1003)]
        self.run_compare(INT32, values, (15, 15))

    def test_int32_not_equal(self):
        rng = random.Random(3)
        values = [rng.randint(1, 50) for _ in range(1003)]
        self.run_compare(INT32, values, (15, 15), negate=True)

    def test_int32_less(self):
        # x < 0 as the closed interval [INT32_MIN, -1]
        rng = random.Random(4)
        values = [rng.randint(INT32_MIN, INT32_MAX) for _ in range(500)]
        self.run_compare(INT32, values, (INT32_MIN, -1))

    def test_int32_shipdate(self):
        # TPC-H Q6: l_shipdate >= 1994-01-01 and < 1995-01-01, DATE as days since 1970-01-01
        rng = random.Random(5)
        values = [rng.randint(8035, 10561) for _ in range(2000)] # 1992-01-01 to 1998-12-01
        self.run_compare(INT32, values, (8766, 9130))

    def test_int32_all(self):
        values = [INT32_MIN, INT32_MIN + 1, -1, 0, 1, INT32_MAX - 1, INT32_MAX] * 5
        self.run_compare(INT32, values, (INT32_MIN, INT32_MAX))

    def test_int32_none(self):
        values = [INT32_MIN, -1, 0, 1, INT32_MAX] * 7
        self.run_compare(INT32, values, (1, 0))

    def test_int32_disabled(self):
        values = list(range(-500, 500))
        self.run_compare(INT32, values, None)

    def test_int32_one_value(self):
        self.run_compare(INT32, [-7], (-7, -7))

    # -- INT64 ------------------------------------------------------------------------------------
    def test_int64_between_signed(self):
        rng = random.Random(6)
        values = [rng.randint(INT64_MIN, INT64_MAX) for _ in range(1001)]
        self.run_compare(INT64, values, (-pow(2, 62), pow(2, 61)))

    def test_int64_beyond_32_bits(self):
        # Bounds and values around the 32 bit borders: a compare of the low halves fails here
        borders = [pow(2, 31), pow(2, 32)]
        values = [sign * border + delta for border in borders for sign in (-1, 1) for delta in (-1, 0, 1)]
        values += [INT64_MIN, INT64_MAX, 0]
        self.run_compare(INT64, values * 3, (-pow(2, 32), pow(2, 32) - 1))

    def test_int64_same_high_half(self):
        # Bounds and values share the high half, so the low halves decide, also with bit 31 set
        rng = random.Random(10)
        base = -3 * pow(2, 32)
        values = [base + rng.randint(0, pow(2, 32) - 1) for _ in range(1001)]
        self.run_compare(INT64, values, (base + pow(2, 30), base + 3 * pow(2, 30)))

    def test_int64_discount(self):
        # TPC-H Q6: l_discount between 0.05 and 0.07, DECIMAL(15,2) as cents
        rng = random.Random(7)
        values = [rng.randint(0, 10) for _ in range(1005)]
        self.run_compare(INT64, values, (5, 7))

    def test_int64_quantity(self):
        # TPC-H Q6: l_quantity < 24, DECIMAL(15,2) as cents
        rng = random.Random(8)
        values = [100 * rng.randint(1, 50) for _ in range(1007)]
        self.run_compare(INT64, values, (INT64_MIN, 2399))

    def test_int64_not_equal(self):
        rng = random.Random(9)
        values = [rng.choice([-pow(2, 40), 0, pow(2, 40)]) for _ in range(301)]
        self.run_compare(INT64, values, (pow(2, 40), pow(2, 40)), negate=True)

    def test_int64_disabled(self):
        values = list(range(-pow(2, 40), -pow(2, 40) + 333))
        self.run_compare(INT64, values, None)

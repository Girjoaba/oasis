import random
from dataclasses import dataclass
from typing import Optional
from coyote_test import fpga_stream

from libstf_utils.configured_test_case import ConfiguredTestCase
from libstf_utils.fpga_configuration import StreamConfig
from oasis_utils.fpga_configuration import FilterConfig, INT32_MIN, INT32_MAX, INT64_MIN, INT64_MAX

INT32 = fpga_stream.StreamType.SIGNED_INT_32
INT64 = fpga_stream.StreamType.SIGNED_INT_64
INT16 = fpga_stream.StreamType.SIGNED_INT_16
UINT8 = fpga_stream.StreamType.UNSIGNED_INT_8


@dataclass
class Chunk:
    """
    One column chunk through the FilterStage of a decoder, with the filter conf of the chunk.
    """

    decoder: int
    stream_type: fpga_stream.StreamType
    values: list[int]
    compare: bool = False
    lo: int = INT64_MIN
    hi: int = INT64_MAX
    negate: bool = False
    load_sel: bool = False
    store_sel: bool = False
    emit: bool = True


class FilterStageTestCase(ConfiguredTestCase):
    """
    Tests the FilterStage: the chunks of decoder I enter on stream I and leave on stream I without
    the values that fail the compare or the selection of the row group, packed into full beats. A
    model of the selection bitmap per decoder gives the expected values; a chunk without expected
    values has no expected output because the host receives nothing for an empty stream.
    """

    alternative_vfpga_top_file = "vfpga-tops/filter_stage_test.sv"
    debug_mode = True

    def configure(self):
        filter_config = self.config.get_config(FilterConfig)
        stream_config = self.config.get_config(StreamConfig)
        bitmaps: dict[int, list[bool]] = {}

        for chunk in self.chunks:
            # The filter conf goes first, like the software writes it before the decoder conf
            filter_config.enqueue(
                chunk.decoder, lo=chunk.lo, hi=chunk.hi, compare=chunk.compare, negate=chunk.negate,
                load_sel=chunk.load_sel, store_sel=chunk.store_sel, emit=chunk.emit,
            )
            stream_config.enqueue_stream_config(chunk.decoder, chunk.stream_type)
            self.set_stream_input(chunk.decoder, fpga_stream.Stream(chunk.stream_type, chunk.values))

            expected = self.expected_values(chunk, bitmaps)
            if expected:
                self.set_expected_output(chunk.decoder, fpga_stream.Stream(chunk.stream_type, expected))

    @staticmethod
    def expected_values(chunk: Chunk, bitmaps: dict[int, list[bool]]) -> list[int]:
        if chunk.compare:
            passes = [(chunk.lo <= value <= chunk.hi) != chunk.negate for value in chunk.values]
        else:
            passes = [True] * len(chunk.values)
        if chunk.load_sel:
            bitmap = bitmaps[chunk.decoder]
            passes = [p and bitmap[i] for i, p in enumerate(passes)]
        if chunk.store_sel:
            bitmaps[chunk.decoder] = passes
        if not chunk.emit:
            return []
        return [value for value, p in zip(chunk.values, passes) if p]

    def run_chunks(self, chunks: list[Chunk]):
        self.chunks = chunks
        self.simulate_fpga()
        self.assert_simulation_output()

    # -- One chunk --------------------------------------------------------------------------------
    def test_int32_passthrough(self):
        # No filter: the values pass unchanged, with a partial last beat
        self.run_chunks([Chunk(0, INT32, list(range(-500, 501)))])

    def test_int32_compare_sparse(self):
        # TPC-H Q6 l_shipdate: about a fifth of the values pass
        rng = random.Random(1)
        values = [rng.randint(8035, 10561) for _ in range(2000)]
        self.run_chunks([Chunk(0, INT32, values, compare=True, lo=8766, hi=9130)])

    def test_int32_compare_dense(self):
        # Most values pass, so almost every output beat merges two input beats
        rng = random.Random(2)
        values = [rng.randint(0, 99) for _ in range(1500)]
        self.run_chunks([Chunk(0, INT32, values, compare=True, lo=0, hi=89)])

    def test_int64_compare(self):
        # TPC-H Q6 l_discount between 0.05 and 0.07 as cents
        rng = random.Random(3)
        values = [rng.randint(0, 10) for _ in range(1005)]
        self.run_chunks([Chunk(0, INT64, values, compare=True, lo=5, hi=7)])

    def test_int64_beyond_32_bits(self):
        borders = [pow(2, 31), pow(2, 32)]
        values = [sign * border + delta for border in borders for sign in (-1, 1) for delta in (-1, 0, 1)]
        values += [INT64_MIN, INT64_MAX, 0]
        self.run_chunks([Chunk(0, INT64, values * 7, compare=True, lo=-pow(2, 32), hi=pow(2, 32) - 1)])

    def test_int32_negate(self):
        rng = random.Random(4)
        values = [rng.randint(1, 50) for _ in range(1003)]
        self.run_chunks([Chunk(0, INT32, values, compare=True, lo=15, hi=15, negate=True)])

    def test_all_pass_full_beats(self):
        # Exactly 16 values per beat and all pass: the output has no partial beat
        values = list(range(16 * 40))
        self.run_chunks([Chunk(0, INT32, values, compare=True, lo=INT32_MIN, hi=INT32_MAX)])

    def test_byte_and_int16_passthrough(self):
        # Types without lanes pass through, with a partial last lane
        self.run_chunks([
            Chunk(0, UINT8, [i % 256 for i in range(13)]),
            Chunk(0, INT16, [i - 20 for i in range(37)]),
            Chunk(1, UINT8, [i % 256 for i in range(64 * 3 + 1)]),
        ])

    # -- Several chunks ---------------------------------------------------------------------------
    def test_all_fail_then_passthrough(self):
        # An empty output stream, then a normal chunk on the same decoder
        values = list(range(100))
        self.run_chunks([
            Chunk(0, INT32, values, compare=True, lo=1, hi=0),
            Chunk(0, INT32, [v * 3 for v in values]),
        ])

    def test_filter_only_then_apply(self):
        # The first chunk only stores the selection, the second one applies it
        rng = random.Random(5)
        n = 1000
        dates = [rng.randint(8035, 10561) for _ in range(n)]
        prices = [rng.randint(100, 10000000) for _ in range(n)]
        self.run_chunks([
            Chunk(0, INT32, dates, compare=True, lo=8766, hi=9130, store_sel=True, emit=False),
            Chunk(0, INT64, prices, load_sel=True),
        ])

    def test_q6_row_group(self):
        # TPC-H Q6: three filter columns of mixed width, the last one is projected, then the price
        rng = random.Random(6)
        n = 1000
        shipdate = [rng.randint(8035, 10561) for _ in range(n)]
        quantity = [100 * rng.randint(1, 50) for _ in range(n)]
        discount = [rng.randint(0, 10) for _ in range(n)]
        price = [rng.randint(100, 10000000) for _ in range(n)]
        self.run_chunks([
            Chunk(0, INT32, shipdate, compare=True, lo=8766, hi=9130, store_sel=True, emit=False),
            Chunk(0, INT64, quantity, compare=True, lo=INT64_MIN, hi=2399, load_sel=True, store_sel=True, emit=False),
            Chunk(0, INT64, discount, compare=True, lo=5, hi=7, load_sel=True, store_sel=True),
            Chunk(0, INT64, price, load_sel=True),
        ])

    def test_two_row_groups(self):
        # The first filter chunk of a row group overwrites the selection of the previous one
        rng = random.Random(7)
        chunks = []
        for n in (1000, 300, 1200):
            keys = [rng.randint(0, 999) for _ in range(n)]
            vals = [rng.randint(-pow(2, 40), pow(2, 40)) for _ in range(n)]
            chunks.append(Chunk(0, INT32, keys, compare=True, lo=250, hi=749, store_sel=True, emit=False))
            chunks.append(Chunk(0, INT64, vals, load_sel=True))
        self.run_chunks(chunks)

    def test_two_decoders(self):
        # Independent row groups on the two decoders
        rng = random.Random(8)
        a = [rng.randint(0, 99) for _ in range(700)]
        b = [rng.randint(0, 99) for _ in range(900)]
        self.run_chunks([
            Chunk(0, INT32, a, compare=True, lo=0, hi=49, store_sel=True, emit=False),
            Chunk(1, INT64, b, compare=True, lo=50, hi=99, store_sel=True, emit=False),
            Chunk(0, INT64, [v * 1000 for v in a], load_sel=True),
            Chunk(1, INT32, [v - 50 for v in b], load_sel=True),
        ])

    def test_int64_store_then_int32_load(self):
        # 1003 rows: the stale half word after the last INT64 beat must not leak into the INT32 load
        rng = random.Random(9)
        n = 1003
        keys = [rng.randint(0, 9) for _ in range(n)]
        self.run_chunks([
            Chunk(0, INT64, list(range(n)), compare=True, lo=0, hi=n, store_sel=True, emit=False),
            Chunk(0, INT64, keys, compare=True, lo=3, hi=6, load_sel=True, store_sel=True, emit=False),
            Chunk(0, INT32, [k * 7 for k in keys], load_sel=True),
        ])

    def test_many_short_chunks(self):
        # Short chunks of alternating type, with and without output, shake the conf hold
        rng = random.Random(10)
        chunks = []
        for i in range(8):
            n = rng.randint(1, 40)
            typ = INT32 if i % 2 == 0 else INT64
            values = [rng.randint(0, 20) for _ in range(n)]
            chunks.append(Chunk(0, typ, values, compare=True, lo=5, hi=15, emit=i % 3 != 2))
        self.run_chunks(chunks)

    def test_short_row_groups_behind_long_chunk(self):
        # Row groups of at most 16 rows: the last store and the first load hit the same bitmap word,
        # and the output of the long chunk in front stalls the store while the load waits. Every row
        # group selects the rows the previous one dropped, so a load of the old word shows. 63
        # chunks stay within the 64 confs of the FilterConfig FIFO, which the test fills ahead of
        # the data.
        rng = random.Random(11)
        chunks = []
        keys = [rng.randint(0, 9) for _ in range(16)]
        for i in range(21):
            n = i % 16 + 1
            typ = INT32 if i % 2 == 0 else INT64
            keys = [(k + 5) % 10 for k in keys]
            chunks.append(Chunk(0, INT32, [rng.randint(0, 99) for _ in range(rng.randint(3000, 4000))]))
            chunks.append(Chunk(0, typ, keys[:n], compare=True, lo=0, hi=4, store_sel=True, emit=False))
            chunks.append(Chunk(0, INT32, [k * 11 for k in keys[:n]], load_sel=True))
        self.run_chunks(chunks)

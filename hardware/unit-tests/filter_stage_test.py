import random
from dataclasses import dataclass
from typing import Optional
from coyote_test import fpga_stream

from libstf_utils.configured_test_case import ConfiguredTestCase
from libstf_utils.fpga_configuration import StreamConfig
from oasis_utils.fpga_configuration import (FilterConfig, INT32_MIN, INT32_MAX, INT64_MIN, INT64_MAX, bloom_hash,
                                            bloom_mask, bloom_words)

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
    lookup: bool = False
    lut_min: int = 0
    shift: int = 0
    base: int = 0
    bloom: bool = False
    hash64: bool = False


def bitmap_words(bits: list[bool]) -> list[int]:
    """The 64 bit words of a bitmap, bit 0 of word 0 first."""
    words = [0] * ((len(bits) + 63) // 64)
    for i, bit in enumerate(bits):
        if bit:
            words[i // 64] |= 1 << (i % 64)
    return words


def lookup_chunk(decoder: int, stream_type: fpga_stream.StreamType, values: list[int], lo: int, hi: int,
                 lut_min: Optional[int] = None, **kwargs) -> Chunk:
    """A chunk that compares with [lo, hi] and looks the values up, from lut_min (default lo) on."""
    return Chunk(decoder, stream_type, values, compare=True, lo=lo, hi=hi, lookup=True,
                 lut_min=lo if lut_min is None else lut_min, **kwargs)


def bloom_chunk(decoder: int, stream_type: fpga_stream.StreamType, values: list[int], num_words: int, base: int,
                **kwargs) -> Chunk:
    """A chunk that looks the values up in the Bloom filter of num_words words at word base."""
    return Chunk(decoder, stream_type, values, lookup=True, bloom=True, shift=num_words.bit_length() - 1, base=base,
                 **kwargs)


def fold_words(words: list[int], num_words: int) -> list[int]:
    """A Bloom filter halved to num_words words by OR of its halves, as DuckDB's BloomFilter::Fold."""
    folded = [0] * num_words
    for i, word in enumerate(words):
        folded[i % num_words] |= word
    return folded


class FilterStageTestCase(ConfiguredTestCase):
    """
    Tests the FilterStage: the chunks of decoder I enter on stream I and leave on stream I without
    the values that fail the compare, the lookup or the selection of the row group, packed into full
    beats. A model of the selection bitmap per decoder and of the lookup table gives the expected
    values (the hash of DuckDB for a Bloom filter); a chunk without expected values has no expected
    output because the host receives nothing for an empty stream. The table regions are written before the confs, like the software writes a
    region before the first chunk that looks it up.
    """

    alternative_vfpga_top_file = "vfpga-tops/filter_stage_test.sv"
    debug_mode = True

    def configure(self):
        filter_config = self.config.get_config(FilterConfig)
        stream_config = self.config.get_config(StreamConfig)
        bitmaps: dict[int, list[bool]] = {}

        self.table_bits = filter_config.table_bits
        for base, words in self.table.items():
            filter_config.write_table(base, words)

        for chunk in self.chunks:
            # The filter conf goes first, like the software writes it before the decoder conf
            filter_config.enqueue(
                chunk.decoder, lo=chunk.lo, hi=chunk.hi, compare=chunk.compare, negate=chunk.negate,
                load_sel=chunk.load_sel, store_sel=chunk.store_sel, emit=chunk.emit,
                lookup=chunk.lookup, lut_min=chunk.lut_min, shift=chunk.shift, base=chunk.base,
                bloom=chunk.bloom, hash64=chunk.hash64,
            )
            stream_config.enqueue_stream_config(chunk.decoder, chunk.stream_type)
            self.set_stream_input(chunk.decoder, fpga_stream.Stream(chunk.stream_type, chunk.values))

            expected = self.expected_values(chunk, bitmaps)
            if expected:
                self.set_expected_output(chunk.decoder, fpga_stream.Stream(chunk.stream_type, expected))

    def table_bit(self, chunk: Chunk, value: int) -> bool:
        # The difference in the width of the value, the index wraps at the size of the table
        width = 32 if chunk.stream_type == INT32 else 64
        diff = (value - chunk.lut_min) % pow(2, width)
        index = (chunk.base * 64 + (diff >> chunk.shift)) % self.table_bits
        word = self.table_words.get(index // 64)
        assert word is not None, f"Lookup of the unwritten table word {index // 64}"
        return (word >> (index % 64)) & 1 == 1

    def bloom_bit(self, chunk: Chunk, value: int) -> bool:
        # The word of the low shift bits of the hash in the region, the four bits of the hash in it
        width = 32 if chunk.stream_type == INT32 else 64
        h = bloom_hash(value, width, chunk.hash64)
        addr = (chunk.base + h % pow(2, chunk.shift)) % (self.table_bits // 64)
        word = self.table_words.get(addr)
        assert word is not None, f"Lookup of the unwritten table word {addr}"
        mask = bloom_mask(h)
        return word & mask == mask

    def expected_values(self, chunk: Chunk, bitmaps: dict[int, list[bool]]) -> list[int]:
        if chunk.compare:
            passes = [(chunk.lo <= value <= chunk.hi) != chunk.negate for value in chunk.values]
        else:
            passes = [True] * len(chunk.values)
        if chunk.lookup:
            lookup = self.bloom_bit if chunk.bloom else self.table_bit
            passes = [p and lookup(chunk, value) for value, p in zip(chunk.values, passes)]
        if chunk.load_sel:
            bitmap = bitmaps[chunk.decoder]
            passes = [p and bitmap[i] for i, p in enumerate(passes)]
        if chunk.store_sel:
            bitmaps[chunk.decoder] = passes
        if not chunk.emit:
            return []
        return [value for value, p in zip(chunk.values, passes) if p]

    def run_chunks(self, chunks: list[Chunk], table: Optional[dict[int, list[int]]] = None):
        self.chunks = chunks
        self.table = table or {}
        self.table_words = {base + i: word for base, words in self.table.items() for i, word in enumerate(words)}
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

    # -- Lookup -----------------------------------------------------------------------------------
    def test_lookup_int32_exact(self):
        # The prefix range filter of a join on an INT32 key: a third of the keys of the span are set
        rng = random.Random(12)
        lo, span = 1000, 5000
        bits = [rng.random() < 0.3 for _ in range(span)]
        values = [rng.randint(lo - 200, lo + span + 200) for _ in range(2000)]
        self.run_chunks(
            [lookup_chunk(0, INT32, values, lo, lo + span - 1)],
            {0: bitmap_words(bits)},
        )

    def test_lookup_int64(self):
        # An INT64 key above 2^40
        rng = random.Random(13)
        lo, span = pow(2, 40) + 7, 3000
        bits = [rng.random() < 0.5 for _ in range(span)]
        values = [rng.randint(lo - 100, lo + span + 100) for _ in range(1001)]
        self.run_chunks(
            [lookup_chunk(0, INT64, values, lo, lo + span - 1, base=5)],
            {5: bitmap_words(bits)},
        )

    def test_lookup_borders(self):
        # The difference crosses the sign of INT32, the 32 bit halves of INT64 and the sign of INT64
        rng = random.Random(14)
        span = 2000
        table = {}
        chunks = []
        for i, (typ, lo) in enumerate([
            (INT32, INT32_MAX - span + 1),
            (INT32, -span // 2),
            (INT32, INT32_MIN),
            (INT64, pow(2, 32) - span // 2),
            (INT64, -pow(2, 32) - span // 2),
            (INT64, -span // 2),
            (INT64, INT64_MAX - span + 1),
        ]):
            base = i * 40
            bits = [rng.random() < 0.5 for _ in range(span)]
            table[base] = bitmap_words(bits)
            values = [rng.randint(lo, lo + span - 1) for _ in range(500)]
            chunks.append(lookup_chunk(0, typ, values, lo, lo + span - 1, base=base))
        self.run_chunks(chunks, table)

    def test_lookup_fold(self):
        # A span larger than its region: 8 keys share one bit
        rng = random.Random(15)
        lo, shift, num_bits = -12345, 3, 900
        hi = lo + (num_bits << shift) - 1
        bits = [rng.random() < 0.2 for _ in range(num_bits)]
        values = [rng.randint(lo, hi) for _ in range(2500)]
        self.run_chunks(
            [lookup_chunk(0, INT32, values, lo, hi, shift=shift, base=3)],
            {3: bitmap_words(bits)},
        )

    def test_lookup_two_regions(self):
        # Two bitmaps in one table, one chunk each, then the first one again
        rng = random.Random(16)
        bits_a = [rng.random() < 0.4 for _ in range(1000)]
        bits_b = [rng.random() < 0.6 for _ in range(1500)]
        keys_a = [rng.randint(0, 999) for _ in range(700)]
        keys_b = [rng.randint(5000, 6499) for _ in range(800)]
        self.run_chunks([
            lookup_chunk(0, INT32, keys_a, 0, 999, base=0),
            lookup_chunk(0, INT64, keys_b, 5000, 6499, base=16),
            lookup_chunk(0, INT32, keys_a[::-1], 0, 999, base=0),
        ], {0: bitmap_words(bits_a), 16: bitmap_words(bits_b)})

    def test_lookup_empty_and_dense(self):
        # No bit set gives an empty stream, every bit set passes the compare interval
        values = list(range(-100, 1100))
        self.run_chunks([
            lookup_chunk(0, INT32, values, 0, 999, base=0),
            lookup_chunk(0, INT32, values, 0, 999, base=20),
            Chunk(0, INT32, values),
        ], {0: [0] * 16, 20: [pow(2, 64) - 1] * 16})

    def test_lookup_with_constant(self):
        # The join key also has a constant range, narrower than the bitmap
        rng = random.Random(17)
        bits = [rng.random() < 0.5 for _ in range(4000)]
        values = [rng.randint(0, 3999) for _ in range(1500)]
        self.run_chunks(
            [lookup_chunk(0, INT64, values, 1000, 2500, lut_min=0)],
            {0: bitmap_words(bits)},
        )

    def test_lookup_filter_only_then_apply(self):
        # TPC-H Q14: the bitmap of l_partkey selects the rows, the price columns load the selection
        rng = random.Random(18)
        n = 1200
        bits = [rng.random() < 0.1 for _ in range(2000)]
        partkey = [rng.randint(1, 2000) for _ in range(n)]
        price = [rng.randint(100, 10000000) for _ in range(n)]
        discount = [rng.randint(0, 10) for _ in range(n)]
        self.run_chunks([
            lookup_chunk(0, INT64, partkey, 1, 2000, store_sel=True, emit=False),
            Chunk(0, INT64, price, load_sel=True),
            Chunk(0, INT64, discount, load_sel=True),
        ], {0: bitmap_words(bits)})

    def test_lookup_then_compare_only(self):
        # The conf changes from lookup to compare and to none between chunks of mixed width
        rng = random.Random(19)
        bits = [rng.random() < 0.5 for _ in range(64)]
        chunks = []
        for i in range(12):
            n = rng.randint(1, 50)
            typ = INT32 if i % 2 == 0 else INT64
            values = [rng.randint(0, 80) for _ in range(n)]
            if i % 3 == 0:
                chunks.append(lookup_chunk(0, typ, values, 0, 63, emit=i % 4 != 3))
            elif i % 3 == 1:
                chunks.append(Chunk(0, typ, values, compare=True, lo=10, hi=40))
            else:
                chunks.append(Chunk(0, typ, values))
        self.run_chunks(chunks, {0: bitmap_words(bits)})

    def test_lookup_two_decoders(self):
        # Both decoders look up the same region of the one table
        rng = random.Random(20)
        bits = [rng.random() < 0.3 for _ in range(3000)]
        a = [rng.randint(0, 2999) for _ in range(900)]
        b = [rng.randint(0, 2999) for _ in range(1100)]
        self.run_chunks([
            lookup_chunk(0, INT32, a, 0, 2999, base=100),
            lookup_chunk(1, INT64, b, 0, 2999, base=100),
        ], {100: bitmap_words(bits)})

    # -- Bloom filter -----------------------------------------------------------------------------
    def bloom_values(self, rng: random.Random, keys: list[int], lo: int, hi: int, n: int) -> list[int]:
        # Half of the values are keys, the rest random over [lo, hi]
        return [rng.choice(keys) if rng.random() < 0.5 else rng.randint(lo, hi) for _ in range(n)]

    def test_bloom_int32(self):
        # The Bloom filter of a join on an INTEGER key, hashed as its zero extension, keys of both signs
        rng = random.Random(30)
        keys = [rng.randint(INT32_MIN, INT32_MAX) for _ in range(400)]
        values = self.bloom_values(rng, keys, INT32_MIN, INT32_MAX, 2000)
        self.run_chunks(
            [bloom_chunk(0, INT32, values, 256, 0)],
            {0: bloom_words(keys, 256, 32)},
        )

    def test_bloom_int64(self):
        # A BIGINT key above 2^40 in a region whose base is no multiple of its size
        rng = random.Random(31)
        lo, hi = pow(2, 40), pow(2, 40) + pow(2, 36)
        keys = [rng.randint(lo, hi) for _ in range(300)]
        values = self.bloom_values(rng, keys, lo - 1000, hi + 1000, 1501)
        self.run_chunks(
            [bloom_chunk(0, INT64, values, 128, 300)],
            {300: bloom_words(keys, 128, 64)},
        )

    def test_bloom_hash64(self):
        # A BIGINT key in a chunk the decoder narrowed to INT32: the hash of the sign extension
        rng = random.Random(32)
        keys = [rng.randint(-pow(2, 30), pow(2, 30)) for _ in range(300)]
        values = self.bloom_values(rng, keys, -pow(2, 30), pow(2, 30), 1800)
        self.run_chunks(
            [bloom_chunk(0, INT32, values, 128, 7, hash64=True)],
            {7: bloom_words(keys, 128, 64)},
        )

    def test_bloom_fold(self):
        # DuckDB's filter of 2048 words folded to the 64 words of its region; every key still passes
        rng = random.Random(33)
        keys = [rng.randint(0, pow(2, 40)) for _ in range(500)]
        folded = fold_words(bloom_words(keys, 2048, 64), 64)
        assert folded == bloom_words(keys, 64, 64)
        values = self.bloom_values(rng, keys, 0, pow(2, 40), 1200)
        self.run_chunks(
            [bloom_chunk(0, INT64, values, 64, 40)],
            {40: folded},
        )

    def test_bloom_with_compare(self):
        # The min/max bounds of the join next to its Bloom filter
        rng = random.Random(34)
        keys = [rng.randint(10000, 2000000) for _ in range(200)]
        values = self.bloom_values(rng, keys, 0, 3000000, 1700)
        self.run_chunks(
            [bloom_chunk(0, INT32, values, 64, 0, compare=True, lo=min(keys), hi=max(keys))],
            {0: bloom_words(keys, 64, 32)},
        )

    def test_bloom_filter_only_then_apply(self):
        # TPC-H Q18: the Bloom filter of l_orderkey selects the rows, the quantity loads the selection
        rng = random.Random(35)
        n = 1300
        keys = [rng.randint(1, 6000000) * 4 for _ in range(100)]
        orderkey = self.bloom_values(rng, keys, 1, 24000000, n)
        quantity = [rng.randint(1, 50) for _ in range(n)]
        self.run_chunks([
            bloom_chunk(0, INT64, orderkey, 32, 0, store_sel=True, emit=False),
            Chunk(0, INT64, quantity, load_sel=True),
        ], {0: bloom_words(keys, 32, 64)})

    def test_bloom_and_bitmap(self):
        # A Bloom filter and a bitmap in one row group, then the conf changes between Bloom filter,
        # bitmap, compare and none on short chunks of mixed width
        rng = random.Random(36)
        n = 1000
        keys = [rng.randint(0, pow(2, 31) - 1) for _ in range(150)]
        bits = [rng.random() < 0.5 for _ in range(64)]
        table = {0: bitmap_words(bits), 8: bloom_words(keys, 16, 32), 24: bloom_words(keys, 16, 64)}
        chunks = [
            bloom_chunk(0, INT32, self.bloom_values(rng, keys, 0, pow(2, 31) - 1, n), 16, 8, store_sel=True,
                        emit=False),
            lookup_chunk(0, INT64, [rng.randint(0, 80) for _ in range(n)], 0, 63, load_sel=True, store_sel=True,
                         emit=False),
            Chunk(0, INT64, [rng.randint(0, 1000) for _ in range(n)], load_sel=True),
        ]
        for i in range(16):
            m = rng.randint(1, 50)
            typ = INT32 if i % 2 == 0 else INT64
            if i % 4 == 0:
                chunks.append(bloom_chunk(0, typ, self.bloom_values(rng, keys, 0, pow(2, 31) - 1, m), 16,
                                          8 if typ == INT32 else 24))
            elif i % 4 == 1:
                chunks.append(lookup_chunk(0, typ, [rng.randint(0, 80) for _ in range(m)], 0, 63))
            elif i % 4 == 2:
                chunks.append(Chunk(0, typ, [rng.randint(0, 80) for _ in range(m)], compare=True, lo=10, hi=40))
            else:
                chunks.append(Chunk(0, typ, [rng.randint(0, 80) for _ in range(m)]))
        self.run_chunks(chunks, table)

    def test_bloom_two_decoders(self):
        # Both decoders look up the same Bloom filter of the one table
        rng = random.Random(37)
        keys = [rng.randint(0, pow(2, 40)) for _ in range(250)]
        a = self.bloom_values(rng, keys, 0, pow(2, 40), 900)
        b = self.bloom_values(rng, keys, 0, pow(2, 40), 1100)
        self.run_chunks([
            bloom_chunk(0, INT64, a, 64, 100),
            bloom_chunk(1, INT64, b, 64, 100),
        ], {100: bloom_words(keys, 64, 64)})

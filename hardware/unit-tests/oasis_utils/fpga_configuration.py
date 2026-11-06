# Python counterpart of the configurations in hardware/src/hdl, built on libstf's configuration
# utils. Keep the register encodings in sync with the hardware.
from libstf_utils.fpga_configuration import Config, RegisterAccess, REGISTER_BITS
from libstf_utils.hashing import MASK64

INT32_MIN, INT32_MAX = -pow(2, 31), pow(2, 31) - 1
INT64_MIN, INT64_MAX = -pow(2, 63), pow(2, 63) - 1


class FilterConfig(Config):
    """
    Queues the filter_conf_t of the next column chunk of a decoder's FilterStage: the closed interval
    lo <= x <= hi, the lookup in the bitmap table or in a Bloom filter of the table and the control
    bits (see filter_ctrl_t in common.sv). Writes the words of the lookup table, the same for all
    decoders.
    """

    ID = 0x14e8418192d46a72 # FILTER_CONFIG_ID

    # Write registers per decoder: lo, hi, the control register and lut_min
    NUM_DECODER_REGS = 4 # NUM_FILTER_CONFIG_WRITE_REGS
    LO_REG      = 0
    HI_REG      = 1
    CONTROL_REG = 2
    LUT_MIN_REG = 3

    # After the registers of the decoders: the table word, then the table data (one word per write)
    TABLE_ADDR_REG = 0
    TABLE_DATA_REG = 1

    # The control register packs (MSB -> LSB) as:
    #   base [24 bits] | reserved [2 bits] | hash64 [1 bit] | bloom [1 bit] | shift [6 bits] |
    #   lookup [1 bit] | compare [1 bit] | negate [1 bit] | load_sel [1 bit] | store_sel [1 bit] |
    #   emit [1 bit]
    EMIT_SHIFT      = 0
    STORE_SEL_SHIFT = 1
    LOAD_SEL_SHIFT  = 2
    NEGATE_SHIFT    = 3
    COMPARE_SHIFT   = 4
    LOOKUP_SHIFT    = 5
    SHIFT_SHIFT     = 6
    BLOOM_SHIFT     = 12
    HASH64_SHIFT    = 13
    BASE_SHIFT      = 16

    def __init__(self, registers: RegisterAccess, addr_offset: int, num_regs: int):
        super().__init__(registers, addr_offset, num_regs)
        self.num_decoders = self.read_register(1)
        self.maximum_num_enqueued_confs = self.read_register(2)
        self.sel_rows = self.read_register(3)
        self.table_bits = self.read_register(4)

    @staticmethod
    def control_value(
        compare: bool = False,
        negate: bool = False,
        load_sel: bool = False,
        store_sel: bool = False,
        emit: bool = True,
        lookup: bool = False,
        shift: int = 0,
        base: int = 0,
        bloom: bool = False,
        hash64: bool = False,
    ) -> int:
        assert 0 <= shift < 32 and 0 <= base < pow(2, 24)
        return (
            base << FilterConfig.BASE_SHIFT
            | int(hash64) << FilterConfig.HASH64_SHIFT
            | int(bloom) << FilterConfig.BLOOM_SHIFT
            | shift << FilterConfig.SHIFT_SHIFT
            | int(lookup) << FilterConfig.LOOKUP_SHIFT
            | int(compare) << FilterConfig.COMPARE_SHIFT
            | int(negate) << FilterConfig.NEGATE_SHIFT
            | int(load_sel) << FilterConfig.LOAD_SEL_SHIFT
            | int(store_sel) << FilterConfig.STORE_SEL_SHIFT
            | int(emit) << FilterConfig.EMIT_SHIFT
        )

    def enqueue(
        self,
        decoder: int,
        lo: int = INT64_MIN,
        hi: int = INT64_MAX,
        compare: bool = False,
        negate: bool = False,
        load_sel: bool = False,
        store_sel: bool = False,
        emit: bool = True,
        lookup: bool = False,
        lut_min: int = 0,
        shift: int = 0,
        base: int = 0,
        bloom: bool = False,
        hash64: bool = False,
    ):
        """
        Queues the conf of the next column chunk of the decoder. With compare the values pass if
        lo <= x <= hi (signed), or if not with negate; INT32 columns compare against the low 32 bits
        of lo and hi. With lookup a value also needs its bit base * 64 + ((x - lut_min) >> shift)
        in the table. With lookup and bloom it needs the four bits of its hash in word
        base + (hash % 2^shift) instead (see bloom_hash); hash64 hashes INT32 values sign-extended.
        The default is the conf of a chunk without filter.
        """
        assert decoder < self.num_decoders
        assert INT64_MIN <= lo <= INT64_MAX and INT64_MIN <= hi <= INT64_MAX
        assert INT64_MIN <= lut_min <= INT64_MAX

        mask = pow(2, REGISTER_BITS) - 1
        regs = decoder * FilterConfig.NUM_DECODER_REGS
        self.write_register(regs + FilterConfig.LO_REG, lo & mask)
        self.write_register(regs + FilterConfig.HI_REG, hi & mask)
        self.write_register(
            regs + FilterConfig.CONTROL_REG,
            FilterConfig.control_value(compare, negate, load_sel, store_sel, emit, lookup, shift, base,
                                       bloom, hash64),
        )
        self.write_register(regs + FilterConfig.LUT_MIN_REG, lut_min & mask)

    def write_table(self, base: int, words: list[int]):
        """Writes the words of a bitmap region from word base on, into the tables of all decoders."""
        assert base + len(words) <= self.table_bits // 64

        regs = self.num_decoders * FilterConfig.NUM_DECODER_REGS
        self.write_register(regs + FilterConfig.TABLE_ADDR_REG, base)
        for word in words:
            self.write_register(regs + FilterConfig.TABLE_DATA_REG, word)


MURMUR_C = 0xd6e8feb86659fd93


def murmur_hash64(x: int) -> int:
    """MurmurHash64 of DuckDB (hash.hpp) of the 64 bit unsigned x."""
    x ^= x >> 32
    x = (x * MURMUR_C) & MASK64
    x ^= x >> 32
    x = (x * MURMUR_C) & MASK64
    x ^= x >> 32
    return x


def bloom_hash(value: int, width: int, hash64: bool = False) -> int:
    """
    The hash of a value of a column of the width (32 or 64 bits), as DuckDB hashes its key: an
    INT32 key as its zero extension, or with hash64 as its sign extension (a BIGINT key in a
    narrowed chunk), an INT64 key as it is.
    """
    if width == 32 and not hash64:
        return murmur_hash64(value % pow(2, 32))
    return murmur_hash64(value % pow(2, 64))


def bloom_mask(hash: int) -> int:
    """The four bits of the word that a hash tests, from the low 6 bits of its bytes 4 to 7 (GetMask)."""
    mask = 0
    for byte in range(4, 8):
        mask |= 1 << ((hash >> (8 * byte)) & 63)
    return mask


def bloom_words(keys: list[int], num_words: int, width: int) -> list[int]:
    """The Bloom filter of the keys in num_words (a power of two) 64 bit words, as DuckDB inserts them."""
    assert num_words & (num_words - 1) == 0
    words = [0] * num_words
    for key in keys:
        h = bloom_hash(key, width)
        words[h % num_words] |= bloom_mask(h)
    return words
